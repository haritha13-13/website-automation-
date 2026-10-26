"""Workflow discovery: ranked clickables (links, buttons, menus), backtracking, hints, saved steps and replay."""
import json

import pytest

from agents.website_analysis_agent import WebsiteAnalysisAgent
from browser.page_inspector import PageInspector
from llm.ollama_client import OllamaConfig
from llm.schemas import AnalysisProposal, NavChoice, NavigationRanking
from services import auth
from services.adapters import AdapterRegistry
from services.discovery import nav_candidates, rank_candidates, replay_steps, score
from services.pipeline import AutomationPipeline, PipelineOptions, parse_hints
from services.secrets import REDACTOR, CredentialStore, Credentials
from tests.fixtures.login_server import EMAIL, PASSWORD
from tests.test_login_pipeline import DASHBOARD, EDITOR, Terminal

GOOD = Credentials(EMAIL, PASSWORD)


@pytest.fixture(autouse=True)
def _clean():
    yield
    REDACTOR.clear()


def _analyzer(obs):  # the real editor has the form; every other page is some dashboard/listing
    return AnalysisProposal.model_validate(EDITOR if obs["title"] == "Mock CMS - Editor" else DASHBOARD)


class Picks:
    """Stand-in for the model's navigation ranking (and a counter of how often it was asked)."""
    def __init__(self, *links):
        self.links, self.calls = links, 0

    def __call__(self, payload):
        self.calls += 1
        return NavigationRanking(choices=[NavChoice(text=t, kind="link", reason="model pick") for t in self.links])


def _pipe(tmp_path, term, picks, analyzer=_analyzer):
    agent = WebsiteAnalysisAgent(OllamaConfig(api_key="k"), analyzer=analyzer, nav_chooser=picks)
    store = CredentialStore()
    store.set_shared(EMAIL, PASSWORD)
    opts = PipelineOptions(allow_local=True, headless=True, db_path=tmp_path / "t.db", output_dir=tmp_path / "out",
                           screenshot_dir=tmp_path / "shots", workflow_dir=tmp_path / "workflows")
    return AutomationPipeline(OllamaConfig(api_key="k"), term.io(), opts, analysis_agent=agent,
                              registry=AdapterRegistry([tmp_path / "none"]), credentials=store)


def test_scoring_and_ranking_demote_marketing_links():
    assert score("Create") > 0 and score("Create a Project") > score("Create") and score("Hire a Freelancer") < 0
    cands = [{"kind": "link", "text": "Hire a Freelancer", "href": "h"}, {"kind": "link", "text": "Jobs", "href": "j"},
             {"kind": "button", "text": "Create", "selector": "#c"}, {"kind": "link", "text": "Gallery", "href": "g"}]
    # The model picks the two marketing links; the keyword match for "Create" still goes first.
    ranked = rank_candidates(cands, [("Hire a Freelancer", "link"), ("Jobs", "link")], [], set())
    assert ranked[0]["text"] == "Create" and [c["text"] for c in ranked][1:] == ["Hire a Freelancer", "Jobs"]
    assert rank_candidates(cands, [], ["gallery"], set())[0]["text"] == "Gallery"  # the person's hint wins
    assert rank_candidates(cands, [], [], {("button", "create")}) == []  # visited is skipped; nothing else looks relevant


def test_parse_hints():
    assert parse_hints("Create, Project") == parse_hints("Create > Project") == ["Create", "Project"]
    assert parse_hints("") == parse_hints(None) == []


def test_candidates_include_buttons_and_skip_hidden_menu_and_logout(browser_page, login_server):
    page = browser_page.page
    browser_page.navigate(login_server.base + "/login")
    assert auth.attempt_login(page, GOOD, "127.0.0.1").ok
    browser_page.navigate(login_server.base + "/studio")
    got = {(c["kind"], c["text"]) for c in nav_candidates(PageInspector(page).inspect())}
    assert ("button", "Create") in got and ("link", "Hire a Freelancer") in got
    assert ("link", "Project") not in got  # the menu is closed, so its item is not visible yet
    assert not any("Logout" in t for _, t in got)
    page.click("#create")
    got = {(c["kind"], c["text"]) for c in nav_candidates(PageInspector(page).inspect())}
    assert ("link", "Project") in got  # the menu opened


def test_finds_create_button_then_project_menu_item_despite_wrong_model_picks(tmp_path, login_server):
    term, picks = Terminal(), Picks("Hire a Freelancer", "Jobs")
    pipe = _pipe(tmp_path, term, picks)
    prep = pipe.analyze_website(login_server.base + "/studio")
    pipe.close()
    plan = prep.plan
    assert plan.validation_status == "passed" and plan.editor_url.endswith("/admin/new")
    assert [(s["action"], s["text"]) for s in plan.steps] == [("click", "Create"), ("goto", "Project")]
    clicks = [m for m in term.log if "Clicking:" in m]
    assert "Create (button)" in clicks[0] and "Project (link)" in clicks[1] and len(clicks) == 2  # no wasted detours
    saved = json.loads((tmp_path / "workflows" / "127.0.0.1.json").read_text())
    assert [s["text"] for s in saved["steps"]] == ["Create", "Project"]


