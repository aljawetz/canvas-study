"""retrieval.py: rank fusion and the hybrid search around it. No index, no
network: a stand-in index, so only the search logic is tested."""

import pytest

import retrieval
from retrieval import Hit, HybridSearcher, reciprocal_rank_fusion


def _h(hit_id, score=0.5):
    return Hit(id=hit_id, text=hit_id, metadata={}, item_id=f"file:{hit_id}", score=score)


def _ids(hits):
    return [h.id for h in hits]


def test_hit_in_both_lists_ranks_first():
    vector = [_h("a"), _h("b"), _h("c")]
    keyword = [_h("c"), _h("d")]
    # c: 1/63 + 1/61, a: 1/61, d: 1/62, b: 1/62. Ties keep first-seen order.
    assert _ids(reciprocal_rank_fusion([vector, keyword], k=60, top_n=5)) == ["c", "a", "b", "d"]


def test_fusion_uses_rank_not_raw_score():
    vector = [_h("a", 0.9), _h("b", 0.8)]
    keyword = [_h("b", 40.0)]
    assert _ids(reciprocal_rank_fusion([vector, keyword])) == ["b", "a"]


def test_duplicates_are_kept_once_with_their_first_score():
    vector = [_h("a", 0.7), _h("a", 0.6)]
    keyword = [_h("a", 12.0)]
    fused = reciprocal_rank_fusion([vector, keyword])
    assert _ids(fused) == ["a"]
    assert fused[0].score == 0.7


def test_top_n_cuts_the_list():
    assert len(reciprocal_rank_fusion([[_h(str(i)) for i in range(20)], []], top_n=5)) == 5


def test_empty_inputs_give_empty_result():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def _row(hit_id, **extra):
    return {"id": hit_id, "text": f"text {hit_id}", "doc_id": f"file:{hit_id}",
            "metadata": {"source": f"{hit_id}.pdf", "page": 1, "item_type": "file"}, **extra}


class _Index:
    """Answers HybridSearcher's two searches from fixed rows: a vector row's
    similarity is 1 - its _distance, a keyword row's score its _score."""

    def __init__(self, vector_rows, fts_rows):
        self._vector, self._fts = vector_rows, fts_rows
        self.limits = {}

    def vector(self, query_vector, limit):
        assert isinstance(query_vector, list)
        self.limits["vector"] = limit
        return [(r, 1.0 - r["_distance"]) for r in self._vector][:limit]

    def keyword(self, query, limit):
        self.limits["fts"] = limit
        return [(r, r["_score"]) for r in self._fts][:limit]


def _searcher(vector_rows, fts_rows, cutoff=0.0):
    return HybridSearcher(_Index(vector_rows, fts_rows), embed=lambda q: [0.1, 0.2], cutoff=cutoff)


def test_vector_scores_are_cosine_similarity_and_the_cutoff_drops_weak_hits():
    s = _searcher([_row("near", _distance=0.2), _row("far", _distance=0.5)], [], cutoff=retrieval.SIMILARITY_CUTOFF)
    hits = s.vector("q")
    assert _ids(hits) == ["near"]
    assert hits[0].score == pytest.approx(0.8)


def test_the_cutoff_is_the_old_exp_l2_threshold_in_cosine_terms():
    # Old rule: keep exp(-squared L2) >= 0.5; for unit vectors squared L2 = 2 - 2 cos.
    import math
    for cos in (0.60, 0.65, 0.66, 0.80):
        assert (math.exp(-(2 - 2 * cos)) >= 0.5) == (cos >= retrieval.SIMILARITY_CUTOFF)


def test_keyword_only_hit_surfaces_with_its_own_score():
    vector = [_row(f"v{i}", _distance=0.1 + i * 0.01) for i in range(8)]
    hits = _searcher(vector, [_row("kw", _score=7.5)]).search("Mockito")
    assert "kw" in _ids(hits) and len(hits) == 5
    assert next(h for h in hits if h.id == "kw").score == 7.5


def test_search_fetches_more_candidates_and_dedupes():
    s = _searcher([_row("a", _distance=0.1), _row("b", _distance=0.2)], [_row("b", _score=3.0), _row("c", _score=2.0)])
    assert _ids(s.search("q")) == ["b", "a", "c"]
    assert s._index.limits == {"vector": retrieval.VECTOR_CANDIDATES, "fts": retrieval.FTS_TOP_K}


def test_hits_carry_text_and_citation_fields():
    hit = _searcher([], [_row("x", _score=1.0)]).search("q")[0]
    assert hit.text == "text x" and hit.item_id == "file:x"
    assert retrieval.citation(hit) == {"source_type": "file", "label": "x.pdf · p.1", "item_id": "file:x"}


def test_no_hits_returns_nothing():
    assert _searcher([], []).search("nothing") == []
