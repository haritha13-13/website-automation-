"""SQLite tracking of runs, items and events, plus duplicate-publish protection."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from services.secrets import REDACTOR

# An item in one of these states may already exist on the site, so an identical item is not re-sent.
BLOCKING = ("in_progress", "submitted", "unverified", "verification_failed", "draft_saved", "published")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_file TEXT, started_at TEXT, finished_at TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES runs(id),
  row_number INTEGER, site TEXT, website_url TEXT, title TEXT, content_hash TEXT, publish_mode TEXT,
  status TEXT NOT NULL, error TEXT, article_url TEXT, created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_items_hash ON items(site, content_hash);
CREATE TABLE IF NOT EXISTS authorized_sites (domain TEXT PRIMARY KEY, approved_at TEXT);
CREATE TABLE IF NOT EXISTS credential_grants (domain TEXT PRIMARY KEY, approved_at TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, item_id INTEGER, ts TEXT, stage TEXT, message TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def start_run(self, source_file: str) -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO runs(source_file, started_at, status) VALUES (?,?,?)",
                                     (source_file, _now(), "running"))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE runs SET finished_at=?, status=? WHERE id=?", (_now(), status, run_id))

    def add_item(self, run_id: int, row_number: int, site: str, website_url: str, title: str,
                 content_hash: str | None, publish_mode: str | None, status: str, error: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO items(run_id,row_number,site,website_url,title,content_hash,publish_mode,status,error,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, row_number, site, website_url, title, content_hash, publish_mode, status, error, _now(), _now()))
            return int(cur.lastrowid)

    def update_item(self, item_id: int, status: str | None = None, error: str | None = None,
                    article_url: str | None = None) -> None:
        with self._lock:
            row = self._conn.execute("SELECT status,error,article_url FROM items WHERE id=?", (item_id,)).fetchone()
            error = REDACTOR.redact(error) if error is not None else None
            self._conn.execute("UPDATE items SET status=?, error=?, article_url=?, updated_at=? WHERE id=?",
                               (status or row["status"], error if error is not None else row["error"],
                                article_url or row["article_url"], _now(), item_id))

    def log(self, run_id: int | None, stage: str, message: str, item_id: int | None = None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events(run_id,item_id,ts,stage,message) VALUES (?,?,?,?,?)",
                               (run_id, item_id, _now(), stage, REDACTOR.redact(message)[:1000]))

    def find_blocking(self, site: str, content_hash: str, exclude_item: int | None = None) -> sqlite3.Row | None:
        marks = ",".join("?" * len(BLOCKING))
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM items WHERE site=? AND content_hash=? AND status IN ({marks}) AND id!=? "
                "ORDER BY id DESC LIMIT 1", (site, content_hash, *BLOCKING, exclude_item or -1)).fetchone()

    def claim(self, item_id: int) -> bool:
        """Atomically mark the item in_progress unless an identical item is already blocking.
        Called immediately before the save/publish click."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                me = self._conn.execute("SELECT site, content_hash FROM items WHERE id=?", (item_id,)).fetchone()
                if self.find_blocking(me["site"], me["content_hash"], exclude_item=item_id):
                    self._conn.execute("ROLLBACK")
                    return False
                self._conn.execute("UPDATE items SET status='in_progress', updated_at=? WHERE id=?", (_now(), item_id))
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def items_for_run(self, run_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM items WHERE run_id=? ORDER BY id", (run_id,)))

    def events_for_run(self, run_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)))

    # ---- website authorization / credential-sharing consent (domain names only; no secrets) --------
    def is_authorized(self, domain: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM authorized_sites WHERE domain=?", (domain.lower(),)).fetchone() is not None

    def authorize(self, domain: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO authorized_sites VALUES (?,?)", (domain.lower(), _now()))

    def has_credential_grant(self, domain: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM credential_grants WHERE domain=?", (domain.lower(),)).fetchone() is not None

    def grant_credentials(self, domain: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO credential_grants VALUES (?,?)", (domain.lower(), _now()))

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))

    def get_item(self, item_id: int) -> sqlite3.Row:
        with self._lock:
            return self._conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
