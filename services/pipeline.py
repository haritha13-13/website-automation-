"""Four-agent sequential CrewAI pipeline:
Content Preparation -> Website Analysis -> Browser Execution -> Verification -> report.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from agents.browser_execution_agent import BrowserExecutionAgent, ExecutionOutcome
from agents.content_preparation_agent import ContentPreparationAgent
from agents.verification_agent import VerificationAgent
from agents.website_analysis_agent import WebsiteAnalysisAgent, build_agent
from agents.base import StageLLM
from browser.playwright_manager import NavigationError
from crew import build_crew
from llm.ollama_client import OllamaConfig, OllamaError
from browser.redaction import safe_screenshot
from services.adapters import AdapterRegistry
from browser.locator_validator import LocatorValidator
from services.auth import LoginResult, host_of
from services.auth import login_state as auth_login_state
from services.analysis_service import ROOT, ServiceIO
from services.browser_worker import BrowserWorker
from services.content_service import PreparationError, site_key
from services.discovery import replay_steps
from services.database import Database
from services.secrets import REDACTOR, CredentialStore, Credentials
from services.playbook import PlaybookStore
from services.workflows import Workflow, WorkflowStore
from services.models import ContentItem, ExecutionPlan, PreparationResult, VerificationResult
from services.report_service import job_status, write_report

logger = logging.getLogger(__name__)


def site_of(url: str) -> str:
    """Browser-tab/plan key: one tab and one plan per website host (or local file)."""
    p = urlparse(url)
    return "file:" + p.path if p.scheme == "file" else (p.hostname or url).lower()


def parse_hints(text: str | None) -> list[str]:
    """'Create, Project' or 'Create > Project' -> ['Create', 'Project'] (what to click after sign-in, in order)."""
    import re
    return [h.strip() for h in re.split(r"[>,;]", text or "") if h.strip()]


def _ollama_unavailable_nav(observation: dict):
    raise OllamaError("Ollama Cloud is unreachable or not configured")


def _ollama_unavailable(observation: dict):
    raise OllamaError("Ollama Cloud is unreachable or not configured; only known adapters can be used")


ALL_TARGETS = {"title", "content", "category", "tags", "images"}


@dataclass
class SitePrep:
    """Outcome of opening one website: login, workflow lookup/analysis, validated plan."""
    site: str
    host: str
    plan: ExecutionPlan | None = None
    login: LoginResult | None = None
    fail_status: str | None = None  # item status to record when plan is None
    fail_error: str | None = None
    workflow: str = "none"  # saved_reused | saved_outdated | new_saved | new_unsaved | none
    workflow_path: str | None = None


@dataclass
class ItemState:
    item: ContentItem
    db_id: int
    status: str = "validated"
    error: str | None = None
    outcome: ExecutionOutcome | None = None
    verification: VerificationResult | None = None


@dataclass
class PipelineOptions:
    allow_local: bool = False
    yes_drafts: bool = False  # auto-approve draft saves (publishing ALWAYS asks)
    headless: bool | None = None
    db_path: Path | str = ROOT / "output" / "automation.db"
    output_dir: Path = ROOT / "output"
    screenshot_dir: Path = ROOT / "screenshots" / "errors"
    max_relogins: int = 2  # per website, to avoid account lockouts
    workflow_dir: Path = ROOT / "workflows"
    playbook_dir: Path = ROOT / "playbooks"
    confirm_login: bool = False  # after an automatic sign-in, the person confirms they see the signed-in site
    skip_login: bool = False  # the browser is already signed in: never type credentials, only check the session
    cdp_url: str | None = None  # attach to the person's own Chrome (see scripts/start_chrome_for_agent.sh)


class AutomationPipeline:
    def __init__(self, cfg: OllamaConfig, io: ServiceIO, options: PipelineOptions | None = None,
                 analysis_agent: WebsiteAnalysisAgent | None = None, registry: AdapterRegistry | None = None,
                 ollama_available: bool = True, credentials: CredentialStore | None = None) -> None:
        self.cfg, self.io = cfg, io
        self.credentials = credentials or CredentialStore()
        self.logins: dict[str, LoginResult] = {}
        self.hosts: dict[str, str] = {}
        self.relogins: dict[str, int] = {}
        self.opts = options or PipelineOptions()
        self.registry = registry or AdapterRegistry()
        self.workflows = WorkflowStore(self.opts.workflow_dir)
        self.playbooks = PlaybookStore(self.opts.playbook_dir)
        self.analysis = analysis_agent or WebsiteAnalysisAgent(cfg)
        self.ollama_available = ollama_available
        self.db = Database(self.opts.db_path)
        self.worker = BrowserWorker(self.opts.headless, self.opts.cdp_url)
        self.prep_agent = ContentPreparationAgent(self.opts.allow_local)
        self.verify_agent = VerificationAgent()
        # run state
        self.run_id = 0
        self.source = ""
        self.started = ""
        self.prep: PreparationResult | None = None
        self.states: list[ItemState] = []
        self.plans: dict[str, ExecutionPlan] = {}
        self.site_urls: dict[str, str] = {}
        self.notes: list[str] = []
        self.invalid_rows: list[dict] = []
        self.fatal: PreparationError | None = None  # unusable input file; later stages are skipped

    # ------------------------------------------------------------------------------------------
    def run(self, source_file: str) -> dict:
        self.source = source_file
        self.started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.run_id = self.db.start_run(source_file)
        self.exec_agent = BrowserExecutionAgent(self.db, self.run_id, self._approve, self._confirm_republish,
                                                self._approve_playbook)
        status = "completed"
        try:
            self._run_crew()
            if self.fatal:
                raise self.fatal
            if not self.worker.is_headless and self.states:
                self.io.ask("\nAll done. The browser stays open so you can look at the result.\n"
                            "Press ENTER to close it and see the report...")
        except PreparationError as exc:
            status = "failed"
            self.notes.append(str(exc))
            raise
        except BaseException:
            status = "aborted"
            raise
        finally:
            self.db.finish_run(self.run_id, status)
            self.worker.close()
        path, report = write_report(self.opts.output_dir, self.run_id, source_file, str(self.opts.db_path),
                                    self._item_report(), self.invalid_rows, self._site_report(),
                                    self.started, self.notes)
        report["report_path"] = str(path)
        return report

    def close(self) -> None:
        self.worker.close()
        self.db.close()

    # ---- CrewAI orchestration -------------------------------------------------------------------
    def _run_crew(self) -> None:
        a1 = self.prep_agent.crew_agent(lambda: self._stage("prepare", self._stage_prepare))
        a2 = build_agent(self.cfg, llm=StageLLM(lambda: self._stage("analyze", self._stage_analyze)))
        a3 = self.exec_agent.crew_agent(lambda: self._stage("execute", self._stage_execute))
        a4 = self.verify_agent.crew_agent(lambda: self._stage("verify", self._stage_verify))
        build_crew(a1, a2, a3, a4).kickoff()

    def _stage(self, name: str, fn: Callable[[], dict]) -> str:
        if self.fatal:
            return json.dumps({"stage": name, "skipped": "input file unusable"})
        try:
            result = fn()
        except PreparationError as exc:
            # Don't raise inside the crew: CrewAI would retry the stage. Report it once, after the crew ends.
            self.fatal = exc
            return json.dumps({"stage": name, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a stage failure must not lose already-recorded results
            logger.exception("Stage %s failed", name)
            self.db.log(self.run_id, name, f"stage error: {type(exc).__name__}: {exc}")
            self.notes.append(f"Stage '{name}' stopped early: {type(exc).__name__}: {str(exc)[:200]}")
            result = {"stage": name, "error": str(exc)[:200]}
        return json.dumps(result)

    # ---- stage 1 ----------------------------------------------------------------------------------
    def _stage_prepare(self) -> dict:
        self.io.info("[Agent 1] Validating content...")
        self.prep = self.prep_agent.prepare(self.source)
        for err in self.prep.errors:
            self.invalid_rows.append(err.model_dump())
            self.db.add_item(self.run_id, err.row_number, "", "", "", None, None, "invalid", "; ".join(err.errors))
            self.io.warn(f"Row {err.row_number} skipped: {'; '.join(err.errors)}")
        for item in self.prep.items:
            db_id = self.db.add_item(self.run_id, item.row_number, site_key(item.website_url), item.website_url,
                                     item.title, item.content_hash, item.publish_mode, "validated")
            self.states.append(ItemState(item, db_id))
        self.io.info(f"{len(self.prep.items)} of {self.prep.total_rows} row(s) valid, {len(self.prep.errors)} skipped.")
        return {"valid": len(self.prep.items), "invalid": len(self.prep.errors),
                "rows": [i.model_dump(exclude={"content", "image_paths"}) for i in self.prep.items]}

    # ---- stage 2 ----------------------------------------------------------------------------------
    def _stage_analyze(self) -> dict:
        self.io.info("[Agent 2] Analyzing websites...")
        by_site: dict[str, list[ItemState]] = {}
        for st in self.states:
            by_site.setdefault(site_of(st.item.website_url), []).append(st)
        for site, group in by_site.items():
            url = group[0].item.website_url
            self.site_urls[site] = url
            try:
                self._analyze_site(site, url, group)
            except NavigationError as exc:
                self._fail_group(group, "analysis_failed", f"could not open website: {exc}")
        return {"websites": {s: self.plans[s].model_dump(exclude={"validation", "playbook_steps"}) for s in self.plans}}

    def _analyze_site(self, site: str, url: str, group: list[ItemState]) -> None:
        targets = {"title", "content"}
        actions: set[str] = set()
        for st in group:
            i = st.item
            targets |= {n for n, present in (("category", i.category), ("tags", i.tags), ("images", i.images)) if present}
            actions.add("publish" if i.publish_mode == "publish" else "save_draft")
        prep = self._open_and_plan(site, url, targets, actions)
        plan = prep.plan
        if plan is None:
            self._fail_group(group, prep.fail_status or "analysis_failed", prep.fail_error or "website could not be prepared")
            return
        for st in group:
            missing = plan.supports(st.item)
            if plan.validation_status == "failed":
                self._set(st, "analysis_failed", "no usable mapping: " + "; ".join(plan.notes)[:300])
            elif missing:
                self._set(st, "analysis_failed", f"website mapping lacks: {', '.join(missing)}")

    def analyze_website(self, url: str, nav_hint: str | None = None) -> SitePrep:
        """Menu option 1: reuse the saved workflow for this website, or analyze it (and offer to save)."""
        from services.analysis_service import validate_url
        url = validate_url(url, self.opts.allow_local)
        self.source = f"analyze:{url}"
        self.run_id = self.db.start_run(self.source)
        self.exec_agent = BrowserExecutionAgent(self.db, self.run_id, self._approve, self._confirm_republish,
                                                self._approve_playbook)
        status = "completed"
        try:
            prep = self._open_and_plan(site_of(url), url, set(ALL_TARGETS), {"save_draft", "publish"},
                                       hints=parse_hints(nav_hint))
            self.io.info("Analysis finished." if prep.plan else "Analysis stopped.")
            return prep
        except BaseException:
            status = "aborted"
            raise
        finally:
            self.db.finish_run(self.run_id, status)
            self.worker.close()

    def _open_and_plan(self, site: str, url: str, targets: set[str], actions: set[str],
                       hints: list[str] | None = None) -> SitePrep:
        host = host_of(url)
        self.hosts[site] = host
        self.site_urls[site] = url
        prep = SitePrep(site=site, host=host)
        page_call = lambda fn: self.worker.page_call(site, fn)  # noqa: E731

        # 1. The user must have explicitly authorized this website.
        if host and not self.db.is_authorized(host):
            if self.io.ask(f"Are you authorized to automate {host}? [y/N] ").strip().lower() not in {"y", "yes"}:
                prep.fail_status, prep.fail_error = "analysis_failed", f"{host} is not authorized for automation"
                return prep
            self.db.authorize(host)

        # 2. Open it and authenticate (deterministic Playwright, never the model).
        self.io.info(f"[Agent 3] Opening {site} ...")
        self.worker.navigate(site, url)
        login = self._authenticate(site, host, page_call, url)
        self.logins[site] = login
        prep.login = login
        if not login.ok:
            self._error_shot(site, "login")
            self.db.log(self.run_id, "login", f"{site}: {login.status}")
            prep.fail_status, prep.fail_error = "login_failed", f"login failed ({login.status}): {login.detail}"
            return prep
        if login.status != "already_logged_in":
            self.io.info("[Agent 3] Authentication successful.")
            if login.ok and login.method in {"auto", "mixed"} and self.opts.confirm_login:
                # The agent typed the credentials; the person validates the result before anything else happens.
                ans = self.io.ask(f"\n>>> PLEASE CHECK <<<\nThe agent signed in to {host or site}. Look at the browser window: "
                                  "is it signed in as you?\nPress ENTER (or type yes) to continue, or 'no' to stop: ").strip().lower()
                if ans in {"n", "no"}:
                    prep.fail_status, prep.fail_error = "login_failed", "you did not confirm the automatic sign-in"
                    return prep
            if login.status in {"success", "manual"}:
                self.worker.navigate(site, url)  # back to the requested page now that the session is valid
        if login.status == "unexpected_redirect":
            if self.io.ask(f"After login the browser is on a different domain ({host_of(login.final_url)}). "
                           "Continue anyway? [y/N] ").strip().lower() not in {"y", "yes"}:
                prep.fail_status, prep.fail_error = "login_failed", "unexpected redirect after login"
                return prep

        # 3a. A playbook for this website (multi-page block editors): Agent 2 plans from it instead of guessing.
        playbook = self.playbooks.find(url)
        if playbook:
            plan = ExecutionPlan(
                website_type="portfolio", source="playbook", adapter_name=f"playbook:{playbook.name}",
                page_type="article_editor", validation_status="passed", requires_human_review=not playbook.verified,
                playbook_name=playbook.name, playbook_steps=playbook.steps, requires=list(playbook.requires),
                notes=([] if playbook.verified else
                       ["This playbook was written from your description and has not completed on the live site yet; "
                        "if a step fails you will get a debug file to fix it."]))
            self.plans[site] = plan
            prep.plan, prep.workflow = plan, "playbook"
            self.io.info(f"[Agent 2] Playbook '{playbook.name}' found for {host}: {len(playbook.steps)} steps "
                         f"({'verified' if playbook.verified else 'not yet verified on the live site'}).")
            self.db.log(self.run_id, "analyze", f"{site}: playbook {playbook.name}")
            return prep

        # 3b. Saved workflow for this website? Replay it (validated live); otherwise discover it from scratch.
        saved = self.workflows.find(url)
        adapter = self.registry.find(url)
        hook = lambda m: self.io.info(f"[Agent 2] Clicking: {m}")  # noqa: E731
        if not self.ollama_available:
            self.analysis._analyzer = _ollama_unavailable
            self.analysis._nav_chooser = _ollama_unavailable_nav
        plan = None
        if saved:
            self.io.info(f"[Agent 2] Saved workflow found for {saved.site} (saved {saved.saved_at[:10]}). "
                         "Checking it against the live website...")
            plan = self._replay_saved(site, url, saved, targets, actions, page_call)
            if plan is not None:
                prep.workflow = "saved_reused"
                self.io.info("[Agent 2] Saved workflow still matches the website: using it (no AI needed).")
            else:
                prep.workflow = "saved_outdated"
                self.io.warn("The saved workflow no longer matches the website; discovering it again.")
                self.worker.navigate(site, url)
        else:
            self.io.info(f"[Agent 2] No saved workflow for {host or site}. "
                         f"{'Known adapter: ' + adapter.name if adapter else 'No adapter either'}"
                         f"{'' if adapter or self.ollama_available else ' and Ollama is unavailable'}.")
        if plan is None:
            if hints:
                self.io.info(f"[Agent 2] Following your directions: {' -> '.join(hints)}")
            self.io.info("[Agent 2] Analyzing page layout and looking for the article-creation page...")
            plan = self.analysis.locate_editor_plan(page_call, url, targets, actions, adapter, on_hop=hook, hints=hints)
        if plan.validation_status == "failed" and self.relogins.get(site, 0) < self.opts.max_relogins \
                and page_call(lambda p: auth_login_state(p)) != "none":
            # Navigation ended on a sign-in page (not logged in yet, or session ended): log in once more.
            self.relogins[site] = self.relogins.get(site, 0) + 1
            self.io.info("[Agent 3] The website is asking for a login again; signing in...")
            again = self._authenticate(site, host, page_call, url)
            self.logins[site] = again
            if again.ok:
                self.worker.navigate(site, url)
                plan = self.analysis.locate_editor_plan(page_call, url, targets, actions, adapter, on_hop=hook, hints=hints)
        if plan.validation_status == "failed" and not self.worker.is_headless:
            self._show_plan(site, plan)
            ans = self.io.ask(
                "\n>>> ACTION NEEDED in this terminal <<<\n"
                f"I could not find the article-creation page on {host or site}.\n"
                "Tell me what to click after sign-in, in order, using the text on the link or button\n"
                "   (for example:  Create, Project )\n"
                "or open the new-article page yourself in the browser and press ENTER. Type 's' to skip this website.\n> ").strip()
            if ans.lower() != "s":
                if ans.lower() in {"", "y", "yes"}:  # the person opened the page themselves
                    self.worker.settle(site)
                    plan = self.analysis.locate_editor_plan(page_call, page_call(lambda p: p.url), targets, actions,
                                                            adapter, max_hops=0)
                else:
                    self.worker.navigate(site, url)
                    plan = self.analysis.locate_editor_plan(page_call, url, targets, actions, adapter, on_hop=hook,
                                                            hints=parse_hints(ans))
        self.plans[site] = plan
        prep.plan = plan
        self._show_plan(site, plan)
        self.db.log(self.run_id, "analyze", f"{site}: {plan.validation_status} via {plan.source}")

        # An AI-derived mapping is an untrusted proposal: the user confirms it even after validation.
        if plan.source == "ollama" and plan.validation_status != "failed":
            if self.io.ask("Use this AI-proposed mapping for this website? [y/N] ").strip().lower() not in {"y", "yes"}:
                plan.validation_status = "failed"
                plan.notes.append("mapping declined by user")
            elif self.io.ask("Save this website's workflow for next time? [Y/n] ").strip().lower() not in {"n", "no"}:
                path = self.workflows.save(Workflow.from_plan(plan, url))
                prep.workflow, prep.workflow_path = "new_saved", str(path)
                self.io.info(f"[Agent 2] Workflow saved: {path}")
            else:
                prep.workflow = "new_unsaved"
        elif prep.workflow == "saved_reused" and plan.validation_status != "failed":
            refreshed = Workflow.from_plan(plan, url)  # keep everything the saved workflow already knew
            refreshed.field_mapping = {**saved.field_mapping, **plan.field_mapping}
            refreshed.actions = {**saved.actions, **plan.actions}
            refreshed.navigation_path = saved.navigation_path or refreshed.navigation_path
            refreshed.steps = saved.steps or refreshed.steps
            refreshed.source = saved.source
            prep.workflow_path = str(self.workflows.save(refreshed))  # refreshes last_validated_at
        return prep

    # ---- authentication -------------------------------------------------------------------------------
    def _credentials_for(self, host: str) -> Credentials | None:
        creds, kind = self.credentials.for_host(host)
        if creds is None or not host:
            return None
        if kind == "shared" and not self.db.has_credential_grant(host):
            ans = self.io.ask(f"Send your SHARED login to the new website '{host}'? [y/N] ").strip().lower()
            if ans not in {"y", "yes"}:
                self.io.info(f"[Agent 3] Not sending credentials to {host}; you will log in manually.")
                return None
            self.db.grant_credentials(host)
        return creds

    def _authenticate(self, site: str, host: str, page_call, url: str | None = None) -> LoginResult:
        # --skip-login: trust the browser's existing session. No credentials are ever sent; if the site still
        # shows a sign-in, the person is asked to sign in by hand (the session is never assumed).
        creds = None if self.opts.skip_login else self._credentials_for(host)

        def remember(new: Credentials) -> None:
            if host in self.credentials.by_host or self.credentials.shared is None:
                self.credentials.by_host[host] = new
            else:
                self.credentials.shared = new
        start = url or self.site_urls.get(site) or ""

        def recheck() -> str:
            """Reload the website and report whether it still asks to sign in ('none' = signed in)."""
            if start:
                self.worker.navigate(site, start)
            return self.worker.page_call(site, auth_login_state)
        return self.exec_agent.ensure_login(
            host, page_call, creds, ask=self.io.ask, ask_secret=self.io.ask_secret, info=self.io.info,
            headless=self.worker.is_headless, on_new_credentials=remember,
            confirm_login_host=lambda idp: self._confirm_login_host(host, idp),
            recheck=recheck, debug_hook=lambda why: self._login_debug(site, host, why))

    def _login_debug(self, site: str, host: str, why: str) -> None:
        """Save what the sign-in page offered (names only) + a masked screenshot, to fix login problems."""
        from browser.redaction import safe_screenshot
        from services import playbook as pb
        try:
            creds, _ = self.credentials.for_host(host)
            base = f"login_debug_run{self.run_id}_{re.sub(r'[^a-z0-9]+', '_', host or site)}_{why}"
            shot = self.opts.output_dir / f"{base}.png"
            self.worker.page_call(site, lambda p: safe_screenshot(p, shot, [creds.email] if creds else []))
            info = self.worker.page_call(site, pb.collect_debug)
            info["text_excerpt"] = self.worker.page_call(site, lambda p: p.evaluate("document.body ? document.body.innerText : ''"))[:600]
            path = pb.write_debug(self.opts.output_dir, self.run_id, 0, f"login_{why}", f"login stopped: {why}", info, str(shot),
                                  name=f"{base}.json")
            self.io.info(f"[Agent 3] Sign-in details saved (paste this file if you need help): {path}")
        except Exception as exc:  # noqa: BLE001 - diagnostics must never break the run
            logger.debug("login debug failed: %s", exc)

    def _replay_saved(self, site: str, url: str, saved: Workflow, targets: set[str], actions: set[str],
                      page_call) -> ExecutionPlan | None:
        """Re-validate a saved workflow without the model: first at its editor URL, then by replaying its steps.
        Returns the validated plan, or None when the website no longer matches."""
        def check(where: str) -> ExecutionPlan | None:
            plan = self.analysis.plan_for_site(page_call, where, targets, actions, saved.to_adapter(), adapter_only=True)
            ok = plan.source == "adapter" and plan.validation_status != "failed" and not plan.unmapped
            if not ok:
                return None
            plan.editor_url = page_call(lambda p: p.url).split("#")[0].split("?")[0]
            plan.steps, plan.navigation_path = list(saved.steps), list(saved.navigation_path)
            return plan
        try:
            if saved.editor_url:
                self.worker.navigate(site, saved.editor_url)
                plan = check(saved.editor_url)
                if plan:
                    return plan
            if saved.steps:
                self.worker.navigate(site, url)
                err = page_call(lambda p: replay_steps(p, saved.steps))
                if err is None:
                    return check(url)
                self.io.warn(f"Replaying the saved steps failed: {err}")
        except NavigationError as exc:
            self.io.warn(f"Saved workflow could not be opened: {exc}")
        return None

    def _confirm_login_host(self, site_host: str, login_host: str) -> bool:
        """The website's sign-in page is on another domain (an identity provider). Credentials go there only
        if the person explicitly approves it; the approval (domain names only) is remembered."""
        key = f"{site_host}>{login_host}"
        if self.db.has_credential_grant(key):
            return True
        ans = self.io.ask(f"\n{site_host} signs you in on a different domain: {login_host}.\n"
                          f"Is that the official login for {site_host}, and may your login be sent there? [y/N] ")
        if ans.strip().lower() in {"y", "yes"}:
            self.db.grant_credentials(key)
            return True
        self.io.info(f"[Agent 3] Not sending credentials to {login_host}.")
        return False

    def _open_editor(self, site: str, item: ContentItem) -> str:
        """Open the editor for one row, recovering from an expired session (bounded)."""
        plan = self.plans[site]
        page_call = lambda fn: self.worker.page_call(site, fn)  # noqa: E731
        url = plan.editor_url or item.website_url
        self.worker.navigate(site, url)
        from services import auth
        if plan.steps and plan.field_mapping.get("title"):  # editor needs clicking to open? replay the saved steps
            title_sel = plan.field_mapping["title"]
            if not page_call(lambda p: LocatorValidator(p).locate(title_sel)[0] is not None) \
                    and not page_call(lambda p: auth.login_visible(p)):
                self.worker.navigate(site, item.website_url)
                err = page_call(lambda p: replay_steps(p, plan.steps))
                if err:
                    raise RuntimeError(f"could not reopen the editor by replaying the workflow ({err})")
        if page_call(lambda p: auth.login_visible(p)):
            if self.relogins.get(site, 0) >= self.opts.max_relogins:
                raise RuntimeError("session expired and the re-login limit was reached")
            self.relogins[site] = self.relogins.get(site, 0) + 1
            self.io.warn(f"[Agent 3] Session expired for {site}; logging in again.")
            res = self._authenticate(site, self.hosts.get(site, ""), page_call, url)
            self.logins[site] = res
            if not res.ok:
                raise RuntimeError(f"session expired and re-login failed ({res.status})")
            self.worker.navigate(site, url)
        return self.worker.page_call(site, lambda p: p.url)

    def _error_shot(self, site: str, tag: str) -> None:
        """Best-effort screenshot with every input and any known secret masked."""
        try:
            creds, _ = self.credentials.for_host(self.hosts.get(site, ""))
            path = self.opts.screenshot_dir / f"run{self.run_id}_{tag}_{abs(hash(site)) % 10000}.png"
            self.worker.page_call(site, lambda p: safe_screenshot(p, path, [creds.email] if creds else []))
            self.notes.append(f"error screenshot (inputs masked): {path}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("error screenshot failed: %s", exc)

    def _show_plan(self, site: str, plan: ExecutionPlan) -> None:
        self.io.info(f"{site}: plan {plan.validation_status.upper()} (source: {plan.source}"
                     f"{', ' + plan.adapter_name if plan.adapter_name else ''})")
        for k, v in {**plan.field_mapping, **plan.actions}.items():
            self.io.info(f"   {k:<10} -> {v}")
        for n in plan.notes:
            self.io.warn(f"   {n}")

    # ---- stage 3 ----------------------------------------------------------------------------------
    def _stage_execute(self) -> dict:
        self.io.info("[Agent 3] Preparing content upload...")
        submitted = 0
        for st in self.states:
            if st.status != "validated":
                continue
            site = site_of(st.item.website_url)
            plan = self.plans.get(site)
            if plan is None:
                self._set(st, "analysis_failed", "no plan for website")
                continue
            self.io.info(f"Row {st.item.row_number}: '{st.item.title}' -> {site} ({st.item.publish_mode})")
            page_call = lambda fn, s=site: self.worker.page_call(s, fn)  # noqa: E731
            if plan.source == "playbook":
                creds, _ = self.credentials.for_host(self.hosts.get(site, ""))
                out = self.exec_agent.execute_playbook(
                    st.item, st.db_id, plan, page_call, lambda s=site, it=st.item: self._open_editor(s, it),
                    ask=self.io.ask, info=self.io.info, debug_dir=self.opts.output_dir,
                    mask=[creds.email] if creds else [])
            else:
                out = self.exec_agent.execute_item(
                    st.item, st.db_id, plan, page_call, lambda s=site, it=st.item: self._open_editor(s, it))
            st.outcome = out
            if out.status == "submitted":
                submitted += 1
                self._set(st, "submitted")
            else:
                self._set(st, out.status, out.error)
                self.io.warn(f"Row {st.item.row_number}: {out.status.replace('_', ' ')}"
                             f"{' - ' + out.error if out.error else ''}")
                if out.status in {"failed", "unverified"}:
                    self._error_shot(site, f"row{st.item.row_number}")
        return {"submitted": submitted}

    # ---- stage 4 ----------------------------------------------------------------------------------
    def _stage_verify(self) -> dict:
        self.io.info("[Agent 4] Verifying results...")
        done = 0
        for st in self.states:
            plan = self.plans.get(site_of(st.item.website_url))
            if st.status != "submitted" or st.outcome is None or plan is None:
                continue
            if plan.source != "playbook" and st.outcome.snapshot is None:
                continue
            site = site_of(st.item.website_url)
            login = self.logins.get(site)
            if plan.source == "playbook":
                o = st.outcome
                result = self.verify_agent.check_playbook(
                    st.item, o.playbook_done, o.playbook_total, o.user_published, o.captured_url,
                    o.editor_url or st.item.website_url, lambda u, s=site: self.worker.fetch_page(u, s),
                    login.status if login else None)
            else:
                result = self.verify_agent.check(
                    st.item, plan, st.outcome.snapshot, st.outcome.editor_url or st.item.website_url,
                    lambda u, s=site: self.worker.fetch_article(u, s), login.status if login else None)
            st.verification = result
            if result.status == "verified":
                self._set(st, "draft_saved" if st.item.publish_mode == "draft" else "published", article_url=result.article_url)
            elif result.rejected_by_site:
                self._set(st, "rejected_by_site", "; ".join(c.detail for c in result.checks if c.status == "FAIL"))
            elif result.status == "failed":
                self._set(st, "verification_failed", "; ".join(f"{c.name}: {c.detail}" for c in result.checks if c.status == "FAIL"))
            else:
                self._set(st, "unverified", "content could not be confirmed on the page")
            if st.status not in {"draft_saved", "published"}:
                self._error_shot(site, f"row{st.item.row_number}")
            done += 1
        return {"verified": done}

    # ---- checkpoints ------------------------------------------------------------------------------
    def _approve(self, item: ContentItem, plan: ExecutionPlan, steps: list[str]) -> bool:
        publishing = item.publish_mode == "publish"
        if not publishing and self.opts.yes_drafts:
            return True
        self.io.info("\n--- Approval required ---")
        self.io.info(f"Row {item.row_number} | {item.website_url}")
        self.io.info(f"Title: {item.title}")
        self.io.info(f"Content: {len(item.content)} chars | Category: {item.category or '-'} | "
                     f"Tags: {', '.join(item.tags) or '-'} | Images: {len(item.images)}")
        self.io.info("The form is filled in the browser. Review it there.")
        q = ("\nConfirm PUBLISHING this live? (yes/no): " if publishing else "\nSave this article as a draft? (yes/no): ")
        return self.io.ask(q).strip().lower() in {"y", "yes"}

    def _approve_playbook(self, item: ContentItem, plan: ExecutionPlan) -> bool:
        self.io.info("\n--- Approval required ---")
        self.io.info(f"Row {item.row_number} | {item.website_url}")
        self.io.info(f"Title: {item.title}")
        self.io.info(f"Backlink: '{item.anchor_text or '-'}' -> {item.blog_url or '-'} | Category: {item.category or '-'} | "
                     f"Tags: {', '.join(item.tags) or '-'} | Images: {len(item.images)} + cover {'yes' if item.cover_image else 'no'}")
        self.io.info(f"Playbook '{plan.playbook_name}' will fill the project step by step in the browser. "
                     "It will NOT publish: you click Publish yourself.")
        return self.io.ask("\nStart this project now? (yes/no): ").strip().lower() in {"y", "yes"}

    def _confirm_republish(self, item: ContentItem, note: str) -> bool:
        return self.io.ask(f"Row {item.row_number}: {note}. Publishing again may create a duplicate. Continue? [y/N] "
                           ).strip().lower() in {"y", "yes"}

    # ---- bookkeeping ------------------------------------------------------------------------------
    def _set(self, st: ItemState, status: str, error: str | None = None, article_url: str | None = None) -> None:
        st.status, st.error = status, error or st.error
        self.db.update_item(st.db_id, status=status, error=error, article_url=article_url)
        self.db.log(self.run_id, "item", f"row {st.item.row_number}: {status}" + (f" ({error})" if error else ""), st.db_id)

    def _fail_group(self, group: list[ItemState], status: str, error: str) -> None:
        for st in group:
            self._set(st, status, error)

    def _item_report(self) -> list[dict]:
        rows = []
        for r in self.db.items_for_run(self.run_id):
            if r["status"] == "invalid":
                continue
            st = next((s for s in self.states if s.db_id == r["id"]), None)
            rows.append({
                "row_number": r["row_number"], "website_url": r["website_url"], "title": r["title"],
                "publish_mode": r["publish_mode"], "content_hash": r["content_hash"], "status": r["status"],
                "job_status": job_status(r["status"]),
                "error": r["error"], "article_url": r["article_url"],
                "steps": st.outcome.steps if st and st.outcome else [],
                "verification": st.verification.model_dump() if st and st.verification else None,
            })
        return rows

    def _site_report(self) -> list[dict]:
        return [{"site": s, "url": self.site_urls.get(s),
                 "login": ({"status": self.logins[s].status, "method": self.logins[s].method,
                            "detail": REDACTOR.redact(self.logins[s].detail)} if s in self.logins else None),
                 **p.model_dump(exclude={"validation", "playbook_steps"}),
                 "validation": [v.model_dump(mode="json") for v in p.validation]} for s, p in self.plans.items()]
