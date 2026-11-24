"""Local, torch-free embeddings.

Wraps the pre-converted ONNX model directly via onnxruntime + tokenizers,
not sentence-transformers (torch) or the huggingface-optimum wrapper
(still pulls torch transitively via optimum, confirmed by installing it). CLS-token
pooling, L2-normalized, no query instruction prefix — verified to match
the sentence-transformers reference bit-for-bit (cosine similarity
~1.0000) for the same input, and sentence-transformers' own packaged
config for this model has empty query/document prompts, so there's
nothing to replicate there either.

Model files aren't in this repo (see .gitignore): model_download.py fetches
them on first use.
"""

import os
from pathlib import Path
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

# SM_MODELS_DIR: where the plugin keeps downloaded models (the installed
# package has no models/ next to it). Unset: the repo's models/, as before.
MODEL_DIR = Path(os.environ.get("SM_MODELS_DIR") or Path(__file__).parent / "models") / "bge-small-en-v1.5-onnx"
EMBEDDING_DIM = 384
MAX_SEQ_LENGTH = 512  # from the model's own config.json (max_position_embeddings)


class OnnxBgeEmbedding:
    """`bge-small-en-v1.5`, ONNX Runtime only — no torch at any point."""

    def __init__(self, model_dir: Path = MODEL_DIR) -> None:
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        # Without this, a chunk that tokenizes past the model's 512-token
        # limit crashes onnxruntime instead of truncating — found by
        # actually indexing real, dense content.
        self._tokenizer.enable_truncation(max_length=MAX_SEQ_LENGTH)
        self._session = ort.InferenceSession(str(model_dir / "model.onnx"))

    def embed(self, text: str) -> list[float]:
        """A unit-length vector. Queries and passages are embedded the same way."""
        enc = self._tokenizer.encode(text)
        input_ids = np.array([enc.ids], dtype=np.int64)
        attention_mask = np.array([enc.attention_mask], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)

        outputs = self._session.run(
            None,
            {"input_ids": input_ids, "attention_mask": attention_mask, "token_type_ids": token_type_ids},
        )
        cls_embedding = outputs[0][:, 0]  # CLS-token pooling, matches this model's reference implementation
        normalized = cls_embedding / np.linalg.norm(cls_embedding, axis=1, keepdims=True)
        return normalized[0].tolist()
