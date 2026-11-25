"""The search index on SQLite: one table of chunks per course, an FTS5 table
for BM25 keyword search, and the embedding vectors as float32 blobs.

SQLite ships with Python, so this replaces LanceDB and PyArrow (~280 MB of
the install). Vector search is a numpy dot product over the course's
vectors: a course is a few thousand chunks, so a scan is a few milliseconds
and needs no ANN index. The vectors are cached per connection and reloaded
only when PRAGMA data_version says another connection (a sync, possibly in
another Claude session's server) has written since.

WAL mode lets a search read while a sync writes. Chunk rows and their FTS
rows are kept in step by triggers, so a delete or insert can't leave them
apart.
"""

import re
import sqlite3
import uuid
from pathlib import Path

import numpy as np

from embeddings import EMBEDDING_DIM

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# FTS5 tokenizer. unicode61 lowercases and strips diacritics; "porter" on
# top stems English ("tests" finds "testing"). Chosen on the search benchmark.
TOKENIZER = "porter unicode61"


def _check(table: str) -> str:
    # Table names are ours (course_<id>), never the student's text, but they
    # are put into SQL, so refuse anything that isn't a plain identifier.
    if not _NAME.match(table):
        raise ValueError(f"bad table name: {table!r}")
    return table


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_check(table),)).fetchone()
    return row is not None


def tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    names = {name for (name,) in rows}
    # A course table is one with its FTS companion; FTS5's own shadow tables
    # (course_1_fts_data, ...) are not courses.
    return sorted(n for n in names if f"{n}_fts" in names)


