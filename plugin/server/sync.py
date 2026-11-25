"""Sync bookkeeping.

Diffs a course's current Canvas listing (ID + updated_at, cheap metadata
only) against its manifest.db into new/changed/deleted buckets; the rest is
unchanged.
Runs when sync_course is called, not as a background daemon.

diff() takes an already-fetched remote listing rather than calling
canvas.py itself, so it's testable against simulated Canvas data without
live access — the caller (wherever real sync gets orchestrated) is
responsible for the actual Canvas calls.
"""

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS manifest (
  canvas_item_id    TEXT NOT NULL,
  item_type         TEXT NOT NULL,
  display_name      TEXT NOT NULL,
  canvas_updated_at TEXT NOT NULL,
  content_hash      TEXT NOT NULL,
  last_synced_at    TEXT NOT NULL,
  PRIMARY KEY (canvas_item_id, item_type)
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def open_manifest(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def index_format(conn: sqlite3.Connection) -> int:
    """The chunk format this course was last fully indexed with; 0 for a
    course indexed before formats were stamped (0.3.5 and earlier)."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'index_format'").fetchone()
    return int(row[0]) if row else 0


def set_index_format(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES ('index_format', ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(version),))
    conn.commit()


def content_hash(text: str) -> str:
    """Hash of extracted text, not raw bytes: a
    metadata-only touch (rename, permission change) shouldn't trigger
    re-embedding if the actual content is unchanged."""
    return hashlib.sha256(text.encode()).hexdigest()


def diff(conn: sqlite3.Connection, item_type: str, remote_items: list[dict],
         rebuild: bool = False) -> dict[str, list[str]]:
    """remote_items: [{"id": str, "updated_at": str}, ...]. Sorts every
    item into at most one bucket (an unchanged item is in none). With rebuild, every item already indexed
    counts as changed: its chunks were written in an older format."""
    rows = conn.execute(
        "SELECT canvas_item_id, canvas_updated_at FROM manifest WHERE item_type = ?", (item_type,)
    ).fetchall()
    manifest_map = dict(rows)
    remote_map = {item["id"]: item["updated_at"] for item in remote_items}

    same = lambda i: remote_map[i] == manifest_map[i] and not rebuild
    return {
        "new": [i for i in remote_map if i not in manifest_map],
        "changed": [i for i in remote_map if i in manifest_map and not same(i)],
        "deleted": [i for i in manifest_map if i not in remote_map],
    }


def mark_synced(
    conn: sqlite3.Connection,
    canvas_item_id: str,
    item_type: str,
    display_name: str,
    canvas_updated_at: str,
    hash_: str,
):
    conn.execute(
        """INSERT INTO manifest (canvas_item_id, item_type, display_name, canvas_updated_at, content_hash, last_synced_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (canvas_item_id, item_type) DO UPDATE SET
             display_name = excluded.display_name,
             canvas_updated_at = excluded.canvas_updated_at,
             content_hash = excluded.content_hash,
             last_synced_at = excluded.last_synced_at""",
        (canvas_item_id, item_type, display_name, canvas_updated_at, hash_, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def forget(conn: sqlite3.Connection, canvas_item_id: str, item_type: str):
    conn.execute("DELETE FROM manifest WHERE canvas_item_id = ? AND item_type = ?", (canvas_item_id, item_type))
    conn.commit()


def get_content_hash(conn: sqlite3.Connection, canvas_item_id: str, item_type: str) -> str | None:
    row = conn.execute(
        "SELECT content_hash FROM manifest WHERE canvas_item_id = ? AND item_type = ?",
        (canvas_item_id, item_type),
    ).fetchone()
    return row[0] if row else None
