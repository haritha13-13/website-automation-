"""Playwright primitives for filling an editor and clicking save/publish. Runs on the browser thread."""
from __future__ import annotations

import re

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from browser.locator_validator import LocatorValidator
from services.auth import login_visible
from services.models import ContentItem, ExecutionPlan, PageSnapshot

SETTLE_MS = 8_000
GENERIC_OUTCOME = r"saved|published|success|error|required|failed|invalid"
BANNER_SELECTORS = "[role=alert], .error, .alert-danger, .notice-error, .error-message"


class ExecutionError(RuntimeError):
    pass


def _unique(page: Page, selector: str, what: str) -> Locator:
    loc, count = LocatorValidator(page).locate(selector)
    if loc is None:
        raise ExecutionError(f"{what}: '{selector}' not found (is the session still logged in?)")
    if count != 1:
        raise ExecutionError(f"{what}: '{selector}' is ambiguous ({count} matches); refusing to guess")
    return loc


def fill_form(page: Page, plan: ExecutionPlan, item: ContentItem) -> list[str]:
    """Enter the item's content. Never clicks save/publish. Returns a log of completed steps."""
    steps: list[str] = []
    fm = plan.field_mapping

    title = _unique(page, fm["title"], "title")
    title.fill(item.title)
    steps.append("title entered")

    body = _unique(page, fm["content"], "content")
    if body.evaluate("el => el.isContentEditable"):
        body.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        lines = item.content.split("\n")
        for i, line in enumerate(lines):
            if line:
                page.keyboard.insert_text(line)
            if i < len(lines) - 1:
                page.keyboard.press("Enter")
    else:
        body.fill(item.content)
    steps.append("content entered")

    if item.category:
        sel = _unique(page, fm["category"], "category")
        if sel.evaluate("el => el.tagName") == "SELECT":
            try:
                sel.select_option(label=item.category, timeout=3_000)
            except PlaywrightTimeout:
                try:
                    sel.select_option(value=item.category, timeout=3_000)
                except PlaywrightTimeout as exc:
                    raise ExecutionError(f"category '{item.category}' is not an option on the page") from exc
        else:
            sel.fill(item.category)
        steps.append(f"category '{item.category}' selected")

    if item.tags:
        tags = _unique(page, fm["tags"], "tags")
        if plan.tags_mode == "enter":
            for t in item.tags:
                tags.fill(t)
                tags.press("Enter")
        else:
            tags.fill(", ".join(item.tags))
        steps.append(f"{len(item.tags)} tag(s) entered")

    if item.image_paths:
        files = _unique(page, fm["images"], "image upload")
        files.set_input_files(item.image_paths)
        steps.append(f"{len(item.image_paths)} image(s) attached")
    return steps


def click_action(page: Page, plan: ExecutionPlan, item: ContentItem) -> str:
    """Click save-draft or publish (caller has already obtained approval and claimed the item)."""
    action = "publish" if item.publish_mode == "publish" else "save_draft"
    _unique(page, plan.actions[action], action).click()
    return action


def wait_for_outcome(page: Page, plan: ExecutionPlan, editor_url: str) -> None:
    """Bounded wait for either a navigation or an outcome message; no fixed sleep."""
    hints = [h for h in (plan.success.draft_text, plan.success.published_text) if h]
    pattern = "|".join([GENERIC_OUTCOME, *hints])
    try:
        page.wait_for_function(
            "([pat, start]) => location.href !== start || new RegExp(pat, 'i').test(document.body.innerText)",
            arg=[pattern, editor_url], timeout=SETTLE_MS)
    except PlaywrightTimeout:
        pass


def capture_snapshot(page: Page, plan: ExecutionPlan) -> PageSnapshot:
    """Read evidence for the Verification Agent. Read-only."""
    v = LocatorValidator(page)

    def one(key: str):
        sel = plan.field_mapping.get(key)
        if not sel:
            return None
        loc, count = v.locate(sel)
        return loc if loc is not None and count == 1 else None

    snap = PageSnapshot(url=page.url, title=page.title(), login_form_visible=login_visible(page),
                        body_text=page.evaluate("document.body ? document.body.innerText : ''")[:20_000])
    banner_sel = ", ".join(s for s in (plan.success.error_selector, BANNER_SELECTORS) if s)
    texts = page.eval_on_selector_all(
        banner_sel, "els => els.filter(e => e.getBoundingClientRect().width > 0)"
                    ".map(e => (e.innerText || '').trim()).filter(Boolean)")
    snap.banners = [t[:300] for t in texts][:5]
    if (loc := one("title")) is not None:
        snap.title_value = loc.evaluate("el => el.value !== undefined ? el.value : el.innerText")
    if (loc := one("content")) is not None:
        snap.content_text = loc.evaluate("el => el.value !== undefined ? el.value : el.innerText")
    if (loc := one("category")) is not None:
        snap.category_selected = loc.evaluate(
            "el => el.tagName === 'SELECT' ? (el.selectedOptions[0] ? el.selectedOptions[0].text : '') : el.value")
    if (loc := one("tags")) is not None:
        snap.tags_value = loc.evaluate("el => el.value !== undefined ? el.value : el.innerText")
    if (loc := one("images")) is not None:
        snap.image_count = loc.evaluate("el => el.files ? el.files.length : 0")
    link_sel = plan.success.article_link_selector
    if link_sel:
        loc, _ = v.locate(link_sel)
        if loc is not None:
            href = loc.get_attribute("href")
            if href and href.strip() and href != "#":
                snap.article_link = page.evaluate("h => new URL(h, location.href).href", href)
    return snap


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
