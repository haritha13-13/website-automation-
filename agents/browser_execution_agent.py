"""Agent 3 - Browser Execution Agent. Deterministic Playwright actions behind explicit approval."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from crewai import Agent
from playwright.sync_api import Page

from agents.base import build_stage_agent
from pathlib import Path

from browser.redaction import safe_screenshot
from services import auth
from services import playbook as pb
from services import execution_service as ex
from services.auth import LoginResult
from services.secrets import Credentials
from services.database import Database
from services.models import ContentItem, ExecutionPlan, PageSnapshot

logger = logging.getLogger(__name__)
PageCall = Callable[[Callable[[Page], object]], object]


@dataclass
class ExecutionOutcome:
    status: str  # submitted | rejected_by_user | duplicate_skipped | failed | unverified
    action: str | None = None
    error: str | None = None
    snapshot: PageSnapshot | None = None
    steps: list[str] = field(default_factory=list)
    editor_url: str | None = None
    captured_url: str | None = None
    playbook_total: int = 0
    playbook_done: int = 0
    user_published: bool = False
    debug_file: str | None = None


class BrowserExecutionAgent:
    ROLE = "Automated Browser Content Publishing Specialist"
    GOAL = ("Use Playwright to enter validated content into the identified website fields and save "
            "or publish it only after explicit user approval.")
    BACKSTORY = ("You operate a real browser session with deterministic Playwright actions. You only "
                 "use selectors that were validated against the live page, you never click save or "
                 "publish without approval, and you never send the same article twice.")

    def __init__(self, db: Database, run_id: int, approve: Callable[[ContentItem, ExecutionPlan, list[str]], bool],
                 confirm_republish: Callable[[ContentItem, str], bool],
                 approve_playbook: Callable[[ContentItem, ExecutionPlan], bool] | None = None) -> None:
        self.db, self.run_id = db, run_id
        self.approve_playbook = approve_playbook
        self.approve = approve
        self.confirm_republish = confirm_republish

    def crew_agent(self, handler: Callable[[], str]) -> Agent:
        return build_stage_agent(self.ROLE, self.GOAL, self.BACKSTORY, handler)

    def ensure_login(self, host: str, page_call: PageCall, creds: Credentials | None, *,
                     ask: Callable[[str], str], ask_secret: Callable[[str], str], info: Callable[[str], None],
                     headless: bool, on_new_credentials: Callable[[Credentials], None] | None = None,
                     confirm_login_host: Callable[[str], bool] | None = None,
                     recheck: Callable[[], str] | None = None,
                     debug_hook: Callable[[str], None] | None = None,
                     max_attempts: int = 2) -> LoginResult:
        """Make the browser logged in for `host`.

        * a public page with a "Log in" / "Sign In" link: the link is opened first;
        * a login form / email-first form: filled automatically, but only on the authorized origin
          (or a login domain the user approved), at most `max_attempts` times (avoids account lockouts);
        * CAPTCHA/MFA, social sign-in (Google, Facebook, ...), unexpected origins and repeated failures
          fall back to manual login by the person. Nothing is bypassed.
        A login only counts as successful when `recheck()` (reload the website, report its login state)
        says the website no longer asks to sign in. Without that check a half-finished login looked like success.
        """
        state = page_call(lambda p: auth.login_state(p))
        if state == "none":
            return LoginResult("already_logged_in", "the website does not ask for a login")
        if state == "entry":
            info(f"[Agent 3] {host} shows a 'Log in' link: opening the sign-in page...")
            failure = page_call(lambda p: auth.open_login_page(p))
            if failure is not None:
                info(f"[Agent 3] {failure.detail}")
                if debug_hook:
                    debug_hook("no_sign_in_page")
                return self._manual_login(page_call, ask, info, headless, recheck, host)
        attempts = 0
        pauses = 0  # human steps (verification code / CAPTCHA) do not count as failed attempts
        approved: set[str] = set()
        while creds is not None and attempts < max_attempts:
            res = page_call(lambda p, c=creds: auth.attempt_login(p, c, host, approved))
            if res.status == "challenge" and pauses < 3 and not headless:
                pauses += 1
                ask("\n>>> YOUR TURN <<<\nThe website asks for a verification step (for example the code it just emailed "
                    f"you or a CAPTCHA).\nComplete it in the browser window, then press ENTER here. "
                    "The agent will then continue by itself (and type the password if it is asked for).\n> ")
                page_call(lambda p: p.wait_for_load_state("domcontentloaded"))
                if page_call(lambda p: auth.login_state(p)) == "none":  # that was the last step
                    if recheck is None or recheck() == "none":
                        return LoginResult("success", "signed in: the agent typed the credentials, you did the verification",
                                           "mixed")
                continue  # the next page (e.g. the password step) is handled automatically
            if res.status == "blocked_origin" and res.can_approve and res.blocked_host not in approved \
                    and confirm_login_host is not None:
                # The website uses a separate identity domain (e.g. Behance -> Adobe). Only after the person says yes.
                if confirm_login_host(res.blocked_host):
                    approved.add(res.blocked_host)
                    continue  # re-check on the approved origin; this is not a failed attempt
            attempts += 1
            info(f"[Agent 3] Login attempt {attempts}: {res.status.replace('_', ' ')}"
                 f"{' - ' + res.detail if res.status != 'success' and res.detail else ''}")
            if res.ok:
                if recheck is not None and recheck() != "none":  # the website does not agree that we are signed in
                    res = LoginResult("failed_credentials", f"{host} still shows a sign-in after the login", "auto", res.final_url)
                    info(f"[Agent 3] {res.detail}")
                else:
                    return res
            elif res.status == "unexpected_redirect":
                return res
            if debug_hook:
                debug_hook(res.status)
            if res.status == "sso_required":
                info("[Agent 3] This account signs in with Google/Facebook/Apple. Google blocks automated browsers, so "
                     "sign in yourself: either use an email+password login for the site, or run with --attach-chrome "
                     "(your own Chrome) so you can sign in there.")
                break
            if res.status == "email_not_accepted":
                info("[Agent 3] Not a password problem: the site did not move on to a password step. Often the account "
                     "uses Google/another provider, or needs a verification step. Details were saved for you.")
                break
            if res.status != "failed_credentials":
                break  # challenge / refused origin / no form: a human has to continue
            if attempts < max_attempts:
                pw = ask_secret(f"The site rejected the password. Re-enter the password for {host} "
                                "(leave blank to log in manually): ")
                if not pw:
                    break
                creds = Credentials(creds.email, pw)
                if on_new_credentials:
                    on_new_credentials(creds)
                if page_call(lambda p: auth.login_state(p)) == "entry":
                    page_call(lambda p: auth.open_login_page(p))
        return self._manual_login(page_call, ask, info, headless, recheck, host)

    @staticmethod
    def _manual_login(page_call: PageCall, ask: Callable[[str], str], info: Callable[[str], None],
                      headless: bool, recheck: Callable[[], str] | None = None, host: str = "the website",
                      rounds: int = 3) -> LoginResult:
        """The person signs in; we only believe it once the website itself no longer asks to sign in."""
        if headless:
            return LoginResult("manual_required", "manual login is needed but the browser is headless")
        info("[Agent 3] Manual login needed (CAPTCHA, MFA, Google/SSO, or no saved credentials).")
        for round_no in range(1, rounds + 1):
            if round_no == 1:
                ask("\n>>> ACTION NEEDED in this terminal <<<\nLog in using the opened browser window (complete any "
                    "CAPTCHA or verification yourself).\nWhen the website shows you as signed in, click here and press ENTER...")
            else:
                ask(f"\n>>> NOT SIGNED IN YET <<<\n{host} still shows a 'Sign In' button after reloading it.\n"
                    "Finish signing in in the SAME browser window the agent opened, then press ENTER. "
                    "(If Google refuses the sign-in in this window, press Ctrl+C and see the README: 'Use your own Chrome'.)")
            page_call(lambda p: p.wait_for_load_state("domcontentloaded"))
            state = recheck() if recheck is not None else page_call(lambda p: auth.login_state(p))
            if state == "none":
                return LoginResult("manual", "signed in manually (the website no longer shows a sign-in)", "manual")
        return LoginResult("manual_required", f"{host} still shows a sign-in after manual login", "manual")

    def execute_playbook(self, item: ContentItem, db_id: int, plan: ExecutionPlan, page_call: PageCall,
                         open_start: Callable[[], str], *, ask: Callable[[str], str], info: Callable[[str], None],
                         debug_dir: Path, mask: list[str] | None = None) -> ExecutionOutcome:
        """Run a site playbook for one row: deterministic steps, a pause for the PERSON to publish, evidence capture.

        Nothing is published by the agent. At the playbook's publish step it claims the item (duplicate protection),
        asks you to click Publish yourself, and continues after you confirm.
        """
        prior = self.db.find_blocking(_site(item), item.content_hash, exclude_item=db_id)
        if prior is not None:
            return ExecutionOutcome("duplicate_skipped",
                                    error=f"identical article already {prior['status']} (run {prior['run_id']}, row {prior['row_number']})")
        if self.approve_playbook and not self.approve_playbook(item, plan):
            return ExecutionOutcome("rejected_by_user")
        try:
            start = open_start()
        except Exception as exc:  # noqa: BLE001
            return ExecutionOutcome("failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        ctx = pb.build_context(item)
        steps = [st for st in plan.playbook_steps if pb.applies(st, item.publish_mode)]
        done: list[str] = []
        completed = 0  # steps that finished or were optional and skipped
        published = False
        for i, step in enumerate(steps, 1):
            label = step.get("title") or step["id"]
            info(f"[Agent 3] Step {i}/{len(steps)}: {label}")
            if step["action"] == "wait_user":
                if not self.db.claim(db_id):
                    return ExecutionOutcome("duplicate_skipped", error="identical article claimed by another item", steps=done)
                answer = ask(f"\n>>> YOUR TURN <<<\n{step.get('message', 'Publish the item yourself in the browser.')}\n"
                             "Then come back here and press ENTER (type 'skip' if you did NOT publish).\n> ")
                if answer.strip().lower() == "skip":
                    return ExecutionOutcome("rejected_by_user", steps=done, error="you chose not to publish")
                published = True
                completed += 1
                done.append(f"{step['id']}: you published")
                continue
            try:
                detail = page_call(lambda p, st=step: pb.execute_step(p, st, ctx))
            except Exception as exc:  # noqa: BLE001 - report exactly which step failed
                if step.get("optional"):
                    info(f"[Agent 3]   (optional step skipped: {str(exc).splitlines()[0][:100]})")
                    completed += 1
                    continue
                msg = str(exc).splitlines()[0][:240]
                debug = self._debug(page_call, plan, item, i, step["id"], msg, debug_dir, mask)
                return ExecutionOutcome("unverified" if published else "failed",
                                        error=f"playbook step {i}/{len(steps)} '{step['id']}' failed: {msg}"
                                              f" (details: {debug})", steps=done, debug_file=debug,
                                        user_published=published, playbook_total=len(steps), playbook_done=completed)
            completed += 1
            done.append(f"{step['id']}: {detail}")
        return ExecutionOutcome("submitted", action="publish" if published else "review", steps=done, editor_url=start,
                                captured_url=ctx.get("captured_url"), playbook_total=len(steps), playbook_done=completed,
                                user_published=published)

    def _debug(self, page_call: PageCall, plan: ExecutionPlan, item: ContentItem, index: int, step_id: str, msg: str,
               debug_dir: Path, mask: list[str] | None) -> str:
        """Masked screenshot + the names of what the page offers, so the step can be corrected."""
        shot = None
        try:
            shot_path = debug_dir / f"playbook_debug_run{self.run_id}_row{item.row_number}_{step_id}.png"
            page_call(lambda p: safe_screenshot(p, shot_path, mask or []))
            shot = str(shot_path)
        except Exception:  # noqa: BLE001
            pass
        try:
            info = page_call(lambda p: pb.collect_debug(p))
        except Exception as exc:  # noqa: BLE001
            info = {"note": f"could not read the page: {exc}"}
        return str(pb.write_debug(debug_dir, self.run_id, item.row_number, step_id, msg, info, shot))

    def execute_item(self, item: ContentItem, db_id: int, plan: ExecutionPlan, page_call: PageCall,
                     navigate: Callable[[], str]) -> ExecutionOutcome:
        """Fill -> approval -> duplicate claim -> click -> capture evidence. Never raises for row-level problems."""
        # Duplicate protection (before touching the browser).
        prior = self.db.find_blocking(_site(item), item.content_hash, exclude_item=db_id)
        if prior is not None:
            note = f"identical article already {prior['status']} (run {prior['run_id']}, row {prior['row_number']})"
            if not (prior["status"] == "draft_saved" and item.publish_mode == "publish"
                    and self.confirm_republish(item, note)):
                return ExecutionOutcome("duplicate_skipped", error=note)
        try:
            editor_url = navigate()
            steps = page_call(lambda p: ex.fill_form(p, plan, item))
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the others
            return ExecutionOutcome("failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")

        if not self.approve(item, plan, steps):
            return ExecutionOutcome("rejected_by_user", steps=steps)
        if not self.db.claim(db_id):
            return ExecutionOutcome("duplicate_skipped", error="identical article claimed by another item", steps=steps)
        try:
            action = page_call(lambda p: ex.click_action(p, plan, item))
            page_call(lambda p: ex.wait_for_outcome(p, plan, editor_url))
            snap = page_call(lambda p: ex.capture_snapshot(p, plan))
        except Exception as exc:  # noqa: BLE001
            # The click may or may not have gone through: stay blocking so it is never blindly retried.
            return ExecutionOutcome("unverified", error=f"{type(exc).__name__}: {str(exc)[:300]}", steps=steps)
        steps.append("draft saved" if action == "save_draft" else "publish clicked")
        return ExecutionOutcome("submitted", action=action, snapshot=snap, steps=steps, editor_url=editor_url)


def _site(item: ContentItem) -> str:
    from services.content_service import site_key
    return site_key(item.website_url)
