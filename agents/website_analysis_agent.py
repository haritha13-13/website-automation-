"""CrewAI Website Analysis Agent: one agent, one task, structured output."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

from typing import Callable, TypeVar

from crewai import Agent, Crew, Process, Task
from playwright.sync_api import Page
from pydantic import ValidationError

from browser.locator_validator import LocatorValidator
from browser.page_inspector import PageInspector
from llm.ollama_client import OllamaCloudLLM, OllamaConfig, OllamaError
from browser.playwright_manager import NavigationError, goto_settled
from llm.prompts import NAV_RULES, SYSTEM_RULES, build_nav_prompt, build_user_prompt
from llm.schemas import AnalysisProposal, NavigationChoice, NavigationRanking, Status, ValidatedItem
from services.auth import same_site
from services.discovery import click_candidate, find_candidate, for_llm, nav_candidates, rank_candidates, step_for
from services.secrets import REDACTOR
from services.adapters import SiteAdapter
from services.models import AdapterSuccess, ExecutionPlan

T = TypeVar("T")
# `page_call(fn)` runs fn(page) wherever the browser lives (possibly another thread) and returns its result.
PageCall = Callable[[Callable[[Page], T]], T]

logger = logging.getLogger(__name__)


class InvalidModelOutput(RuntimeError):
    pass


FORBIDDEN_LINK = re.compile(r"log ?out|sign ?out|delete|remove|unsubscribe|deactivate|password|account", re.I)


def _assert_no_secrets(payload: dict) -> None:
    """Hard stop: credentials must never reach the model."""
    if REDACTOR.contains(json.dumps(payload, ensure_ascii=False)):
        raise RuntimeError("Refusing to send page data to the model: it contains a credential value.")


def _clean_url(url: str) -> str:
    """Drop the query string and fragment: they can hold emails, tokens and long redirect state."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}" if p.scheme in {"http", "https"} else url.split("#")[0].split("?")[0]


def resolve_nav_choice(choice: NavigationChoice, observation: dict, current_url: str) -> str | None:
    """Turn the model's pick into a URL only if it is an observed, same-site, non-destructive link."""
    if not choice.link_text:
        return None
    for link in observation.get("links", []):
        if link["text"].strip().lower() != choice.link_text.strip().lower():
            continue
        href = link["href"]
        if FORBIDDEN_LINK.search(link["text"]) or FORBIDDEN_LINK.search(href):
            return None
        target = urlparse(href)
        cur = urlparse(current_url)
        if target.scheme in {"http", "https", "file"} and (cur.scheme == "file" or same_site(target.hostname or "", cur.hostname or "")):
            return href
    return None


def build_agent(cfg: OllamaConfig, llm=None) -> Agent:
    return Agent(
        role="AI Website Structure and Workflow Analyst",
        goal=(
            "Understand website form structures using Playwright observations and identify the "
            "controls required for content creation and publishing."
        ),
        backstory=(
            "You are the Website Analysis Agent. You study sanitized DOM observations of a CMS page, "
            "decide whether it is a content editor, and map title, body, category/tags, image upload, "
            "Save Draft and Publish controls to observed elements. You never invent elements."
        ),
        llm=llm or OllamaCloudLLM(cfg),
        allow_delegation=False,
        verbose=False,
    )


