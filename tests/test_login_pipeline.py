"""Authentication integration tests against a LOCAL login server with throwaway test credentials."""
import json

import pandas as pd
import pytest

from agents.website_analysis_agent import WebsiteAnalysisAgent, _assert_no_secrets, resolve_nav_choice
from llm.ollama_client import OllamaConfig
from llm.schemas import AnalysisProposal, NavigationChoice
from services.adapters import ROOT, AdapterRegistry, SiteAdapter
from services.analysis_service import ServiceIO
from services.pipeline import AutomationPipeline, PipelineOptions
from services.secrets import REDACTOR, CredentialStore
from tests.fixtures.login_server import EMAIL, PASSWORD

NULL = {"selector": None, "confidence": 0, "reason": "not on this page"}
DASHBOARD = {"website_type": "custom_cms", "page_type": "dashboard",
             "fields": {k: NULL for k in ("title", "content", "category", "tags", "image")},
             "actions": {"save_draft": {"selector": None, "reason": "n/a"}, "publish": {"selector": None, "reason": "n/a"}}}
EDITOR = {"website_type": "custom_cms", "page_type": "article_editor",
          "fields": {"title": {"selector": "#headline", "confidence": 0.9}, "content": {"selector": "#article-body", "confidence": 0.9},
                     "category": {"selector": "#category", "confidence": 0.9}, "tags": {"selector": "#tags", "confidence": 0.9},
                     "image": {"selector": "#cover", "confidence": 0.9}},
          "actions": {"save_draft": {"selector": "#save-draft"}, "publish": {"selector": "#publish"}}}


@pytest.fixture(autouse=True)
def _clean():
    yield
    REDACTOR.clear()


class Terminal:
    def __init__(self, answers=None, secrets=None):
        self.answers, self.secrets, self.prompts, self.log, self.hooks = answers or {}, list(secrets or []), [], [], []

    def ask(self, prompt):
        self.prompts.append(prompt)
        for fn in self.hooks:
            fn(prompt)
        for key, val in self.answers.items():
            if key in prompt:
                return val
        return "y"

    def ask_secret(self, prompt):
        self.prompts.append(prompt)
        return self.secrets.pop(0) if self.secrets else ""

    def io(self):
        return ServiceIO(ask=self.ask, ask_secret=self.ask_secret, info=self.log.append, warn=self.log.append)


def _adapter_registry(tmp_path):
    base = SiteAdapter.model_validate_json((ROOT / "adapters" / "mock_cms.json").read_text())
    d = tmp_path / "adapters"
    d.mkdir(exist_ok=True)
    (d / "local.json").write_text(base.model_copy(update={"name": "local_login_cms", "url_contains": [],
                                                           "hosts": ["127.0.0.1"]}).model_dump_json())
    return AdapterRegistry([d, tmp_path / "learned"])


def _book(tmp_path, url, titles=("Login Draft One",)):
    rows = [{"website_url": url, "title": t, "content": f"Body for {t}", "category": "News", "tags": "a, b",
             "images": "", "publish_mode": "draft"} for t in titles]
    p = tmp_path / "c.xlsx"
    pd.DataFrame(rows).to_excel(p, index=False)
    return p


def _pipe(tmp_path, term, creds=None, registry=None, seen=None):
    def analyzer(obs):
        if seen is not None:
            seen.append(json.dumps(obs))
        return AnalysisProposal.model_validate(DASHBOARD if obs["title"] == "Dashboard" else EDITOR)
    agent = WebsiteAnalysisAgent(OllamaConfig(api_key="k"), analyzer=analyzer,
                                 nav_chooser=lambda obs: NavigationChoice(link_text="New Article", reason="creates posts"))
    store = CredentialStore()
    if creds:
        store.set_shared(*creds)
    opts = PipelineOptions(allow_local=True, headless=True, db_path=tmp_path / "t.db", output_dir=tmp_path / "out",
                           screenshot_dir=tmp_path / "shots", workflow_dir=tmp_path / "workflows")
    return AutomationPipeline(OllamaConfig(api_key="k"), term.io(), opts, analysis_agent=agent,
                              registry=registry or AdapterRegistry([tmp_path / "none"]), credentials=store)


def _everything_text(tmp_path, report, term, pipe):
    db = pipe.db
    dump = json.dumps([dict(r) for r in db._conn.execute("select * from items")] +
                      [dict(r) for r in db._conn.execute("select * from events")] +
                      [dict(r) for r in db._conn.execute("select * from authorized_sites")] +
                      [dict(r) for r in db._conn.execute("select * from credential_grants")])
    files = "".join(p.read_text(errors="ignore") for p in (tmp_path / "out").glob("*.json"))
    return dump + files + json.dumps(report) + "\n".join(term.log) + "\n".join(term.prompts)


def test_real_login_adapter_site_end_to_end(tmp_path, login_server):
    term = Terminal()
    pipe = _pipe(tmp_path, term, (EMAIL, PASSWORD), _adapter_registry(tmp_path))
    report = pipe.run(str(_book(tmp_path, login_server.base + "/admin/new")))
    item = report["items"][0]
    assert item["status"] == "draft_saved" and item["job_status"] == "COMPLETED"
    checks = {c["name"]: c for c in item["verification"]["checks"]}
    assert checks["login"]["status"] == "PASS" and "automatic" in checks["login"]["detail"]
    assert [p.get("email") for p in login_server.state.posted] == [EMAIL]  # exactly one login, to the right server
    site = report["websites"][0]
    assert site["login"]["status"] == "success" and site["login"]["method"] == "auto" and site["source"] == "adapter"
    assert any("Authentication successful" in m for m in term.log)
    # consent prompts: authorize the domain + send shared login to a new domain
    assert any("authorized to automate 127.0.0.1" in p for p in term.prompts)
    assert any("SHARED login" in p for p in term.prompts)
    text = _everything_text(tmp_path, report, term, pipe)
    assert PASSWORD not in text and EMAIL not in text
    pipe.close()

    # Second run: consent is remembered (domain-level, no secrets stored), content is a duplicate.
    term2 = Terminal()
    pipe2 = _pipe(tmp_path, term2, (EMAIL, PASSWORD), _adapter_registry(tmp_path))
    r2 = pipe2.run(str(_book(tmp_path, login_server.base + "/admin/new")))
    assert r2["items"][0]["status"] == "duplicate_skipped"
    assert not any("authorized to automate" in p or "SHARED login" in p for p in term2.prompts)
    pipe2.close()


