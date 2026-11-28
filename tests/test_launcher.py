"""plugin/scripts/server, the sh launcher: where it tells the server to keep
the search model. A fake uvx on PATH stands in for the real one and reports
the environment it was started with."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "plugin" / "scripts" / "server"
SETUP_SKILL = ROOT / "plugin" / "skills" / "setup" / "SKILL.md"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake uvx is a POSIX script")


def _run(tmp_path, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uvx = bin_dir / "uvx"
    uvx.write_text('#!/bin/sh\necho "SM_MODELS_DIR=$SM_MODELS_DIR"\n')
    uvx.chmod(uvx.stat().st_mode | stat.S_IEXEC)
    base = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    out = subprocess.run(["sh", str(LAUNCHER), "--version"], env={**base, **env}, capture_output=True, text=True,
                         check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_the_model_goes_in_the_plugins_data_folder_when_nothing_else_says(tmp_path):
    """Claude's shell, where setup downloads the model, has CLAUDE_PLUGIN_DATA
    (passed by the skill) but not .mcp.json's SM_MODELS_DIR. Before this, the
    model landed inside uv's package cache and the server downloaded it again."""
    data = tmp_path / "data"
    assert _run(tmp_path, CLAUDE_PLUGIN_DATA=str(data))["SM_MODELS_DIR"] == f"{data}/models"


def test_an_explicit_models_folder_wins(tmp_path):
    assert _run(tmp_path, CLAUDE_PLUGIN_DATA=str(tmp_path / "data"),
                SM_MODELS_DIR="/somewhere/else")["SM_MODELS_DIR"] == "/somewhere/else"


def test_setup_passes_the_plugins_data_folder_to_its_download():
    """Claude Code fills ${CLAUDE_PLUGIN_DATA} into skill text (checked on a
    fresh install), but Claude's shell doesn't have it as a variable."""
    assert 'CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/scripts/server" --download-model' \
        in SETUP_SKILL.read_text()
