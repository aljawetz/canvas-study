"""session_note.py and scripts/session-start: at session start, Claude is
told which courses the student has indexed, so a concept question about one
of them reaches the plugin instead of being answered from general knowledge."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import session_note

HOOK = Path(__file__).resolve().parent.parent / "plugin" / "scripts" / "session-start"


def _hook(home: Path) -> str:
    """Runs the hook the way Claude Code does: through sh (Git for Windows' sh
    on Windows, which can't execute a script file directly)."""
    path = os.environ["PATH"] if os.name == "nt" else "/usr/bin:/bin"
    return subprocess.run([shutil.which("sh", path=path) or "sh", str(HOOK)], env={"CANVAS_CONTEXT_HOME": str(home), "PATH": path},
                          capture_output=True, text=True, check=True).stdout


def test_the_note_names_indexed_courses_only(tmp_path):
    session_note.remember_names(tmp_path, {1: "Software Testing", 2: "Requirements", 3: "Orientation"})
    session_note.write_note(tmp_path, [1, 2])

    note = json.loads((tmp_path / session_note.NOTE_FILE).read_text())
    text = note["hookSpecificOutput"]["additionalContext"]
    assert note["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Software Testing" in text and "Requirements" in text and "Orientation" not in text
    assert "canvas-context:answering-course-questions" in text
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


def test_the_hook_prints_the_note_once_a_course_is_indexed(tmp_path):
    (tmp_path / "index.db").touch()
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


def test_both_notes_say_reconnect_not_restart(tmp_path):
    """After a failed start, new sessions skip a plugin's server for ~15 minutes
    while /mcp reconnect all works at once (checked in a real session). Without
    this line, Claude told the student to restart in 6 of 6 runs."""
    session_note.remember_names(tmp_path / "set-up", {1: "Software Testing"})
    (tmp_path / "set-up" / "index.db").touch()
    session_note.write_note(tmp_path / "set-up", [1])
    for home in (tmp_path / "set-up", tmp_path / "new"):
        home.mkdir(exist_ok=True)
        context = json.loads(_hook(home))["hookSpecificOutput"]["additionalContext"]
        assert "/mcp reconnect all" in context and "not" in context.split("/mcp reconnect all")[1][:40]
