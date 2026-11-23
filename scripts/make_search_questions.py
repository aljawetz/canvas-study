"""Builds a private search benchmark from a student's own index.

Samples passages from each indexed course (stratified by item type, skipping
fragments), asks Claude for the question a student would ask that each one
answers, and writes one JSON line per question with the passage's citation
label as the expected answer. Near-duplicate passages (the same slides posted
twice) are accepted too, so finding the copy isn't scored as a miss.

The output holds real course content: keep it outside the repo.

    uv run python scripts/make_search_questions.py --home <plugin data folder> --out ~/.canvas-study-bench/questions.jsonl
"""

import argparse
import json
import random
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin" / "server"))

MIN_CHARS = 300
BATCH = 10
PROMPT = """Each passage below comes from a university course's Canvas materials. For each one, write the question a \
student in that course would naturally ask that this passage answers. Paraphrase: don't copy the passage's distinctive \
wording or rare phrases, and don't mention "the passage", "the slide" or page numbers. If a passage has nothing a \
student would ask about (a title slide, a table of contents, boilerplate), give null.

{style}

Reply with only a JSON object mapping each passage id to its question string or null.

{passages}"""


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text.lower()))


def load_chunks(home: Path) -> dict[int, list[dict]]:
    import lancedb

    import generation

    db = lancedb.connect(str(home / "index.lancedb"))
    courses = {}
    for name in db.list_tables().tables:
        if not name.startswith("course_"):
            continue
        df = db.open_table(name).to_pandas()
        rows = []
        for _, r in df.iterrows():
            md = dict(r["metadata"])
            label = generation.citation_for(md, md.get("ref_doc_id") or md.get("doc_id") or "")["label"]
            rows.append({"id": r["id"], "text": r["text"], "label": label, "type": md.get("item_type") or "file"})
        courses[int(name.split("_")[1])] = rows
    return courses


def sample(rows: list[dict], n: int, rng: random.Random) -> list[dict]:
    """n passages spread across item types (files dominate, so each type gets a fair share first)."""
    by_type = defaultdict(list)
    for r in rows:
        if len(r["text"]) >= MIN_CHARS:
            by_type[r["type"]].append(r)
    for group in by_type.values():
        rng.shuffle(group)
    picked = []
    while len(picked) < n and any(by_type.values()):
        for group in list(by_type.values()):
            if group and len(picked) < n:
                picked.append(group.pop())
    return picked


def accepted_labels(target: dict, rows: list[dict], threshold: float = 0.8) -> list[str]:
    """The target's label plus labels of passages whose words nearly match it."""
    tw = _words(target["text"])
    labels = {target["label"]}
    for r in rows:
        w = _words(r["text"])
        if tw and w and len(tw & w) / len(tw | w) >= threshold:
            labels.add(r["label"])
    return sorted(labels)


STYLES = {
    "full": "Write it as a full, specific question.",
    "short": "Write it the way a student actually types into a chat: short (4 to 10 words), casual, a bit vague, "
             "using everyday words rather than the passage's terms (a proper name or course term is fine if a "
             "student would know it).",
}


def ask_claude(batch: list[dict], model: str, style: str) -> dict:
    passages = "\n\n".join(f"<{p['qid']}>\n{p['text'][:2500]}\n</{p['qid']}>" for p in batch)
    out = subprocess.run(["claude", "-p", PROMPT.format(passages=passages, style=STYLES[style]), "--model", model, "--output-format", "json",
                          "--max-turns", "1", "--tools", ""], capture_output=True, text=True, timeout=600)
    text = json.loads(out.stdout)["result"]
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--per-course", type=int, default=30)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--style", choices=sorted(STYLES), default="full")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    written = 0
    with a.out.open("w") as f:
        for course, rows in sorted(load_chunks(a.home).items()):
            picked = sample(rows, a.per_course, rng)
            for i, p in enumerate(picked):
                p["qid"] = f"c{course}_{i}"
            for start in range(0, len(picked), BATCH):
                batch = picked[start:start + BATCH]
                questions = ask_claude(batch, a.model, a.style)
                for p in batch:
                    q = questions.get(p["qid"])
                    if not q:
                        continue
                    f.write(json.dumps({"qid": p["qid"], "course_id": course, "question": q, "type": p["type"], "style": a.style,
                                        "expected": accepted_labels(p, rows), "chunk_id": p["id"]}) + "\n")
                    written += 1
            print(f"course {course}: {len(picked)} passages sampled", file=sys.stderr)
    print(f"{written} questions -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
