"""Playbooks: the Behance-style block-editor flow, run end to end against a local look-alike site."""
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from agents.website_analysis_agent import WebsiteAnalysisAgent
from llm.ollama_client import OllamaConfig
from services.adapters import ROOT, AdapterRegistry
from services.content_service import PreparationError, prepare_content
from services.models import ContentItem
from services.pipeline import AutomationPipeline, PipelineOptions
from services.playbook import Playbook, PlaybookStore, render
from services.secrets import REDACTOR, CredentialStore
from services.verification_service import verify_playbook
from tests.fixtures.login_server import EMAIL, PASSWORD
from tests.test_login_pipeline import Terminal


@pytest.fixture(autouse=True)
def _clean():
    yield
    REDACTOR.clear()


def _playbook_dir(tmp_path, mutate=None):
    """The real Behance playbook, retargeted at the local look-alike site (127.0.0.1)."""
    data = json.loads((ROOT / "playbooks" / "behance.net.json").read_text())
    data["site"], data["verified"] = "127.0.0.1", True
    if mutate:
        mutate(data)
    d = tmp_path / "playbooks"
    d.mkdir(exist_ok=True)
    (d / "local.json").write_text(json.dumps(data))
    return d


def _pipe(tmp_path, term, login_server, pb_dir=None):
    store = CredentialStore()
    store.set_shared(EMAIL, PASSWORD)
    opts = PipelineOptions(allow_local=True, headless=True, db_path=tmp_path / "t.db", output_dir=tmp_path / "out",
                           screenshot_dir=tmp_path / "shots", workflow_dir=tmp_path / "workflows",
                           playbook_dir=pb_dir or _playbook_dir(tmp_path))
    pipe = AutomationPipeline(OllamaConfig(api_key="k"), term.io(), opts,
                              analysis_agent=WebsiteAnalysisAgent(OllamaConfig(api_key="k")),
                              registry=AdapterRegistry([tmp_path / "none"]), credentials=store)

    def publish_when_asked(prompt):  # the PERSON clicks Publish in the browser
        if "YOUR TURN" in prompt:
            pipe.worker.page_call("127.0.0.1", lambda p: p.click("#publish"))
    term.hooks.append(publish_when_asked)
    return pipe


def _book(tmp_path, login_server, png, **over):
    row = {"website_url": login_server.base + "/b/home", "title": "My Design Project",
           "description": "A short story about this design. Read the full case study on our blog.",
           "anchor_text": "full case study", "blog_url": login_server.base + "/blog-post",
           "images": f"{png}", "cover_image": f"{png}", "category": "Illustration", "tags": "design, case study",
           "publish_mode": "publish"}
    row.update(over)
    p = tmp_path / "behance.xlsx"
    pd.DataFrame([row]).to_excel(p, index=False)
    return p


# ---------------------------------------------------------------------------------------------------------
def test_playbook_file_is_validated_and_behance_playbook_loads():
    pb = PlaybookStore().find("https://www.behance.net/")
    assert pb.name == "behance_project" and pb.verified is False and len(pb.steps) == 15
    assert {"title", "content", "blog_url", "anchor_text", "images", "cover_image"} <= set(pb.requires)
    with pytest.raises(ValidationError):
        Playbook(name="x", site="a.com", steps=[{"id": "a", "action": "teleport"}])
    with pytest.raises(ValidationError):
        Playbook(name="x", site="a.com", steps=[{"id": "a", "action": "click"}])  # no target
    with pytest.raises(ValidationError):
        Playbook(name="x", site="a.com", steps=[{"id": "a", "action": "capture_url"}, {"id": "a", "action": "capture_url"}])
    assert PlaybookStore().find("https://example.com/") is None


def test_placeholders():
    assert render("{tags}", {"tags": ["a", "b"]}) == ["a", "b"] and render("Hi {title}!", {"title": "T"}) == "Hi T!"
    with pytest.raises(Exception, match="no value"):
        render("{blog_url}", {})


