"""Claude Code only updates an installed plugin when plugin.json's version
changes, so a release must bump it; the server's own release number must
say the same thing."""

import json
from pathlib import Path

import version

PLUGIN_JSON = Path(__file__).resolve().parent.parent / "plugin" / ".claude-plugin" / "plugin.json"


def test_plugin_and_server_release_versions_match():
    assert json.loads(PLUGIN_JSON.read_text())["version"] == version.RELEASE
