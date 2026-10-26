"""Playbooks: an ordered list of deterministic steps for sites whose editor is not a simple form
(for example a block editor with a second "publish settings" page). Steps are stored as JSON, run with
Playwright, and every control is found by what the person sees (role + visible name), never by guesswork.
The model is not used while a playbook runs."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from pydantic import BaseModel, Field, ValidationError, model_validator

from browser.page_inspector import PageInspector
from browser.playwright_manager import goto_settled
from services.auth import same_site
from services.discovery import FORBIDDEN
from services.secrets import REDACTOR

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
STEP_TIMEOUT_MS = 15_000
ACTIONS = {"goto", "click", "upload", "type_text", "link_selection", "fill", "select", "add_tags", "expect",
           "wait_user", "capture_url"}
NEEDS_TARGET = {"click", "upload", "fill", "select", "add_tags", "expect"}


class PlaybookError(RuntimeError):
    def __init__(self, message: str, step_id: str = "") -> None:
        super().__init__(message)
        self.step_id = step_id


class Playbook(BaseModel):
    name: str
    site: str  # host (and subdomains) this playbook applies to
    description: str = ""
    verified: bool = False  # True once it has completed on the live website
    requires: list[str] = Field(default_factory=list, description="ContentItem fields a row must have")
    steps: list[dict[str, Any]]

    @model_validator(mode="after")
    def _check(self) -> "Playbook":
        seen: set[str] = set()
        for i, st in enumerate(self.steps, 1):
            sid = st.setdefault("id", f"step{i}")
            if sid in seen:
                raise ValueError(f"duplicate step id '{sid}'")
            seen.add(sid)
            if st.get("action") not in ACTIONS:
                raise ValueError(f"step '{sid}': unknown action '{st.get('action')}'")
            if st["action"] in NEEDS_TARGET and not st.get("target", {}).get("candidates"):
                raise ValueError(f"step '{sid}': action '{st['action']}' needs target.candidates")
        return self


class PlaybookStore:
    def __init__(self, directory: Path | str = ROOT / "playbooks") -> None:
        self.directory = Path(directory)

    def find(self, url: str) -> Playbook | None:
        host = (urlparse(url).hostname or "").lower()
        if not host or not self.directory.is_dir():
            return None
        for path in sorted(self.directory.glob("*.json")):
            try:
                pb = Playbook.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValidationError, ValueError, OSError) as exc:
                logger.warning("Ignoring invalid playbook %s: %s", path.name, str(exc)[:160])
                continue
            if same_site(host, pb.site.lower()):
                return pb
        return None


# ---- placeholders -------------------------------------------------------------------------------------
def render(value: Any, ctx: dict[str, Any]) -> Any:
    """'{title}' -> ctx['title'] (keeps lists); 'Hello {title}' -> text."""
    if not isinstance(value, str):
        return value
    whole = re.fullmatch(r"\{(\w+)\}", value)
    if whole:
        if whole.group(1) not in ctx:
            raise PlaybookError(f"no value for {{{whole.group(1)}}} in this row")
        return ctx[whole.group(1)]
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx.get(m.group(1), m.group(0))), value)


# ---- finding controls -----------------------------------------------------------------------------------
def _locator(page: Page, cand: dict[str, Any]) -> Locator:
    exact = bool(cand.get("exact"))
    pat = lambda s: s if exact else re.compile(re.escape(s), re.I)  # noqa: E731
    if "role" in cand:
        name = cand.get("name")
        return page.get_by_role(cand["role"], name=pat(name) if name else None, exact=exact if name else None)
    if "text" in cand:
        return page.get_by_text(cand["text"], exact=exact) if exact else page.get_by_text(pat(cand["text"]))
    if "placeholder" in cand:
        return page.get_by_placeholder(pat(cand["placeholder"]))
    if "label" in cand:
        return page.get_by_label(pat(cand["label"]))
    if "css" in cand:
        return page.locator(cand["css"])
    raise PlaybookError(f"unusable target candidate: {cand}")


def resolve(page: Page, target: dict[str, Any], timeout_ms: int = STEP_TIMEOUT_MS, hidden_ok: bool = False) -> Locator:
    """Wait (bounded) until any candidate is on the page, then return the best one: candidates are in
    order of preference. Controls whose visible name looks destructive are never returned."""
    cands = target["candidates"]
    locs = [_locator(page, c) for c in cands]
    union = locs[0]
    for extra in locs[1:]:
        union = union.or_(extra)
    try:
        union.first.wait_for(state="attached" if hidden_ok else "visible", timeout=timeout_ms)
    except PlaywrightTimeout as exc:
        names = ", ".join(c.get("name") or c.get("text") or c.get("placeholder") or c.get("label") or c.get("css", "?")
                          for c in cands)
        raise PlaywrightTimeout(f"none of these controls appeared: {names}") from exc
    for loc in locs:
        for i in range(min(loc.count(), 10)):
            item = loc.nth(i)
            if not hidden_ok and not item.is_visible():
                continue
            label = (item.inner_text(timeout=1_000) if item.is_visible() else "") or ""
            if FORBIDDEN.search(label.strip()[:60]):
                continue
            return item
    raise PlaybookError("the control exists but is not usable (hidden or looks destructive)")


def _settle(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=3_000)
    except PlaywrightTimeout:
        pass


_SELECT_TEXT_JS = """
(anchor) => {
  const editors = [...document.querySelectorAll('[contenteditable]:not([contenteditable="false"])')];
  for (const ed of editors) {
    const w = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      let i = n.nodeValue.indexOf(anchor);
      if (i < 0) i = n.nodeValue.toLowerCase().indexOf(anchor.toLowerCase());
      if (i >= 0) {
        ed.focus();
        const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + anchor.length);
        const s = getSelection(); s.removeAllRanges(); s.addRange(r);
        return true;
      }
    }
  }
  return false;
}
"""
LINK_BUTTON = {"candidates": [{"role": "button", "name": "Link"}, {"css": "[aria-label*='link' i]"}]}
LINK_INPUT = {"candidates": [{"placeholder": "link"}, {"placeholder": "url"}, {"role": "textbox", "name": "link"},
                              {"role": "textbox", "name": "url"}, {"placeholder": "http"}]}


def execute_step(page: Page, step: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Run one browser step. Returns a short detail string; raises on any problem (never guesses)."""
    action, timeout = step["action"], int(step.get("timeout_ms", STEP_TIMEOUT_MS))
    if action == "goto":
        goto_settled(page, render(step["url"], ctx))
        return "opened"
    if action == "capture_url":
        _settle(page)
        ctx["captured_url"] = page.url
        return "recorded the page address"
    if action in NEEDS_TARGET:
        loc = resolve(page, step["target"], timeout, hidden_ok=action == "upload" or bool(step.get("hidden_ok")))
        if action == "expect":
            return "present"
        if action == "click":
            loc.click(timeout=timeout)
            _settle(page)
            return "clicked"
        if action == "fill":
            loc.fill(str(render(step["value"], ctx)), timeout=timeout)
            return "filled"
        if action == "upload":
            files = render(step["files"], ctx)
            files = [files] if isinstance(files, str) else list(files or [])
            if not files:
                raise PlaybookError("no file to upload for this row")
            is_input = loc.evaluate("el => el.tagName === 'INPUT' && el.type === 'file'")
            if is_input:
                loc.set_input_files(files, timeout=timeout)
            else:
                with page.expect_file_chooser(timeout=timeout) as chooser:
                    loc.click(timeout=timeout)
                chooser.value.set_files(files)
            _settle(page)
            return f"uploaded {len(files)} file(s)"
        if action == "select":
            value = str(render(step["value"], ctx))
            if loc.evaluate("el => el.tagName") == "SELECT":
                try:
                    loc.select_option(label=value, timeout=3_000)
                except PlaywrightTimeout:
                    loc.select_option(value=value, timeout=3_000)
                return f"selected '{value}'"
            loc.click(timeout=timeout)
            option = page.get_by_role("option", name=re.compile(re.escape(value), re.I))
            if option.count() == 0:
                loc.fill(value) if loc.evaluate("el => el.tagName === 'INPUT'") else None
            try:
                option.first.wait_for(state="visible", timeout=4_000)
            except PlaywrightTimeout as exc:
                shown = [t.strip() for t in page.get_by_role("option").all_inner_texts()][:15]
                raise PlaybookError(f"'{value}' is not an available option (seen: {', '.join(shown) or 'none'})") from exc
            option.first.click()
            return f"selected '{value}'"
        if action == "add_tags":
            tags = render(step["value"], ctx)
            tags = [tags] if isinstance(tags, str) else list(tags or [])
            if not tags:
                return "no tags for this row"
            loc.click(timeout=timeout)
            for tag in tags:
                loc.fill(tag, timeout=timeout)
                loc.press("Enter")
            return f"added {len(tags)} tag(s)"
    if action == "type_text":
        if step.get("target"):
            resolve(page, step["target"], timeout).click(timeout=timeout)
        text = str(render(step["value"], ctx))
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                page.keyboard.insert_text(line)
            if i < len(lines) - 1:
                page.keyboard.press("Enter")
        return f"typed {len(text)} characters"
    if action == "link_selection":
        anchor, url = str(render(step["text"], ctx)), str(render(step["url"], ctx))
        if not page.evaluate(_SELECT_TEXT_JS, anchor):
            raise PlaybookError(f"the text '{anchor}' was not found in an editable block (it must be inside one text run)")
        resolve(page, step.get("toolbar") or LINK_BUTTON, timeout).click(timeout=timeout)
        box = resolve(page, step.get("input") or LINK_INPUT, timeout)
        box.fill(url, timeout=timeout)
        if step.get("confirm"):
            resolve(page, step["confirm"], timeout).click(timeout=timeout)
        else:
            box.press("Enter")
        _settle(page)
        return f"linked '{anchor}'"
    raise PlaybookError(f"cannot execute action '{action}' here")


