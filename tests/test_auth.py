import pytest

from browser.redaction import safe_screenshot
from services import auth
from services.secrets import REDACTOR, CredentialStore, Credentials
from tests.fixtures.login_server import EMAIL, PASSWORD

GOOD = Credentials(EMAIL, PASSWORD)


@pytest.fixture(autouse=True)
def _clean():
    yield
    REDACTOR.clear()


def test_credentials_never_show_in_repr_or_logs():
    c = Credentials("me@x.com", "s3cret-pass")
    assert "s3cret-pass" not in repr(c) and "s3cret-pass" not in str(c) and "me@x.com" not in repr(c)
    assert REDACTOR.redact("error for me@x.com with s3cret-pass") == "error for *** with ***"
    store = CredentialStore()
    store.set_shared("a@b.com", "pw-one")
    store.set_for_host("Other.com", "c@d.com", "pw-two")
    assert store.for_host("other.com")[1] == "site" and store.for_host("x.com")[1] == "shared"
    store.clear()
    assert store.for_host("x.com") == (None, "none") and not REDACTOR.contains("pw-one")


@pytest.mark.parametrize("page,action,host,ok", [
    ("https://cms.example.com/login", "https://cms.example.com/login", "cms.example.com", True),
    ("https://auth.example.com/login", "", "example.com", True),            # subdomain of the authorized site
    ("https://evil.com/login", "", "cms.example.com", False),                # wrong origin
    ("https://cms.example.com/login", "https://evil.com/x", "cms.example.com", False),  # form posts elsewhere
    ("http://cms.example.com/login", "", "cms.example.com", False),          # not HTTPS
    ("http://127.0.0.1:8000/login", "http://127.0.0.1:8000/login", "127.0.0.1", True),
    ("https://notexample.com/login", "", "example.com", False),              # lookalike suffix
])
def test_origin_rules(page, action, host, ok):
    assert (auth.origin_problem(page, action, host) is None) is ok


def test_login_success_wrong_password_and_session(browser_page, login_server):
    page = browser_page.page
    browser_page.navigate(login_server.base + "/admin/new")  # redirected to /login
    assert auth.login_visible(page)
    bad = auth.attempt_login(page, Credentials(EMAIL, "wrong-password"), "127.0.0.1")
    assert bad.status == "failed_credentials" and not bad.ok
    assert page.inner_text("[role=alert]") == "Invalid email or password"
    good = auth.attempt_login(page, GOOD, "127.0.0.1")
    assert good.status == "success" and good.ok and page.url.endswith("/dashboard")
    assert login_server.state.login_attempts == 2
    browser_page.navigate(login_server.base + "/admin/new")
    assert not auth.login_visible(page)  # session cookie reused, no second login
    login_server.state.sessions.clear()  # server-side expiry
    browser_page.navigate(login_server.base + "/admin/new")
    assert auth.login_visible(page)


def test_credentials_not_entered_on_wrong_origin_or_evil_form(browser_page, login_server):
    page = browser_page.page
    # Authorized host differs from where the login page actually lives.
    browser_page.navigate(login_server.base + "/login")
    r = auth.attempt_login(page, GOOD, "somewhere-else.example")
    assert r.status == "blocked_origin" and "NOT entered" in r.detail
    assert page.input_value("[data-wa-login=password]") == ""
    # The form posts to another domain.
    browser_page.navigate(login_server.base + "/login-evil-action")
    r = auth.attempt_login(page, GOOD, "127.0.0.1")
    assert r.status == "blocked_origin" and "submits to" in r.detail
    assert login_server.state.posted == []


def test_captcha_requires_manual_login(browser_page, login_server):
    browser_page.navigate(login_server.base + "/login-captcha")
    r = auth.attempt_login(browser_page.page, GOOD, "127.0.0.1")
    assert r.status == "challenge" and login_server.state.posted == []


def test_error_screenshot_masks_inputs(browser_page, login_server, tmp_path):
    page = browser_page.page
    browser_page.navigate(login_server.base + "/login")
    page.fill("input[name=email]", EMAIL)
    page.fill("input[name=password]", PASSWORD)
    out = safe_screenshot(page, tmp_path / "err.png", [EMAIL])
    assert out.exists() and out.stat().st_size > 0
