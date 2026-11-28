"""The package version for hatchling (pyproject.toml [tool.hatch.version]):
the release number plus a short hash of the server code and pyproject.toml, so every code change
is a new version and uvx builds a fresh env for it."""

import hashlib
from pathlib import Path

RELEASE = "0.4.6"

_here = Path(__file__).resolve().parent
_digest = hashlib.sha256()
for _path in sorted([*_here.glob("*.py"), _here / "pyproject.toml"]):
    if _path.name != "version.py":
        _digest.update(_path.name.encode() + b"\0" + _path.read_bytes())
__version__ = f"{RELEASE}+{_digest.hexdigest()[:10]}"