def collect_debug(page: Page) -> dict[str, Any]:
    """What the page offers right now (names only, no typed values): to paste when a step fails."""
    obs = PageInspector(page).inspect()
    p = urlparse(obs.url)
    return {"page": f"{p.scheme}://{p.netloc}{p.path}", "title": obs.title,
            "buttons": [e.text or (e.labels[0] if e.labels else "") for e in obs.elements if e.kind == "button" and e.visible][:40],
            "inputs": [{"kind": e.kind, "label": (e.labels[0] if e.labels else ""), "placeholder": e.placeholder, "name": e.name}
                       for e in obs.elements if e.kind != "button" and e.visible][:30],
            "links": [l.text for l in obs.links if l.visible][:40]}


def write_debug(directory: Path, run_id: int, row: int, step_id: str, error: str, info: dict[str, Any],
                screenshot: str | None, name: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name or f"playbook_debug_run{run_id}_row{row}_{step_id}.json")
    text = json.dumps({"failed_step": step_id, "error": error, "screenshot_inputs_masked": screenshot, **info}, indent=2)
    path.write_text(REDACTOR.redact(text), encoding="utf-8")
    return path


def build_context(item) -> dict[str, Any]:
    """Values a playbook can reference as {name}."""
    return {"title": item.title, "content": item.content, "description": item.content, "category": item.category,
            "tags": list(item.tags), "blog_url": item.blog_url, "anchor_text": item.anchor_text,
            "images": list(item.image_paths), "cover_image_files": [item.cover_image_path] if item.cover_image_path else []}


def applies(step: dict[str, Any], publish_mode: str) -> bool:
    modes = step.get("modes")
    return not modes or publish_mode in modes
