"""indexing.split_text: how long text becomes chunks. Every chunk is
embedded on its own, so a chunk past the model's window, lost text, or a
sentence cut without overlap is a search miss nobody sees."""

import re
import textwrap

import indexing
from indexing import CHUNK_OVERLAP, CHUNK_SIZE, split_text

SENTENCES = [f"Sentence number {i} explains test design technique {i} in a few plain words." for i in range(120)]
PROSE = " ".join(SENTENCES)


def _tokens(text):
    return indexing._count(text)


def test_text_that_fits_stays_whole():
    assert split_text("  Late work loses ten percent per day.  ") == ["Late work loses ten percent per day."]
    assert split_text("   \n  ") == []


def test_long_text_is_cut_under_the_size_and_loses_nothing():
    chunks = split_text(PROSE)
    assert len(chunks) > 1
    assert all(_tokens(c) <= CHUNK_SIZE for c in chunks)
    for sentence in SENTENCES:
        assert any(sentence in c for c in chunks), sentence  # whole sentences: cuts fall between them


def _overlap(before: str, after: str) -> str:
    """The longest start of after that is also the end of before."""
    for k in range(len(after), 0, -1):
        if before.endswith(after[:k]):
            return after[:k]
    return ""


def test_each_chunk_starts_with_the_end_of_the_one_before_within_the_overlap():
    chunks = split_text(PROSE)
    for before, after in zip(chunks, chunks[1:]):
        shared = _overlap(before, after)
        assert re.match(r"Sentence number \d+ .*\.$", shared), shared  # at least one whole sentence
        assert _tokens(shared) <= CHUNK_OVERLAP


def test_paragraphs_that_fit_become_their_own_chunks():
    # Four ~250-token paragraphs: two don't fit in one chunk, so each paragraph is a chunk,
    # rather than sentence-packed chunks that straddle paragraph breaks.
    paragraphs = [" ".join(SENTENCES[i * 18:(i + 1) * 18]) for i in range(4)]
    assert split_text("\n\n".join(paragraphs)) == paragraphs


def test_a_run_with_no_breaks_is_cut_by_tokens():
    # No whitespace, but many tokens: a hyphen-joined dump. (A single 6000-letter
    # word would be one [UNK] token to BGE's WordPiece, which fits anyway.)
    blob = "-".join(["data"] * 1500)
    chunks = split_text(blob)
    assert len(chunks) > 1 and all(_tokens(c) <= CHUNK_SIZE for c in chunks)
    assert "".join(chunks) == blob


def test_section_chunks_with_a_long_heading_stay_under_the_size():
    heading = "Syllabus › " + "Very Long Section Heading " * 15
    chunks = indexing.sections_to_nodes([{"heading": heading, "text": PROSE}], "Syllabus", "syllabus:1", "syllabus")
    assert all(_tokens(c.text) <= CHUNK_SIZE for c in chunks)
    assert all(c.text.startswith(f"Syllabus › {heading}\n") for c in chunks)


def test_pdf_line_wraps_are_not_sentence_breaks():
    # PDF text breaks every printed line, mid-sentence. Chunks must still
    # start where a sentence starts, not wherever a line happened to wrap.
    wrapped = textwrap.fill(PROSE, 60)  # printed lines break at spaces
    chunks = split_text(wrapped)
    assert len(chunks) > 1
    for chunk in chunks[1:]:
        assert chunk.startswith("Sentence number"), chunk[:40]
