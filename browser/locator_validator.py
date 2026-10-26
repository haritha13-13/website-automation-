"""Verify AI-proposed selectors against the live page. Read-only: never clicks or fills."""
from __future__ import annotations

import logging

from playwright.sync_api import Frame, Page

from llm.schemas import AnalysisProposal, Status, ValidatedItem

logger = logging.getLogger(__name__)

# Capability rule per logical field/action: JS evaluated on the matched element.
_CAPABILITY_JS = {
    "title": """el => ['INPUT','TEXTAREA'].includes(el.tagName) && !['file','checkbox','radio','button','submit','hidden'].includes((el.type||'').toLowerCase())
                 || el.isContentEditable""",
    "content": """el => el.isContentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'IFRAME'
                 || ['textbox'].includes(el.getAttribute('role')) || el.hasAttribute('contenteditable')""",
    "category": """el => el.tagName === 'SELECT' || ['combobox','listbox'].includes(el.getAttribute('role'))
                 || (el.tagName === 'INPUT' && !['file','checkbox','radio'].includes((el.type||'').toLowerCase()))""",
    "tags": """el => el.tagName === 'SELECT' || el.tagName === 'TEXTAREA' || el.isContentEditable
                 || ['combobox','listbox','textbox'].includes(el.getAttribute('role'))
                 || (el.tagName === 'INPUT' && !['file','checkbox','radio','button','submit'].includes((el.type||'').toLowerCase()))""",
    "image": """el => el.tagName === 'INPUT' && (el.type||'').toLowerCase() === 'file'""",
    "save_draft": """el => ['BUTTON','A'].includes(el.tagName) || el.getAttribute('role') === 'button'
                 || (el.tagName === 'INPUT' && ['button','submit'].includes((el.type||'').toLowerCase()))""",
    "publish": """el => ['BUTTON','A'].includes(el.tagName) || el.getAttribute('role') === 'button'
                 || (el.tagName === 'INPUT' && ['button','submit'].includes((el.type||'').toLowerCase()))""",
}
# File inputs are routinely styled invisible; don't flag them for visibility.
_VISIBILITY_OPTIONAL = {"image"}


class LocatorValidator:
    def __init__(self, page: Page) -> None:
        self.page = page

    def validate(self, proposal: AnalysisProposal) -> list[ValidatedItem]:
        items: list[ValidatedItem] = []
        for name in ("title", "content", "category", "tags", "image"):
            fp = getattr(proposal.fields, name)
            items.append(self.validate_one(name, fp.selector, fp.reason))
        for name in ("save_draft", "publish"):
            ap = getattr(proposal.actions, name)
            items.append(self.validate_one(name, ap.selector, ap.reason))
        return items

    def validate_one(self, name: str, selector: str | None, reason: str | None = None) -> ValidatedItem:
        if not selector:
            return ValidatedItem(
                name=name, selector=None, status=Status.NOT_FOUND,
                notes=[f"Model proposed no selector: {reason or 'no reason given'}"],
            )
        frame, count, err = self._find(selector)
        if err:
            return ValidatedItem(name=name, selector=selector, status=Status.FAIL,
                                 notes=[f"Invalid selector: {err}"])
        if count == 0 or frame is None:
            return ValidatedItem(name=name, selector=selector, status=Status.NOT_FOUND,
                                 notes=["Selector is valid but matches no element on the page"])
        return self._inspect_match(name, selector, frame, count)

    def locate(self, selector: str):
        """Return (Locator for the first match | None, match count). Looks in iframes too."""
        frame, count, err = self._find(selector)
        if err or frame is None or count == 0:
            return None, 0
        return frame.locator(selector).first, count

    def _frames(self) -> list[Frame]:
        main = self.page.main_frame
        return [main] + [f for f in self.page.frames if f != main]

    def _find(self, selector: str) -> tuple[Frame | None, int, str | None]:
        """Return (frame with matches, count, syntax error). Main frame first, then iframes."""
        first_err: str | None = None
        for frame in self._frames():
            try:
                count = frame.locator(selector).count()
            except Exception as exc:  # noqa: BLE001 - Playwright raises on bad syntax
                first_err = first_err or str(exc).splitlines()[0]
                continue
            if count:
                return frame, count, None
        # Syntax error only counts if every frame rejected it.
        if first_err and not self._syntax_ok(selector):
            return None, 0, first_err
        return None, 0, None

    def _syntax_ok(self, selector: str) -> bool:
        try:
            self.page.main_frame.locator(selector).count()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _inspect_match(self, name: str, selector: str, frame: Frame, count: int) -> ValidatedItem:
        notes: list[str] = []
        status = Status.PASS
        loc = frame.locator(selector)
        if count > 1:
            status = Status.NEEDS_REVIEW
            notes.append(f"Ambiguous: selector matches {count} elements (expected 1)")
        target = loc.first
        capable = bool(target.evaluate(_CAPABILITY_JS[name]))
        if not capable:
            return ValidatedItem(
                name=name, selector=selector, status=Status.FAIL, match_count=count,
                visible=target.is_visible(), interactable=False,
                notes=notes + [f"Element does not support the interaction required for '{name}'"],
            )
        visible = target.is_visible()
        if not visible and name not in _VISIBILITY_OPTIONAL:
            status = Status.NEEDS_REVIEW if status == Status.PASS else status
            notes.append("Element exists but is not visible")
        interactable = visible or name in _VISIBILITY_OPTIONAL
        if target.evaluate("el => !!el.disabled || el.hasAttribute('readonly')"):
            interactable = False
            status = Status.NEEDS_REVIEW if status == Status.PASS else status
            notes.append("Element is disabled or read-only")
        if frame != self.page.main_frame:
            notes.append(f"Located inside iframe '{frame.name or frame.url}'")
        return ValidatedItem(name=name, selector=selector, status=status, match_count=count,
                             visible=visible, interactable=interactable, notes=notes)
