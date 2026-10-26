"""Saved per-website workflows: how to get from the site's start page to the article editor,
plus the validated selectors for every field and button. One readable JSON file per website.
Contains no credentials and no article content."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError

from services.adapters import SiteAdapter
from services.models import AdapterSuccess, ExecutionPlan

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def workflow_key(url: str) -> str:
    """One workflow per website: the host, or a slug of the path for local files."""
    p = urlparse(url)
    raw = (p.hostname or "") if p.scheme != "file" else "file_" + p.path
    return re.sub(r"[^a-z0-9._-]+", "_", raw.lower()).strip("_") or "site"


class Workflow(BaseModel):
    version: int = 1
    site: str
    start_url: str
    editor_url: str | None = None
    navigation_path: list[str] = Field(default_factory=list)
    steps: list[dict] = Field(default_factory=list, description="ordered clicks/navigations to reach the editor")
    page_type: str = "article_editor"
    website_type: str = "custom_cms"
    field_mapping: dict[str, str] = Field(default_factory=dict)
    actions: dict[str, str] = Field(default_factory=dict)
    tags_mode: str = "comma"
    success: AdapterSuccess = Field(default_factory=AdapterSuccess)
    source: str = "ollama"
    saved_at: str = Field(default_factory=_now)
    last_validated_at: str = Field(default_factory=_now)

    @classmethod
    def from_plan(cls, plan: ExecutionPlan, start_url: str) -> "Workflow":
        return cls(site=workflow_key(start_url), start_url=start_url.split("#")[0],
                   editor_url=plan.editor_url, navigation_path=plan.navigation_path, steps=list(plan.steps), page_type=plan.page_type,
                   website_type=plan.website_type, field_mapping=dict(plan.field_mapping),
                   actions=dict(plan.actions), tags_mode=plan.tags_mode, success=plan.success, source=plan.source)

    def to_adapter(self) -> SiteAdapter:
        """Replay form: validated against the live page exactly like any adapter."""
        host = urlparse(self.start_url).hostname
        return SiteAdapter(name=f"workflow:{self.site}", website_type=self.website_type,
                           hosts=[host] if host else [], url_contains=[] if host else [self.start_url],
                           fields=self.field_mapping, actions=self.actions, tags_mode=self.tags_mode,
                           success=self.success)


class WorkflowStore:
    def __init__(self, directory: Path | str = ROOT / "workflows") -> None:
        self.directory = Path(directory)

    def path_for(self, url: str) -> Path:
        return self.directory / f"{workflow_key(url)}.json"

    def find(self, url: str) -> Workflow | None:
        path = self.path_for(url)
        if not path.is_file():
            return None
        try:
            return Workflow.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, OSError) as exc:
            logger.warning("Ignoring unreadable workflow %s: %s", path.name, str(exc)[:120])
            return None

    def save(self, workflow: Workflow) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(workflow.start_url)
        previous = self.find(workflow.start_url)
        if previous:
            workflow.saved_at = previous.saved_at
        path.write_text(json.dumps(workflow.model_dump(), indent=2), encoding="utf-8")
        return path

    def delete(self, url: str) -> bool:
        path = self.path_for(url)
        if path.is_file():
            path.unlink()
            return True
        return False

    def all(self) -> list[Workflow]:
        found = []
        for path in sorted(self.directory.glob("*.json")) if self.directory.is_dir() else []:
            try:
                found.append(Workflow.model_validate_json(path.read_text(encoding="utf-8")))
            except (ValidationError, ValueError, OSError):
                continue
        return found
