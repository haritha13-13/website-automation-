"""Workflow discovery helpers used by Agent 2: which observed link/button to click next, and how to
click it or replay a saved sequence. Everything here is deterministic and validated against what the
browser actually shows; the model only suggests candidates by their visible text."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from playwright.sync_api import Page

from browser.page_inspector import PageObservation
from browser.playwright_manager import NavigationError, goto_settled
from services.auth import same_site

FORBIDDEN = re.compile(r"log ?out|sign ?out|delete|remove|unsubscribe|deactivate|password|account|settings|billing", re.I)
WANT = re.compile(r"\b(create|new|add|write|compose|start|upload|publish|submit)\b", re.I)
WANT_NOUN = re.compile(r"\b(project|article|post|story|portfolio|content|editor|entry|page)s?\b", re.I)
AVOID = re.compile(r"\b(hire|hiring|freelanc\w*|jobs?|career\w*|pricing|plans?|help|support|terms|privacy|cookie\w*|"
                   r"about|contact|download|app|learn|press|advertis\w*)\b", re.I)


def score(text: str) -> int:
    t = text or ""
    return (3 if WANT.search(t) else 0) + (2 if WANT_NOUN.search(t) else 0) - (4 if AVOID.search(t) else 0)


def nav_candidates(obs: PageObservation) -> list[dict]:
    """Links and buttons that are visible and safe to click (never form submit buttons)."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    cur = urlparse(obs.url)
    for link in obs.links:
        t = link.text.strip()
        target = urlparse(link.href)
        if not t or not link.visible or FORBIDDEN.search(t) or FORBIDDEN.search(link.href):
            continue
        if target.scheme not in {"http", "https", "file"}:
            continue
        if cur.scheme != "file" and not same_site(target.hostname or "", cur.hostname or ""):
            continue
        if link.href.rstrip("/") == f"{cur.scheme}://{cur.netloc}{cur.path}".rstrip("/"):
            continue  # a link to the page we are on
        key = ("link", t.lower())
        if key not in seen:
            seen.add(key)
            out.append({"kind": "link", "text": t, "href": link.href})
    for e in obs.elements:
        t = (e.text or e.labels[0] if (e.text or e.labels) else "").strip()
        if e.kind != "button" or not t or not e.visible or e.disabled or e.form or not e.selector or e.frame:
            continue
        if e.type in {"submit", "reset"} or FORBIDDEN.search(t):
            continue
        key = ("button", t.lower())
        if key not in seen:
            seen.add(key)
            out.append({"kind": "button", "text": t, "selector": e.selector})
    return out


def for_llm(cands: list[dict], limit: int = 40) -> list[dict]:
    """Most promising first, so the useful ones are never cut off on busy pages."""
    ranked = sorted(cands, key=lambda c: -score(c["text"]))[:limit]
    return [{"kind": c["kind"], "text": c["text"]} for c in ranked]


def find_candidate(cands: list[dict], text: str, kind: str | None = None) -> dict | None:
    want = text.strip().casefold()
    pool = [c for c in cands if kind in (None, c["kind"])]
    exact = [c for c in pool if c["text"].casefold() == want]
    if exact:
        return exact[0]
    partial = [c for c in pool if want and want in c["text"].casefold()]
    return partial[0] if len(partial) == 1 else (sorted(partial, key=lambda c: len(c["text"]))[0] if partial else None)


_find = find_candidate  # backwards-compatible alias


def rank_candidates(cands: list[dict], llm: list[tuple[str, str]], hints: list[str], visited: set[tuple[str, str]],
                    limit: int = 3) -> list[dict]:
    """hints (the person's words) > model picks that look right > keyword matches > model picks that look wrong."""
    ordered: list[dict] = []
    for h in hints:
        c = find_candidate(cands, h)
        if c:
            ordered.append(c)
    picks = [c for text, kind in llm if (c := find_candidate(cands, text, kind or None))]
    ordered += [c for c in picks if score(c["text"]) >= 0]
    ordered += [c for c in sorted(cands, key=lambda c: -score(c["text"])) if score(c["text"]) > 0]
    ordered += [c for c in picks if score(c["text"]) < 0]
    result, used = [], set()
    for c in ordered:
        key = (c["kind"], c["text"].lower())
        if key in used or key in visited:
            continue
        used.add(key)
        result.append(c)
    return result[:limit]


def click_candidate(page: Page, cand: dict) -> str:
    """Perform one validated click (or navigation for a link). Returns the URL afterwards."""
    if cand["kind"] == "link":
        return goto_settled(page, cand["href"])
    loc = page.locator(cand["selector"])
    if loc.count() != 1 or not loc.first.is_visible():
        raise NavigationError(f"button '{cand['text']}' is not uniquely visible any more")
    start = page.url
    loc.first.click()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
        page.wait_for_load_state("networkidle", timeout=4_000)
    except Exception:  # noqa: BLE001 - menus open without any load event
        pass
    return page.url if page.url != start else start


def step_for(cand: dict) -> dict:
    s = {"action": "goto" if cand["kind"] == "link" else "click", "kind": cand["kind"], "text": cand["text"]}
    s["url" if cand["kind"] == "link" else "selector"] = cand.get("href") or cand.get("selector")
    return s


def replay_steps(page: Page, steps: list[dict]) -> str | None:
    """Replay saved steps deterministically. Returns an error message, or None when every step worked."""
    for i, step in enumerate(steps, 1):
        try:
            if step["action"] == "goto":
                goto_settled(page, step["url"])
                continue
            loc = page.locator(step["selector"])
            if loc.count() != 1:  # the selector changed: fall back to the button's accessible name
                loc = page.get_by_role("button", name=step["text"], exact=True)
                if loc.count() != 1:
                    loc = page.get_by_role("menuitem", name=step["text"], exact=True)
            if loc.count() != 1 or not loc.first.is_visible():
                return f"step {i}: '{step['text']}' not found exactly once on the page"
            loc.first.click()
            try:
                page.wait_for_load_state("networkidle", timeout=4_000)
            except Exception:  # noqa: BLE001
                pass
        except NavigationError as exc:
            return f"step {i}: {exc}"
    return None