def test_backtracks_from_a_dead_end_to_the_next_candidate(tmp_path, login_server):
    term = Terminal()
    pipe = _pipe(tmp_path, term, Picks("New Post"))  # keyword + model both prefer the decoy "New Post"
    prep = pipe.analyze_website(login_server.base + "/studio2")
    pipe.close()
    clicks = [m for m in term.log if "Clicking:" in m]
    assert "New Post (link)" in clicks[0] and any("Create (button)" in c for c in clicks)  # tried it, went back
    assert prep.plan.validation_status == "passed"
    assert [s["text"] for s in prep.plan.steps] == ["Create", "Project"]  # the dead end is not in the workflow
    assert not any(u.endswith("/dead-end") for u in prep.plan.navigation_path)


def test_user_hints_are_followed_in_order(tmp_path, login_server):
    term, picks = Terminal(), Picks()  # the model offers nothing
    pipe = _pipe(tmp_path, term, picks)
    prep = pipe.analyze_website(login_server.base + "/studio", nav_hint="Create, Project")
    pipe.close()
    assert prep.plan.validation_status == "passed"
    assert any("Following your directions: Create -> Project" in m for m in term.log)
    assert [s["text"] for s in prep.plan.steps] == ["Create", "Project"]


def test_unmatched_hint_falls_back_to_automatic_search(tmp_path, login_server):
    pipe = _pipe(tmp_path, Terminal(), Picks())
    prep = pipe.analyze_website(login_server.base + "/studio", nav_hint="Totally Missing Button")
    pipe.close()
    assert prep.plan.validation_status == "passed"
    assert any("matches your hint" in n for n in prep.plan.notes)


def test_saved_steps_replay_without_the_model_even_if_the_editor_url_moved(tmp_path, login_server):
    p1 = _pipe(tmp_path, Terminal(), Picks("Jobs"))
    p1.analyze_website(login_server.base + "/studio")
    p1.close()
    f = tmp_path / "workflows" / "127.0.0.1.json"
    data = json.loads(f.read_text())
    data["editor_url"] = login_server.base + "/gone-page"  # the stored address no longer shows the editor
    f.write_text(json.dumps(data))

    picks, term = Picks("Jobs"), Terminal()
    calls = []
    p2 = _pipe(tmp_path, term, picks, analyzer=lambda obs: calls.append(1) or _analyzer(obs))
    prep = p2.analyze_website(login_server.base + "/studio")
    p2.close()
    assert prep.workflow == "saved_reused" and prep.plan.validation_status == "passed"
    assert picks.calls == 0 and calls == []  # replayed deterministically: no model at all
    assert prep.plan.editor_url.endswith("/admin/new")


def test_replay_reports_the_failing_step(browser_page, login_server):
    page = browser_page.page
    browser_page.navigate(login_server.base + "/login")
    assert auth.attempt_login(page, GOOD, "127.0.0.1").ok
    browser_page.navigate(login_server.base + "/studio")
    steps = [{"action": "click", "kind": "button", "text": "Create", "selector": "#create"},
             {"action": "goto", "kind": "link", "text": "Project", "url": login_server.base + "/admin/new"}]
    assert replay_steps(page, steps) is None and page.url.endswith("/admin/new")
    browser_page.navigate(login_server.base + "/studio")
    bad = [{"action": "click", "kind": "button", "text": "Renamed Button", "selector": "#nope"}]
    assert "step 1" in replay_steps(page, bad) and "Renamed Button" in replay_steps(page, bad)


def test_upload_reopens_the_editor_by_replaying_steps(tmp_path, login_server):
    import pandas as pd
    p1 = _pipe(tmp_path, Terminal(), Picks())
    p1.analyze_website(login_server.base + "/studio")
    p1.close()
    book = tmp_path / "b.xlsx"
    pd.DataFrame([{"website_url": login_server.base + "/studio", "title": "Created Through Menu",
                   "content": "Backlink post https://example.com", "category": "News", "tags": "seo", "images": "",
                   "publish_mode": "draft"}]).to_excel(book, index=False)

    class Pipe(AutomationPipeline):
        def _stage_execute(self):
            for pl in self.plans.values():  # simulate an editor address that no longer works on its own
                pl.editor_url = login_server.base + "/gone-page"
            return super()._stage_execute()
    agent = WebsiteAnalysisAgent(OllamaConfig(api_key="k"), analyzer=_analyzer, nav_chooser=Picks())
    store = CredentialStore()
    store.set_shared(EMAIL, PASSWORD)
    opts = PipelineOptions(allow_local=True, headless=True, db_path=tmp_path / "t.db", output_dir=tmp_path / "out",
                           screenshot_dir=tmp_path / "shots", workflow_dir=tmp_path / "workflows")
    pipe = Pipe(OllamaConfig(api_key="k"), Terminal().io(), opts, analysis_agent=agent,
                registry=AdapterRegistry([tmp_path / "none"]), credentials=store)
    report = pipe.run(str(book))
    pipe.close()
    assert report["items"][0]["job_status"] == "COMPLETED", report["items"][0]
