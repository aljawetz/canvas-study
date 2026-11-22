"""Chunking + indexing.

Turns ingestion.py's extraction output into LlamaIndex nodes carrying
citation metadata: a page or slide is a node, not an arbitrary token
window, and SentenceSplitter only kicks in for prose long enough to need
it. Each course is one LanceDB table (<data folder>/index.lancedb/
course_<id>), through LlamaIndex's LanceDB integration. Callers pass
db_path: the plugin's data folder, or a scratch dir in tests.
"""

from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa
from lancedb.expr import col, lit
from lancedb.index import FTS
from llama_index.core import StorageContext, VectorStoreIndex, set_global_tokenizer
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import NodeRelationship, RelatedNodeInfo, TextNode
from llama_index.vector_stores.lancedb import LanceDBVectorStore
from tokenizers import Tokenizer

from embeddings import EMBEDDING_DIM, MODEL_DIR, OnnxBgeEmbedding

# Tokens, counted with BGE's own tokenizer (below). Must stay under the
# embedding model's 512-token window (embeddings.MAX_SEQ_LENGTH), with room
# for sections_to_nodes()'s heading line: the embedding silently truncates
# anything past 512. At the old 700, 23% of real chunks ran over, and the
# part past the cut was invisible to vector search — 18-654's grading table
# started at token 513 of its syllabus chunk, and search never found it.
CHUNK_SIZE = 450
CHUNK_OVERLAP = 60  # ~15%, same ratio as before

# Chunks are counted in BGE's own tokens, not SentenceSplitter's default
# tiktoken: a "450-token" chunk by tiktoken's count can run past BGE's real
# 512-token window. Loaded on first use, not at import: the server imports
# this module before setup has downloaded the models
# (tests/test_plugin_imports.py).
_bge_tokenizer: Tokenizer | None = None


def _tokenize(text: str) -> list[str]:
    global _bge_tokenizer
    if _bge_tokenizer is None:
        _bge_tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    return _bge_tokenizer.encode(text).tokens

# llama-index falls back to this global (tiktoken by default) wherever it
# counts tokens itself; with ours set, nothing ever loads tiktoken, which
# downloads its encoding on first use.
set_global_tokenizer(_tokenize)

_splitter = SentenceSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, tokenizer=_tokenize)


def _with_ref_doc(node: TextNode, canvas_item_id: str) -> TextNode:
    """Sets the node's ref_doc_id to canvas_item_id, which is what
    both the LanceDB integration's doc_id column and delete_ref_doc rely on."""
    node.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(node_id=canvas_item_id)
    return node


def _metadata(source: str, item_type: str, *, page=None, slide=None, timestamp=None) -> dict:
    """Every node carries the same metadata keys, most of them None: see
    TABLE_SCHEMA. "timestamp" is never set any more but stays in the
    schema, so tables built before it was retired still accept inserts."""
    return {"source": source, "item_type": item_type, "page": page, "slide": slide, "timestamp": timestamp}


def pages_to_nodes(pages: list[dict], source: str, canvas_item_id: str, item_type: str = "file") -> list[TextNode]:
    """One node per page, split further only if the page's prose actually
    exceeds the target chunk size — most extracted pages don't.

    item_type is "file" for PDFs; course_sync.py passes other types for
    Canvas content that isn't paginated, whose pages carry page=None."""
    nodes = []
    for page in pages:
        text = page["text"]
        if not text.strip():
            continue
        for chunk in _splitter.split_text(text):
            node = TextNode(text=chunk, metadata=_metadata(source, item_type, page=page["page"]))
            nodes.append(_with_ref_doc(node, canvas_item_id))
    return nodes


def sections_to_nodes(sections: list[dict], source: str, canvas_item_id: str, item_type: str) -> list[TextNode]:
    """Canvas HTML sections (ingestion.extract_html_sections) -> nodes, one
    section at a time so a chunk never mixes sections (a syllabus's grading
    table no longer shares a chunk with its staff list).

    Every chunk starts with a "Syllabus › Grading Algorithm" line, so both
    the embedding and Claude see what the chunk is part of even when
    the section's own text is just "12.5% | Project" rows. A heading with no
    text of its own ("Class Schedule" directly followed by another bold
    line) becomes a line of the next section instead of a near-empty chunk."""
    nodes = []
    pending: list[str] = []
    for section in sections:
        if not section["text"]:
            if section["heading"]:
                pending.append(section["heading"])
            continue
        path = f"{source} › {section['heading']}" if section["heading"] else source
        body = "\n".join([*pending, section["text"]])
        pending = []
        for chunk in _splitter.split_text(body):
            node = TextNode(text=f"{path}\n{chunk}", metadata=_metadata(source, item_type))
            nodes.append(_with_ref_doc(node, canvas_item_id))
    if pending:
        node = TextNode(text=f"{source}\n" + "\n".join(pending), metadata=_metadata(source, item_type))
        nodes.append(_with_ref_doc(node, canvas_item_id))
    return nodes


