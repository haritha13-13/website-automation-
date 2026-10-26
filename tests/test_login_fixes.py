"""Regression tests for the real Behance run: late password step, Google-only accounts, false 'signed in',
saved sign-in diagnostics, and attaching to a Chrome the person started."""
import json
import socket
import subprocess
import time

import httpx
import pytest
from playwright.sync_api import sync_playwright

from agents.browser_execution_agent import BrowserExecutionAgent
from agents.website_analysis_agent import WebsiteAnalysisAgent
from browser.playwright_manager import PlaywrightManager
from llm.ollama_client import OllamaConfig
from services import auth
from services.adapters import AdapterRegistry
from services.database import Database
from services.pipeline import AutomationPipeline, PipelineOptions
from services.secrets import REDACTOR, CredentialStore, Credentials
from tests.fixtures.login_server import EMAIL, PASSWORD
from tests.test_login_pipeline import Terminal

GOOD = Credentials(EMAIL, PASSWORD)


@pytest.fixture(autouse=True)
def _clean():
    yield
    REDACTOR.clear()


def test_password_box_drawn_after_the_address_changes(browser_page, login_server):
    """Adobe-style single-page app: the URL changes first, the password field appears ~1.5s later."""
    page = browser_page.page
    browser_page.navigate(login_server.base + "/signin-slow")
    res = auth.attempt_login(page, GOOD, "127.0.0.1")
    assert res.status == "success", res
    assert [p["path"] for p in login_server.state.posted] == ["/signin-slow/email", "/signin/password"]


def test_google_only_account_is_reported_clearly_not_as_a_password_problem(browser_page, login_server):
    page = browser_page.page
    browser_page.navigate(login_server.base + "/signin-social")
    res = auth.attempt_login(page, GOOD, "127.0.0.1")
    assert res.status == "sso_required" and "Google" in res.detail
    assert [p["path"] for p in login_server.state.posted] == ["/signin-social/email"]  # the password was never typed


def _agent():
    return BrowserExecutionAgent(Database(":memory:"), 1, lambda *a: True, lambda *a: False)


def test_manual_login_is_only_believed_when_the_site_agrees(browser_page, login_server):
    page, base = browser_page.page, login_server.base
    prompts = []

    def recheck():
        browser_page.navigate(base + "/public")
        return auth.login_state(page)

    def person(prompt):
        prompts.append(prompt)
        if len(prompts) == 2:  # first ENTER pressed without signing in; now they really sign in
            browser_page.navigate(base + "/login")
            assert auth.attempt_login(page, GOOD, "127.0.0.1").ok
        return ""
    browser_page.navigate(base + "/public")
    res = _agent().ensure_login("127.0.0.1", lambda fn: fn(page), None, ask=person, ask_secret=lambda p: "",
                                info=lambda m: None, headless=False, recheck=recheck)
    assert res.status == "manual" and len(prompts) == 2
    assert "NOT SIGNED IN YET" in prompts[1] and "still shows a 'Sign In'" in prompts[1]


def test_manual_login_gives_up_after_three_rounds(browser_page, login_server):
    page, base = browser_page.page, login_server.base
    browser_page.navigate(base + "/public")
    asked = []
    res = _agent().ensure_login(
        "127.0.0.1", lambda fn: fn(page), None, ask=lambda p: asked.append(p) or "", ask_secret=lambda p: "",
        info=lambda m: None, headless=False,
        recheck=lambda: (browser_page.navigate(base + "/public"), auth.login_state(page))[1])
    assert res.status == "manual_required" and len(asked) == 3  # never reports success for a site that still says Sign In


def test_automatic_login_must_also_be_confirmed_by_the_site(browser_page, login_server):
    page, base = browser_page.page, login_server.base
    browser_page.navigate(base + "/signin")
    reasons = []
    res = _agent().ensure_login("127.0.0.1", lambda fn: fn(page), GOOD, ask=lambda p: "", ask_secret=lambda p: "",
                                info=lambda m: None, headless=True, recheck=lambda: "entry",  # the site disagrees
                                debug_hook=reasons.append)
    assert res.status == "manual_required" and reasons == ["failed_credentials"]


def test_sign_in_diagnostics_are_saved_without_secrets(tmp_path, login_server):
    term = Terminal()
    store = CredentialStore()
    store.set_shared(EMAIL, PASSWORD)
    opts = PipelineOptions(allow_local=True, headless=True, db_path=tmp_path / "t.db", output_dir=tmp_path / "out",
                           screenshot_dir=tmp_path / "shots", workflow_dir=tmp_path / "wf", playbook_dir=tmp_path / "pb")
    pipe = AutomationPipeline(OllamaConfig(api_key="k"), term.io(), opts,
                              analysis_agent=WebsiteAnalysisAgent(OllamaConfig(api_key="k")),
                              registry=AdapterRegistry([tmp_path / "none"]), credentials=store)
    prep = pipe.analyze_website(login_server.base + "/signin-social")
    pipe.close()
    assert prep.plan is None and prep.fail_status == "login_failed"
    assert any("This account signs in with Google" in m for m in term.log)
    debug = next((tmp_path / "out").glob("login_debug_run*_sso_required.json"))
    text = debug.read_text()
    info = json.loads(text)
    assert "Continue with Google" in info["buttons"] and "Choose how to continue" in info["text_excerpt"]
    assert EMAIL not in text and PASSWORD not in text
    assert debug.with_suffix(".png").exists()