class WebsiteAnalysisAgent:
    def __init__(self, cfg: OllamaConfig, analyzer: Callable[[dict], AnalysisProposal] | None = None,
                 nav_chooser: Callable[[dict], NavigationChoice | NavigationRanking] | None = None) -> None:
        self.cfg = cfg
        self._nav_chooser = nav_chooser
        self.agent = build_agent(cfg)
        self._analyzer = analyzer  # test/offline override for the LLM step

    # ---- multi-website planning (adapter first, Ollama for unfamiliar layouts) -------------------
    def plan_for_site(self, page_call: PageCall, url: str, targets: set[str], actions_needed: set[str],
                      adapter: SiteAdapter | None = None, adapter_only: bool = False) -> ExecutionPlan:
        """Build a *validated* execution plan for the page already open in the shared browser session.

        targets: content fields rows need among {"title","content","category","tags","images"}.
        actions_needed: subset of {"save_draft","publish"}.
        Known adapters are tried first; the LLM is only used when none matches or it no longer validates.
        LLM output is an untrusted proposal: only selectors the browser confirms (PASS) enter the plan.
        """
        notes: list[str] = []
        proposal: AnalysisProposal | None = None
        items: list[ValidatedItem] = []
        source = "adapter"
        if adapter is not None:
            proposal = adapter.to_proposal()
            items = page_call(lambda p: LocatorValidator(p).validate(proposal))
            if not self._satisfied(items, targets, actions_needed):
                if adapter_only:  # cheap check of a saved workflow: never ask the model here
                    return self._failed_plan(adapter, notes + [f"'{adapter.name}' does not match this page."], "adapter")
                notes.append(f"Adapter '{adapter.name}' did not fully validate on this page; asking the model.")
                proposal = None
        if proposal is None:
            source = "ollama"
            try:
                obs = page_call(lambda p: PageInspector(p).inspect())
                if obs.is_empty:
                    # A dashboard/listing: links but no editor controls. Report it so the caller can navigate onward.
                    plan = self._failed_plan(adapter, notes + [f"No form controls on this page ({len(obs.links)} links)."], source)
                    plan.page_type = "dashboard" if obs.links else "unknown"
                    return plan
                analyze = self._analyzer or self.analyze
                proposal = analyze(obs.for_llm())
                items = page_call(lambda p: LocatorValidator(p).validate(proposal))
            except (OllamaError, InvalidModelOutput) as exc:
                return self._failed_plan(adapter, notes + [f"Model analysis unavailable: {exc}"], source)
            if proposal.page_type != "article_editor":
                notes.append(f"Page looks like '{proposal.page_type}', not an article editor."
                             + (f" Suggestion: {proposal.next_step_suggestion}" if proposal.next_step_suggestion else ""))
        return self._build_plan(proposal, items, targets, actions_needed, adapter if source == "adapter" else None,
                                source, notes)

    @staticmethod
    def _satisfied(items: list[ValidatedItem], targets: set[str], actions_needed: set[str]) -> bool:
        ok = {i.name for i in items if i.status == Status.PASS}
        want = {("image" if t == "images" else t) for t in targets} | set(actions_needed)
        return want <= ok

    @staticmethod
    def _failed_plan(adapter: SiteAdapter | None, notes: list[str], source: str) -> ExecutionPlan:
        return ExecutionPlan(website_type=adapter.website_type if adapter else "unknown", source=source,
                             validation_status="failed", requires_human_review=True, notes=notes)

    def _build_plan(self, proposal: AnalysisProposal, items: list[ValidatedItem], targets: set[str],
                    actions_needed: set[str], adapter: SiteAdapter | None, source: str,
                    notes: list[str]) -> ExecutionPlan:
        by = {i.name: i for i in items}
        mapping, actions, unmapped = {}, {}, []
        for t in sorted(targets):
            item = by["image" if t == "images" else t]
            if item.status == Status.PASS and item.selector:
                mapping[t] = item.selector
            else:
                unmapped.append(t)
                notes.append(f"{t}: {item.status.value} - {'; '.join(item.notes) or 'no selector'}")
        for a in sorted(actions_needed):
            item = by[a]
            if item.status == Status.PASS and item.selector:
                actions[a] = item.selector
            else:
                unmapped.append(a)
                notes.append(f"{a}: {item.status.value} - {'; '.join(item.notes) or 'no selector'}")
        # Never present a non-editor page as ready.
        editor = proposal.page_type == "article_editor" or source == "adapter"
        core_ok = {"title", "content"} <= set(mapping) and not (actions_needed - set(actions))
        if not unmapped and editor and not proposal.requires_human_review:
            status = "passed"
        elif core_ok and editor:
            status = "needs_review"
        else:
            status = "failed"
        return ExecutionPlan(
            website_type=proposal.website_type, source=source, adapter_name=adapter.name if adapter else None,
            page_type=proposal.page_type,
            field_mapping=mapping, actions=actions, validation_status=status,
            requires_human_review=status != "passed" or proposal.requires_human_review,
            validation=items, notes=notes, unmapped=unmapped,
            tags_mode=adapter.tags_mode if adapter else "comma",
            success=adapter.success if adapter else AdapterSuccess(),
        )

    def analyze(self, observation: dict, retries: int = 1) -> AnalysisProposal:
        _assert_no_secrets(observation)
        # Playwright's sync API keeps an event loop running on the calling thread, and CrewAI
        # refuses to run synchronously inside one, so run the crew on a worker thread.
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(self._analyze, observation, retries).result()

    def _analyze(self, observation: dict, retries: int) -> AnalysisProposal:
        last: Exception | None = None
        for attempt in range(retries + 1):
            task = Task(
                description=SYSTEM_RULES + "\n\n" + build_user_prompt(observation),
                expected_output="JSON matching the AnalysisProposal schema",
                agent=self.agent,
                output_pydantic=AnalysisProposal,
            )
            crew = Crew(agents=[self.agent], tasks=[task], process=Process.sequential, verbose=False)
            try:
                out = crew.kickoff()
                if out.pydantic is not None:
                    return AnalysisProposal.model_validate(out.pydantic.model_dump())
                return AnalysisProposal.model_validate_json(out.raw)
            except (ValidationError, ValueError) as exc:
                last = exc
                logger.warning("Model output invalid (attempt %d): %s", attempt + 1, str(exc)[:200])
        raise InvalidModelOutput(f"The model returned invalid JSON for the analysis schema: {str(last)[:300]}")


    # ---- finding the article-creation page after login ------------------------------------------------
    def rank_navigation(self, payload: dict) -> list[tuple[str, str]]:
        """Ask the model which observed clickables lead toward content creation: [(text, kind), ...] best first."""
        _assert_no_secrets(payload)
        if self._nav_chooser:
            raw = self._nav_chooser(payload)
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                raw = pool.submit(self._choose_nav, payload).result()
        if isinstance(raw, NavigationChoice):
            return [(raw.link_text, "link")] if raw.link_text else []
        return [(c.text, c.kind) for c in raw.choices]

    def _choose_nav(self, payload: dict) -> NavigationRanking:
        task = Task(description=NAV_RULES + "\n\n" + build_nav_prompt(payload),
                    expected_output="JSON matching the NavigationRanking schema", agent=self.agent,
                    output_pydantic=NavigationRanking)
        out = Crew(agents=[self.agent], tasks=[task], process=Process.sequential, verbose=False).kickoff()
        if out.pydantic is not None:
            return NavigationRanking.model_validate(out.pydantic.model_dump())
        return NavigationRanking.model_validate_json(out.raw)

    def locate_editor_plan(self, page_call: PageCall, url: str, targets: set[str], actions_needed: set[str],
                           adapter: SiteAdapter | None = None, max_hops: int = 3,
                           on_hop: Callable[[str], None] | None = None, hints: list[str] | None = None,
                           max_candidates: int = 3, max_clicks: int = 8) -> ExecutionPlan:
        """plan_for_site, plus workflow discovery across pages.

        If the (authenticated) page is a dashboard/listing/menu, try the most promising observed links and buttons
        (the person's `hints` first, then the model's picks, then "create/new/project"-style keywords), going back
        and trying the next candidate when one leads nowhere: up to `max_hops` deep and `max_clicks` in total.
        Only observed, same-site, non-destructive clickables are used. The clicks that worked are recorded in
        `plan.steps` so the workflow can be replayed without the model.
        """
        hints = [h.strip() for h in (hints or []) if h and h.strip()]
        steps: list[dict] = []
        path: list[str] = [page_call(lambda p: p.url)]
        start_host = urlparse(path[0]).hostname or ""
        plan = self.plan_for_site(page_call, url, targets, actions_needed, adapter)
        visited: set[tuple[str, str]] = set()
        clicks = 0
        found: list[ExecutionPlan] = []
        trail: list[str] = []  # what discovery did along the way; kept on the final plan

        def explore(depth: int) -> bool:
            nonlocal clicks
            if depth >= max_hops or clicks >= max_clicks:
                return False
            try:
                obs = page_call(lambda p: PageInspector(p).inspect())
            except Exception as exc:  # noqa: BLE001 - page mid-navigation
                trail.append(f"Could not inspect the page: {exc}")
                return False
            cands = nav_candidates(obs)
            if not cands:
                return False
            picks: list[tuple[str, str]] = []
            try:
                picks = self.rank_navigation({"title": obs.title, "url": _clean_url(obs.url), "nav_candidates": for_llm(cands)})
            except (OllamaError, InvalidModelOutput, ValidationError, ValueError) as exc:
                trail.append(f"Model navigation hint unavailable ({str(exc)[:80]}); using keyword ranking.")
            hint = [hints[depth]] if depth < len(hints) else []
            if hint and find_candidate(cands, hint[0]) is None:
                trail.append(f"Nothing on the page matches your hint '{hint[0]}'; searching automatically.")
                hint = []
            ordered = rank_candidates(cands, picks, hint, visited, limit=1 if hint else max_candidates)
            for cand in ordered:
                if clicks >= max_clicks:
                    return False
                visited.add((cand["kind"], cand["text"].lower()))
                before = page_call(lambda p: p.url)
                if on_hop:
                    on_hop(f"{cand['text']} ({cand['kind']})")
                try:
                    landed = page_call(lambda p: click_candidate(p, cand))
                except NavigationError as exc:
                    trail.append(str(exc))
                    continue
                clicks += 1
                steps.append(step_for(cand))
                moved = _clean_url(landed) != _clean_url(before)
                if moved:
                    path.append(_clean_url(landed))
                if urlparse(landed).scheme != "file" and not same_site(urlparse(landed).hostname or "", start_host):
                    trail.append(f"'{cand['text']}' went to another domain ({urlparse(landed).hostname}); went back.")
                    steps.pop()
                    path.pop() if moved else None
                    page_call(lambda p: goto_settled(p, before))
                    continue
                attempt = self.plan_for_site(page_call, landed, targets, actions_needed, None)
                if attempt.validation_status != "failed":
                    found.append(attempt)
                    return True
                if attempt.page_type != "login" and explore(depth + 1):
                    return True
                steps.pop()  # dead end: back out and try the next candidate
                if moved and path:
                    path.pop()
                page_call(lambda p: goto_settled(p, before))
            return False

        if plan.validation_status == "failed" and plan.page_type not in {"article_editor", "unknown", "login"} \
                and max_hops > 0:
            if explore(0):
                plan = found[-1]
            else:
                trail.append("Could not find the article-creation page by following links and buttons.")
            plan.notes = trail + plan.notes  # keep what discovery did, even when it succeeded
        final = page_call(lambda p: p.url)
        plan.editor_url = _clean_url(final)
        plan.steps = steps
        plan.navigation_path = path + ([plan.editor_url] if plan.editor_url not in path else [])
        return plan
