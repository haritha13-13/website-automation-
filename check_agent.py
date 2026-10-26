"""Run ONE agent at a time to check it works.

  python check_agent.py prepare  FILE                 # Agent 1: read + validate the spreadsheet
  python check_agent.py analyze  URL                  # Agent 2: inspect the page, build a validated mapping
  python check_agent.py execute  FILE [--submit]      # Agent 3: fill the first valid row (clicks only with --submit)
  python check_agent.py verify   FILE                 # Agent 4: save a draft of the first row, then verify it
Add --allow-local for file:// pages (the local mock CMS) and --headed to watch the browser.
"""
from __future__ import annotations

import argparse
import json
import sys

from agents.browser_execution_agent import BrowserExecutionAgent
from agents.content_preparation_agent import ContentPreparationAgent
from agents.verification_agent import VerificationAgent
from agents.website_analysis_agent import WebsiteAnalysisAgent
from browser.playwright_manager import PlaywrightManager
from llm.ollama_client import OllamaConfig
from services.adapters import AdapterRegistry
from services.content_service import PreparationError, site_key
from services.database import Database
from services.models import ContentItem, ExecutionPlan


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str))


def first_item(path: str, allow_local: bool) -> ContentItem:
    res = ContentPreparationAgent(allow_local).prepare(path)
    if not res.items:
        sys.exit(f"No valid rows in {path}: {[e.model_dump() for e in res.errors]}")
    return res.items[0]


def build_plan(page_call, item: ContentItem) -> ExecutionPlan:
    cfg = OllamaConfig.from_env()
    adapter = AdapterRegistry().find(item.website_url)
    targets = {"title", "content"} | {n for n, v in (("category", item.category), ("tags", item.tags),
                                                       ("images", item.images)) if v}
    action = "publish" if item.publish_mode == "publish" else "save_draft"
    return WebsiteAnalysisAgent(cfg).plan_for_site(page_call, item.website_url, targets, {action}, adapter)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("agent", choices=["prepare", "analyze", "execute", "verify"])
    ap.add_argument("target", help="spreadsheet (prepare/execute/verify) or URL (analyze)")
    ap.add_argument("--submit", action="store_true", help="execute: really click save/publish after you confirm")
    ap.add_argument("--allow-local", action="store_true")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    args = ap.parse_args()

    if args.agent == "prepare":
        try:
            res = ContentPreparationAgent(args.allow_local).prepare(args.target)
        except PreparationError as exc:
            print(f"[FAIL] {exc}")
            return 1
        show("valid rows", [i.model_dump(exclude={"content"}) for i in res.items])
        show("invalid rows", [e.model_dump() for e in res.errors] or "none")
        print(f"\n[OK] Content Preparation Agent: {len(res.items)} valid / {len(res.errors)} invalid")
        return 0

    mgr = PlaywrightManager(headless=not args.headed)
    try:
        page = mgr.start()
        page_call = lambda fn: fn(page)  # noqa: E731 - single-thread session

        if args.agent == "analyze":
            from services.analysis_service import validate_url
            url = validate_url(args.target, args.allow_local)
            mgr.navigate(url)
            if args.headed:
                input("Log in / open the editor in the browser, then press ENTER...")
            item = ContentItem(row_number=0, website_url=url, title="t", content="c", category="c", tags=["t"],
                               images=["i"], publish_mode="draft", content_hash="0" * 64)
            plan = build_plan(page_call, item)
            show("execution plan", plan.model_dump(exclude={"validation"}))
            show("selector validation", [{"field": v.name, "selector": v.selector, "status": v.status.value,
                                          "notes": v.notes} for v in plan.validation])
            print(f"\n[{'OK' if plan.validation_status == 'passed' else 'REVIEW'}] Website Analysis Agent: "
                  f"{plan.validation_status} (source: {plan.source})")
            return 0 if plan.validation_status == "passed" else 4

        item = first_item(args.target, args.allow_local)
        mgr.navigate(item.website_url)
        if args.headed:
            input("Log in / open the editor in the browser, then press ENTER...")
        plan = build_plan(page_call, item)
        show("plan", plan.model_dump(exclude={"validation"}))
        if plan.validation_status == "failed" or plan.supports(item):
            print(f"[FAIL] plan unusable for this row; missing: {plan.supports(item)}")
            return 1

        db = Database(":memory:")
        run = db.start_run("check_agent")
        db_id = db.add_item(run, item.row_number, site_key(item.website_url), item.website_url, item.title,
                            item.content_hash, item.publish_mode, "validated")
        auto = args.agent == "verify"
        if args.agent == "verify":
            item = item.model_copy(update={"publish_mode": "draft"})

        def approve(i, p, steps):
            show("filled (not submitted yet)", steps)
            if auto:
                return True
            if not args.submit:
                print("(no --submit: not clicking save/publish)")
                return False
            return input(f"Click {i.publish_mode} for '{i.title}'? [y/N] ").strip().lower() in {"y", "yes"}

        agent = BrowserExecutionAgent(db, run, approve, lambda i, n: False)
        out = agent.execute_item(item, db_id, plan, page_call, lambda: mgr.navigate(item.website_url))
        show("execution outcome", {"status": out.status, "action": out.action, "error": out.error, "steps": out.steps})
        if args.headed and out.status in {"rejected_by_user", "submitted"}:
            input("Look at the browser, then press ENTER to continue...")

        if args.agent == "execute":
            ok = out.status in {"submitted", "rejected_by_user"}
            print(f"\n[{'OK' if ok else 'FAIL'}] Browser Execution Agent: {out.status}")
            return 0 if ok else 1

        if out.snapshot is None:
            print(f"[FAIL] nothing to verify ({out.status}: {out.error})")
            return 1
        res = VerificationAgent().check(item, plan, out.snapshot, out.editor_url or item.website_url, None)
        show("verification checks", [c.model_dump() for c in res.checks])
        print(f"\n[{'OK' if res.status == 'verified' else 'FAIL'}] Verification Agent: {res.status}")
        return 0 if res.status == "verified" else 1
    finally:
        mgr.close()


if __name__ == "__main__":
    sys.exit(main())