def test_spreadsheet_columns_for_backlinks(tmp_path, png, login_server):
    ok = prepare_content(_book(tmp_path, login_server, png), allow_local=True).items[0]
    assert ok.anchor_text == "full case study" and ok.blog_url.endswith("/blog-post") and ok.cover_image_path
    assert "case study" in ok.content and ok.tags == ["design", "case study"]
    bad = _book(tmp_path, login_server, png, anchor_text="words that are not there", blog_url="ftp://x", cover_image="missing.png")
    errs = prepare_content(bad, allow_local=True).errors[0].errors
    text = " ".join(errs)
    assert "anchor_text" in text and "blog_url" in text and "cover_image" in text


def test_full_behance_style_flow_with_your_own_publish_click(tmp_path, png, login_server):
    term = Terminal()
    pipe = _pipe(tmp_path, term, login_server)
    report = pipe.run(str(_book(tmp_path, login_server, png)))
    pipe.close()
    it = report["items"][0]
    assert it["status"] == "published" and it["job_status"] == "COMPLETED", it
    assert it["article_url"].startswith(login_server.base + "/b/project")
    checks = {c["name"]: c for c in it["verification"]["checks"]}
    assert {n: checks[n]["status"] for n in ("login", "playbook", "published", "project_url", "backlink_present", "backlink_target")} \
        == {"login": "PASS", "playbook": "PASS", "published": "PASS", "project_url": "PASS",
            "backlink_present": "PASS", "backlink_target": "PASS"}
    assert checks["playbook"]["detail"] == "15 of 15 steps completed" or "of 15" in checks["playbook"]["detail"]
    assert any("Step 1/15" in m for m in term.log) and any("Playbook 'behance_project' found" in m for m in term.log)
    assert any("Start this project now?" in p for p in term.prompts) and any("YOUR TURN" in p for p in term.prompts)
    assert not any("PUBLISHING" in p for p in term.prompts)  # the agent never publishes by itself
    assert report["websites"][0]["source"] == "playbook"


def test_failed_step_stops_with_a_debug_file_and_masked_screenshot(tmp_path, png, login_server):
    def break_creative_field(data):
        for st in data["steps"]:
            if st["id"] == "creative_field":
                st["target"] = {"candidates": [{"role": "combobox", "name": "Categories That Do Not Exist"}]}
                st["timeout_ms"] = 1500
    term = Terminal()
    pipe = _pipe(tmp_path, term, login_server, _playbook_dir(tmp_path, break_creative_field))
    report = pipe.run(str(_book(tmp_path, login_server, png)))
    pipe.close()
    it = report["items"][0]
    assert it["status"] == "failed" and it["job_status"] == "FAILED"
    assert "step 12/15 'creative_field' failed" in it["error"]
    debug = tmp_path / "out" / "playbook_debug_run1_row2_creative_field.json"
    info = json.loads(debug.read_text())
    assert info["failed_step"] == "creative_field" and "Creative Fields" in info["buttons"]  # what the page really offers
    assert (tmp_path / "out" / "playbook_debug_run1_row2_creative_field.png").exists()
    assert not any("YOUR TURN" in p for p in term.prompts)  # never reached the publish step
    assert PASSWORD not in debug.read_text() and EMAIL not in debug.read_text()


def test_unavailable_category_is_reported_with_the_real_options(tmp_path, png, login_server):
    term = Terminal()
    pipe = _pipe(tmp_path, term, login_server)
    report = pipe.run(str(_book(tmp_path, login_server, png, category="Sculpture")))
    pipe.close()
    err = report["items"][0]["error"]
    assert "'creative_field'" in err and report["items"][0]["status"] == "failed"
    info = json.loads((tmp_path / "out" / "playbook_debug_run1_row2_creative_field.json").read_text())
    assert info["error"].startswith("'Sculpture' is not an available option") and "Illustration" in info["error"]


