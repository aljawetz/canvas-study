"""Writes plugin/evals/mocks/courses/_tools.json from the real server: the
tool list the evals' mock server shows Claude. Run it after changing a tool's
name, arguments or description; a test fails while the file is out of date.

    uv run python scripts/export_tool_mocks.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "plugin" / "evals" / "mocks" / "courses" / "_tools.json"
sys.path.insert(0, str(ROOT / "plugin" / "server"))


def tools_json() -> str:
    import anyio
    from mcp import Client

    import mcp_server

    async def listing():
        async with Client(mcp_server.build_server(Path("/nonexistent"), courses_for=list)) as client:
            return await client.list_tools()

    dump = anyio.run(listing).model_dump(mode="json", by_alias=True, exclude_none=True)
    return json.dumps(dump, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    OUT.write_text(tools_json())
    print(f"wrote {OUT.relative_to(ROOT)}")
