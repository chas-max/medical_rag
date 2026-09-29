import math
import sys
from types import SimpleNamespace
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


def test_embedder_uses_cached_model_without_network(monkeypatch):
    class SentenceTransformer:
        kwargs = None

        def __init__(self, model_name, **kwargs):
            SentenceTransformer.kwargs = kwargs

        def encode(self, texts, normalize_embeddings):
            return [[1.0] + [0.0] * 511 for _ in texts]

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=SentenceTransformer))
    BgeEmbedder().encode(["测试"])

    assert SentenceTransformer.kwargs["local_files_only"] is True


def test_reranker_normalizes_logits_to_probability():
    class CrossEncoder:
        def predict(self, pairs):
            assert pairs == [["问题", "证据甲"], ["问题", "证据乙"]]
            return [0.0, 2.0]

    scores = BgeReranker(model=CrossEncoder()).score("问题", ["证据甲", "证据乙"])
    assert scores[0] == 0.5
    assert 0.8 < scores[1] < 0.9


def test_reranker_falls_back_when_cross_encoder_is_unavailable():
    class Unavailable:
        def predict(self, pairs):
            raise OSError("模型文件不可用")

    reranker = BgeReranker(model=Unavailable())
    scores = reranker.score("高血压管理", ["高血压患者应规律监测血压", "今日天气晴朗"])

    assert len(scores) == 2
    assert scores[0] > scores[1]
    assert all(0.0 <= score <= 1.0 for score in scores)
    assert scores[0] >= 0.5
    assert scores[1] < 0.5


def test_reranker_caches_lazy_load_failure(monkeypatch):
    class CrossEncoder:
        calls = 0
        kwargs = None

        def __init__(self, model_name, **kwargs):
            CrossEncoder.calls += 1
            CrossEncoder.kwargs = kwargs
            raise OSError(f"无法下载 {model_name}")

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=CrossEncoder))
    reranker = BgeReranker()

    first = reranker.score("高血压管理", ["高血压患者应规律监测血压"])
    second = reranker.score("高血压管理", ["高血压患者应规律监测血压"])

    assert first == second
    assert CrossEncoder.calls == 1
    assert CrossEncoder.kwargs["local_files_only"] is True
    assert CrossEncoder.kwargs["max_length"] == 512
    assert CrossEncoder.kwargs["model_kwargs"] == {"torch_dtype": "bfloat16"}
