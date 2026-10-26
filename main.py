"""Multi-website content automation (4 CrewAI agents) - terminal entry point."""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

from browser.playwright_manager import BrowserStartError, NavigationError
from llm.ollama_client import OllamaConfig, OllamaError, RedactingFilter, check_connectivity
from llm.schemas import AnalysisResult, Status
from services.secrets import REDACTOR, CredentialStore, RedactingLogFilter
from services.analysis_service import (AnalysisService, EmptyPageError, ServiceIO,
                                       UnsafeTargetError)

BANNER = """\
========================================
       WEBSITE ANALYSIS AI AGENT
========================================

AI Model: GPT-OSS 20B Cloud
Browser Engine: Playwright
Agent Framework: CrewAI
"""

LABELS = {"title": "Title Field", "content": "Content Field", "category": "Category",
          "tags": "Tags", "image": "Image Upload"}
ACTION_LABELS = {"save_draft": "Save Draft", "publish": "Publish"}
SHOWN = {Status.PASS: "PASS", Status.NOT_FOUND: "NOT FOUND", Status.NEEDS_REVIEW: "NEEDS REVIEW",
         Status.FAIL: "FAIL"}


def setup_logging(secret: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    handler.addFilter(RedactingFilter(secret))
    handler.addFilter(RedactingLogFilter())  # credentials entered later are redacted too
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "crewai", "LiteLLM", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


def render(result: AnalysisResult, saved: Path) -> str:
    p = result.ai_proposal
    by = {i.name: i for i in result.browser_validation}
    bar = "=" * 40
    out = [bar, "        WEBSITE ANALYSIS RESULT", bar, "",
           f"Website Type : {p.website_type.replace('_', ' ').title().replace('Cms', 'CMS')}",
           f"Page Type    : {p.page_type.replace('_', ' ').title()}", "",
           "DETECTED FIELDS", "-" * 40]
    for key, label in LABELS.items():
        item = by[key]
        shown = item.selector or "NOT FOUND"
        if item.selector and item.status != Status.PASS:
            shown += "  (unconfirmed)"
        out.append(f"{label:<14}: {shown}")
    out += ["", "DETECTED ACTIONS", "-" * 40]
    for key, label in ACTION_LABELS.items():
        out.append(f"{label:<14}: {'Found' if by[key].status == Status.PASS else SHOWN[by[key].status]}")
    out += ["", "VALIDATION", "-" * 40]
    for key, label in {**{k: v.replace(' Field', '') for k, v in LABELS.items()}, **ACTION_LABELS}.items():
        out.append(f"{label:<14}: {SHOWN[by[key].status]}")
        for note in by[key].notes:
            out.append(f"    - {note}")
    if p.next_step_suggestion and p.page_type != "article_editor":
        out += ["", f"Suggested next step: {p.next_step_suggestion}"]
    status = {"COMPLETED": "COMPLETED", "COMPLETED_WITH_WARNINGS": "COMPLETED WITH WARNINGS",
              "NEEDS_REVIEW": "NEEDS REVIEW"}[result.status]
    out += ["", f"Analysis Status: {status}", "", "Result saved to:", str(saved)]
    return "\n".join(out)


SPREADSHEET_EXT = {".csv", ".xlsx", ".xlsm"}
MENU_BANNER = """\
========================================
   MULTI-WEBSITE AUTOMATION SYSTEM
========================================

1. Analyze Website
2. Upload Content to Real Website
3. Configure Login Credentials
4. View Job History
5. Exit
"""
DEFAULT_DB = "output/automation.db"
DEFAULT_WORKFLOWS = Path(__file__).resolve().parent / "workflows"


def cdp_url(args: argparse.Namespace) -> str | None:
    port = getattr(args, "attach_chrome", None)
    env = os.environ.get("CHROME_CDP_URL")
    return f"http://127.0.0.1:{port}" if port else (env or None)


def say(message: str) -> None:
    print(REDACTOR.redact(message))


def render_run_summary(report: dict) -> str:
    bar = "=" * 40
    items = report["items"]
    count = lambda st: sum(1 for i in items if i["job_status"] == st)  # noqa: E731
    out = [bar, "        CONTENT AUTOMATION REPORT", bar, "",
           f"File           : {report['source_file']}",
           f"Rows in file   : {report['summary']['rows_in_file']}   (invalid: {report['summary']['invalid_rows']})",
           f"COMPLETED      : {count('COMPLETED')}  (published {report['summary']['published']}, "
           f"drafts {report['summary']['drafts_saved']})",
           f"NEEDS_REVIEW   : {count('NEEDS_REVIEW')}",
           f"FAILED         : {count('FAILED')}",
           f"SKIPPED        : {count('SKIPPED')}  (duplicates {report['summary']['duplicates_skipped']}, "
           f"declined {report['summary']['rejected_by_user']})", "", "WEBSITES", "-" * 40]
    for site in report["websites"]:
        lg = site.get("login") or {}
        out.append(f"{site['site'][:50]}: login {lg.get('status', 'n/a')} ({lg.get('method', '-')}), "
                   f"plan {site['validation_status']} via {site['source']}")
    out += ["", "JOBS", "-" * 40]
    for bad in report["invalid_rows"]:
        out.append(f"Row {bad['row_number']:<4} INVALID        {'; '.join(bad['errors'])[:90]}")
    for it in report["items"]:
        out.append(f"Row {it['row_number']:<4} {it['job_status']:<13} {it['title'][:45]}")
        out.append(f"         Status: {it['job_status']}  ({it['status']})")
        if it["article_url"]:
            out.append(f"         Article URL: {it['article_url']}")
        if it["error"]:
            out.append(f"         {it['error'][:110]}")
    out += ["", "Report saved to:", report["report_path"], "Database:", report["database"]]
    return REDACTOR.redact("\n".join(out))


def prompt_credentials(store: CredentialStore) -> None:
    """Ask for the login to use for this upload (hidden password). Blank email = log in manually."""
    if store.configured:
        if input("Use the configured login credentials? [Y/n] ").strip().lower() in {"", "y", "yes"}:
            return
    email = input("\nEnter login email (leave blank to log in manually in the browser):\n> ").strip()
    if not email:
        return
    store.set_shared(email, getpass.getpass("Enter password:\n> "))
    print("(Credentials are kept in memory for this session only and are never saved or logged.)")


def configure_credentials(store: CredentialStore) -> None:
    while True:
        state = ("shared login set" if store.shared else "no shared login") + \
                f", {len(store.by_host)} site-specific"
        print(f"\nLOGIN CREDENTIALS ({state}; memory only)\n1. Set shared login (used on every authorized site)\n"
              "2. Set a website-specific login\n3. Clear all credentials\n4. Back")
        choice = input("Select: ").strip()
        if choice == "1":
            email = input("Email:\n> ").strip()
            if email:
                store.set_shared(email, getpass.getpass("Password:\n> "))
                print("Shared login saved for this session.")
        elif choice == "2":
            host = input("Website domain (e.g. cms.example.com):\n> ").strip().lower()
            email = input("Email:\n> ").strip() if host else ""
            if host and email:
                store.set_for_host(host, email, getpass.getpass("Password:\n> "))
                print(f"Login saved for {host}.")
        elif choice == "3":
            store.clear()
            print("All credentials cleared.")
        elif choice == "4":
            return


def show_history(db_path: str) -> None:
    from services.database import Database
    if not Path(db_path).exists():
        print("\nNo jobs yet.")
        return
    db = Database(db_path)
    try:
        runs = db.recent_runs(10)
        if not runs:
            print("\nNo jobs yet.")
            return
        print(f"\n{'Run':<5}{'Started (UTC)':<22}{'Status':<11}File")
        for r in runs:
            print(f"{r['id']:<5}{r['started_at'][:19]:<22}{r['status']:<11}{r['source_file']}")
        pick = input("\nEnter a run number to see its jobs (or press ENTER): ").strip()
        if pick.isdigit():
            from services.report_service import job_status
            for it in db.items_for_run(int(pick)):
                if it["status"] == "invalid":
                    print(f"Row {it['row_number']:<4} INVALID   {it['error']}")
                else:
                    print(f"Row {it['row_number']:<4} {job_status(it['status']):<13} {it['title'][:40]:<40} {it['article_url'] or ''}")
                    if it["error"]:
                        print(f"          {it['error'][:100]}")
    finally:
        db.close()


def run_pipeline(path: str, cfg: OllamaConfig, ollama_ok: bool, args: argparse.Namespace,
                 store: CredentialStore) -> int:
    from services.content_service import PreparationError
    from services.pipeline import AutomationPipeline, PipelineOptions

    if not ollama_ok:
        say("[WARN] Ollama Cloud is unavailable: only websites with known adapters can be automated.")
    if not (args.manual_login or args.skip_login):
        prompt_credentials(store)
    opts = PipelineOptions(allow_local=args.allow_local, yes_drafts=args.yes_drafts, db_path=args.db,
                           skip_login=args.skip_login, confirm_login=not args.no_confirm_login,
                           workflow_dir=Path(args.workflows),
                           cdp_url=cdp_url(args))
    io = ServiceIO(ask=input, ask_secret=getpass.getpass, info=say, warn=lambda m: say(f"[WARN] {m}"))
    pipe = AutomationPipeline(cfg, io, opts, ollama_available=ollama_ok, credentials=store)
    try:
        report = pipe.run(path)
    except PreparationError as exc:
        print(f"\n[ERROR] {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n[INFO] Cancelled. Browser closed. Items already submitted are recorded in the database.")
        return 130
    finally:
        pipe.close()
    print()
    print(render_run_summary(report))
    bad = [i for i in report["items"] if i["job_status"] in {"FAILED", "NEEDS_REVIEW"}]
    return 4 if bad or report["invalid_rows"] else 0


WORKFLOW_LABELS = {
    "saved_reused": "SAVED workflow reused (checked against the live site, no AI used)",
    "saved_outdated": "saved workflow was OUTDATED; analyzed again",
    "new_saved": "NEW - analyzed now and saved for next time",
    "new_unsaved": "NEW - analyzed now (not saved)",
    "playbook": "PLAYBOOK for this website (ordered steps, no AI needed)",
    "none": "no workflow",
}


def render_workflow(prep) -> str:
    bar = "=" * 40
    plan = prep.plan
    out = [bar, "        WEBSITE WORKFLOW", bar, "", f"Website  : {prep.host or prep.site}"]
    if prep.login:
        out.append(f"Login    : {prep.login.status.replace('_', ' ')} ({prep.login.method})")
    if plan is None:
        out += ["", f"Result   : STOPPED - {prep.fail_error}"]
        return REDACTOR.redact("\n".join(out))
    out += [f"Workflow : {WORKFLOW_LABELS.get(prep.workflow, prep.workflow)}",
            f"Page type: {plan.page_type.replace('_', ' ')}"]
    if len(plan.navigation_path) > 1:
        out.append("Path     : " + "  ->  ".join(plan.navigation_path))
    elif plan.editor_url:
        out.append(f"Editor   : {plan.editor_url}")
    if plan.source == "playbook":
        out += ["", f"PLAYBOOK '{plan.playbook_name}' - {len(plan.playbook_steps)} steps"
                    f"{'' if not plan.requires_human_review else '  (NOT yet verified on the live site)'}", "-" * 40]
        out += [f"{i:>2}. {st.get('title') or st['id']}" for i, st in enumerate(plan.playbook_steps, 1)]
        out += ["", "Spreadsheet columns needed: website_url, " + ", ".join(plan.requires) + ", publish_mode",
                "You click Publish yourself; the agent then opens the project and checks the backlink."]
        out += [f"  - {n}" for n in plan.notes[:3]]
        out += ["", "Status   : READY"]
        return REDACTOR.redact("\n".join(out))
    by = {v.name: v for v in plan.validation}
    labels = {"title": "Title", "content": "Content", "category": "Category", "tags": "Tags", "image": "Image upload",
              "save_draft": "Save Draft", "publish": "Publish"}
    out += ["", "FIELDS AND ACTIONS (validated in the browser)", "-" * 40]
    for key, label in labels.items():
        v = by.get(key)
        if v is None:
            continue
        shown = {"PASS": v.selector, "NOT_FOUND": "NOT FOUND", "NEEDS_REVIEW": f"NEEDS REVIEW ({v.selector})",
                 "FAIL": f"FAIL ({v.selector})"}[v.status.value]
        out.append(f"{label:<13}: {shown}")
    for note in plan.notes[:6]:
        out.append(f"  - {note}")
    ok = plan.validation_status != "failed"
    out += ["", f"Status   : {'COMPLETED' if plan.validation_status == 'passed' else 'NEEDS REVIEW' if ok else 'FAILED'}"]
    if prep.workflow_path:
        out += ["", "Workflow file:", prep.workflow_path]
    return REDACTOR.redact("\n".join(out))


def run_site_analysis(url: str, cfg: OllamaConfig, ollama_ok: bool, args: argparse.Namespace,
                      store: CredentialStore) -> int:
    """Option 1: look up the website's saved workflow; reuse it, or analyze and offer to save one."""
    from services.analysis_service import UnsafeTargetError as _Unsafe
    from services.pipeline import AutomationPipeline, PipelineOptions

    try:
        url = url or input("Enter Website URL:\n> ").strip()
        if not url:
            return 1
        if not (args.manual_login or args.skip_login):
            prompt_credentials(store)
        hint = input("\nOptional: what do you click after sign-in to start a new article/project? "
                     "Use the text on the link or button, in order, e.g.  Create, Project\n"
                     "(press ENTER to let the agent find it)\n> ").strip()
        opts = PipelineOptions(allow_local=args.allow_local, db_path=args.db, skip_login=args.skip_login,
                               confirm_login=not args.no_confirm_login, workflow_dir=Path(args.workflows),
                               cdp_url=cdp_url(args))
        io = ServiceIO(ask=input, ask_secret=getpass.getpass, info=say, warn=lambda m: say(f"[WARN] {m}"))
        pipe = AutomationPipeline(cfg, io, opts, ollama_available=ollama_ok, credentials=store)
        try:
            saved = pipe.workflows.find(url if "://" in url else "https://" + url)
            say(f"[Agent 2] {'Saved workflow found for this website.' if saved else 'No saved workflow for this website yet.'}")
            if not ollama_ok and not saved:
                say("[WARN] Ollama Cloud is unavailable, so a NEW website cannot be analyzed right now.")
            prep = pipe.analyze_website(url, nav_hint=hint)
        finally:
            pipe.close()
    except _Unsafe as exc:
        print(f"\n[ERROR] {exc}")
        return 1
    except (BrowserStartError, NavigationError) as exc:
        print(f"\n[ERROR] {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n[INFO] Cancelled. Browser closed.")
        return 130
    print()
    print(render_workflow(prep))
    return 0 if prep.plan is not None and prep.plan.validation_status == "passed" else 4


def run_analysis(url: str, cfg: OllamaConfig, args: argparse.Namespace) -> int:
    """Single-website analysis (the original agent, unchanged)."""
    from agents.website_analysis_agent import InvalidModelOutput, WebsiteAnalysisAgent
    agent = WebsiteAnalysisAgent(cfg)
    service = AnalysisService(agent.analyze, ServiceIO(ask=input, info=logging.info, warn=logging.warning),
                              allow_local=args.allow_local)
    try:
        result = service.run(url or input("Enter Website URL:\n> "))
    except UnsafeTargetError as exc:
        print(f"\n[ERROR] {exc}")
        return 1
    except (BrowserStartError, NavigationError, EmptyPageError) as exc:
        print(f"\n[ERROR] {exc}")
        return 1
    except InvalidModelOutput as exc:
        print(f"\n[ERROR] {exc}\nStatus: NEEDS REVIEW - no mapping was accepted.")
        return 3
    except OllamaError as exc:
        print(f"\n[ERROR] {exc}")
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\n[INFO] Cancelled. Browser closed.")
        return 130
    print()
    print(render(result, Path("output/website_analysis.json")))
    return 0 if result.status != "NEEDS_REVIEW" else 4


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Multi-website content automation (4 CrewAI agents)")
    ap.add_argument("target", nargs="?", help="Excel/CSV file to upload, or a single URL to analyze (skips the menu)")
    ap.add_argument("--allow-local", action="store_true", help="allow file:// URLs (mock CMS)")
    ap.add_argument("--yes-drafts", action="store_true",
                    help="auto-approve saving drafts (publishing always asks for confirmation)")
    ap.add_argument("--manual-login", action="store_true", help="never ask for credentials; log in by hand")
    ap.add_argument("--no-confirm-login", action="store_true",
                    help="do not ask you to confirm the sign-in after the agent signs in automatically")
    ap.add_argument("--skip-login", action="store_true",
                    help="the browser is already signed in (e.g. with --attach-chrome): do not ask for or type credentials; "
                         "the session is still checked, and you are asked to sign in only if the site shows 'Sign In'")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite tracking database")
    ap.add_argument("--attach-chrome", nargs="?", const=9222, type=int, metavar="PORT",
                    help="use YOUR OWN Chrome (started with scripts/start_chrome_for_agent.sh) so you can sign in there, "
                         "e.g. with Google; default port 9222")
    ap.add_argument("--workflows", default=str(DEFAULT_WORKFLOWS), help="folder of saved website workflows (JSON)")
    ap.add_argument("--page-only", action="store_true",
                    help="option 1: only inspect the page you open (original behaviour), no workflow reuse")
    args = ap.parse_args(argv)

    cfg = OllamaConfig.from_env()
    setup_logging(cfg.api_key)
    store = CredentialStore()

    conn_error: OllamaError | None = None
    try:
        check_connectivity(cfg)
    except OllamaError as exc:
        conn_error = exc
    ollama_ok = conn_error is None

    def go_upload(path: str) -> int:
        return run_pipeline(path, cfg, ollama_ok, args, store)

    def go_analyze(url: str) -> int:
        if args.page_only:  # original single-page analysis
            if conn_error:
                print(f"\n[ERROR] {conn_error}")
                return 2
            return run_analysis(url, cfg, args)
        return run_site_analysis(url, cfg, ollama_ok, args, store)

    try:
        if args.target:  # direct mode, no menu
            print(MENU_BANNER.split("\n\n")[0] + "\n")
            return go_upload(args.target) if Path(args.target).suffix.lower() in SPREADSHEET_EXT else go_analyze(args.target)

        print(MENU_BANNER)
        print(f"AI model: {'GPT-OSS 20B Cloud (connected)' if ollama_ok else 'UNAVAILABLE - ' + str(conn_error)}")
        while True:
            choice = input("\nSelect Option: ").strip()
            if choice == "1":
                go_analyze("")
            elif choice == "2":
                path = input("\nEnter Excel/CSV path:\n> ").strip()
                if path:
                    go_upload(path)
            elif choice == "3":
                configure_credentials(store)
            elif choice == "4":
                show_history(args.db)
            elif choice == "5":
                return 0
            else:
                print("Please choose 1-5.")
            print("\n" + MENU_BANNER)
    except (KeyboardInterrupt, EOFError):
        print("\n[INFO] Cancelled.")
        return 130
    finally:
        store.clear()


if __name__ == "__main__":
    sys.exit(main())