def test_dashboard_navigation_to_editor_via_ai_and_no_secrets_sent(tmp_path, login_server):
    seen = []
    term = Terminal()
    pipe = _pipe(tmp_path, term, (EMAIL, PASSWORD), seen=seen)
    report = pipe.run(str(_book(tmp_path, login_server.base + "/")))
    site = report["websites"][0]
    assert site["source"] == "ollama" and site["page_type"] == "article_editor"
    assert site["editor_url"].endswith("/admin/new") and len(site["navigation_path"]) == 2 and site["navigation_path"][0].endswith("/")
    assert report["items"][0]["job_status"] == "COMPLETED"
    assert len(seen) == 1 and all(PASSWORD not in s and EMAIL not in s for s in seen)  # editor observation only
    assert any("Clicking: New Article" in m for m in term.log)
    assert PASSWORD not in _everything_text(tmp_path, report, term, pipe)
    pipe.close()


def test_wrong_password_reentry_is_bounded(tmp_path, login_server):
    term = Terminal(secrets=[PASSWORD])
    pipe = _pipe(tmp_path, term, (EMAIL, "wrong-password-1"), _adapter_registry(tmp_path))
    report = pipe.run(str(_book(tmp_path, login_server.base + "/admin/new")))
    assert report["items"][0]["job_status"] == "COMPLETED"
    assert login_server.state.login_attempts == 2
    pipe.close()


def test_repeated_bad_password_stops_after_two_attempts(tmp_path, login_server):
    term = Terminal(secrets=["still-wrong-2"])
    pipe = _pipe(tmp_path, term, (EMAIL, "wrong-password-1"), _adapter_registry(tmp_path))
    report = pipe.run(str(_book(tmp_path, login_server.base + "/admin/new")))
    it = report["items"][0]
    assert it["status"] == "login_failed" and it["job_status"] == "FAILED"
    assert login_server.state.login_attempts == 2  # never hammers the account
    assert any(p.name.endswith(".png") for p in (tmp_path / "shots").glob("*"))  # masked error screenshot
    assert "wrong-password-1" not in _everything_text(tmp_path, report, term, pipe)
    pipe.close()


def test_declined_credential_grant_sends_nothing(tmp_path, login_server):
    term = Terminal({"SHARED login": "n"})
    pipe = _pipe(tmp_path, term, (EMAIL, PASSWORD), _adapter_registry(tmp_path))
    report = pipe.run(str(_book(tmp_path, login_server.base + "/admin/new")))
    assert login_server.state.posted == []  # credentials never left the machine
    assert report["items"][0]["status"] == "login_failed"  # headless: manual login impossible
    pipe.close()


def test_unauthorized_website_is_not_touched(tmp_path, login_server):
    term = Terminal({"authorized to automate": "n"})
    pipe = _pipe(tmp_path, term, (EMAIL, PASSWORD), _adapter_registry(tmp_path))
    report = pipe.run(str(_book(tmp_path, login_server.base + "/admin/new")))
    assert report["items"][0]["status"] == "analysis_failed" and login_server.state.posted == []
    assert login_server.state.login_attempts == 0
    pipe.close()


def test_session_expiry_between_rows_triggers_one_relogin(tmp_path, login_server):
    term = Terminal()
    seen_draft_prompts = []

    def expire_after_first(prompt):
        if "draft" in prompt:
            seen_draft_prompts.append(1)
            if len(seen_draft_prompts) == 1:
                login_server.state.sessions.clear()
    term.hooks.append(expire_after_first)
    pipe = _pipe(tmp_path, term, (EMAIL, PASSWORD), _adapter_registry(tmp_path))
    report = pipe.run(str(_book(tmp_path, login_server.base + "/admin/new", ("Expiry One", "Expiry Two"))))
    assert [i["job_status"] for i in report["items"]] == ["COMPLETED", "COMPLETED"]
    assert login_server.state.login_attempts == 2 and any("Session expired" in m for m in term.log)
    pipe.close()


def test_credentials_never_reach_the_model_layer():
    REDACTOR.add("hunter2-secret")
    with pytest.raises(RuntimeError, match="credential"):
        _assert_no_secrets({"elements": [{"placeholder": "hunter2-secret"}]})


def test_navigation_choice_only_follows_observed_safe_links():
    obs = {"links": [{"text": "New Article", "href": "http://127.0.0.1:1/admin/new"},
                     {"text": "Logout", "href": "http://127.0.0.1:1/logout"},
                     {"text": "Elsewhere", "href": "https://evil.example/x"}]}
    cur = "http://127.0.0.1:1/dashboard"
    assert resolve_nav_choice(NavigationChoice(link_text="new article"), obs, cur).endswith("/admin/new")
    assert resolve_nav_choice(NavigationChoice(link_text="Logout"), obs, cur) is None       # destructive
    assert resolve_nav_choice(NavigationChoice(link_text="Elsewhere"), obs, cur) is None    # other site
    assert resolve_nav_choice(NavigationChoice(link_text="Invented Link"), obs, cur) is None  # not observed
