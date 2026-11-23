"""The note the SessionStart hook (scripts/session-start) hands Claude: which
courses this student has indexed.

Without it, "In Software Testing, what's a mock?" reads to Claude like a
general question, and smaller models answer it from general knowledge
without touching the plugin. Naming the student's courses at session start
is what ties the question to their Canvas.

Courses change about once a semester, so the note is rewritten only when
something can have changed: when list_courses reads names from Canvas, and
when a sync finishes. The hook only prints the file (it can't call Canvas,
and JSON is escaped here rather than in shell).
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("canvas-study")

NAMES_FILE = "course_names.json"
NOTE_FILE = "session_start.json"


def _names(home: Path) -> dict[str, str]:
    try:
        return json.loads((home / NAMES_FILE).read_text())
    except (OSError, ValueError):
        return {}


def remember_names(home: Path, names: dict[int, str]) -> None:
    """Course names as Canvas last gave them, kept for the note."""
    merged = {**_names(home), **{str(cid): name for cid, name in names.items() if name}}
    _write(home / NAMES_FILE, json.dumps(merged, ensure_ascii=False))


def note_text(names: list[str]) -> str:
    listed = "; ".join(names)
    return (
        f"The student's courses indexed by Canvas Study: {listed}. A question about one of these courses, "
        "including a concept or topic it teaches, is a question about their Canvas course: use the "
        "canvas-study:ask skill and answer from their course material, not from general knowledge."
    )


def write_note(home: Path, indexed_ids: list[int]) -> None:
    """The hook's output for these indexed courses; no note when none has a known name."""
    names = _names(home)
    known = [names[str(cid)] for cid in indexed_ids if names.get(str(cid))]
    path = home / NOTE_FILE
    if not known:
        path.unlink(missing_ok=True)
        return
    note = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note_text(known)}}
    _write(path, json.dumps(note, ensure_ascii=False))


def _write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(path)
    except OSError as e:  # the note is a hint: never fail a tool call over it
        logger.warning("could not write %s: %s", path.name, e)
