import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.embeddings import BgeEmbedder, BgeReranker


class Encoder:
    def encode(self, texts, normalize_embeddings):
        assert normalize_embeddings is True
        return [[1.0] + [0.0] * 511 for _ in texts]


def test_bge_embedding_has_normalized_512_dimensions():
    vectors = BgeEmbedder(model=Encoder()).encode(["高血压", "糖尿病"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 512


def test_embedding_rejects_nonfinite_values():
    class Invalid:
        def encode(self, texts, normalize_embeddings):
            return [[math.nan] * 512]

    with pytest.raises(ValueError, match="向量"):
        BgeEmbedder(model=Invalid()).encode(["测试"])


def test_reranker_normalizes_logits_to_probability():
    class CrossEncoder:
        def predict(self, pairs):
            assert pairs == [["问题", "证据甲"], ["问题", "证据乙"]]
            return [0.0, 2.0]

    scores = BgeReranker(model=CrossEncoder()).score("问题", ["证据甲", "证据乙"])
    assert scores[0] == 0.5
    assert 0.8 < scores[1] < 0.9
