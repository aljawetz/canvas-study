"""Chunking + indexing.

Turns ingestion.py's extraction output into chunks carrying citation
metadata: a page or slide is a chunk, not an arbitrary token window, and
split_text only cuts prose long enough to need it. Each course is one
table in the SQLite index (<data folder>/index.db, store.py).
Callers pass db_path: the index file (<data folder>/index.db), or a scratch file in tests.
"""

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from tokenizers import Tokenizer

import store
from embeddings import EMBEDDING_DIM, MODEL_DIR

# Tokens, counted with BGE's own tokenizer. Must stay under the embedding
# model's 512-token window (embeddings.MAX_SEQ_LENGTH), with room for
# sections_to_nodes()'s heading line: the embedding silently truncates
# anything past 512. At the old 700, 23% of real chunks ran over, and the
# part past the cut was invisible to vector search: 18-654's grading table
# started at token 513 of its syllabus chunk, and search never found it.
# 400 is what the index actually held before this splitter: llama-index's
# limit was 450 but it counted each chunk's metadata against it, so a split
# page's largest chunk had a median of 405 tokens. Splitting to a true 450
# kept hit@5 but put the right page at #1 less often (search benchmark,
# 225 questions), so the smaller, more focused chunks stay.
# What an indexed chunk looks like: its text (splitting, headings like
# "Course syllabus"), metadata and storage. Bump it in any change to those,
# so every install's next sync re-indexes each course once in the new format
# (course_sync, sync.index_format); an incremental sync otherwise only
# re-indexes what changed on Canvas.
# 2: LanceDB -> SQLite (#36).
INDEX_FORMAT = 2

CHUNK_SIZE = 400
CHUNK_OVERLAP = 60  # ~15%

# Loaded on first use, not at import: the server imports this module before
# setup has downloaded the models (tests/test_plugin_imports.py).
_bge_tokenizer: Tokenizer | None = None


def _tokenizer() -> Tokenizer:
    global _bge_tokenizer
    if _bge_tokenizer is None:
        _bge_tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    return _bge_tokenizer


def _tokenize(text: str) -> list[str]:
    return _tokenizer().encode(text).tokens


def _count(text: str) -> int:
    return len(_tokenizer().encode(text, add_special_tokens=False).ids)


def _pieces(text: str, separator: str) -> list[str]:
    """text cut after each match of separator, each piece keeping its
    separator, so "".join(pieces) == text."""
    out, start = [], 0
    for m in re.finditer(separator, text):
        if m.end() > start:
            out.append(text[start:m.end()])
            start = m.end()
    if start < len(text):
        out.append(text[start:])
    return out


def _token_windows(text: str, size: int) -> list[str]:
    """A run with no usable break (a long URL, a table without spaces), cut every size tokens."""
    enc = _tokenizer().encode(text, add_special_tokens=False)
    cuts = [enc.offsets[i][0] for i in range(size, len(enc.ids), size)]
    bounds = [0, *cuts, len(text)]
    return [text[a:b] for a, b in zip(bounds, bounds[1:]) if b > a]


# Coarsest break first: paragraphs, then sentences, then lines, then words.
# Lines come after sentences because PDF text breaks every printed line,
# mid-sentence: a chunk cut at a line wrap starts mid-thought and buries the
# next topic in the middle of the chunk before it.
_SEPARATORS = [r"\n\s*\n", r"(?<=[.!?])\s+", r"\n", r"\s+"]


def _units(text: str, size: int, level: int = 0) -> list[str]:
    """text cut into pieces of at most size tokens, at the coarsest break that gets there."""
    if _count(text) <= size:
        return [text]
    if level == len(_SEPARATORS):
        return _token_windows(text, size)
    pieces = _pieces(text, _SEPARATORS[level])
    if len(pieces) == 1:
        return _units(text, size, level + 1)
    return [u for piece in pieces for u in _units(piece, size, level + 1)]


def split_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Chunks of at most size tokens. Text that fits stays whole (most pages
    do); longer text is packed from paragraphs, sentences or words, and each
    chunk after the first starts with up to overlap tokens from the end of
    the one before, so a sentence cut at a boundary is still found whole."""
    if _count(text) <= size:
        return [text.strip()] if text.strip() else []
    units = [(u, _count(u)) for u in _units(text, size)]
    chunks, current, tokens = [], [], 0
    for unit, n in units:
        if current and tokens + n > size:
            chunks.append("".join(u for u, _ in current))
            kept, kept_tokens = [], 0
            for u, m in reversed(current):
                if kept_tokens + m > overlap or kept_tokens + m + n > size:
                    break
                kept.insert(0, (u, m))
                kept_tokens += m
            current, tokens = kept, kept_tokens
        current.append((unit, n))
        tokens += n
    if current:
        chunks.append("".join(u for u, _ in current))
    return [c.strip() for c in chunks if c.strip()]


@dataclass
class Chunk:
    """What gets embedded and stored: the text, its citation metadata, and
    the Canvas item it came from (e.g. "file:123"), which delete works by."""

    text: str
    metadata: dict
    item_id: str


def _metadata(source: str, item_type: str, *, page=None, slide=None) -> dict:
    """Every chunk carries the same metadata keys, most of them None: see TABLE_SCHEMA."""
    return {"source": source, "item_type": item_type, "page": page, "slide": slide}


def pages_to_nodes(pages: list[dict], source: str, canvas_item_id: str, item_type: str = "file") -> list[Chunk]:
    """One chunk per page, split further only if the page's prose actually
    exceeds the target chunk size: most extracted pages don't.

    item_type is "file" for PDFs; course_sync.py passes other types for
    Canvas content that isn't paginated, whose pages carry page=None."""
    return [
        Chunk(chunk, _metadata(source, item_type, page=page["page"]), canvas_item_id)
        for page in pages
        for chunk in split_text(page["text"])
    ]


