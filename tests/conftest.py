import os
from pathlib import Path

# The tests chunk and embed with the real bge-small model, fetched the way the
# plugin fetches it (pinned, sha256-checked) into this repo's models/ folder.
# Set before any plugin module is imported: they read SM_MODELS_DIR at import.
os.environ.setdefault("SM_MODELS_DIR", str(Path(__file__).resolve().parent.parent / "models"))

import model_download  # noqa: E402

model_download.ensure_embedding_model()