def test_you_can_decline_to_start_or_skip_publishing(tmp_path, png, login_server):
    p1 = _pipe(tmp_path, Terminal({"Start this project now": "no"}), login_server)
    assert p1.run(str(_book(tmp_path, login_server, png)))["items"][0]["status"] == "rejected_by_user"
    p1.close()
    term = Terminal({"YOUR TURN": "skip"})
    p2 = _pipe(tmp_path, term, login_server)
    term.hooks[:] = []  # this time the person does not click Publish
    report = p2.run(str(_book(tmp_path, login_server, png)))
    p2.close()
    assert report["items"][0]["status"] == "rejected_by_user" and report["items"][0]["job_status"] == "SKIPPED"
    assert report["items"][0]["article_url"] is None


def test_published_project_is_never_created_twice(tmp_path, png, login_server):
    book = _book(tmp_path, login_server, png)
    p1 = _pipe(tmp_path, Terminal(), login_server)
    assert p1.run(str(book))["items"][0]["status"] == "published"
    p1.close()
    term = Terminal()
    p2 = _pipe(tmp_path, term, login_server)
    r2 = p2.run(str(book))
    p2.close()
    assert r2["items"][0]["status"] == "duplicate_skipped"
    assert not any("Step 1/" in m for m in term.log) and not any("YOUR TURN" in p for p in term.prompts)


def test_draft_rows_stop_at_review_and_need_review(tmp_path, png, login_server):
    term = Terminal()
    pipe = _pipe(tmp_path, term, login_server)
    report = pipe.run(str(_book(tmp_path, login_server, png, publish_mode="draft")))
    pipe.close()
    it = report["items"][0]
    assert it["job_status"] == "NEEDS_REVIEW" and it["status"] == "unverified"
    assert not any("YOUR TURN" in p for p in term.prompts) and any("Step 13/13" in m for m in term.log)


def test_row_without_required_fields_is_rejected_before_any_click(tmp_path, png, login_server):
    term = Terminal()
    pipe = _pipe(tmp_path, term, login_server)
    report = pipe.run(str(_book(tmp_path, login_server, png, cover_image="", anchor_text="")))
    pipe.close()
    it = report["items"][0]
    assert it["status"] == "analysis_failed" and "cover_image" in it["error"] and "anchor_text" in it["error"]
    assert not any("Step 1/" in m for m in term.log)


def _item(**kw):
    base = dict(row_number=2, website_url="https://x.com/", title="My Design Project", content="Read the full case study here.",
                publish_mode="publish", content_hash="h" * 64, blog_url="https://blog.example.com/post/",
                anchor_text="full case study")
    return ContentItem(**{**base, **kw})


def test_backlink_checks_catch_missing_wrong_text_and_broken_links():
    page = lambda links, status=200, text="My Design Project": {"status": status, "final_url": "u", "text": text, "links": links}  # noqa: E731
    good = [{"text": "full case study", "href": "https://blog.example.com/post"}]
    def run(links, target_status=200, **kw):
        fetch = lambda u: page(links) if "project" in u else {"status": target_status, "final_url": u, "text": "", "links": []}  # noqa: E731
        return verify_playbook(_item(**kw), 15, 15, True, "https://x.com/project/1", "https://x.com/", fetch, "success")
    assert run(good).status == "verified"  # trailing slash and case differences do not matter
    assert run([]).status == "failed"  # no link at all
    wrong_text = run([{"text": "click here", "href": "https://blog.example.com/post"}])
    assert wrong_text.status == "failed" and "not 'full case study'" in str(wrong_text.checks)
    assert run(good, target_status=404).status == "failed"  # the link does not open
    assert verify_playbook(_item(), 15, 15, False, None, "https://x.com/", None).status == "unconfirmed"
    assert verify_playbook(_item(), 15, 15, True, "https://x.com/", "https://x.com/", None).status == "failed"  # no new address
    assert verify_playbook(_item(), 9, 15, True, "https://x.com/p", "https://x.com/", None).checks[1].status == "FAIL"
