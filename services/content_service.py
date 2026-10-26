"""Deterministic Excel/CSV reading and validation. No LLM involved."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from services.analysis_service import UnsafeTargetError, validate_url
from services.models import ContentItem, PreparationResult, RowError

REQUIRED_COLUMNS = ("website_url", "title", "content")
KNOWN_COLUMNS = {*REQUIRED_COLUMNS, "description", "category", "tags", "images", "publish_mode", "blog_url",
                 "anchor_text", "cover_image"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TITLE = 300
MAX_TAGS = 30
MAX_TAG_LEN = 50
LIST_SPLIT = re.compile(r"[,;|\n]+")


class PreparationError(RuntimeError):
    """The whole file is unusable (not a row-level problem)."""


def read_table(path: Path | str) -> pd.DataFrame:
    p = Path(path).expanduser()
    if not p.is_file():
        raise PreparationError(f"File not found: {p}")
    ext = p.suffix.lower()
    try:
        if ext == ".csv":
            df = pd.read_csv(p, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        elif ext in {".xlsx", ".xlsm"}:
            df = pd.read_excel(p, dtype=str, keep_default_na=False, engine="openpyxl")
        else:
            raise PreparationError(f"Unsupported file type '{ext}'. Use .xlsx or .csv.")
    except PreparationError:
        raise
    except Exception as exc:  # noqa: BLE001 - corrupt/locked files
        raise PreparationError(f"Could not read {p.name}: {type(exc).__name__}: {str(exc)[:150]}") from exc
    df.columns = [re.sub(r"\s+", "_", str(c).strip().lower()) for c in df.columns]
    return df


def _clean(value: object) -> str:
    return str(value if value is not None else "").replace("\x00", "").strip()


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_category(raw: str) -> str | None:
    c = collapse(raw)
    if not c:
        return None
    return c.title() if c.islower() or c.isupper() else c


def split_list(raw: str) -> list[str]:
    return [collapse(part) for part in LIST_SPLIT.split(raw) if collapse(part)]


def normalize_tags(raw: str) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for t in split_list(raw):
        if t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    return tags


def site_key(url: str) -> str:
    """Stable identity of a target editor location: host+path (or file path)."""
    p = urlparse(url)
    if p.scheme == "file":
        return "file:" + p.path
    return f"{(p.hostname or '').lower()}{p.path.rstrip('/')}"


def content_hash(url: str, title: str, content: str) -> str:
    payload = json.dumps([site_key(url), collapse(title).lower(), collapse(content).lower()], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_image(ref: str, base_dir: Path) -> tuple[Path | None, str | None]:
    candidate = Path(ref).expanduser()
    options = [candidate] if candidate.is_absolute() else [base_dir / candidate, Path.cwd() / candidate]
    path = next((o for o in options if o.is_file()), None)
    if path is None:
        return None, f"image not found: {ref}"
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None, f"unsupported image type '{path.suffix}': {ref}"
    if path.stat().st_size > MAX_IMAGE_BYTES:
        return None, f"image larger than {MAX_IMAGE_BYTES // 1024 // 1024} MB: {ref}"
    if path.stat().st_size == 0:
        return None, f"image file is empty: {ref}"
    return path.resolve(), None


def prepare_content(path: Path | str, allow_local: bool = False) -> PreparationResult:
    """Read, validate and normalize every row. Invalid rows are reported (with row numbers) and skipped."""
    source = Path(path).expanduser()
    df = read_table(source)
    # "description" is accepted in place of "content" (block-editor sites call the text a description).
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns and not (c == "content" and "description" in df.columns)]
    if missing:
        raise PreparationError(
            f"Missing required column(s): {', '.join(missing)}. Found: {', '.join(df.columns) or 'none'}.")
    items: list[ContentItem] = []
    errors: list[RowError] = []
    seen_hashes: dict[str, int] = {}
    total = 0
    for idx, row in df.iterrows():
        values = {c: _clean(row[c]) for c in df.columns}
        if not any(values.values()):
            continue  # fully blank row
        total += 1
        row_number = int(idx) + 2  # header is row 1
        problems: list[str] = []

        url = ""
        try:
            url = validate_url(values["website_url"], allow_local_files=allow_local)
        except UnsafeTargetError as exc:
            problems.append(f"website_url: {exc}")
        title = collapse(values["title"])
        if not title:
            problems.append("title is empty")
        elif len(title) > MAX_TITLE:
            problems.append(f"title longer than {MAX_TITLE} characters")
        body = (values.get("content") or values.get("description", "")).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not body:
            problems.append("content is empty")

        mode = values.get("publish_mode", "").lower() or "draft"
        if mode not in {"draft", "publish"}:
            problems.append(f"publish_mode must be 'draft' or 'publish', got '{values.get('publish_mode')}'")

        tags = normalize_tags(values.get("tags", ""))
        if len(tags) > MAX_TAGS:
            problems.append(f"more than {MAX_TAGS} tags")
        if any(len(t) > MAX_TAG_LEN for t in tags):
            problems.append(f"a tag is longer than {MAX_TAG_LEN} characters")

        image_refs, image_paths = [], []
        for ref in split_list(values.get("images", "")):
            resolved, err = _resolve_image(ref, source.resolve().parent)
            if err:
                problems.append(err)
            else:
                image_refs.append(ref)
                image_paths.append(str(resolved))

        blog_url = None
        if values.get("blog_url"):
            try:
                blog_url = validate_url(values["blog_url"], allow_local_files=False if not allow_local else allow_local)
            except UnsafeTargetError as exc:
                problems.append(f"blog_url: {exc}")
        anchor = collapse(values.get("anchor_text", "")) or None
        if anchor and anchor.lower() not in collapse(body).lower():
            problems.append(f"anchor_text '{anchor}' does not appear in the content/description")
        cover, cover_path = None, None
        if values.get("cover_image"):
            resolved, err = _resolve_image(values["cover_image"], source.resolve().parent)
            if err:
                problems.append("cover_image: " + err)
            else:
                cover, cover_path = values["cover_image"], str(resolved)

        digest = content_hash(url, title, body) if (url and title and body) else ""
        if digest and digest in seen_hashes:
            problems.append(f"duplicate of row {seen_hashes[digest]} (same website, title and content)")

        if problems:
            errors.append(RowError(row_number=row_number, errors=problems))
            continue
        seen_hashes[digest] = row_number
        items.append(ContentItem(
            row_number=row_number, website_url=url, title=title, content=body,
            category=normalize_category(values.get("category", "")), tags=tags, images=image_refs,
            publish_mode=mode, content_hash=digest, image_paths=image_paths, blog_url=blog_url, anchor_text=anchor,
            cover_image=cover, cover_image_path=cover_path,
            metadata={c: v for c, v in values.items() if c not in KNOWN_COLUMNS and v},
        ))
    return PreparationResult(source_file=str(source), total_rows=total, items=items, errors=errors)
