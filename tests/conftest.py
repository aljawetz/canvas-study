import os
from pathlib import Path

# The tests chunk and embed with the real bge-small model, fetched the way the
# plugin fetches it (pinned, sha256-checked) into this repo's models/ folder.
# Set before any plugin module is imported: they read SM_MODELS_DIR at import.
os.environ.setdefault("SM_MODELS_DIR", str(Path(__file__).resolve().parent.parent / "models"))

import model_download  # noqa: E402

model_download.ensure_embedding_model()

import pytest  # noqa: E402

import reranker


@pytest.fixture(autouse=True)
def _no_default_reranker(monkeypatch, request):
    """Tests get rank fusion only unless they pass a reranker in, so the
    result does not depend on whether this machine has the reranker model
    (CI does not). Tests marked real_reranker load the real one."""
    if "real_reranker" not in request.keywords:
        monkeypatch.setattr(reranker, "load_reranker", lambda *args, **kwargs: None)
