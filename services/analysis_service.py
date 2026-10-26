"""Orchestrates the full analysis flow. I/O and the LLM are injectable for testing."""
from __future__ import annotations

import getpass
import ipaddress
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from browser.locator_validator import LocatorValidator
from browser.page_inspector import PageInspector, PageObservation
from browser.playwright_manager import PlaywrightManager
from llm.schemas import AnalysisProposal, AnalysisResult, Status, ValidatedItem

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = {"title", "content"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class UnsafeTargetError(ValueError):
    pass


class EmptyPageError(RuntimeError):
    pass


def validate_url(raw: str, allow_local_files: bool = False) -> str:
    url = raw.strip()
    if not url:
        raise UnsafeTargetError("No URL entered.")
    if "://" not in url and not url.startswith("file:"):
        url = "https://" + url
    p = urlparse(url)
    if p.scheme == "file":
        if not allow_local_files:
            raise UnsafeTargetError("file:// URLs are only allowed with --allow-local (for the mock CMS).")
        return url
    if p.scheme not in {"http", "https"}:
        raise UnsafeTargetError(f"Unsupported URL scheme '{p.scheme}'. Use http or https.")
    if not p.hostname:
        raise UnsafeTargetError("URL has no host name.")
    if p.username or p.password:
        raise UnsafeTargetError("URLs containing embedded credentials are not accepted.")
    return url


def is_local_target(url: str) -> bool:
    p = urlparse(url)
    if p.scheme == "file":
        return True
    host = p.hostname or ""
    if host in LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_private_target(url: str) -> bool:
    try:
        ip = ipaddress.ip_address(urlparse(url).hostname or "")
        return ip.is_private and not ip.is_loopback
    except ValueError:
        return False


def overall_status(proposal: AnalysisProposal, items: list[ValidatedItem]) -> str:
    by = {i.name: i for i in items}
    if proposal.page_type != "article_editor" or proposal.requires_human_review:
        return "NEEDS_REVIEW"
    if any(by[n].status != Status.PASS for n in REQUIRED):
        return "NEEDS_REVIEW"
    if any(i.status in (Status.FAIL, Status.NEEDS_REVIEW) for i in items):
        return "NEEDS_REVIEW"
    if any(i.status == Status.NOT_FOUND for i in items):
        return "COMPLETED_WITH_WARNINGS"
    return "COMPLETED"


@dataclass
class ServiceIO:
    """Terminal hooks, replaceable in tests."""
    ask: Callable[[str], str] = input
    ask_secret: Callable[[str], str] = getpass.getpass  # hidden input; used for passwords
    info: Callable[[str], None] = lambda m: logger.info(m)
    warn: Callable[[str], None] = lambda m: logger.warning(m)


class AnalysisService:
    def __init__(self, analyzer: Callable[[dict], AnalysisProposal], io: ServiceIO | None = None,
                 headless: bool | None = None, output_dir: Path | str = ROOT / "output",
                 screenshot_dir: Path | str = ROOT / "screenshots", allow_local: bool = False,
                 confirm_authorized: bool = True) -> None:
        self.analyzer = analyzer
        self.io = io or ServiceIO()
        self.headless = headless
        self.output_dir = Path(output_dir)
        self.screenshot_dir = Path(screenshot_dir)
        self.allow_local = allow_local
        self.confirm_authorized = confirm_authorized

    def confirm_target(self, raw_url: str) -> str:
        url = validate_url(raw_url, self.allow_local)
        if is_private_target(url):
            self.io.warn("Target is on a private network address.")
        if self.confirm_authorized and not is_local_target(url):
            ans = self.io.ask(f"Are you authorized to analyze {urlparse(url).hostname}? [y/N] ").strip().lower()
            if ans not in {"y", "yes"}:
                raise UnsafeTargetError("Target not confirmed as authorized. Aborting.")
        return url

    def run(self, raw_url: str) -> AnalysisResult:
        url = self.confirm_target(raw_url)
        mgr = PlaywrightManager(self.headless)
        try:
            self.io.info("Starting browser...")
            page = mgr.start()
            self.io.info("Navigating to website...")
            final = mgr.navigate(url)
            self.io.info("Website loaded.")
            if mgr.redirected_across_hosts(url, final):
                self.io.warn(f"Redirected to a different host: {urlparse(final).hostname}")

            inspector = PageInspector(page, self.screenshot_dir)
            if not mgr.headless:
                self.io.info("Authentication may be required.")
                self.io.ask("\nIf needed, log in using the opened browser (and open the editor page).\n"
                            "Press ENTER when the page you want analyzed is showing...")
                mgr.wait_until_settled()

            self.io.info("Inspecting page structure...")
            self.io.info("Extracting interactive elements...")
            obs = inspector.inspect()
            if obs.is_empty:
                raise EmptyPageError("No form controls were found on this page.")
            if obs.has_password_field:
                self.io.warn("Page still shows a password field; you may not be logged in.")

            self.io.info("Sending sanitized layout data to Ollama...")
            self.io.info("Generating field mapping...")
            proposal = self.analyzer(obs.for_llm())

            self.io.info("Validating proposed selectors in the browser...")
            items = LocatorValidator(page).validate(proposal)

            shot = None
            try:
                shot = str(inspector.take_screenshot("website_analysis.png"))
            except Exception as exc:  # noqa: BLE001 - screenshot is optional
                logger.warning("Screenshot failed: %s", exc)

            result = AnalysisResult(
                url=_strip_query(obs.url), page_title=obs.title,
                analyzed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                status=overall_status(proposal, items), ai_proposal=proposal,
                browser_validation=items, observed_element_count=len(obs.elements), screenshot=shot,
            )
            self.save(result)
            return result
        finally:
            mgr.close()

    def save(self, result: AnalysisResult) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / "website_analysis.json"
        path.write_text(json.dumps(result.model_dump(mode="json"), indent=2), encoding="utf-8")
        return path


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]
