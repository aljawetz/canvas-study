"""The ONNX bge-small embedding (conftest.py downloads the model).

The stronger check (ONNX output matches the sentence-transformers/torch
reference, cosine similarity ~1.0) was done once by hand during development,
not here: re-deriving it in every test run would bring torch back as a
dependency, which is what this module exists to avoid.
"""

import numpy as np
import pytest

from embeddings import EMBEDDING_DIM, OnnxBgeEmbedding


@pytest.fixture(scope="module")
def embedding():
    return OnnxBgeEmbedding()


def test_embedding_has_the_model_width_and_unit_length(embedding):
    vec = embedding.embed("This is a test sentence about retrieval systems.")
    assert len(vec) == EMBEDDING_DIM
    assert np.isclose(np.linalg.norm(vec), 1.0, atol=1e-5)


def test_related_text_scores_higher_than_unrelated(embedding):
    q = embedding.embed("What is a mock object in unit testing?")
    near = embedding.embed("Mocks record the calls they receive so a test can verify the interaction.")
    far = embedding.embed("The cafeteria opens at seven in the morning.")
    assert np.dot(q, near) > np.dot(q, far) + 0.1


def test_text_past_the_model_window_is_truncated_not_an_error(embedding):
    assert len(embedding.embed("word " * 2000)) == EMBEDDING_DIM
