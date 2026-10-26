"""JSON execution report."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from services.secrets import REDACTOR

COMPLETED = {"draft_saved", "published"}
NEEDS_REVIEW = {"unverified"}
SKIPPED = {"duplicate_skipped", "rejected_by_user"}


def job_status(db_status: str) -> str:
    """COMPLETED only after successful verification; otherwise FAILED or NEEDS_REVIEW (or SKIPPED by choice)."""
    if db_status in COMPLETED:
        return "COMPLETED"
    if db_status in NEEDS_REVIEW:
        return "NEEDS_REVIEW"
    if db_status in SKIPPED:
        return "SKIPPED"
    return "FAILED"


def write_report(output_dir: Path, run_id: int, source_file: str, db_path: str, items: list[dict],
                 errors: list[dict], sites: list[dict], started_at: str, notes: list[str]) -> tuple[Path, dict]:
    counts = Counter(i["status"] for i in items)
    report = {
        "run_id": run_id, "source_file": source_file, "database": db_path,
        "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "rows_in_file": len(items) + len(errors), "invalid_rows": len(errors),
            "published": counts.get("published", 0), "drafts_saved": counts.get("draft_saved", 0),
            "duplicates_skipped": counts.get("duplicate_skipped", 0),
            "rejected_by_user": counts.get("rejected_by_user", 0),
            "failed": sum(counts.get(s, 0) for s in ("failed", "analysis_failed", "rejected_by_site",
                                                      "verification_failed", "login_failed")),
            "needs_attention": counts.get("unverified", 0),
        },
        "invalid_rows": errors, "websites": sites, "items": items, "notes": notes,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"execution_report_run{run_id}.json"
    path.write_text(REDACTOR.redact(json.dumps(report, indent=2, ensure_ascii=False)), encoding="utf-8")
    return path, report
