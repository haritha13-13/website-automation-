"""Owns the shared Playwright session on one dedicated thread.

Playwright's sync API is bound to the thread that created it, while CrewAI must run on a thread
without a running event loop. The pipeline therefore runs in its own thread and sends every
browser operation here. One browser, one tab per website, shared login state.
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from typing import Callable, TypeVar

from playwright.sync_api import Page

from browser.playwright_manager import PlaywrightManager, headless_from_env

T = TypeVar("T")
_STOP = object()


class BrowserWorker:
    def __init__(self, headless: bool | None = None, cdp_url: str | None = None) -> None:
        self.headless = headless
        self.cdp_url = cdp_url
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="browser-worker", daemon=True)
        self._mgr: PlaywrightManager | None = None
        self._pages: dict[str, Page] = {}
        self._thread.start()

    # ---- thread body --------------------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            job = self._q.get()
            if job is _STOP:
                break
            fn, fut = job
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - forwarded to the caller
                fut.set_exception(exc)
        if self._mgr:
            self._mgr.close()

    def _submit(self, fn: Callable[[], T]) -> T:
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut.result()

    # ---- public API (called from other threads) -------------------------------------------------
    @property
    def is_headless(self) -> bool:
        return self.headless if self.headless is not None else headless_from_env()

    def _page(self, site: str) -> Page:
        if self._mgr is None:
            self._mgr = PlaywrightManager(self.headless, self.cdp_url)
            first = self._mgr.start()
            self._pages[site] = first
        if site not in self._pages:
            self._pages[site] = self._mgr.new_isolated_page()  # separate session per website/account
        return self._pages[site]

    def navigate(self, site: str, url: str) -> str:
        return self._submit(lambda: self._mgr_navigate(site, url))

    def _mgr_navigate(self, site: str, url: str) -> str:
        page = self._page(site)
        assert self._mgr is not None
        return self._mgr.navigate(url, page)

    def page_call(self, site: str, fn: Callable[[Page], T]) -> T:
        return self._submit(lambda: fn(self._page(site)))

    def settle(self, site: str) -> None:
        self._submit(lambda: self._mgr.wait_until_settled(self._page(site)))

    def fetch_article(self, url: str, site: str | None = None) -> tuple[int | None, str]:
        """Open url in a throwaway tab of that site's session; return (HTTP status, visible text)."""
        def job():
            base = self._pages.get(site) if site else None
            assert self._mgr is not None or base is not None
            page = base.context.new_page() if base is not None else self._mgr.new_page()
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:  # noqa: BLE001
                    pass
                return (resp.status if resp else None), page.evaluate("document.body ? document.body.innerText : ''")
            finally:
                page.close()
        return self._submit(job)

    def fetch_page(self, url: str, site: str | None = None) -> dict:
        """Open url in a throwaway tab of that site's session: status, visible text, anchors, final URL."""
        def job():
            base = self._pages.get(site) if site else None
            assert self._mgr is not None or base is not None
            page = base.context.new_page() if base is not None else self._mgr.new_page()
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:  # noqa: BLE001
                    pass
                links = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => ({text: (e.innerText || e.getAttribute('aria-label') || '').trim(), href: e.href}))")
                return {"status": resp.status if resp else None, "final_url": page.url,
                        "text": page.evaluate("document.body ? document.body.innerText : ''")[:20000], "links": links[:300]}
            finally:
                page.close()
        return self._submit(job)

    def close(self) -> None:
        if self._thread.is_alive():
            self._q.put(_STOP)
            self._thread.join(timeout=20)
