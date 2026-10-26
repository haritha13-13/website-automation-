"""Agent 4 - Verification Agent. Deterministic checks only."""
from __future__ import annotations

from typing import Callable

from crewai import Agent

from agents.base import build_stage_agent
from services.models import ContentItem, ExecutionPlan, PageSnapshot, VerificationResult
from services.verification_service import ArticleFetcher, verify, verify_playbook


class VerificationAgent:
    ROLE = "Publishing Verification Specialist"
    GOAL = "Confirm with deterministic checks that content was saved or published accurately and find its URL."
    BACKSTORY = ("You never trust a click. You compare what the site shows with what was supposed to be "
                 "entered, look for confirmation or error messages, and confirm the article URL loads.")

    def check(self, item: ContentItem, plan: ExecutionPlan, snapshot: PageSnapshot, editor_url: str,
              fetch_article: ArticleFetcher | None, login: str | None = None) -> VerificationResult:
        return verify(item, plan, snapshot, editor_url, fetch_article, login)

    def check_playbook(self, item, steps_done, steps_total, user_published, captured_url, editor_url, fetch_page,
                       login=None) -> VerificationResult:
        return verify_playbook(item, steps_done, steps_total, user_published, captured_url, editor_url, fetch_page, login)

    def crew_agent(self, handler: Callable[[], str]) -> Agent:
        return build_stage_agent(self.ROLE, self.GOAL, self.BACKSTORY, handler)
