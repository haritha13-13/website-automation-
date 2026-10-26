"""Deterministic post-submission checks: content accuracy, status, and article URL."""
from __future__ import annotations

import re
from typing import Callable

from playwright.sync_api import Page

from services.execution_service import normalize
from services.models import Check, ContentItem, ExecutionPlan, PageSnapshot, VerificationResult

DEFAULT_SUCCESS = {"draft": r"\bsaved\b|\bsuccess", "publish": r"\bpublished\b|\bsuccess"}
# `page_call` opens/uses a *separate* tab to fetch the article; supplied by the pipeline.
ArticleFetcher = Callable[[str], tuple[int | None, str]]  # url -> (http status or None, body text)


def verify(item: ContentItem, plan: ExecutionPlan, snap: PageSnapshot, editor_url: str,
           fetch_article: ArticleFetcher | None = None, login: str | None = None) -> VerificationResult:
    checks: list[Check] = []
    mode = "publish" if item.publish_mode == "publish" else "draft"
    body = normalize(snap.body_text)

    # 0. Was the browser still logged in when the submission happened?
    if snap.login_form_visible:
        checks.append(Check(name="login", status="FAIL", detail="login form is showing after submission (session ended)"))
    elif login in {"success", "already_logged_in", "manual"}:
        checks.append(Check(name="login", status="PASS",
                            detail={"success": "automatic login accepted", "manual": "manual login completed",
                                    "already_logged_in": "session already authenticated"}[login]))
    else:
        checks.append(Check(name="login", status="SKIPPED", detail="site did not ask for a login"))

    # 1. Did the site accept the submission?
    hint = plan.success.published_text if mode == "publish" else plan.success.draft_text
    pattern = hint or DEFAULT_SUCCESS[mode]
    url_changed = snap.url != editor_url
    rejected = False
    if snap.banners:
        rejected = True
        checks.append(Check(name="submission", status="FAIL", detail=f"site reported: {snap.banners[0]}"))
    elif re.search(pattern, body, re.I) or url_changed:
        why = "confirmation text found" if re.search(pattern, body, re.I) else "page navigated"
        checks.append(Check(name="submission", status="PASS", detail=why))
    else:
        checks.append(Check(name="submission", status="FAIL", detail="no confirmation message or navigation seen"))

    # 2-6. Content accuracy, against whatever the page still exposes.
    checks.append(_compare("title", normalize(item.title), snap.title_value, body))
    exp_content = normalize(item.content)
    if snap.content_text is None:
        checks.append(Check(name="content", status="SKIPPED", detail="editor not visible after submission"))
    else:
        got = normalize(snap.content_text)
        checks.append(Check(name="content", status="PASS" if got == exp_content or exp_content in got else "FAIL",
                            detail="matches" if got == exp_content or exp_content in got
                            else f"expected {len(exp_content)} chars, page has {len(got)}"))
    if item.category:
        if snap.category_selected is None:
            checks.append(Check(name="category", status="SKIPPED", detail="control not visible after submission"))
        else:
            ok = normalize(snap.category_selected).lower() == item.category.lower()
            checks.append(Check(name="category", status="PASS" if ok else "FAIL",
                                detail="matches" if ok else f"page shows '{snap.category_selected}'"))
    if item.tags:
        if snap.tags_value is None:
            checks.append(Check(name="tags", status="SKIPPED", detail="control not visible after submission"))
        else:
            have = normalize(snap.tags_value).lower()
            missing = [t for t in item.tags if t.lower() not in have]
            checks.append(Check(name="tags", status="FAIL" if missing else "PASS",
                                detail=f"missing: {', '.join(missing)}" if missing else "all tags present"))
    if item.images:
        if snap.image_count is None:
            checks.append(Check(name="images", status="SKIPPED", detail="upload control not visible after submission"))
        else:
            ok = snap.image_count == len(item.images)
            checks.append(Check(name="images", status="PASS" if ok else "FAIL",
                                detail=f"{snap.image_count} of {len(item.images)} attached"))

    # 7. Article URL (required for publish).
    article_url = snap.article_link or (snap.url if url_changed else None)
    if mode == "draft":
        checks.append(Check(name="article_url", status="SKIPPED",
                            detail=article_url or "drafts are not publicly addressable"))
    elif not article_url:
        checks.append(Check(name="article_url", status="FAIL", detail="no article URL could be determined"))
    elif fetch_article is None:
        checks.append(Check(name="article_url", status="SKIPPED", detail=f"not fetched: {article_url}"))
    else:
        status, text = fetch_article(article_url)
        ok = (status is None or status < 400) and normalize(item.title).lower() in normalize(text).lower()
        checks.append(Check(name="article_url", status="PASS" if ok else "FAIL",
                            detail=f"HTTP {status or 'n/a'}; title {'found' if ok else 'not found'} at {article_url}"))

    failed = [c for c in checks if c.status == "FAIL"]
    required_skipped = [c for c in checks if c.name in {"title", "content"} and c.status == "SKIPPED"]
    if failed:
        status = "failed"
    elif len(required_skipped) == 2:
        status = "unconfirmed"  # nothing about the content could be checked
    else:
        status = "verified"
    return VerificationResult(status=status, checks=checks, article_url=article_url if mode == "publish" or snap.article_link else None,
                              rejected_by_site=rejected or snap.login_form_visible)