def sections_to_nodes(sections: list[dict], source: str, canvas_item_id: str, item_type: str,
                      title: str | None = None) -> list[Chunk]:
    """Canvas HTML sections (ingestion.extract_html_sections) -> chunks, one
    section at a time so a chunk never mixes sections (a syllabus's grading
    table no longer shares a chunk with its staff list).

    Every chunk starts with a "Syllabus › Grading Algorithm" line, so both
    the embedding and Claude see what the chunk is part of even when the
    section's own text is just "12.5% | Project" rows. The heading line is
    counted against the chunk size, so heading plus text stays under the
    embedding window. A heading with no text of its own ("Class Schedule"
    directly followed by another bold line) becomes a line of the next
    section instead of a near-empty chunk.

    title, when given, is what the heading line calls the item (the
    citation label stays source): "Course syllabus" for a syllabus labelled
    "Syllabus"."""
    title = title or source
    chunks = []
    pending: list[str] = []
    for section in sections:
        if not section["text"]:
            if section["heading"]:
                pending.append(section["heading"])
            continue
        path = f"{title} › {section['heading']}" if section["heading"] else title
        body = "\n".join([*pending, section["text"]])
        pending = []
        room = max(CHUNK_SIZE - _count(path) - 1, CHUNK_SIZE // 2)
        for chunk in split_text(body, size=room):
            chunks.append(Chunk(f"{path}\n{chunk}", _metadata(source, item_type), canvas_item_id))
    if pending:
        chunks.append(Chunk(f"{title}\n" + "\n".join(pending), _metadata(source, item_type), canvas_item_id))
    return chunks


# Course-level questions ("what is this course about?", "what will I
# learn?") say "course"; a syllabus chunk headed only "Syllabus › List of
# Topics" ranked 13th to 17th for them. Headed "Course syllabus", syllabus
# questions went from 70% to 83% found in the top 5 (search benchmark),
# with no question lost elsewhere.
SYLLABUS_TITLE = "Course syllabus"


def mark_syllabus_file(chunks: list[Chunk], name: str) -> list[Chunk]:
    """A file named like a syllabus (many Canvas syllabus pages only link a
    PDF) gets the same "Course syllabus" line on each chunk."""
    if "syllabus" not in name.lower():
        return chunks
    return [Chunk(f"{SYLLABUS_TITLE}: {name}\n{c.text}", c.metadata, c.item_id) for c in chunks]


def slides_to_nodes(slides: list[dict], source: str, canvas_item_id: str) -> list[Chunk]:
    """One chunk per slide, not split: a slide is a citation unit
    ("slide 12"), not something to fragment across chunks."""
    return [
        Chunk(slide["text"], _metadata(source, "file", slide=slide["slide"]), canvas_item_id)
        for slide in slides
        if slide["text"].strip()
    ]


_embedder = None


def _default_embed(text: str) -> list[float]:
    global _embedder
    if _embedder is None:
        from embeddings import OnnxBgeEmbedding

        _embedder = OnnxBgeEmbedding()
    return _embedder.embed(text)


def index_exists(db_path: Path, table_name: str) -> bool:
    if not Path(db_path).exists():
        return False
    conn = store.connect(db_path)
    try:
        return store.table_exists(conn, table_name)
    finally:
        conn.close()


def add_nodes(chunks: list[Chunk], db_path: Path, table_name: str, embed=None) -> None:
    """Embeds chunks and adds them to a course's table (store.py), creating
    it on the first write. embed is a test seam; by default the BGE model."""
    if not chunks:
        return
    embed = embed or _default_embed
    rows = [{"id": str(uuid.uuid4()), "doc_id": c.item_id, "vector": embed(c.text), "text": c.text,
             "metadata": c.metadata} for c in chunks]
    conn = store.connect(db_path)
    try:
        store.add(conn, table_name, rows)
    finally:
        conn.close()


def delete_ref_doc_nodes(db_path: Path, table_name: str, ref_doc_id: str) -> None:
    """Removes every chunk of one Canvas item (a changed or deleted file)."""
    if not Path(db_path).exists():
        return
    conn = store.connect(db_path)
    try:
        store.delete_doc(conn, table_name, ref_doc_id)
    finally:
        conn.close()
