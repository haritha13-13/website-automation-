"""Screenshots that never show typed values: every input is masked."""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page

from services.secrets import REDACTOR

MASK_SELECTOR = "input, textarea, select, [contenteditable]"


def safe_screenshot(page: Page, path: Path | str, secrets: list[str] | None = None) -> Path:
    """Full-page screenshot with all form controls (and any visible secret text) covered."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    masks = [page.locator(MASK_SELECTOR)]
    for s in secrets or []:
        if s and len(s) >= 3:
            masks.append(page.get_by_text(s, exact=False))
    page.screenshot(path=str(target), full_page=True, mask=masks, mask_color="#000000")
    return target
