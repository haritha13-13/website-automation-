"""Public pages with a 'Log in' link, email-first sign-in, separate login domains, and SSO providers."""
import pytest

from agents.website_analysis_agent import WebsiteAnalysisAgent
from llm.ollama_client import OllamaConfig
from llm.schemas import AnalysisProposal, NavigationChoice
from services import auth
from services.adapters import AdapterRegistry
from services.pipeline import AutomationPipeline, PipelineOptions
from services.secrets import REDACTOR, CredentialStore, Credentials
from tests.fixtures.login_server import EMAIL, PASSWORD
from tests.test_login_pipeline import EDITOR, Terminal

GOOD = Credentials(EMAIL, PASSWORD)


@pytest.fixture(autouse=True)
def _clean():
    yield
    REDACTOR.clear()


def test_public_page_login_link_then_two_step_login(browser_page, login_server):
    page = browser_page.page
    browser_page.navigate(login_server.base + "/public")
    assert auth.login_state(page) == "entry"  # public page: only a "Log in" link, no form
    assert auth.open_login_page(page) is None and page.url.endswith("/signin")
    form = auth.detect_login_form(page)
    assert form.found and form.email_first  # email first, password on the next step
    res = auth.attempt_login(page, GOOD, "127.0.0.1")
    assert res.status == "success" and page.url.endswith("/public")
    assert auth.login_state(page) == "none"  # now shows Logout, no Log in link
    assert [p["path"] for p in login_server.state.posted] == ["/signin/email", "/signin/password"]


def test_unknown_email_and_wrong_password_are_failures(browser_page, login_server):
    page = browser_page.page
    browser_page.navigate(login_server.base + "/signin")
    r = auth.attempt_login(page, Credentials("nobody@example.com", PASSWORD), "127.0.0.1")
    assert r.status == "email_not_accepted" and "email" in r.detail
    browser_page.navigate(login_server.base + "/signin")
    r = auth.attempt_login(page, Credentials(EMAIL, "bad-password-123"), "127.0.0.1")
    assert r.status == "failed_credentials"
    assert auth.login_state(page) == "form"


def test_social_sign_in_providers_are_never_auto_filled():
    for host in ("accounts.google.com", "www.facebook.com", "appleid.apple.com", "login.microsoftonline.com"):
        assert auth.is_sso_provider(host)
        problem = auth.origin_problem(f"https://{host}/signin", "", "www.behance.net", {host})  # even if "approved"
        assert problem and "blocks automated browsers" in problem
        assert auth._blocked(problem, f"https://{host}/signin").can_approve is False
    assert not auth.is_sso_provider("auth.services.adobe.com")
    # A first-party login on an approved identity domain is allowed.
    assert auth.origin_problem("https://auth.services.adobe.com/en/signin", "", "www.behance.net",
                               {"auth.services.adobe.com"}) is None
    assert auth.origin_problem("https://auth.services.adobe.com/en/signin", "", "www.behance.net") is not None


def test_secrets_are_redacted_even_inside_urls():
    Credentials("speedbot@example.com", "pw-secret-9")
    url = "https://accounts.example.com/signin?idnf=speedbot%40example.com&x=1"
    assert "speedbot" not in REDACTOR.redact(url) and "%40" not in REDACTOR.redact(url)
    assert REDACTOR.contains("a/b?e=speedbot%40example.com")


def _pipe(tmp_path, term, nav_text="New Article"):
    agent = WebsiteAnalysisAgent(OllamaConfig(api_key="k"), analyzer=lambda obs: AnalysisProposal.model_validate(EDITOR),
                                 nav_chooser=lambda obs: NavigationChoice(link_text=nav_text, reason="x"))
    store = CredentialStore()
    store.set_shared(EMAIL, PASSWORD)
    opts = PipelineOptions(allow_local=True, headless=True, db_path=tmp_path / "t.db", output_dir=tmp_path / "out",
                           screenshot_dir=tmp_path / "shots", workflow_dir=tmp_path / "workflows")
    return AutomationPipeline(OllamaConfig(api_key="k"), term.io(), opts, analysis_agent=agent,
                              registry=AdapterRegistry([tmp_path / "none"]), credentials=store)


