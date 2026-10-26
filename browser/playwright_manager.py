"""Playwright lifecycle: launch Chromium, navigate, close. No cookies/storage are read."""
from __future__ import annotations

import logging
import os
from types import TracebackType
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 8_000


class BrowserStartError(RuntimeError):
    pass


class NavigationError(RuntimeError):
    pass


def headless_from_env() -> bool:
    return os.getenv("HEADLESS", "false").strip().lower() in {"1", "true", "yes"}


class PlaywrightManager:
    def __init__(self, headless: bool | None = None, cdp_url: str | None = None) -> None:
        self.headless = headless_from_env() if headless is None else headless
        self.cdp_url = cdp_url  # attach to a Chrome the PERSON started (their own sign-ins), instead of launching one
        self._opened_pages: list[Page] = []
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._extra_contexts: list[BrowserContext] = []
        self.page: Page | None = None

    def start(self) -> Page:
        try:
            self._pw = sync_playwright().start()
            if self.cdp_url:
                self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
                # Use the browser's own default profile: the person's cookies and sign-ins are already there.
                self._context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
                self.page = self._context.new_page()
                self._opened_pages.append(self.page)
                self.page.set_default_timeout(NAV_TIMEOUT_MS)
                return self.page
            self._browser = self._pw.chromium.launch(headless=self.headless)
            # Fresh in-memory context: nothing persisted to disk.
            self._context = self._browser.new_context(viewport={"width": 1366, "height": 900})
            self.page = self._context.new_page()
            self.page.set_default_timeout(NAV_TIMEOUT_MS)
        except Exception as exc:
            self.close()
            hint = (f"Could not attach to Chrome at {self.cdp_url} ({str(exc).splitlines()[0]}). Start it first with "
                    "scripts/start_chrome_for_agent.sh" if self.cdp_url else
                    f"Could not start Chromium ({exc}). Try: playwright install chromium")
            raise BrowserStartError(hint) from exc
        return self.page

    def new_page(self) -> Page:
        """Another tab in the same (in-memory) browser context, so a login session is shared."""
        assert self._context is not None, "browser not started"
        page = self._context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)
        return page

    def new_isolated_page(self) -> Page:
        """A tab in a brand-new in-memory context: its cookies/session are separate from every other site."""
        assert self._browser is not None, "browser not started"
        if self.cdp_url:  # one shared profile: that is the point of attaching to your own Chrome
            page = self._context.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)
            self._opened_pages.append(page)
            return page
        ctx = self._browser.new_context(viewport={"width": 1366, "height": 900})
        self._extra_contexts.append(ctx)
        page = ctx.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)
        return page

    def navigate(self, url: str, page: Page | None = None) -> str:
        """Go to url, wait for the DOM to settle; return final URL."""
        page = page or self.page
        assert page is not None, "browser not started"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            raise NavigationError(f"Timed out loading {url}") from exc
        except PlaywrightError as exc:
            raise NavigationError(f"Could not load {url}: {str(exc).splitlines()[0]}") from exc
        self.wait_until_settled(page)
        return page.url

    def wait_until_settled(self, page: Page | None = None) -> None:
        """Wait for network idle if it happens soon; dynamic pages that never idle are still inspected."""
        page = page or self.page
        assert page is not None
        try:
            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
        except PlaywrightTimeout:
            logger.debug("Network did not go idle within %sms; continuing", SETTLE_TIMEOUT_MS)

    @staticmethod
    def redirected_across_hosts(requested: str, final: str) -> bool:
        return urlparse(requested).hostname != urlparse(final).hostname

    def close(self) -> None:
        if self.cdp_url:  # never close the person's browser: close only the tabs we opened, then disconnect
            for page in self._opened_pages:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
            self._opened_pages = []
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._extra_contexts = []
            self._context = self._browser = self._pw = self.page = None
            return
        for obj in (*self._extra_contexts, self._context, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:  # noqa: BLE001 - best-effort shutdown
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._extra_contexts = []
        self._context = self._browser = self._pw = self.page = None

    def __enter__(self) -> "PlaywrightManager":
        self.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()


def goto_settled(page: Page, url: str) -> str:
    """Navigate an existing page and wait (bounded) for it to settle. Returns the final URL."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        raise NavigationError(f"Timed out loading {url}") from exc
    except PlaywrightError as exc:
        raise NavigationError(f"Could not load {url}: {str(exc).splitlines()[0]}") from exc
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
    except PlaywrightTimeout:
        pass
    return page.url
