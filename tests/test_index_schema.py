"""Course tables hold every material type in any order — TABLE_SCHEMA
(indexing.py).

Guards the real failure where LanceDB inferred column types from a table's
first batch: a course whose first batch had no page numbers rejected every
PDF ("cannot cast field 'page' from Int64 to Null"), and a PDF-first course
rejected pptx slides.
"""

from pathlib import Path

import lancedb
import pytest

import indexing


TABLE = "course_1"


def _pdf():
    return indexing.pages_to_nodes([{"page": 3, "text": "Boundary value analysis picks inputs at the edges."}], "BVA.pdf", "file:10")


def _slides():
    return indexing.slides_to_nodes([{"slide": 2, "text": "The test pyramid: many unit tests, few UI tests."}], "deck.pptx", "file:11")


def _page():
    return indexing.sections_to_nodes([{"heading": "Grading", "text": "Project 40%"}], "Syllabus", "syllabus:1", "syllabus")


def _unit(text):
    return [1.0] + [0.0] * (indexing.EMBEDDING_DIM - 1)


def _anchors(db_path: Path) -> dict[str, tuple]:
    df = lancedb.connect(str(db_path)).open_table(TABLE).to_pandas()
    return {m["item_type"] + ":" + m["source"]: (m["page"], m["slide"]) for m in df["metadata"]}



@pytest.mark.parametrize(
    "order",
    [
        ["page", "pdf", "slides"],
        ["slides", "pdf", "page"],
        ["pdf", "slides", "page"],
    ],
)
def test_any_material_order_indexes_into_one_course(tmp_path, order):
    makers = {"page": _page, "pdf": _pdf, "slides": _slides}
    for kind in order:
        indexing.add_nodes(makers[kind](), tmp_path, TABLE, embed=_unit)

    assert _anchors(tmp_path) == {
        "syllabus:Syllabus": (None, None),
        "file:BVA.pdf": (3, None),
        "file:deck.pptx": (None, 2),
    }
    assert lancedb.connect(str(tmp_path)).open_table(TABLE).schema == indexing.TABLE_SCHEMA

