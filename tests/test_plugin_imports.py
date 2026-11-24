"""The server must start on a student's machine before setup has downloaded
the models, without an LLM SDK (no tool calls a model; Claude Code is the
LLM) and without llama-index, which it no longer uses. Runs the imports in a
fresh subprocess with those blocked.
"""

import os
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "plugin" / "server"

BLOCKED = ["openai", "llama_index"]

PROBE = f"""
import importlib.abc, sys
BLOCKED = {BLOCKED!r}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == b or name.startswith(b + ".") for b in BLOCKED):
            raise ImportError(f"server imported blocked module {{name}}")
sys.meta_path.insert(0, Block())
import mcp_server, course_sync, course_items, retrieval, indexing, assignments, citation_links, model_download, sync_jobs
mcp_server.build_server()
print("ok")
"""


def test_server_starts_without_models_or_llm_packages(tmp_path):
    # An empty models folder, as on a student's machine before setup downloads
    # the models: the server must still start (and say what's missing later).
    env = {**os.environ, "SM_MODELS_DIR": str(tmp_path / "models"), "SM_HOME": str(tmp_path / "home")}
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=SERVER, capture_output=True, text=True, timeout=120, env=env)

    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip().endswith("ok")
