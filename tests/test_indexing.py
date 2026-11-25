"""A 4-query retrieval smoke test over the documents in
tests/fixtures/ingestion/ (make_ingestion_fixtures.py), with the real
embedding model. The other files are there as distractors.
"""

import tempfile
from pathlib import Path

import pytest

import embeddings
import indexing
import ingestion
import retrieval

FIXTURES = Path(__file__).parent / "fixtures" / "ingestion"



@pytest.fixture(scope="module")
def retriever():
    nodes = []

    guide_pages = ingestion.extract_pdf(FIXTURES / "citation_guide.pdf")
    for p in guide_pages:
        if p["needs_fallback"]:
            p["text"] = ingestion.merge_ocr_text(
                p["text"], ingestion.ocr_pdf_page(FIXTURES / "citation_guide.pdf", p["page"])
            )
    nodes += indexing.pages_to_nodes(guide_pages, "zotero", "file_zotero")

    slides = ingestion.extract_pptx(FIXTURES / "ai_assistant_slides.pptx")
    nodes += indexing.slides_to_nodes(slides, "ai_research", "file_ai_research")

    nodes += indexing.pages_to_nodes(
        ingestion.extract_pdf(FIXTURES / "research_methods.pdf"), "research_methods", "file_research_methods"
    )
    nodes += indexing.pages_to_nodes(
        ingestion.extract_pdf(FIXTURES / "dense_article.pdf"), "dense_article", "file_dense_article"
    )

    with tempfile.TemporaryDirectory() as tmp:
        indexing.add_nodes(nodes, Path(tmp), "smoke_test")
        # Vector search only, no cutoff, top 3: what this smoke test has always measured.
        searcher = retrieval.HybridSearcher(indexing.LanceIndex(indexing.open_table(Path(tmp), "smoke_test")),
                                            embeddings.OnnxBgeEmbedding().embed, cutoff=-1.0)
        yield lambda query: searcher.vector(query)[:3]


def _hit_rank(results, source, expected_pages):
    for i, r in enumerate(results, 1):
        anchor = r.metadata.get("page") or r.metadata.get("slide")
        if r.metadata.get("source") == source and anchor in expected_pages:
            return i
    return None


# Page 8's answer exists only inside its image (the page's own text is a
# footer), so this passes only if OCR-recovered text is good enough to embed
# and rank first.
def test_ocr_dependent_query_ranks_first(retriever):
    results = retriever("What citation style should I choose?")
    assert _hit_rank(results, "zotero", {8}) == 1


# Pages 3 and 4 are near-duplicates (both about installation): either is right.
def test_install_query_hits_top_3(retriever):
    results = retriever("How do I install Zotero?")
    assert _hit_rank(results, "zotero", {3, 4}) is not None


def test_collection_query_hits_top_3(retriever):
    results = retriever("How do I create a collection in Zotero?")
    assert _hit_rank(results, "zotero", {5}) is not None


def test_cross_file_query_hits_top_3(retriever):
    results = retriever("What is a vector database?")
    assert _hit_rank(results, "ai_research", {9}) is not None
