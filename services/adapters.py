"""Website adapters: known, tested control mappings that are preferred over asking the LLM."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError

from llm.schemas import AnalysisProposal
from services.models import AdapterSuccess, ExecutionPlan

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
FIELD_KEYS = ("title", "content", "category", "tags", "images")
ACTION_KEYS = ("save_draft", "publish")


class SiteAdapter(BaseModel):
    name: str
    website_type: str = "custom_cms"
    hosts: list[str] = Field(default_factory=list)
    url_contains: list[str] = Field(default_factory=list)
    fields: dict[str, str] = Field(default_factory=dict)
    actions: dict[str, str] = Field(default_factory=dict)
    tags_mode: str = "comma"
    success: AdapterSuccess = Field(default_factory=AdapterSuccess)

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if host and host in {h.lower() for h in self.hosts}:
            return True
        return any(part and part in url for part in self.url_contains)

    def to_proposal(self) -> AnalysisProposal:
        """Express the adapter as an (unverified) proposal so it goes through the same validator."""
        def f(key: str, name: str | None = None):
            sel = self.fields.get(key)
            return ({"selector": sel, "confidence": 1.0, "reason": "defined by adapter"} if sel else
                    {"selector": None, "confidence": 0.0, "reason": f"{name or key} not defined in adapter"})

        def a(key: str):
            sel = self.actions.get(key)
            return {"selector": sel} if sel else {"selector": None, "reason": f"{key} not defined in adapter"}

        return AnalysisProposal.model_validate({
            "website_type": self.website_type, "page_type": "article_editor",
            "fields": {"title": f("title"), "content": f("content"), "category": f("category"),
                       "tags": f("tags"), "image": f("images")},
            "actions": {"save_draft": a("save_draft"), "publish": a("publish")},
        })


class AdapterRegistry:
    def __init__(self, directories: list[Path] | None = None) -> None:
        self.directories = directories if directories is not None else [ROOT / "adapters", ROOT / "adapters" / "learned"]
        self.adapters: list[SiteAdapter] = []
        self.reload()

    def reload(self) -> None:
        self.adapters = []
        for d in self.directories:
            for path in sorted(Path(d).glob("*.json")):
                try:
                    self.adapters.append(SiteAdapter.model_validate_json(path.read_text(encoding="utf-8")))
                except (ValidationError, ValueError, OSError) as exc:
                    logger.warning("Ignoring invalid adapter %s: %s", path.name, str(exc)[:120])

    def find(self, url: str) -> SiteAdapter | None:
        # Later directories (learned) can't override curated adapters: first match wins.
        return next((a for a in self.adapters if a.matches(url)), None)

    def save_learned(self, plan: ExecutionPlan, url: str) -> Path:
        """Persist a user-approved, validated AI mapping as an adapter for next time."""
        host = (urlparse(url).hostname or "site").lower()
        slug = re.sub(r"[^a-z0-9]+", "_", host).strip("_") or "site"
        adapter = SiteAdapter(
            name=f"learned_{slug}", website_type=plan.website_type, hosts=[host] if urlparse(url).hostname else [],
            url_contains=[] if urlparse(url).hostname else [url.split("#")[0]],
            fields=dict(plan.field_mapping), actions=dict(plan.actions),
            tags_mode=plan.tags_mode, success=plan.success,
        )
        target = Path(self.directories[-1])
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{slug}.json"
        path.write_text(json.dumps(adapter.model_dump(), indent=2), encoding="utf-8")
        self.reload()
        return path
