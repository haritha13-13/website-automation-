"""Saved website workflows: analyze once, save, reuse next time, re-analyze when the site changed."""
import json

import pytest

import main
from agents.website_analysis_agent import WebsiteAnalysisAgent
from llm.ollama_client import OllamaConfig
from llm.schemas import AnalysisProposal, NavigationChoice
from services.adapters import AdapterRegistry
from services.analysis_service import ServiceIO
from services.models import ExecutionPlan
from services.pipeline import AutomationPipeline, PipelineOptions
from services.secrets import REDACTOR, CredentialStore
from services.workflows import Workflow, WorkflowStore, workflow_key
from tests.fixtures.login_server import EMAIL, PASSWORD
from tests.test_login_pipeline import DASHBOARD, EDITOR, Terminal


@pytest.fixture(autouse=True)
def _clean():
    yield
    REDACTOR.clear()


class Counters:
    def __init__(self):
        self.analyze = self.nav = 0


def _pipe(tmp_path, term, counters, ollama=True):
    def analyzer(obs):
        counters.analyze += 1
        return AnalysisProposal.model_validate(DASHBOARD if obs["title"] == "Dashboard" else EDITOR)

    def nav(obs):
        counters.nav += 1
        return NavigationChoice(link_text="New Article", reason="creates posts")
    agent = WebsiteAnalysisAgent(OllamaConfig(api_key="k"), analyzer=analyzer, nav_chooser=nav)
    store = CredentialStore()
    store.set_shared(EMAIL, PASSWORD)
    opts = PipelineOptions(allow_local=True, headless=True, db_path=tmp_path / "t.db", output_dir=tmp_path / "out",
                           screenshot_dir=tmp_path / "shots", workflow_dir=tmp_path / "workflows")
    return AutomationPipeline(OllamaConfig(api_key="k"), term.io(), opts, analysis_agent=agent,
                              registry=AdapterRegistry([tmp_path / "none"]), credentials=store, ollama_available=ollama)


def test_store_roundtrip_key_and_corrupt_file(tmp_path):
    store = WorkflowStore(tmp_path)
    plan = ExecutionPlan(website_type="custom_cms", source="ollama", validation_status="passed", page_type="article_editor",
                         editor_url="https://cms.example.com/new", navigation_path=["https://cms.example.com/", "https://cms.example.com/new"],
                         field_mapping={"title": "#t", "content": "#c"}, actions={"save_draft": "#s"})
    wf = Workflow.from_plan(plan, "https://cms.example.com/dashboard?x=1#top")
    assert wf.site == workflow_key("https://CMS.example.com/anything") == "cms.example.com"
    path = store.save(wf)
    assert path.name == "cms.example.com.json" and store.find("https://cms.example.com/other").editor_url.endswith("/new")
    assert store.find("https://other.com") is None
    path.write_text("{broken")
    assert store.find("https://cms.example.com/") is None and store.all() == []
    assert store.delete("https://cms.example.com/") is True


def test_analyze_saves_then_reuses_without_ai(tmp_path, login_server):
    url = login_server.base + "/"
    # 1st time: nothing saved -> analyze (AI navigation + mapping), confirm, save.
    c1, t1 = Counters(), Terminal()
    p1 = _pipe(tmp_path, t1, c1)
    prep = p1.analyze_website(url)
    p1.close()
    assert prep.workflow == "new_saved" and prep.plan.validation_status == "passed" and c1.analyze == 1 and c1.nav == 1
    saved = json.loads((tmp_path / "workflows" / "127.0.0.1.json").read_text())
    assert saved["editor_url"].endswith("/admin/new") and len(saved["navigation_path"]) == 2
    assert saved["field_mapping"]["title"] == "#headline" and saved["actions"]["publish"] == "#publish"
    text = json.dumps(saved)
    assert PASSWORD not in text and EMAIL not in text  # no credentials in a workflow file
    assert any("No saved workflow" in m for m in t1.log)

    # 2nd time: the saved workflow is found and reused; the model is never called.
    c2, t2 = Counters(), Terminal()
    p2 = _pipe(tmp_path, t2, c2)
    prep2 = p2.analyze_website(url)
    p2.close()
    assert prep2.workflow == "saved_reused" and prep2.plan.source == "adapter" and prep2.plan.validation_status == "passed"
    assert c2.analyze == 0 and c2.nav == 0
    assert not any("AI-proposed" in p for p in t2.prompts)
    assert any("Saved workflow found" in m for m in t2.log)
    assert prep2.plan.editor_url.endswith("/admin/new")

    # Saved workflows work even when Ollama is down.
    p3 = _pipe(tmp_path, Terminal(), Counters(), ollama=False)
    assert p3.analyze_website(url).workflow == "saved_reused"
    p3.close()


