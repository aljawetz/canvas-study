"""Extraction measured against the documents in tests/fixtures/ingestion/
and the numbers taken from them (tests/fixtures/ingestion_baseline.json).
Both come from tests/fixtures/make_ingestion_fixtures.py; rerun it after an
intended change to extraction.
"""

import json
from pathlib import Path

import pytest

import ingestion

FIXTURES = Path(__file__).parent / "fixtures" / "ingestion"
BASELINE = json.loads((Path(__file__).parent / "fixtures" / "ingestion_baseline.json").read_text())



def test_image_only_pdf_flags_every_page():
    expected = BASELINE["citation_guide.pdf"]
    pages = ingestion.extract_pdf(FIXTURES / "citation_guide.pdf")
    assert len(pages) == expected["pages"]
    assert [p["char_count"] for p in pages] == expected["char_counts"]
    assert [p["needs_fallback"] for p in pages] == expected["flagged"]


def test_ocr_recovers_the_image_text():
    expected = BASELINE["citation_guide.pdf"]["ocr_total_chars"]
    total = sum(len(ingestion.ocr_pdf_page(FIXTURES / "citation_guide.pdf", i)) for i in range(1, 12))
    # OCR isn't bit-for-bit the same across Tesseract versions: close, not exact.
    assert abs(total - expected) / expected < 0.05


def test_pptx_matches_baseline():
    expected = BASELINE["ai_assistant_slides.pptx"]
    slides = ingestion.extract_pptx(FIXTURES / "ai_assistant_slides.pptx")
    assert len(slides) == expected["slides"]
    assert [s["char_count"] for s in slides] == expected["char_counts"]
    assert [s["has_image"] for s in slides] == expected["has_image"]


@pytest.mark.parametrize("filename", ["research_methods.pdf", "dense_article.pdf"])
def test_text_pdfs_match_baseline(filename):
    expected = BASELINE[filename]
    pages = ingestion.extract_pdf(FIXTURES / filename)
    assert len(pages) == expected["pages"]
    assert [p["char_count"] for p in pages] == expected["char_counts"]
    assert [p["needs_fallback"] for p in pages] == expected["flagged"]