def create_table(conn: sqlite3.Connection, table: str) -> None:
    t = _check(table)
    with conn:
        conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {t} (
          rowid     INTEGER PRIMARY KEY,
          id        TEXT NOT NULL UNIQUE,
          doc_id    TEXT NOT NULL,
          text      TEXT NOT NULL,
          source    TEXT,
          item_type TEXT,
          page      INTEGER,
          slide     INTEGER,
          vector    BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS {t}_doc ON {t}(doc_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS {t}_fts USING fts5(
          text, content='{t}', content_rowid='rowid', tokenize='{TOKENIZER}'
        );
        CREATE TRIGGER IF NOT EXISTS {t}_ai AFTER INSERT ON {t} BEGIN
          INSERT INTO {t}_fts(rowid, text) VALUES (new.rowid, new.text);
        END;
        CREATE TRIGGER IF NOT EXISTS {t}_ad AFTER DELETE ON {t} BEGIN
          INSERT INTO {t}_fts({t}_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
        END;
        """)


def add(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    """rows: {"doc_id", "text", "metadata": {source, item_type, page, slide}, "vector"}; an "id" is made
    when missing."""
    t = _check(table)
    create_table(conn, t)
    values = []
    for r in rows:
        vector = np.asarray(r["vector"], dtype=np.float32)
        if vector.shape != (EMBEDDING_DIM,):
            raise ValueError(f"vector has shape {vector.shape}, expected ({EMBEDDING_DIM},)")
        m = r.get("metadata") or {}
        values.append((r.get("id") or str(uuid.uuid4()), r["doc_id"], r["text"], m.get("source"), m.get("item_type"),
                       m.get("page"), m.get("slide"), vector.tobytes()))
    with conn:
        conn.executemany(
            f"INSERT INTO {t} (id, doc_id, text, source, item_type, page, slide, vector) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )


def delete_doc(conn: sqlite3.Connection, table: str, doc_id: str) -> None:
    t = _check(table)
    if not table_exists(conn, t):
        return
    with conn:
        conn.execute(f"DELETE FROM {t} WHERE doc_id = ?", (doc_id,))


_COLUMNS = "id, doc_id, text, source, item_type, page, slide"


def _row(values) -> dict:
    id_, doc_id, text, source, item_type, page, slide = values
    return {"id": id_, "doc_id": doc_id, "text": text,
            "metadata": {"source": source, "item_type": item_type, "page": page, "slide": slide}}


def rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Every chunk without its vector, in insertion order."""
    t = _check(table)
    if not table_exists(conn, t):
        return []
    return [_row(v) for v in conn.execute(f"SELECT {_COLUMNS} FROM {t} ORDER BY rowid")]


# English stop words left out of keyword queries: every chunk has them, so
# OR-ing them in only adds noise to the ranking. These are the words LanceDB's
# full-text search (stem + remove_stop_words) dropped, measured by indexing
# each word of the Snowball English list alone and searching for it; with
# only the 33-word Lucene list, "what do I need to hand in for task 4?"
# ranked on "what", "do" and "I" (#36).
STOP_WORDS = frozenset("""a about after again against all am an and are as at be been being below between both but by did do doing down
each few for from further had has have having he her here hers herself him himself his how i if in into is it
its itself just me more most my myself of off on or other our ours out over own same she should so some such
than that the their theirs them then there these they this those through to too under until up was we were
what when where which while who whom will with you your yours yourself""".split())


def _fts_query(text: str) -> str | None:
    """The student's words as an FTS5 query that can't be a syntax error:
    each word quoted, any of them may match (BM25 ranks rows with more and
    rarer words first)."""
    words = [w for w in re.findall(r"\w+", text) if w.lower() not in STOP_WORDS]
    return " OR ".join('"' + w.replace('"', "") + '"' for w in words) or None


class CourseIndex:
    """Search over one course's table, for retrieval.HybridSearcher. Holds
    its own connection; safe to keep for the server's lifetime."""

    def __init__(self, db_path: Path, table: str):
        self._conn = connect(db_path)
        self._table = _check(table)
        self._version = None
        self._ids: list[int] = []
        self._matrix = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)

    def _vectors(self):
        version = self._conn.execute("PRAGMA data_version").fetchone()[0]
        if version != self._version or self._version is None:
            fetched = self._conn.execute(f"SELECT rowid, vector FROM {self._table}").fetchall()
            self._ids = [rowid for rowid, _ in fetched]
            self._matrix = (np.frombuffer(b"".join(v for _, v in fetched), dtype=np.float32)
                            .reshape(len(fetched), EMBEDDING_DIM)) if fetched else np.zeros((0, EMBEDDING_DIM), np.float32)
            self._version = version
        return self._ids, self._matrix

    def _by_rowid(self, rowids: list[int]) -> dict[int, dict]:
        if not rowids:
            return {}
        marks = ",".join("?" * len(rowids))
        found = self._conn.execute(f"SELECT rowid, {_COLUMNS} FROM {self._table} WHERE rowid IN ({marks})", rowids)
        return {v[0]: _row(v[1:]) for v in found}

    def vector(self, query_vector, limit: int) -> list[tuple[dict, float]]:
        """The `limit` nearest chunks by cosine similarity (vectors are unit length), best first."""
        ids, matrix = self._vectors()
        if not ids:
            return []
        scores = matrix @ np.asarray(query_vector, dtype=np.float32)
        top = np.argsort(-scores, kind="stable")[:limit]
        found = self._by_rowid([ids[i] for i in top])
        return [(found[ids[i]], float(scores[i])) for i in top if ids[i] in found]

    def keyword(self, query: str, limit: int) -> list[tuple[dict, float]]:
        """BM25 over chunk text, best first; scores are positive, higher is better."""
        match = _fts_query(query)
        if match is None:
            return []
        t = self._table
        found = self._conn.execute(
            f"SELECT {t}.rowid, -bm25({t}_fts) FROM {t}_fts JOIN {t} ON {t}.rowid = {t}_fts.rowid "
            f"WHERE {t}_fts MATCH ? ORDER BY bm25({t}_fts) LIMIT ?",
            (match, limit),
        ).fetchall()
        by_id = self._by_rowid([rowid for rowid, _ in found])
        return [(by_id[rowid], score) for rowid, score in found if rowid in by_id]