def test_outdated_workflow_is_reanalyzed_and_replaced(tmp_path, login_server):
    url = login_server.base + "/"
    p1 = _pipe(tmp_path, Terminal(), Counters())
    p1.analyze_website(url)
    p1.close()
    f = tmp_path / "workflows" / "127.0.0.1.json"
    data = json.loads(f.read_text())
    data["field_mapping"]["title"] = "#renamed-by-the-site"  # the site changed its layout
    f.write_text(json.dumps(data))

    c, t = Counters(), Terminal()
    p2 = _pipe(tmp_path, t, c)
    prep = p2.analyze_website(url)
    p2.close()
    assert prep.workflow == "new_saved" or prep.workflow == "saved_outdated"
    assert c.analyze >= 1  # the model was used again
    assert any("no longer matches" in m for m in t.log)
    assert json.loads(f.read_text())["field_mapping"]["title"] == "#headline"  # replaced with the validated mapping


def test_declining_save_keeps_nothing(tmp_path, login_server):
    t = Terminal({"Save this website's workflow": "n"})
    p = _pipe(tmp_path, t, Counters())
    prep = p.analyze_website(login_server.base + "/")
    p.close()
    assert prep.workflow == "new_unsaved" and not (tmp_path / "workflows").exists()


def test_upload_also_reuses_saved_workflow(tmp_path, login_server):
    import pandas as pd
    p1 = _pipe(tmp_path, Terminal(), Counters())
    p1.analyze_website(login_server.base + "/")
    p1.close()
    book = tmp_path / "b.xlsx"
    pd.DataFrame([{"website_url": login_server.base + "/", "title": "From Saved Workflow", "content": "Backlink post https://example.com",
                   "category": "News", "tags": "seo", "images": "", "publish_mode": "draft"}]).to_excel(book, index=False)
    c, t = Counters(), Terminal()
    p2 = _pipe(tmp_path, t, c)
    report = p2.run(str(book))
    p2.close()
    assert report["items"][0]["job_status"] == "COMPLETED" and c.analyze == 0 and c.nav == 0
    assert report["websites"][0]["source"] == "adapter"


def test_menu_option_1_reports_saved_workflow(monkeypatch, capsys, tmp_path, login_server):
    monkeypatch.setenv("HEADLESS", "true")
    monkeypatch.setattr(main, "check_connectivity", lambda cfg: None)
    monkeypatch.setattr(main.getpass, "getpass", lambda prompt="": PASSWORD)
    # Seed a workflow (as a previous analysis would have).
    p = _pipe(tmp_path, Terminal(), Counters())
    p.analyze_website(login_server.base + "/")
    p.close()
    REDACTOR.clear()

    menu = iter(["1", "5"])

    def fake_input(prompt=""):
        if "Select Option" in prompt:
            return next(menu)
        if "Enter Website URL" in prompt:
            return login_server.base + "/"
        if "login email" in prompt:
            return EMAIL
        if "what do you click" in prompt:
            return ""
        return "y"
    monkeypatch.setattr("builtins.input", fake_input)
    assert main.main(["--db", str(tmp_path / "t.db"), "--workflows", str(tmp_path / "workflows")]) == 0
    out = capsys.readouterr().out
    assert "Saved workflow found for this website." in out
    assert "SAVED workflow reused" in out and "WEBSITE WORKFLOW" in out and "Status   : COMPLETED" in out
    assert PASSWORD not in out and EMAIL not in out