def _compare(name: str, expected: str, actual: str | None, body: str) -> Check:
    if actual is None:
        if expected.lower() in body.lower():
            return Check(name=name, status="PASS", detail="found in page text")
        return Check(name=name, status="SKIPPED", detail="field not visible after submission")
    ok = normalize(actual) == expected
    return Check(name=name, status="PASS" if ok else "FAIL", detail="matches" if ok else f"page shows '{normalize(actual)[:80]}'")


def _norm_url(u: str) -> str:
    return (u or "").split("#")[0].rstrip("/").lower()


def verify_playbook(item: ContentItem, steps_done: int, steps_total: int, user_published: bool, captured_url: str | None,
                    editor_url: str, fetch_page: Callable[[str], dict] | None, login: str | None = None) -> VerificationResult:
    """Deterministic checks for a published playbook row: login, steps, project page, backlink present, backlink works."""
    checks: list[Check] = []
    if login in {"success", "already_logged_in", "manual"}:
        checks.append(Check(name="login", status="PASS", detail={"success": "automatic login accepted",
                                                                  "manual": "manual login completed",
                                                                  "already_logged_in": "session already authenticated"}[login]))
    else:
        checks.append(Check(name="login", status="SKIPPED", detail="site did not ask for a login"))
    checks.append(Check(name="playbook", status="PASS" if steps_done >= steps_total else "FAIL",
                        detail=f"{steps_done} of {steps_total} steps completed"))
    if not user_published:
        checks.append(Check(name="published", status="SKIPPED", detail="stopped at review: nothing was published"))
        return VerificationResult(status="unconfirmed", checks=checks)
    checks.append(Check(name="published", status="PASS", detail="you confirmed publishing"))

    url = captured_url if captured_url and _norm_url(captured_url) != _norm_url(editor_url) else None
    if not url:
        checks.append(Check(name="project_url", status="FAIL", detail="no published project address was recorded"))
        return VerificationResult(status="failed", checks=checks)
    if fetch_page is None:
        checks.append(Check(name="project_url", status="SKIPPED", detail=f"not fetched: {url}"))
        return VerificationResult(status="unconfirmed", checks=checks, article_url=url)
    page = fetch_page(url)
    ok = (page["status"] is None or page["status"] < 400) and normalize(item.title).lower() in normalize(page["text"]).lower()
    checks.append(Check(name="project_url", status="PASS" if ok else "FAIL",
                        detail=f"HTTP {page['status'] or 'n/a'}; title {'found' if ok else 'not found'} at the project page"))
    if item.blog_url:
        want = _norm_url(item.blog_url)
        hits = [l for l in page["links"] if _norm_url(l["href"]) == want]
        if not hits:
            checks.append(Check(name="backlink_present", status="FAIL", detail=f"no link to {item.blog_url} on the project page"))
        else:
            text_ok = not item.anchor_text or any(item.anchor_text.lower() in normalize(l["text"]).lower() for l in hits)
            checks.append(Check(name="backlink_present", status="PASS" if text_ok else "FAIL",
                                detail="link found" + ("" if text_ok else f", but its text is not '{item.anchor_text}'")))
            target = fetch_page(hits[0]["href"])
            good = target["status"] is None or target["status"] < 400
            checks.append(Check(name="backlink_target", status="PASS" if good else "FAIL",
                                detail=f"the link opens (HTTP {target['status'] or 'n/a'}) at {target['final_url'].split('?')[0]}"))
    failed = any(c.status == "FAIL" for c in checks)
    return VerificationResult(status="failed" if failed else "verified", checks=checks, article_url=url)