def slides_to_nodes(slides: list[dict], source: str, canvas_item_id: str) -> list[TextNode]:
    """One node per slide, not split — a slide is a citation unit
    ("slide 12"), not something to fragment across chunks."""
    nodes = []
    for slide in slides:
        text = slide["text"]
        if not text.strip():
            continue
        node = TextNode(text=text, metadata=_metadata(source, "file", slide=slide["slide"]))
        nodes.append(_with_ref_doc(node, canvas_item_id))
    return nodes


# Every course table's layout, stated up front instead of inferred. LanceDB
# otherwise infers column types from the first batch written, and a column
# that's None in every node of that batch becomes type null for good, so
# whichever material reached a course first decided what it could ever
# hold (a course whose first batch had no page numbers rejected every PDF).
# The metadata fields are _metadata()'s keys plus the ones llama-index's
# node_to_metadata_dict adds, in its order.
_METADATA_TYPE = pa.struct(
    [
        ("source", pa.string()),
        ("item_type", pa.string()),
        ("page", pa.int64()),
        ("slide", pa.int64()),
        ("timestamp", pa.string()),
        ("_node_content", pa.string()),
        ("_node_type", pa.string()),
        ("document_id", pa.string()),
        ("doc_id", pa.string()),
        ("ref_doc_id", pa.string()),
    ]
)
TABLE_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("doc_id", pa.string()),
        ("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
        ("text", pa.string()),
        ("metadata", _METADATA_TYPE),
    ]
)



class _PatchedLanceDBVectorStore(LanceDBVectorStore):
    """Two changes to llama-index's LanceDB store.

    delete(): upstream (still in 0.7.0) builds `doc_id = "x"` with double
    quotes, which LanceDB's SQL parses as a column name, so every delete
    fails. This uses LanceDB's col/lit expressions instead. Remove once
    upstream fixes it.

    add(): creates a missing table with TABLE_SCHEMA rather than letting
    the base class infer one from the first batch."""

    def delete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        self.table.delete(col(self.doc_id_key) == lit(ref_doc_id))

    def add(self, nodes, **add_kwargs: Any) -> list[str]:
        if self._table is None and nodes:
            self._table = self._connection.create_table(self._table_name, schema=TABLE_SCHEMA, mode=self.mode)
        return super().add(nodes, **add_kwargs)


def build_index(nodes: list[TextNode], db_path: Path, table_name: str) -> VectorStoreIndex:
    vector_store = _PatchedLanceDBVectorStore(uri=str(db_path), table_name=table_name)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    # Explicit transformations: the default is a tiktoken SentenceSplitter.
    index = VectorStoreIndex(
        nodes, storage_context=storage_context, embed_model=OnnxBgeEmbedding(), transformations=[_splitter]
    )
    ensure_fts_index(vector_store.table)
    return index


def load_index(db_path: Path, table_name: str) -> VectorStoreIndex:
    """Opens an existing table without adding nodes."""
    vector_store = _PatchedLanceDBVectorStore(uri=str(db_path), table_name=table_name)
    return VectorStoreIndex.from_vector_store(
        vector_store, embed_model=OnnxBgeEmbedding(), transformations=[_splitter]
    )


def index_exists(db_path: Path, table_name: str) -> bool:
    if not db_path.exists():
        return False
    return table_name in lancedb.connect(str(db_path)).list_tables().tables


def ensure_fts_index(table) -> None:
    """BM25 full-text index for HybridRetriever (generation.py), which
    exact names and keywords need. Built only when missing: LanceDB keeps it
    on the table. Same settings and name (text_idx) as the deprecated
    create_fts_index("text"), so indexes built before still match."""
    if any(getattr(i, "index_type", None) == "FTS" for i in table.list_indices()):
        return
    table.create_index("text", config=FTS(), replace=True)


def add_nodes(nodes: list[TextNode], db_path: Path, table_name: str) -> None:
    """Adds to a course's table, creating it on the first write."""
    if index_exists(db_path, table_name):
        index = load_index(db_path, table_name)
        index.insert_nodes(nodes)
        ensure_fts_index(index.vector_store.table)
    else:
        build_index(nodes, db_path, table_name)


def delete_ref_doc_nodes(db_path: Path, table_name: str, ref_doc_id: str) -> None:
    """Removes every chunk of one Canvas item (a changed or deleted file)."""
    if not index_exists(db_path, table_name):
        return
    _PatchedLanceDBVectorStore(uri=str(db_path), table_name=table_name).delete(ref_doc_id)
