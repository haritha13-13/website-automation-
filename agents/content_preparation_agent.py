"""Agent 1 - Content Preparation Agent (deterministic; Python does the processing)."""
from __future__ import annotations

from typing import Callable

from crewai import Agent

from agents.base import build_stage_agent
from services.content_service import prepare_content
from services.models import PreparationResult


class ContentPreparationAgent:
    ROLE = "Content Preparation and Data Validation Specialist"
    GOAL = "Read and prepare content from Excel/CSV files for upload to different websites."
    BACKSTORY = ("You read spreadsheets with deterministic tools, validate every row, normalize "
                 "categories and tags, check images and URLs, and hand clean content objects downstream.")

    def __init__(self, allow_local: bool = False) -> None:
        self.allow_local = allow_local

    def prepare(self, path: str) -> PreparationResult:
        return prepare_content(path, allow_local=self.allow_local)

    def crew_agent(self, handler: Callable[[], str]) -> Agent:
        return build_stage_agent(self.ROLE, self.GOAL, self.BACKSTORY, handler)
