"""Search for search_course_material: vector search and BM25 full-text
search over one course's index (store.py), merged by rank, plus citations.
Claude Code writes the answer; nothing here calls a language model.

Why two searches: vector search finds concepts ("what is a test double")
but is weak on exact names and rare words, which BM25 finds. Their scores
are on different scales (cosine vs corpus-dependent BM25), so they are
never compared: each list is cut on its own terms, then merged by rank with
reciprocal rank fusion. On the private search benchmark (scripts/
search_benchmark.py) the merge beats either half alone by 5 to 7 points of
hit@5 on short student questions.
"""

import math
from dataclasses import dataclass

# Vector hits below this cosine similarity are dropped. It is the cutoff the
# search has always used, written as what it is: the old code kept
# exp(-squared L2 distance) >= 0.5, and for these unit-length vectors
# squared L2 = 2 - 2 cos, so that is cos >= 1 - ln2 / 2 (about 0.653).
# Calibrated on real courses: on-topic matches scored above it, off-topic
# control queries below.
SIMILARITY_CUTOFF = 1 - math.log(2) / 2

# BM25 scores depend on each course's word statistics, so no fixed
# threshold transfers between courses: the top 10 are taken by rank, and a
# weak keyword match only reaches the final results if nothing ranks above it.
FTS_TOP_K = 10

# Both searches fetch more candidates than they return before fusion.
# RRF_K = 60 is the usual value from the original reciprocal rank fusion paper.
VECTOR_CANDIDATES = 20
RRF_K = 60


@dataclass(frozen=True)
class Hit:
    """One chunk found by search."""

    id: str
    text: str
    metadata: dict
    item_id: str  # the Canvas item the chunk came from, e.g. "file:123"
    score: float  # cosine similarity for vector hits, BM25 for keyword hits; only used within its own list


def _hit(row: dict, score: float) -> Hit:
    return Hit(id=row["id"], text=row["text"], metadata=row["metadata"], item_id=row["doc_id"], score=score)


def reciprocal_rank_fusion(ranked_lists: list[list[Hit]], k: int = RRF_K, top_n: int = 5) -> list[Hit]:
    """Merge ranked lists into one. Each hit gets the sum of 1 / (k + rank)
    over the lists it is in (rank starts at 1). A hit in two lists is kept
    once, as its first copy, so its original score is not changed. Ties
    keep the order the hits were first seen in."""
    fused: dict[str, float] = {}
    first: dict[str, Hit] = {}
    for ranked in ranked_lists:
        seen_here: set[str] = set()
        for rank, hit in enumerate(ranked, start=1):
            if hit.id in seen_here:
                continue
            seen_here.add(hit.id)
            fused[hit.id] = fused.get(hit.id, 0.0) + 1.0 / (k + rank)
            first.setdefault(hit.id, hit)
    order = sorted(fused, key=lambda hit_id: -fused[hit_id])
    return [first[hit_id] for hit_id in order[:top_n]]


class HybridSearcher:
    """Search over one course's index. embed turns a query into a unit-length
    vector. The index answers vector(query_vector, limit) with (row, cosine
    similarity) pairs and keyword(query, limit) with (row, BM25 score) pairs,
    best first (store.CourseIndex)."""

    def __init__(self, index, embed, top_k: int = 5, cutoff: float = SIMILARITY_CUTOFF):
        self._index = index
        self._embed = embed
        self._top_k = top_k
        self._cutoff = cutoff

    def vector(self, query: str) -> list[Hit]:
        hits = [_hit(row, score) for row, score in self._index.vector(self._embed(query), VECTOR_CANDIDATES)]
        return [h for h in hits if h.score >= self._cutoff]

    def keyword(self, query: str) -> list[Hit]:
        return [_hit(row, score) for row, score in self._index.keyword(query, FTS_TOP_K)]

    def search(self, query: str) -> list[Hit]:
        return reciprocal_rank_fusion([self.vector(query), self.keyword(query)], top_n=self._top_k)


def location(metadata: dict) -> str:
    """Where in its document a chunk sits: "p.3", "slide 4", or ""."""
    # Every chunk carries both keys, often None, so check the value.
    if metadata.get("page") is not None:
        return f"p.{metadata['page']}"
    if metadata.get("slide") is not None:
        return f"slide {metadata['slide']}"
    return ""


def citation_for(metadata: dict, item_id: str) -> dict:
    """One chunk's citation."""
    source = metadata.get("source") or ""
    where = location(metadata)
    return {
        "source_type": metadata.get("item_type") or "file",
        "label": f"{source} · {where}" if where else source,
        "item_id": item_id,
    }


def citation(hit: Hit) -> dict:
    return citation_for(hit.metadata, hit.item_id)