# ---- attach to a Chrome the person started ------------------------------------------------------------
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def own_chrome(tmp_path):
    with sync_playwright() as p:
        exe = p.chromium.executable_path
    port = _free_port()
    proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                             f"--user-data-dir={tmp_path / 'profile'}", "--no-first-run", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            httpx.get(url + "/json/version", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.skip("could not start a debuggable Chrome here")
    yield proc, url
    proc.terminate()
    proc.wait(timeout=10)


def test_attach_to_your_own_chrome_keeps_your_sign_in_and_never_closes_it(own_chrome, login_server):
    proc, cdp = own_chrome
    first = PlaywrightManager(cdp_url=cdp)
    page = first.start()
    first.navigate(login_server.base + "/login", page)
    assert auth.attempt_login(page, GOOD, "127.0.0.1").ok  # the person (or the agent) signs in once
    first.close()
    assert proc.poll() is None  # the browser itself is still running

    second = PlaywrightManager(cdp_url=cdp)  # a later run attaches again
    page2 = second.start()
    second.navigate(login_server.base + "/admin/new", page2)
    assert auth.login_state(page2) == "none" and "/admin/new" in page2.url  # still signed in: no new login needed
    second.close()
    assert proc.poll() is None


def test_attach_failure_explains_what_to_do():
    from browser.playwright_manager import BrowserStartError
    with pytest.raises(BrowserStartError, match="start_chrome_for_agent.sh"):
        PlaywrightManager(cdp_url=f"http://127.0.0.1:{_free_port()}").start()


# ---- already signed in: skip the login step ---------------------------------------------------------------
def _skip_pipe(tmp_path, term, cdp=None):
    from services.adapters import ROOT, SiteAdapter
    base = SiteAdapter.model_validate_json((ROOT / "adapters" / "mock_cms.json").read_text())
    adir = tmp_path / "adapters"
    adir.mkdir(exist_ok=True)
    (adir / "l.json").write_text(base.model_copy(update={"name": "local", "url_contains": [], "hosts": ["127.0.0.1"]}).model_dump_json())
    store = CredentialStore()
    store.set_shared(EMAIL, PASSWORD)  # present, but --skip-login must never use it
    opts = PipelineOptions(allow_local=True, headless=True, skip_login=True, cdp_url=cdp, db_path=tmp_path / "t.db",
                           output_dir=tmp_path / "out", screenshot_dir=tmp_path / "shots", workflow_dir=tmp_path / "wf",
                           playbook_dir=tmp_path / "pb")
    return AutomationPipeline(OllamaConfig(api_key="k"), term.io(), opts,
                              analysis_agent=WebsiteAnalysisAgent(OllamaConfig(api_key="k")),
                              registry=AdapterRegistry([adir, tmp_path / "learned"]), credentials=store)


def test_skip_login_never_types_credentials_on_a_signed_out_site(tmp_path, login_server):
    term = Terminal()
    pipe = _skip_pipe(tmp_path, term)
    prep = pipe.analyze_website(login_server.base + "/admin/new")  # not signed in, headless: cannot continue
    pipe.close()
    assert login_server.state.posted == [] and prep.plan is None and prep.fail_status == "login_failed"
    assert not any("SHARED login" in p for p in term.prompts)  # no credential questions either


def test_already_signed_in_browser_skips_login_and_moves_on(tmp_path, own_chrome, login_server):
    proc, cdp = own_chrome
    me = PlaywrightManager(cdp_url=cdp)  # "I am already signed in" in my own Chrome
    page = me.start()
    me.navigate(login_server.base + "/login", page)
    assert auth.attempt_login(page, GOOD, "127.0.0.1").ok
    me.close()
    before = len(login_server.state.posted)

    term = Terminal()
    pipe = _skip_pipe(tmp_path, term, cdp)
    prep = pipe.analyze_website(login_server.base + "/admin/new")
    pipe.close()
    assert prep.login.status == "already_logged_in"  # the login step was skipped, not repeated
    assert prep.plan.validation_status == "passed" and prep.plan.editor_url.endswith("/admin/new")  # moved on
    assert len(login_server.state.posted) == before  # no second login was sent
    assert not any("SHARED login" in p or "login email" in p for p in term.prompts)
    assert proc.poll() is None


# ---- the agent types the credentials; the person validates -------------------------------------------------
def _confirm_pipe(tmp_path, term):
    pipe = _skip_pipe(tmp_path, term)
    pipe.opts.skip_login = False
    pipe.opts.confirm_login = True
    return pipe


@pytest.mark.parametrize("answer,proceeds", [("", True), ("yes", True), ("no", False)])
def test_person_validates_the_automatic_sign_in(tmp_path, login_server, answer, proceeds):
    term = Terminal({"PLEASE CHECK": answer})
    pipe = _confirm_pipe(tmp_path, term)
    prep = pipe.analyze_website(login_server.base + "/admin/new")
    pipe.close()
    assert [p["email"] for p in login_server.state.posted] == [EMAIL]  # the AGENT typed the credentials
    assert sum("PLEASE CHECK" in p for p in term.prompts) == 1
    if proceeds:
        assert prep.plan is not None and prep.plan.validation_status == "passed"
    else:
        assert prep.plan is None and prep.fail_status == "login_failed" and "did not confirm" in prep.fail_error
    assert EMAIL not in "".join(term.prompts) and PASSWORD not in "".join(term.prompts)


# ---- email -> verification code (you) -> password (the agent) ----------------------------------------------
def _human_enters_code(page):
    page.fill("input[name=code]", "123456")
    page.click("button:has-text('Verify')")
    page.wait_for_load_state("domcontentloaded")


def test_you_enter_the_code_then_the_agent_types_the_password(browser_page, login_server):
    page, base = browser_page.page, login_server.base
    browser_page.navigate(base + "/signin-code")
    prompts = []

    def person(prompt):
        prompts.append(prompt)
        assert "YOUR TURN" in prompt
        _human_enters_code(page)  # the person types the emailed code in the browser
        return ""
    res = _agent().ensure_login("127.0.0.1", lambda fn: fn(page), GOOD, ask=person, ask_secret=lambda p: "",
                                info=lambda m: None, headless=False,
                                recheck=lambda: (browser_page.navigate(base + "/public"), auth.login_state(page))[1])
    assert res.status == "success" and res.method == "auto"
    assert len(prompts) == 1  # exactly one human step: the code
    paths = [p["path"] for p in login_server.state.posted]
    assert paths == ["/signin-code/email", "/signin-code/verify", "/signin/password"]  # the AGENT typed email + password
    assert login_server.state.posted[-1]["password"] == PASSWORD and login_server.state.posted[0]["email"] == EMAIL


def test_code_after_the_password_is_also_your_step(browser_page, login_server):
    page, base = browser_page.page, login_server.base
    browser_page.navigate(base + "/signin-code2")
    prompts = []

    def person(prompt):
        prompts.append(prompt)
        _human_enters_code(page)
        return ""
    res = _agent().ensure_login("127.0.0.1", lambda fn: fn(page), GOOD, ask=person, ask_secret=lambda p: "",
                                info=lambda m: None, headless=False,
                                recheck=lambda: (browser_page.navigate(base + "/public"), auth.login_state(page))[1])
    assert res.status == "success" and res.method == "mixed" and len(prompts) == 1
    assert [p["path"] for p in login_server.state.posted] == ["/signin-code2/email", "/signin-code2/password", "/signin-code2/verify"]


def test_a_code_step_that_never_gets_done_is_not_reported_as_signed_in(browser_page, login_server):
    page, base = browser_page.page, login_server.base
    browser_page.navigate(base + "/signin-code")
    asked = []
    res = _agent().ensure_login("127.0.0.1", lambda fn: fn(page), GOOD, ask=lambda p: asked.append(p) or "",
                                ask_secret=lambda p: "", info=lambda m: None, headless=False,
                                recheck=lambda: (browser_page.navigate(base + "/public"), auth.login_state(page))[1])
    assert not res.ok and 1 <= len(asked) <= 6  # bounded: it stops asking and never claims success


def test_verify_your_identity_continue_is_clicked_by_the_agent_and_only_the_code_is_yours(browser_page, login_server):
    """Adobe: email -> 'Verify your identity' + Continue (sends the code) -> code -> password."""
    page, base = browser_page.page, login_server.base
    browser_page.navigate(base + "/signin-verify")
    prompts = []

    def person(prompt):
        prompts.append(prompt)
        assert page.locator("input[name=code]").is_visible()  # the code box is already showing: the agent sent the code
        _human_enters_code(page)
        return ""
    res = _agent().ensure_login("127.0.0.1", lambda fn: fn(page), GOOD, ask=person, ask_secret=lambda p: "",
                                info=lambda m: None, headless=False,
                                recheck=lambda: (browser_page.navigate(base + "/public"), auth.login_state(page))[1])
    assert res.status == "success" and len(prompts) == 1
    assert [p["path"] for p in login_server.state.posted] == [
        "/signin-verify/email", "/signin-verify/send", "/signin-code/verify", "/signin/password"]
    assert login_server.state.posted[-1]["password"] == PASSWORD
