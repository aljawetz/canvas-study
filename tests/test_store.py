"""store.py: the SQLite search index. Real SQLite and FTS5, no embedding
model: vectors are made by hand so nearest neighbours are known."""

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import store
from embeddings import EMBEDDING_DIM


def _unit(*hot):
    """A unit vector with weight on the given dimensions."""
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    for i, w in hot:
        v[i] = w
    return (v / np.linalg.norm(v)).tolist()


def _row(doc_id, text, vector, **metadata):
    return {"doc_id": doc_id, "text": text, "vector": vector,
            "metadata": {"source": metadata.get("source", doc_id), "item_type": "file", "page": metadata.get("page"),
                         "slide": None}}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "index.db"
    conn = store.connect(path)
    store.add(conn, "course_1", [
        _row("file:1", "Stub queries; mock actions.", _unit((0, 1.0)), source="06 Test Doubles.pdf", page=4),
        _row("file:2", "Mutation testing checks that tests fail when the code is broken.", _unit((1, 1.0))),
        _row("file:2", "Equivalent mutants cannot be killed by any test.", _unit((1, 1.0), (2, 1.0))),
    ])
    yield path, conn
    conn.close()


def test_vector_search_ranks_by_cosine_similarity(db):
    path, _ = db
    index = store.CourseIndex(path, "course_1")

    found = index.vector(_unit((1, 1.0), (2, 0.2)), limit=2)

    assert [r["text"][:8] for r, _ in found] == ["Mutation", "Equivale"]
    assert found[0][1] == pytest.approx(float(np.dot(_unit((1, 1.0)), _unit((1, 1.0), (2, 0.2)))), rel=1e-5)
    assert found[0][1] > found[1][1]


def test_rows_keep_their_citation_metadata(db):
    path, _ = db
    row, _ = store.CourseIndex(path, "course_1").vector(_unit((0, 1.0)), limit=1)[0]
    assert row["doc_id"] == "file:1"
    assert row["metadata"] == {"source": "06 Test Doubles.pdf", "item_type": "file", "page": 4, "slide": None}


def test_keyword_search_ranks_by_bm25_with_positive_scores(db):
    path, _ = db
    found = store.CourseIndex(path, "course_1").keyword("equivalent mutants", limit=10)
    assert found[0][0]["text"].startswith("Equivalent mutants")
    assert all(score > 0 for _, score in found)
    assert [s for _, s in found] == sorted((s for _, s in found), reverse=True)


@pytest.mark.parametrize("query", ['what does "stub" mean?', "C++ AND NOT (mock*)", "NEAR(", '"', "  ", "-mock"])
def test_any_student_text_is_a_safe_keyword_query(db, query):
    path, _ = db
    store.CourseIndex(path, "course_1").keyword(query, limit=5)  # no FTS5 syntax error


def test_deleting_an_item_removes_it_from_both_searches(db):
    path, conn = db
    store.delete_doc(conn, "course_1", "file:2")
    index = store.CourseIndex(path, "course_1")

    assert index.keyword("mutation", limit=5) == []
    assert {r["doc_id"] for r, _ in index.vector(_unit((1, 1.0)), limit=5)} == {"file:1"}
    assert [r["doc_id"] for r in store.rows(conn, "course_1")] == ["file:1"]


_WRITER = """
import sys
import numpy as np
import store
from embeddings import EMBEDDING_DIM
v = np.zeros(EMBEDDING_DIM, dtype=np.float32); v[5] = 1.0
store.add(store.connect(sys.argv[1]), "course_1",
          [{"doc_id": "page:quiz", "text": "Weekly quizzes are open book.", "vector": v.tolist(),
            "metadata": {"source": "quiz policy", "item_type": "page", "page": None, "slide": None}}])
"""


def test_an_open_index_sees_writes_from_another_process(db):
    path, _ = db
    index = store.CourseIndex(path, "course_1")
    assert index.keyword("quizzes", limit=5) == []
    index.vector(_unit((5, 1.0)), limit=1)  # vectors cached now

    subprocess.run([sys.executable, "-c", _WRITER, str(path)], check=True,
                   env={**os.environ, "PYTHONPATH": str(Path(store.__file__).parent)})

    assert [r["doc_id"] for r, _ in index.keyword("quizzes", limit=5)] == ["page:quiz"]
    assert index.vector(_unit((5, 1.0)), limit=1)[0][0]["doc_id"] == "page:quiz"


def test_tables_lists_courses_not_fts_internals(db):
    _, conn = db
    store.add(conn, "course_22", [_row("file:9", "x", _unit((0, 1.0)))])
    assert store.tables(conn) == ["course_1", "course_22"]
    assert store.table_exists(conn, "course_22") and not store.table_exists(conn, "course_3")


def test_an_empty_or_missing_course_searches_to_nothing(tmp_path):
    conn = store.connect(tmp_path / "index.db")
    store.create_table(conn, "course_5")
    index = store.CourseIndex(tmp_path / "index.db", "course_5")
    assert index.vector(_unit((0, 1.0)), limit=5) == [] and index.keyword("anything", limit=5) == []
    store.delete_doc(conn, "course_6", "file:1")  # no table: nothing to delete, no error


def test_table_names_and_vector_sizes_are_checked(tmp_path):
    conn = store.connect(tmp_path / "index.db")
    with pytest.raises(ValueError):
        store.create_table(conn, "course_1; DROP TABLE x")
    with pytest.raises(ValueError):
        store.add(conn, "course_1", [_row("file:1", "x", [1.0, 0.0])])
