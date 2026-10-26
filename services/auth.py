"""Deterministic login detection and submission. Runs on the browser thread; no LLM involved.

Handles three kinds of sites:
  * a login form already on the page (email + password),
  * a public page with a "Log in" / "Sign in" link or button (click it first),
  * email-first logins (email -> Continue -> password), including an identity provider on another domain
    that the user has explicitly approved.
Credentials are typed only after the page origin and the form's submit target have been checked.
Social/SSO providers (Google, Facebook, Apple, Microsoft) are never auto-filled: they block automation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from services.secrets import Credentials

LOGIN_SETTLE_MS = 10_000
ERROR_TEXT = re.compile(r"invalid|incorrect|wrong|failed|denied|not recognized|no account|couldn.t find|try again|unauthori[sz]ed", re.I)
CHALLENGE_TEXT = re.compile(
    r"verification code|two-factor|2-step|two step|authenticator|one-time|captcha|are you a (robot|human)|"
    r"security check|verify it'?s you|enter the code|code we sent|code (was )?sent|6-digit|six-digit|security code|"
    r"check your (email|inbox)", re.I)
# "Verify your identity - we'll send you a verification code" + a Continue button (the code is sent on click)
VERIFY_INTRO = re.compile(r"verify your identity|(we.ll|we will) send you a (verification )?code|send you a verification code", re.I)
SOCIAL_ONLY = re.compile(r"continue with (google|facebook|apple|microsoft)|sign in with (google|facebook|apple|microsoft)", re.I)
# Providers that reject automated browsers (and whose pages must be completed by the person).
SSO_PROVIDERS = ("google.com", "facebook.com", "apple.com", "live.com", "microsoftonline.com", "microsoft.com")

# Marks the login controls with a temporary attribute so Playwright can target them; reports facts only.
_DETECT_JS = r"""
() => {
  const visible = el => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  document.querySelectorAll('[data-wa-login]').forEach(e => e.removeAttribute('data-wa-login'));
  const text = (document.body ? document.body.innerText : '');
  const challengeDom = !!document.querySelector(
    'iframe[src*="captcha" i], iframe[title*="captcha" i], [class*="captcha" i], [id*="captcha" i], .g-recaptcha, .h-captcha');
  const otp = [...document.querySelectorAll('input')].some(i => visible(i) && (
    (i.autocomplete || '') === 'one-time-code' || /otp|mfa|2fa|totp|verification/i.test((i.name || '') + (i.id || ''))));
  const base = {challenge_dom: challengeDom, otp_field: otp, text: text.slice(0, 4000)};
  const userHint = /user|email|login|account|mail/i;
  const submitIn = scope => scope.querySelector('[type=submit]') ||
      [...scope.querySelectorAll('button, input[type=button], [role=button]')].find(b => visible(b) &&
        /log ?in|sign ?in|continue|submit|next/i.test(b.innerText || b.value || ''));
  const textInputs = scope => [...scope.querySelectorAll('input')].filter(i => visible(i) &&
      ['text', 'email', 'tel', ''].includes((i.getAttribute('type') || '').toLowerCase()));

  // 1. A password field is showing: classic form, or the password step of a two-step login.
  const pw = [...document.querySelectorAll('input[type=password]')].filter(visible);
  if (pw.length) {
    const p = pw[0], form = p.form, scope = form || document;
    const before = textInputs(scope).filter(i => i.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING);
    const user = before.filter(i => userHint.test((i.name || '') + (i.id || '') + (i.autocomplete || '') + (i.placeholder || '') + (i.type || '')))
        .pop() || before.pop() || null;
    const submit = submitIn(scope);
    p.setAttribute('data-wa-login', 'password');
    if (user) user.setAttribute('data-wa-login', 'user');
    if (submit) submit.setAttribute('data-wa-login', 'submit');
    return {found: true, email_first: false, has_user: !!user, has_submit: !!submit,
            action: (form ? form.action : location.href) || location.href, ...base};
  }
  // 2. No password yet, but an email/username box on a page that is clearly a sign-in page (email-first login).
  const loginish = /log ?in|sign ?in|sign-in|signin|auth|account|oauth|sso/i.test(location.hostname + location.pathname) ||
      [...document.querySelectorAll('h1, h2, [role=heading]')].some(h => visible(h) && /sign ?in|log ?in/i.test(h.innerText));
  if (loginish) {
    const emails = textInputs(document).filter(i => userHint.test((i.name || '') + (i.id || '') + (i.autocomplete || '') + (i.placeholder || '') + (i.type || '')));
    if (emails.length) {
      const u = emails[0], form = u.form, scope = form || document, submit = submitIn(scope);
      u.setAttribute('data-wa-login', 'user');
      if (submit) submit.setAttribute('data-wa-login', 'submit');
      return {found: true, email_first: true, has_user: true, has_submit: !!submit,
              action: (form ? form.action : location.href) || location.href, ...base};
    }
  }
  // 3. Neither: is there a "Log in" / "Sign in" entry point (public page), and no sign-out control?
  const signedIn = [...document.querySelectorAll('a, button, [role=button], [role=menuitem]')].some(
      e => visible(e) && /^(log ?out|sign ?out)$/i.test((e.innerText || e.getAttribute('aria-label') || '').trim()));
  let entry = null;
  if (!signedIn) {
    entry = [...document.querySelectorAll('a, button, [role=button]')].find(e => visible(e) &&
      /^(log ?in|sign ?in|log ?in ?\/ ?sign ?up|sign ?in ?\/ ?register)$/i.test((e.innerText || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ')));
    if (entry) entry.setAttribute('data-wa-login', 'entry');
  }
  return {found: false, entry: !!entry, entry_href: entry ? (entry.getAttribute('href') || '') : '', ...base};
}
"""


@dataclass
class LoginForm:
    found: bool
    has_user: bool = False
    has_submit: bool = False
    email_first: bool = False
    entry: bool = False  # a visible "Log in" link/button (the user is logged out)
    entry_href: str = ""
    action: str = ""
    challenge: bool = False  # captcha / MFA indicators on the page
    text: str = ""


@dataclass
class LoginResult:
    status: str  # success | already_logged_in | manual | failed_credentials | email_not_accepted | sso_required |
    #              challenge | blocked_origin | no_login_form | unexpected_redirect | manual_required | skipped
    detail: str = ""
    method: str = "none"  # auto | manual | none
    final_url: str = ""
    blocked_host: str = ""  # for blocked_origin: the host that was refused (the caller may ask the user)
    can_approve: bool = False  # blocked host could be approved by the user (not an SSO provider)

    @property
    def ok(self) -> bool:
        return self.status in {"success", "already_logged_in", "manual"}


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def same_site(a: str, b: str) -> bool:
    """Same host or one is a subdomain of the other. Deliberately strict: no public-suffix guessing."""
    a, b = (a or "").lower(), (b or "").lower()
    return bool(a) and bool(b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def is_sso_provider(host: str) -> bool:
    return any(same_site(host, p) for p in SSO_PROVIDERS)


def secure_enough(url: str) -> bool:
    p = urlparse(url)
    return p.scheme == "https" or (p.scheme == "http" and (p.hostname or "") in {"localhost", "127.0.0.1", "::1"}) \
        or p.scheme == "file"


def origin_problem(page_url: str, form_action: str, site_host: str, allowed_hosts: tuple[str, ...] | set[str] = ()) -> str | None:
    """Reason credentials must NOT be entered here, or None when it is safe."""
    if urlparse(page_url).scheme == "file":
        return None
    trusted = [site_host, *allowed_hosts]
    host = host_of(page_url)
    if is_sso_provider(host) and not same_site(host, site_host):
        return f"'{host}' is a social sign-in provider that blocks automated browsers; sign in there yourself"
    if not any(same_site(host, t) for t in trusted):
        return f"login page is on '{host or 'unknown'}', not the authorized website '{site_host}'"
    if not secure_enough(page_url):
        return "login page is not served over HTTPS"
    if form_action and urlparse(form_action).scheme in {"http", "https"}:
        ah = host_of(form_action)
        if not any(same_site(ah, t) for t in trusted):
            return f"login form submits to '{ah}', not '{site_host}'"
        if not secure_enough(form_action):
            return "login form submits over plain HTTP"
    return None


def detect_login_form(page: Page) -> LoginForm:
    d = page.evaluate(_DETECT_JS)
    text = d.get("text", "")
    # Wording alone only counts on a page that still asks to sign in; a dashboard may mention "verification".
    challenge = bool(d.get("challenge_dom") or d.get("otp_field")
                     or (d.get("found") and CHALLENGE_TEXT.search(text)))
    return LoginForm(found=bool(d.get("found")), has_user=bool(d.get("has_user")),
                     has_submit=bool(d.get("has_submit")), email_first=bool(d.get("email_first")),
                     entry=bool(d.get("entry")), entry_href=d.get("entry_href", "") or "",
                     action=d.get("action", ""), challenge=challenge, text=text)


def login_visible(page: Page) -> bool:
    """A login FORM (password or email-first) is showing. Used to spot expired sessions."""
    return detect_login_form(page).found


def login_state(page: Page) -> str:
    """'form' = a login form is showing; 'entry' = public page with a Log in link; 'none' = nothing asks for login."""
    f = detect_login_form(page)
    return "form" if f.found else ("entry" if f.entry else "none")


def open_login_page(page: Page) -> LoginResult | None:
    """Click the 'Log in' entry point. Returns None when a login form is now showing, else a failure result."""
    form = detect_login_form(page)
    if form.found:
        return None
    if not form.entry:
        return LoginResult("no_login_form", "no 'Log in' link or button found")
    start = page.url
    page.locator("[data-wa-login=entry]").first.click()
    try:
        page.wait_for_function(
            "(u) => location.href !== u || !!document.querySelector('input[type=password], input[type=email]')",
            arg=start, timeout=LOGIN_SETTLE_MS)
    except PlaywrightTimeout:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightTimeout:
        pass
    if detect_login_form(page).found:
        return None
    return LoginResult("no_login_form", f"clicked 'Log in' but no login form appeared at {_short(page.url)}")


def _short(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _blocked(problem: str, page_url: str) -> LoginResult:
    host = host_of(page_url)
    return LoginResult("blocked_origin", f"credentials NOT entered: {problem}", blocked_host=host,
                       can_approve=not is_sso_provider(host))


def _wait_after_submit(page: Page, start_url: str, *, until_password: bool = False) -> None:
    """Wait (bounded) for the site's reaction. For the email step that means a visible password box, an error,
    or a hand-off to another host. A changed URL alone is NOT enough: many sites change the address first
    and draw the next step a moment later."""
    if until_password:
        js = """(u) => {
          const vis = e => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
            return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
          if ([...document.querySelectorAll('input[type=password]')].some(vis)) return true;
          if ([...document.querySelectorAll('[role=alert]')].some(vis)) return true;
          return location.hostname !== new URL(u).hostname;
        }"""
        timeout = 14_000
    else:
        js = ("(u) => !document.querySelector('input[type=password]') || location.href !== u || "
              "!!document.querySelector('[role=alert]')")
        timeout = LOGIN_SETTLE_MS
    try:
        page.wait_for_function(js, arg=start_url, timeout=timeout)
    except PlaywrightTimeout:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=4_000)
    except PlaywrightTimeout:
        pass


def attempt_login(page: Page, creds: Credentials, site_host: str,
                  allowed_hosts: tuple[str, ...] | set[str] = ()) -> LoginResult:
    """One automatic login attempt (email-first logins take two steps). Never retries by itself."""
    form = detect_login_form(page)
    if not form.found:
        if form.challenge:
            return LoginResult("challenge", "verification step shown instead of a login form")
        return LoginResult("no_login_form", "no login form on the page")
    problem = origin_problem(page.url, form.action, site_host, allowed_hosts)
    if problem:
        return _blocked(problem, page.url)
    if not form.has_submit:
        return LoginResult("no_login_form", "could not identify the submit button")
    if form.challenge:
        return LoginResult("challenge", "CAPTCHA or verification detected before submitting")

    if form.email_first:  # step 1: email, then Continue
        start = page.url
        page.locator("[data-wa-login=user]").fill(creds.email)
        page.locator("[data-wa-login=submit]").click()
        _wait_after_submit(page, start, until_password=True)
        form = detect_login_form(page)
        if not form.found and VERIFY_INTRO.search(form.text) and not form.challenge:
            # Only asks to send a code: clicking Continue just requests it. Entering the code stays the person's step.
            btn = page.get_by_role("button", name=re.compile(r"^\s*(continue|send( code)?)\s*$", re.I))
            if btn.count() and btn.first.is_visible():
                before = page.url
                btn.first.click()
                try:
                    page.wait_for_function(
                        "(u) => [...document.querySelectorAll('input')].some(i => i.getBoundingClientRect().width > 0) || "
                        "!!document.querySelector('[role=alert]') || location.href !== u", arg=before, timeout=12_000)
                except PlaywrightTimeout:
                    pass
                _wait_after_submit(page, before, until_password=True)
                form = detect_login_form(page)
        if not form.found or form.email_first:
            if form.challenge:
                return LoginResult("challenge", "verification required after the email step", "auto", page.url)
            if is_sso_provider(host_of(page.url)):
                return _blocked("the site sent you to a social sign-in provider", page.url)
            if not form.found and CHALLENGE_TEXT.search(form.text):
                return LoginResult("challenge", "the site asks for a verification code after the email step", "auto", page.url)
            if not form.found and SOCIAL_ONLY.search(form.text):
                return LoginResult("sso_required", "this account signs in through Google/Facebook/Apple, not with a "
                                   "password the agent can type", "auto", page.url)
            if ERROR_TEXT.search(form.text):
                return LoginResult("email_not_accepted", "the site did not accept the email address", "auto", page.url)
            return LoginResult("email_not_accepted", "no password step appeared after the email step", "auto", page.url)
        problem = origin_problem(page.url, form.action, site_host, allowed_hosts)  # the password step may be elsewhere
        if problem:
            return _blocked(problem, page.url)
        if form.challenge:
            return LoginResult("challenge", "verification required before the password step", "auto", page.url)

    if not form.has_submit:
        return LoginResult("no_login_form", "could not identify the submit button on the password step")
    start_url = page.url
    if form.has_user:  # classic single-page form
        page.locator("[data-wa-login=user]").fill(creds.email)
    page.locator("[data-wa-login=password]").fill(creds.password)
    page.locator("[data-wa-login=submit]").click()
    _wait_after_submit(page, start_url)

    after = detect_login_form(page)
    final = page.url
    if after.found:
        if after.challenge:
            return LoginResult("challenge", "verification required after submitting", "auto", final)
        hint = "site reported an error" if ERROR_TEXT.search(after.text) else "login form is still showing"
        return LoginResult("failed_credentials", f"login rejected ({hint})", "auto", final)
    if after.challenge or CHALLENGE_TEXT.search(after.text):  # e.g. "Enter the code we sent you" after the password
        return LoginResult("challenge", "verification required after submitting", "auto", final)
    if urlparse(final).scheme != "file" and not any(same_site(host_of(final), h) for h in (site_host, *allowed_hosts)):
        return LoginResult("unexpected_redirect", f"after login the browser is on '{host_of(final)}'", "auto", final)
    return LoginResult("success", "login form accepted", "auto", final)
