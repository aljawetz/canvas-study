"""The search benchmark's scoring. A wrong score would make every Phase 3
search decision wrong, so the rules are pinned here."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bench = _load("search_benchmark")
make = _load("make_search_questions")


def test_score_uses_the_first_accepted_label():
    labels = ["a · p.1", "b · p.2", "c · p.3", "b · p.2"]
    assert bench.score(labels, ["b · p.2"]) == (0, 1, 0.5)
    assert bench.score(labels, ["a · p.1"]) == (1, 1, 1.0)
    assert bench.score(["x"] * 5 + ["b · p.2"], ["b · p.2"]) == (0, 0, 1 / 6)  # rank 6: in MRR@10, not hit@5
    assert bench.score(labels, ["z"]) == (0, 0, 0.0)
    # a near-duplicate page is accepted too: finding the copy counts
    assert bench.score(labels, ["z", "c · p.3"]) == (0, 1, 1 / 3)


def test_a_near_duplicate_page_is_an_accepted_answer():
    words = "mocks record calls so the test can verify the interaction afterwards with stubs " * 3
    target = {"text": words, "label": "03 Unit Testing.pdf · p.4"}
    rows = [target,
            {"text": words + "note", "label": "03 Unit Testing - note taking version.pdf · p.4"},
            {"text": "late labs lose ten percent per day for up to three days", "label": "syllabus · p.5"}]
    assert make.accepted_labels(target, rows) == ["03 Unit Testing - note taking version.pdf · p.4",
                                                   "03 Unit Testing.pdf · p.4"]


def test_sampling_skips_fragments_and_spreads_across_item_types():
    import random

    rows = ([{"text": "x" * 400, "type": "file", "label": f"f{i}"} for i in range(50)]
            + [{"text": "x" * 400, "type": "syllabus", "label": "s"}, {"text": "short", "type": "quiz", "label": "q"}])
    picked = make.sample(rows, 6, random.Random(1))
    assert len(picked) == 6
    assert {r["type"] for r in picked} == {"file", "syllabus"}  # the 5-character quiz is a fragment


def test_compare_counts_gains_and_losses_per_question():
    before = {"q1": 1, "q2": 3, "q3": None, "q4": 6}
    after = {"q1": 2, "q2": None, "q3": 4, "q4": 1}
    out = bench.compare(before, after)
    assert "hit@1: 1 gained, 1 lost  lost: q1" in out
    assert "hit@5: 2 gained, 1 lost  lost: q2" in out
