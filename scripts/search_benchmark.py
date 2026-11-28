"""Scores search_course_material on a question set (scripts/make_search_questions.py).

For each question it runs the real search path (the retriever the MCP server
builds) and, for comparison, its two halves alone: vector search with the
similarity cutoff, and BM25 full-text search. A hit is a result whose
citation label is one of the question's accepted labels. Reports hit@1,
hit@5, MRR@10 and search latency, overall and by item type.

    uv run python scripts/search_benchmark.py --home <plugin data folder> --questions ~/.canvas-context-bench/questions.jsonl
"""

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin" / "server"))

K = 10


def searchers(home: Path, course_id: int):
    """mode -> query -> ranked citation labels, all on one table, with the server's own search."""
    import mcp_server
    import retrieval
    import store

    searcher = mcp_server.make_searcher(store.CourseIndex(home / "index.db", f"course_{course_id}"),
                                        mcp_server._query_embedder())

    def labels(hits):
        return [retrieval.citation(h)["label"] for h in hits[:K]]

    return {"hybrid": lambda q: labels(searcher.search(q)), "vector": lambda q: labels(searcher.vector(q)),
            "fts": lambda q: labels(searcher.keyword(q))}


def score(labels: list[str], expected: list[str]) -> tuple[int, int, float]:
    rank = next((i for i, label in enumerate(labels, start=1) if label in expected), None)
    return int(rank == 1), int(rank is not None and rank <= 5), (1 / rank if rank else 0.0)


def compare(before: dict, after: dict) -> str:
    """Question by question: which got better or worse at rank 5 and at rank 1."""
    def top(rank, n):
        return rank is not None and rank <= n
    common = sorted(set(before) & set(after))
    lines = [f"paired over {len(common)} questions (before -> after):"]
    for n in (1, 5):
        won = [q for q in common if top(after[q], n) and not top(before[q], n)]
        lost = [q for q in common if top(before[q], n) and not top(after[q], n)]
        lines.append(f"  hit@{n}: {len(won)} gained, {len(lost)} lost" + (f"  lost: {', '.join(lost)}" if lost else ""))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", type=Path, required=True)
    ap.add_argument("--questions", type=Path, required=True)
    ap.add_argument("--modes", default="hybrid,vector,fts")
    ap.add_argument("--misses", action="store_true", help="print the hybrid misses")
    ap.add_argument("--save", type=Path, help="write each question's hybrid rank to this JSON file")
    ap.add_argument("--compare", type=Path, help="a --save file from another version to compare against, question by question")
    a = ap.parse_args()
    questions = [json.loads(line) for line in a.questions.read_text().splitlines() if line.strip()]
    modes = a.modes.split(",")
    by_course = defaultdict(list)
    for q in questions:
        by_course[q["course_id"]].append(q)

    results = {m: [] for m in modes}  # (type, hit1, hit5, rr)
    ranks = {}  # qid -> hybrid rank (None: not in the top K)
    latency = []
    misses = []
    for course_id, qs in sorted(by_course.items()):
        run = searchers(a.home, course_id)
        run["hybrid"]("warm up")  # the first query loads the embedding model
        for q in qs:
            for m in modes:
                start = time.perf_counter()
                labels = run[m](q["question"])
                if m == "hybrid":
                    latency.append(time.perf_counter() - start)
                s = score(labels, q["expected"])
                results[m].append((q["type"], *s))
                if m == "hybrid":
                    ranks[q["qid"]] = round(1 / s[2]) if s[2] else None
                    if not s[1]:
                        misses.append((q, labels[:5]))

    def line(rows):
        n = len(rows)
        return (f"n={n:3}  hit@1 {sum(r[1] for r in rows) / n:5.1%}  hit@5 {sum(r[2] for r in rows) / n:5.1%}  "
                f"MRR@10 {sum(r[3] for r in rows) / n:.3f}")

    for m in modes:
        print(f"{m:7} all    {line(results[m])}")
    print("\nhybrid by item type:")
    types = sorted({r[0] for r in results.get("hybrid", [])})
    for t in types:
        print(f"  {t:13} {line([r for r in results['hybrid'] if r[0] == t])}")
    if latency:
        print(f"\nhybrid latency: median {statistics.median(latency) * 1000:.0f} ms, "
              f"p90 {sorted(latency)[int(len(latency) * 0.9)] * 1000:.0f} ms")
    if a.save:
        a.save.write_text(json.dumps(ranks, indent=1))
    if a.compare:
        print("\n" + compare(json.loads(a.compare.read_text()), ranks))
    if a.misses:
        for q, labels in misses:
            print(f"\nMISS [{q['type']}] {q['question']}\n  expected {q['expected']}\n  got      {labels}")


if __name__ == "__main__":
    main()
