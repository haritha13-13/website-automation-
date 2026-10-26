"""Data models shared by the four pipeline agents."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from llm.schemas import ValidatedItem


class ContentItem(BaseModel):
    """One validated Excel/CSV row, ready for the Browser Execution Agent."""

    row_number: int
    website_url: str
    title: str
    content: str
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    publish_mode: Literal["draft", "publish"] = "draft"
    blog_url: str | None = Field(default=None, description="the backlink target")
    anchor_text: str | None = Field(default=None, description="words in the content to turn into the backlink")
    cover_image: str | None = None
    cover_image_path: str | None = None
    content_hash: str
    validation_status: Literal["passed"] = "passed"
    image_paths: list[str] = Field(default_factory=list, description="absolute, existing files")
    metadata: dict[str, str] = Field(default_factory=dict, description="extra columns")


class RowError(BaseModel):
    row_number: int
    errors: list[str]


class PreparationResult(BaseModel):
    source_file: str
    total_rows: int
    items: list[ContentItem]
    errors: list[RowError]


class AdapterSuccess(BaseModel):
    """Optional per-site hints used by verification."""

    draft_text: str | None = None  # regex matched against page text after saving a draft
    published_text: str | None = None
    error_selector: str | None = None
    article_link_selector: str | None = None


class ExecutionPlan(BaseModel):
    """Validated mapping from content fields to page controls for one website."""

    website_type: str
    source: Literal["adapter", "ollama", "playbook"]
    adapter_name: str | None = None
    playbook_name: str | None = None
    playbook_steps: list[dict] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list, description="row fields a playbook needs")
    page_type: str = "unknown"
    editor_url: str | None = Field(default=None, description="page where the plan was validated")
    navigation_path: list[str] = Field(default_factory=list, description="pages visited to reach the editor")
    steps: list[dict] = Field(default_factory=list, description="ordered clicks/navigations from the start page to the editor")
    field_mapping: dict[str, str] = Field(default_factory=dict)  # title/content/category/tags/images
    actions: dict[str, str] = Field(default_factory=dict)  # save_draft/publish
    validation_status: Literal["passed", "needs_review", "failed"]
    requires_human_review: bool = False
    validation: list[ValidatedItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    unmapped: list[str] = Field(default_factory=list, description="requested but not confirmed")
    tags_mode: Literal["comma", "enter"] = "comma"
    success: AdapterSuccess = Field(default_factory=AdapterSuccess)

    def supports(self, item: ContentItem) -> list[str]:
        """Names of controls this item needs that the plan lacks (empty = fully supported)."""
        if self.source == "playbook":
            return [f for f in self.requires if not getattr(item, f, None)]
        need = ["title", "content"]
        if item.category:
            need.append("category")
        if item.tags:
            need.append("tags")
        if item.images:
            need.append("images")
        missing = [n for n in need if n not in self.field_mapping]
        action = "publish" if item.publish_mode == "publish" else "save_draft"
        if action not in self.actions:
            missing.append(action)
        return missing


class PageSnapshot(BaseModel):
    """Evidence read from the page right after the save/publish click."""

    url: str
    title: str = ""
    body_text: str = ""
    banners: list[str] = Field(default_factory=list)
    title_value: str | None = None
    content_text: str | None = None
    category_selected: str | None = None
    tags_value: str | None = None
    image_count: int | None = None
    article_link: str | None = None
    login_form_visible: bool = False


class Check(BaseModel):
    name: str
    status: Literal["PASS", "FAIL", "SKIPPED"]
    detail: str = ""


class VerificationResult(BaseModel):
    status: Literal["verified", "failed", "unconfirmed"]
    checks: list[Check] = Field(default_factory=list)
    article_url: str | None = None
    rejected_by_site: bool = False