def test_analyze_public_site_logs_in_via_link_then_finds_editor(tmp_path, login_server):
    term = Terminal()
    pipe = _pipe(tmp_path, term)
    prep = pipe.analyze_website(login_server.base + "/public")
    pipe.close()
    assert prep.login.status == "success" and prep.login.method == "auto"
    assert any("shows a 'Log in' link" in m for m in term.log) and any("Authentication successful" in m for m in term.log)
    assert prep.plan.validation_status == "passed" and prep.plan.editor_url.endswith("/admin/new")
    assert all("?" not in u for u in prep.plan.navigation_path)
    assert login_server.state.login_attempts == 1


def test_login_on_another_domain_needs_the_users_approval(tmp_path, login_server):
    # approved -> credentials go to the identity domain once; the approval (domain names only) is remembered
    term = Terminal()
    pipe = _pipe(tmp_path, term)
    prep = pipe.analyze_website(login_server.base + "/public-idp")
    pipe.close()
    assert any("signs you in on a different domain: localhost" in p for p in term.prompts)
    assert prep.login.status == "success" and prep.plan.validation_status == "passed"
    assert {p["path"] for p in login_server.state.posted} == {"/idp/email", "/idp/password"}
    # second run: remembered, no question
    term2 = Terminal()
    pipe2 = _pipe(tmp_path, term2)
    pipe2.analyze_website(login_server.base + "/public-idp")
    pipe2.close()
    assert not any("different domain" in p for p in term2.prompts)


def test_declining_the_login_domain_sends_nothing(tmp_path, login_server):
    term = Terminal({"different domain": "n"})
    pipe = _pipe(tmp_path, term)
    prep = pipe.analyze_website(login_server.base + "/public-idp")
    pipe.close()
    assert prep.plan is None and prep.fail_status == "login_failed"
    assert login_server.state.posted == []  # not even the email left the machine
    assert any("Not sending credentials to localhost" in m for m in term.log)


def test_ranking_skips_the_marketing_link_the_model_picked(browser_page, login_server):
    page = browser_page.page
    browser_page.navigate(login_server.base + "/public")
    auth.open_login_page(page)
    assert auth.attempt_login(page, GOOD, "127.0.0.1").ok
    agent = WebsiteAnalysisAgent(OllamaConfig(api_key="k"), analyzer=lambda o: AnalysisProposal.model_validate(EDITOR),
                                 nav_chooser=lambda o: NavigationChoice(link_text="Hire a Freelancer", reason="x"))
    plan = agent.locate_editor_plan(lambda fn: fn(page), page.url, {"title", "content"}, {"save_draft"})
    assert plan.validation_status == "passed" and plan.editor_url.endswith("/admin/new")
    assert [st["text"] for st in plan.steps] == ["New Article"]  # "Hire a Freelancer" was never clicked
    assert all("?" not in u and "email_hint" not in u for u in plan.navigation_path)


def test_a_click_that_leaves_the_site_is_undone(browser_page, login_server):
    page = browser_page.page
    browser_page.navigate(login_server.base + "/public")
    auth.open_login_page(page)
    assert auth.attempt_login(page, GOOD, "127.0.0.1").ok
    agent = WebsiteAnalysisAgent(OllamaConfig(api_key="k"), analyzer=lambda o: AnalysisProposal.model_validate(EDITOR),
                                 nav_chooser=lambda o: NavigationChoice(link_text="x"))
    # The person explicitly asks for the link that bounces to another domain (the identity provider).
    plan = agent.locate_editor_plan(lambda fn: fn(page), page.url, {"title", "content"}, {"save_draft"},
                                    hints=["Hire a Freelancer"])
    assert plan.validation_status == "failed"
    assert any("went to another domain" in n and "went back" in n for n in plan.notes)
    assert plan.steps == [] and page.url.startswith(login_server.base + "/public")  # back where it started
    assert all("?" not in u and "email_hint" not in u for u in plan.navigation_path)
