"""session_note.py and scripts/session-start: at session start, Claude is
told which courses the student has indexed, so a concept question about one
of them reaches the plugin instead of being answered from general knowledge."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import session_note

HOOK = Path(__file__).resolve().parent.parent / "plugin" / "scripts" / "session-start"


def _hook(home: Path) -> str:
    """Runs the hook the way Claude Code does: through sh (Git for Windows' sh
    on Windows, which can't execute a script file directly)."""
    path = os.environ["PATH"] if os.name == "nt" else "/usr/bin:/bin"
    return subprocess.run([shutil.which("sh", path=path) or "sh", str(HOOK)], env={"SM_HOME": str(home), "PATH": path},
                          capture_output=True, text=True, check=True).stdout


def test_the_note_names_indexed_courses_only(tmp_path):
    session_note.remember_names(tmp_path, {1: "Software Testing", 2: "Requirements", 3: "Orientation"})
    session_note.write_note(tmp_path, [1, 2])

    note = json.loads((tmp_path / session_note.NOTE_FILE).read_text())
    text = note["hookSpecificOutput"]["additionalContext"]
    assert note["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Software Testing" in text and "Requirements" in text and "Orientation" not in text
    assert "canvas-study:answering-course-questions" in text
    assert "systemMessage" not in note  # a hint for Claude, not a banner shown to the student every session


def test_names_are_merged_and_survive_a_partial_listing(tmp_path):
    session_note.remember_names(tmp_path, {1: "Software Testing"})
    session_note.remember_names(tmp_path, {2: "Requirements"})  # Canvas offline: only some names came back
    session_note.write_note(tmp_path, [1, 2])
    assert "Software Testing; Requirements" in (tmp_path / session_note.NOTE_FILE).read_text()


def test_no_known_name_means_no_note(tmp_path):
    session_note.remember_names(tmp_path, {1: "Software Testing"})
    session_note.write_note(tmp_path, [1])
    session_note.write_note(tmp_path, [9])  # the only indexed course has no name on record
    assert not (tmp_path / session_note.NOTE_FILE).exists()


@pytest.mark.parametrize("index", ["index.db", "index.lancedb"])  # 0.3.5's index counts until it is rebuilt
def test_the_hook_prints_the_note_once_a_course_is_indexed(tmp_path, index):
    (tmp_path / index).mkdir() if index.endswith("lancedb") else (tmp_path / index).touch()
    session_note.remember_names(tmp_path, {1: "Software Testing \"ST\""})  # quotes must stay valid JSON
    session_note.write_note(tmp_path, [1])

    out = json.loads(_hook(tmp_path))
    assert 'Software Testing "ST"' in out["hookSpecificOutput"]["additionalContext"]


def test_the_hook_says_nothing_when_indexed_but_without_a_note(tmp_path):
    (tmp_path / "index.db").touch()
    assert _hook(tmp_path) == ""


def test_the_hook_points_to_setup_before_anything_is_indexed(tmp_path):
    out = json.loads(_hook(tmp_path))
    assert "isn't set up yet" in out["systemMessage"]
