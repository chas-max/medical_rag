import math
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag import embeddings
from medical_rag.embeddings import BgeEmbedder, BgeReranker, resolve_device
from medical_rag.types import SCORE_LEXICAL, SCORE_RERANK


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
    assert SentenceTransformer.kwargs["device"] == resolve_device()


def test_device_prefers_cuda_when_available(monkeypatch):
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert resolve_device() == "cuda"


def test_device_falls_back_to_cpu_without_cuda(monkeypatch):
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert resolve_device() == "cpu"


def test_device_env_override_wins_over_detection(monkeypatch):
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setenv("RAG_MODEL_DEVICE", "cpu")
    assert resolve_device() == "cpu"


def test_reranker_normalizes_logits_to_probability():
    class CrossEncoder:
        def predict(self, pairs):
            assert pairs == [["问题", "证据甲"], ["问题", "证据乙"]]
            return [0.0, 2.0]

    scores = BgeReranker(model=CrossEncoder()).score("问题", ["证据甲", "证据乙"])
    assert scores[0] == 0.5
    assert 0.8 < scores[1] < 0.9


def test_reranker_skips_duplicate_sigmoid_when_model_has_sigmoid_activation():
    class Sigmoid:
        pass

    class ProbCrossEncoder:
        activation_fn = Sigmoid()

        def predict(self, pairs):
            return [0.9975, 0.0001]

    scores, kind = BgeReranker(model=ProbCrossEncoder()).score_with_kind(
        "问题", ["强相关证据", "无关文本"]
    )
    assert kind == SCORE_RERANK
    assert scores == [0.9975, 0.0001]


def test_reranker_falls_back_when_cross_encoder_is_unavailable():
    class Unavailable:
        def predict(self, pairs):
            raise OSError("模型文件不可用")

    reranker = BgeReranker(model=Unavailable())
    scores, kind = reranker.score_with_kind("高血压管理", ["高血压患者应规律监测血压", "今日天气晴朗"])

    assert kind == SCORE_LEXICAL
    assert len(scores) == 2
    assert scores[0] > scores[1]
    assert all(0.0 <= score <= 1.0 for score in scores)
    assert scores[0] >= 0.6
    assert scores[1] == 0.0


def test_reranker_retries_after_the_cooldown_window(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(embeddings, "monotonic", lambda: now[0])

    class Flaky:
        calls = 0

        def predict(self, pairs):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise OSError("临时 CUDA 错误")
            return [2.0 for _ in pairs]

    reranker = BgeReranker(model=Flaky())
    first, first_kind = reranker.score_with_kind("高血压管理", ["高血压患者应规律监测血压"])

    assert first_kind == SCORE_LEXICAL
    assert reranker.available is False
    assert reranker.status()["reason"] == "reranker_unavailable"

    now[0] = embeddings.RERANKER_RETRY_SECONDS + 1
    second, second_kind = reranker.score_with_kind("高血压管理", ["高血压患者应规律监测血压"])

    assert second_kind == SCORE_RERANK
    assert 0.8 < second[0] < 0.9
    assert reranker.available is True
    assert Flaky.calls == 2


def test_reranker_caches_lazy_load_failure(monkeypatch):
    # Pin CPU detection so the failure path is a single attempt; the CUDA-then-CPU
    # retry is covered separately in test_reranker_retries_on_cpu_when_gpu_load_fails.
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))

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
    assert CrossEncoder.kwargs["device"] == "cpu"


def test_reranker_retries_on_cpu_when_gpu_load_fails(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True)))

    devices = []

    class CrossEncoder:
        def __init__(self, model_name, **kwargs):
            devices.append(kwargs["device"])
            if kwargs["device"] != "cpu":
                raise RuntimeError("CUDA out of memory")

        def predict(self, pairs):
            return [0.0 for _ in pairs]

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=CrossEncoder))
    scores = BgeReranker().score("高血压管理", ["高血压患者应规律监测血压"])

    assert devices == ["cuda", "cpu"]
    assert scores == [0.5]
