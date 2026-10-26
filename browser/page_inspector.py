"""Extract a sanitized, structured description of the current page's form controls."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

from playwright.sync_api import Frame, Page
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_ELEMENTS = 120
MAX_TEXT = 120
SENSITIVE_NAME = re.compile(r"csrf|xsrf|token|nonce|session|secret|passw|otp|api[_-]?key|auth", re.I)

# Runs inside each frame. Never reads input values; skips password/hidden/sensitive controls.
_COLLECTOR_JS = r"""
({maxElements, maxText, sensitive}) => {
  const sensitiveRe = new RegExp(sensitive, 'i');
  const clip = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, maxText);
  const visible = el => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const esc = v => CSS.escape(v);
  const unique = sel => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  const selectorFor = el => {
    const tag = el.tagName.toLowerCase();
    if (el.id && unique('#' + esc(el.id))) return '#' + esc(el.id);
    for (const a of ['data-testid', 'data-test', 'name']) {
      const v = el.getAttribute(a);
      if (v) { const s = `${tag}[${a}="${v.replace(/"/g, '\\"')}"]`; if (unique(s)) return s; }
    }
    if (el.getAttribute('contenteditable') === 'true' || el.getAttribute('contenteditable') === '') {
      if (unique('[contenteditable]')) return '[contenteditable]';
    }
    const path = []; let n = el;
    while (n && n.nodeType === 1 && n !== document.body) {
      let p = n.tagName.toLowerCase();
      const sib = n.parentElement ? [...n.parentElement.children].filter(c => c.tagName === n.tagName) : [];
      if (sib.length > 1) p += `:nth-of-type(${sib.indexOf(n) + 1})`;
      path.unshift(p); n = n.parentElement;
    }
    return path.join(' > ');
  };
  const labelFor = el => {
    const out = [];
    if (el.labels) for (const l of el.labels) out.push(clip(l.innerText));
    const al = el.getAttribute('aria-label'); if (al) out.push(clip(al));
    const lb = el.getAttribute('aria-labelledby');
    if (lb) for (const id of lb.split(/\s+/)) { const t = document.getElementById(id); if (t) out.push(clip(t.innerText)); }
    return [...new Set(out.filter(Boolean))];
  };
  const kindOf = el => {
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textarea';
    if (tag === 'button' || (tag === 'input' && ['button', 'submit', 'reset'].includes(type)) || ['button', 'menuitem', 'tab'].includes(el.getAttribute('role'))) return 'button';
    if (tag === 'input' && type === 'file') return 'file';
    if (tag === 'input') return 'input';
    if (el.isContentEditable) return 'contenteditable';
    if (['textbox', 'combobox', 'listbox'].includes(el.getAttribute('role'))) return 'custom_' + el.getAttribute('role');
    return null;
  };
  const sel = 'input, textarea, select, button, [contenteditable]:not([contenteditable="false"]), [role=button], [role=menuitem], [role=tab], [role=textbox], [role=combobox], [role=listbox]';
  const elements = [];
  for (const el of document.querySelectorAll(sel)) {
    if (elements.length >= maxElements) break;
    const kind = kindOf(el); if (!kind) continue;
    if (el.parentElement && el.parentElement.closest('[contenteditable]:not([contenteditable="false"])') && kind !== 'contenteditable') continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    const name = el.getAttribute('name') || '';
    if (type === 'password' || type === 'hidden') continue;
    if (sensitiveRe.test(name) || sensitiveRe.test(el.id || '')) continue;
    const e = {
      kind, tag: el.tagName.toLowerCase(), type: type || null,
      id: el.id || null, name: name || null,
      role: el.getAttribute('role'), labels: labelFor(el),
      placeholder: clip(el.getAttribute('placeholder')) || null,
      text: kind === 'button' ? clip(el.innerText || el.getAttribute('value')) : null,
      title_attr: clip(el.getAttribute('title')) || null,
      visible: visible(el), disabled: !!el.disabled,
      readonly: el.hasAttribute('readonly'), required: el.required === true,
      accept: el.getAttribute('accept'), multiple: el.multiple === true || null,
      selector: selectorFor(el),
      form: el.form ? (el.form.id || el.form.getAttribute('name') || 'form') : null,
    };
    if (kind === 'select') e.options = [...el.options].slice(0, 30).map(o => clip(o.text));
    elements.push(e);
  }
  const links = [...document.querySelectorAll('a[href]')].slice(0, 80).map(a => {
    let href = a.getAttribute('href') || '';
    try { const u = new URL(href, location.href); href = u.origin + u.pathname; } catch (e) {}
    return {text: clip(a.innerText || a.getAttribute('aria-label')), href, visible: visible(a)};
  }).filter(l => l.text);
  const sections = [...document.querySelectorAll('form, fieldset, [role=form], main h1, main h2, h1, h2')]
    .filter(visible).slice(0, 25).map(s => ({
      tag: s.tagName.toLowerCase(), id: s.id || null,
      text: clip(s.tagName === 'FORM' || s.tagName === 'FIELDSET' ? (s.getAttribute('aria-label') || (s.querySelector('legend,h1,h2,h3') || {}).innerText) : s.innerText),
    }));
  return {
    elements, links, sections,
    has_password_field: !!document.querySelector('input[type=password]'),
    body_text_length: (document.body ? document.body.innerText.length : 0),
  };
}
"""


class ElementObservation(BaseModel):
    kind: Literal[
        "input", "textarea", "select", "button", "file", "contenteditable",
        "custom_textbox", "custom_combobox", "custom_listbox",
    ]
    tag: str
    type: str | None = None
    id: str | None = None
    name: str | None = None
    role: str | None = None
    labels: list[str] = Field(default_factory=list)
    placeholder: str | None = None
    text: str | None = None
    title_attr: str | None = None
    visible: bool = True
    disabled: bool = False
    readonly: bool = False
    required: bool = False
    accept: str | None = None
    multiple: bool | None = None
    selector: str
    form: str | None = None
    options: list[str] | None = None
    frame: str | None = Field(default=None, description="None = main frame, else iframe name/url")


class LinkObservation(BaseModel):
    text: str
    href: str
    visible: bool = True


class SectionObservation(BaseModel):
    tag: str
    id: str | None = None
    text: str | None = None


class PageObservation(BaseModel):
    title: str
    url: str
    elements: list[ElementObservation] = Field(default_factory=list)
    links: list[LinkObservation] = Field(default_factory=list)
    sections: list[SectionObservation] = Field(default_factory=list)
    has_password_field: bool = False
    frame_count: int = 1
    inspected_frames: int = 1

    @property
    def is_empty(self) -> bool:
        return not self.elements

    def for_llm(self) -> dict:
        """Compact, sanitized payload. Contains no values, cookies, or tokens."""
        return {
            "title": self.title,
            "url": _strip_query(self.url),
            "elements": [e.model_dump(exclude_none=True, exclude_defaults=False) for e in self.elements],
            "links": [l.model_dump() for l in self.links[:25] if l.visible],
            "sections": [s.model_dump(exclude_none=True) for s in self.sections],
        }


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]


class PageInspector:
    def __init__(self, page: Page, screenshot_dir: Path | str = "screenshots") -> None:
        self.page = page
        self.screenshot_dir = Path(screenshot_dir)

    def inspect(self) -> PageObservation:
        """Inspect the current page only (plus its accessible iframes)."""
        args = {"maxElements": MAX_ELEMENTS, "maxText": MAX_TEXT, "sensitive": SENSITIVE_NAME.pattern}
        self._wait_for_content()
        main = self._collect(self.page.main_frame, args)
        obs = PageObservation(
            title=self.page.title(), url=self.page.url,
            elements=[ElementObservation(**e) for e in main["elements"]],
            links=[LinkObservation(**l) for l in main["links"]],
            sections=[SectionObservation(**s) for s in main["sections"]],
            has_password_field=main["has_password_field"],
        )
        frames = [f for f in self.page.frames if f != self.page.main_frame]
        obs.frame_count = 1 + len(frames)
        for frame in frames:
            if len(obs.elements) >= MAX_ELEMENTS:
                break
            try:
                data = self._collect(frame, args)
            except Exception as exc:  # noqa: BLE001 - cross-origin / detached frames
                logger.debug("Skipping inaccessible iframe %s: %s", frame.url, exc)
                continue
            label = frame.name or _strip_query(frame.url) or "iframe"
            obs.inspected_frames += 1
            obs.has_password_field |= data["has_password_field"]
            for e in data["elements"]:
                obs.elements.append(ElementObservation(**e, frame=label))
        return obs

    def _wait_for_content(self) -> None:
        """Wait (bounded) for at least one interactive control to render; no fixed sleeps."""
        # A page that already shows links (a dashboard or listing) is unlikely to be still rendering: wait less.
        try:
            has_links = self.page.locator("a[href]").count() > 0
        except Exception:  # noqa: BLE001 - mid-navigation
            has_links = False
        try:
            self.page.wait_for_selector(
                "input:not([type=hidden]), textarea, select, button, [contenteditable]",
                state="attached", timeout=2_000 if has_links else 8_000,
            )
        except Exception:  # noqa: BLE001 - empty/unexpected DOM is reported later
            logger.debug("No interactive controls appeared within the wait window")

    @staticmethod
    def _collect(frame: Frame, args: dict) -> dict:
        return frame.evaluate(_COLLECTOR_JS, args)

    def take_screenshot(self, name: str = "page.png") -> Path:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.screenshot_dir / name
        self.page.screenshot(path=str(path), full_page=True)
        return path
