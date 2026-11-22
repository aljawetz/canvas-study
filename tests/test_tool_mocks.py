"""The evals' mock server must show Claude the same tools the real server
has, or the evals test a plugin that doesn't exist."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_eval_tool_mocks_match_the_real_server():
    spec = importlib.util.spec_from_file_location("export_tool_mocks", ROOT / "scripts" / "export_tool_mocks.py")
    export = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(export)

    assert export.OUT.read_text() == export.tools_json(), (
        "plugin/evals/mocks/courses/_tools.json is out of date: run `uv run python scripts/export_tool_mocks.py`"
    )
