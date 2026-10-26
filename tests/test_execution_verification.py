import pytest

from agents.browser_execution_agent import BrowserExecutionAgent
from agents.verification_agent import VerificationAgent
from services import execution_service as ex
from services.adapters import AdapterRegistry
from services.content_service import site_key
from services.database import Database
from services.models import ContentItem, ExecutionPlan, PageSnapshot
from services.verification_service import verify


def _plan(url):
    a = AdapterRegistry().find(url)
    return ExecutionPlan(website_type="custom_cms", source="adapter", adapter_name=a.name,
                         field_mapping=dict(a.fields), actions=dict(a.actions), validation_status="passed",
                         success=a.success)


def _item(url, png, mode="draft", **kw):
    base = dict(row_number=2, website_url=url, title="Benefits of AI", content="Line one.\nLine two.",
                category="Technology", tags=["AI", "Automation"], images=["images/automation.png"],
                publish_mode=mode, content_hash="h" * 64, image_paths=[str(png)])
    return ContentItem(**{**base, **kw})


def _run(browser_page, url, png, mode, approve=True):
    mgr = browser_page
    db = Database(":memory:")
    run = db.start_run("t")
    item = _item(url, png, mode)
    db_id = db.add_item(run, 2, site_key(url), url, item.title, item.content_hash, mode, "validated")
    asked = []
    agent = BrowserExecutionAgent(db, run, lambda i, p, s: (asked.append(i) or approve), lambda i, n: False)
    out = agent.execute_item(item, db_id, _plan(url), lambda fn: fn(mgr.page), lambda: mgr.navigate(url))
    return item, out, db, db_id, asked, mgr


def test_draft_is_filled_saved_and_verified(browser_page, publish_url, png):
    item, out, db, db_id, asked, mgr = _run(browser_page, publish_url, png, "draft")
    assert out.status == "submitted" and out.action == "save_draft" and asked
    snap = out.snapshot
    assert snap.title_value == "Benefits of AI" and snap.category_selected == "Technology"
    assert snap.image_count == 1 and "Draft saved" in snap.body_text
    res = VerificationAgent().check(item, _plan(publish_url), snap, out.editor_url, None)
    assert res.status == "verified", res.checks
    assert db.get_item(db_id)["status"] == "in_progress"  # claimed before the click


def test_publish_verifies_article_url(browser_page, publish_url, png):
    item, out, *_ = _run(browser_page, publish_url, png, "publish")
    assert out.action == "publish" and out.snapshot.article_link.startswith(publish_url + "#article=")
    fetch = lambda url: (200, "Benefits of AI body")  # noqa: E731
    res = verify(item, _plan(publish_url), out.snapshot, out.editor_url, fetch)
    assert res.status == "verified" and res.article_url == out.snapshot.article_link
    bad = verify(item, _plan(publish_url), out.snapshot, out.editor_url, lambda u: (404, "Not found"))
    assert bad.status == "failed"


def test_declined_approval_never_clicks(browser_page, publish_url, png):
    _, out, db, db_id, asked, mgr = _run(browser_page, publish_url, png, "publish", approve=False)
    assert out.status == "rejected_by_user" and asked
    assert mgr.page.inner_text("#status") == ""  # nothing was submitted
    assert db.get_item(db_id)["status"] == "validated"


def test_duplicate_is_skipped_before_touching_browser(browser_page, publish_url, png):
    mgr = browser_page
    db = Database(":memory:")
    run = db.start_run("t")
    item = _item(publish_url, png, "publish")
    db.add_item(run, 1, site_key(publish_url), publish_url, item.title, item.content_hash, "publish", "published")
    me = db.add_item(run, 2, site_key(publish_url), publish_url, item.title, item.content_hash, "publish", "validated")
    nav = []
    agent = BrowserExecutionAgent(db, run, lambda *a: True, lambda *a: False)
    out = agent.execute_item(item, me, _plan(publish_url), lambda fn: fn(mgr.page), lambda: nav.append(1) or "")
    assert out.status == "duplicate_skipped" and not nav


def test_missing_control_fails_row_without_clicking(browser_page, publish_url, png):
    plan = _plan(publish_url)
    plan.field_mapping["category"] = "#no-such-select"
    db = Database(":memory:"); run = db.start_run("t")
    item = _item(publish_url, png)
    me = db.add_item(run, 2, "s", publish_url, item.title, item.content_hash, "draft", "validated")
    agent = BrowserExecutionAgent(db, run, lambda *a: True, lambda *a: False)
    out = agent.execute_item(item, me, plan, lambda fn: fn(browser_page.page), lambda: browser_page.navigate(publish_url))
    assert out.status == "failed" and "category" in out.error


def test_unknown_category_option_fails(browser_page, publish_url, png):
    db = Database(":memory:"); run = db.start_run("t")
    item = _item(publish_url, png, category="Sports")
    me = db.add_item(run, 2, "s", publish_url, item.title, item.content_hash, "draft", "validated")
    agent = BrowserExecutionAgent(db, run, lambda *a: True, lambda *a: False)
    out = agent.execute_item(item, me, _plan(publish_url), lambda fn: fn(browser_page.page), lambda: browser_page.navigate(publish_url))
    assert out.status == "failed" and "Sports" in out.error


def test_site_rejection_detected(browser_page, publish_url, png):
    plan = _plan(publish_url)
    item = _item(publish_url, png, content="x")
    browser_page.navigate(publish_url)
    page = browser_page.page
    page.fill("#headline", "")  # leave empty so the mock CMS rejects
    page.click("#save-draft")
    snap = ex.capture_snapshot(page, plan)
    res = verify(item, plan, snap, publish_url, None)
    assert res.status == "failed" and res.rejected_by_site


def test_verification_catches_wrong_content():
    plan = ExecutionPlan(website_type="x", source="adapter", validation_status="passed")
    item = ContentItem(row_number=2, website_url="https://a.com/x", title="Real title", content="Real body",
                       category="News", publish_mode="draft", content_hash="h" * 64)
    snap = PageSnapshot(url="https://a.com/x", body_text="Draft saved", title_value="Other title",
                        content_text="Real body", category_selected="Sports")
    res = verify(item, plan, snap, "https://a.com/x", None)
    failed = {c.name for c in res.checks if c.status == "FAIL"}
    assert res.status == "failed" and failed == {"title", "category"}


def test_unconfirmed_when_nothing_observable():
    plan = ExecutionPlan(website_type="x", source="adapter", validation_status="passed")
    item = ContentItem(row_number=2, website_url="https://a.com/x", title="T", content="C",
                       publish_mode="draft", content_hash="h" * 64)
    res = verify(item, plan, PageSnapshot(url="https://a.com/y"), "https://a.com/x", None)
    assert res.status == "unconfirmed"
