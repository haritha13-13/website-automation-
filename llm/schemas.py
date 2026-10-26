"""Pydantic schemas. AI proposals and browser-confirmed validation are kept separate."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class FieldProposal(BaseModel):
    """A model-proposed mapping. `confidence` is the model's own estimate, not a measurement."""

    selector: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str | None = None

    @model_validator(mode="after")
    def _null_needs_reason(self) -> "FieldProposal":
        if self.selector is not None and not self.selector.strip():
            self.selector = None
        if self.selector is None and not (self.reason and self.reason.strip()):
            raise ValueError("a null selector must include a reason")
        return self


class ActionProposal(BaseModel):
    selector: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _null_needs_reason(self) -> "ActionProposal":
        if self.selector is not None and not self.selector.strip():
            self.selector = None
        if self.selector is None and not (self.reason and self.reason.strip()):
            raise ValueError("a null selector must include a reason")
        return self


class Fields(BaseModel):
    title: FieldProposal
    content: FieldProposal
    category: FieldProposal
    tags: FieldProposal
    image: FieldProposal


class Actions(BaseModel):
    save_draft: ActionProposal
    publish: ActionProposal


class AnalysisProposal(BaseModel):
    """Structured output the LLM must return."""

    website_type: str = Field(description="e.g. wordpress, custom_cms, unknown")
    page_type: Literal["article_editor", "login", "dashboard", "listing", "other", "unknown"]
    fields: Fields
    actions: Actions
    missing_fields: list[str] = Field(default_factory=list)
    requires_human_review: bool = False
    next_step_suggestion: str | None = Field(
        default=None, description="What to inspect next if this page is not an editor"
    )

    @field_validator("website_type")
    @classmethod
    def _norm(cls, v: str) -> str:
        return v.strip() or "unknown"


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"  # invalid selector or wrong kind of element
    NOT_FOUND = "NOT_FOUND"  # selector valid but matches nothing / none proposed
    NEEDS_REVIEW = "NEEDS_REVIEW"  # ambiguous or hidden/disabled


class ValidatedItem(BaseModel):
    """Browser-confirmed facts about one proposed locator."""

    name: str
    selector: str | None
    status: Status
    match_count: int = 0
    visible: bool | None = None
    interactable: bool | None = None
    notes: list[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    url: str
    page_title: str
    analyzed_at: str
    status: Literal["COMPLETED", "COMPLETED_WITH_WARNINGS", "NEEDS_REVIEW"]
    ai_proposal: AnalysisProposal  # unverified model output
    browser_validation: list[ValidatedItem]  # confirmed observations
    observed_element_count: int
    screenshot: str | None = None
    note: str = "ai_proposal is model-generated; confidence values are model estimates."


class NavigationChoice(BaseModel):
    """Which OBSERVED link leads toward the article-creation page (null if none does)."""

    link_text: str | None = None
    reason: str = ""


class NavChoice(BaseModel):
    text: str
    kind: Literal["link", "button"] = "link"
    reason: str = ""


class NavigationRanking(BaseModel):
    """Up to three observed clickables, best first, that lead toward the article-creation page."""

    choices: list[NavChoice] = Field(default_factory=list)
