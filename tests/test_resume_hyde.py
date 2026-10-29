import json
import threading
from pathlib import Path

import pytest

from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.nodes import retrieval


class Model:
    def __init__(self, result):
        self.result = result
        self.timeout = None

    def complete_with_timeout(self, provider, system, user, timeout_seconds):
        self.timeout = timeout_seconds
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def complete(self, provider, system, user):
        return json.dumps({"sections": ["解释", "注意事项"]})


class Embedder:
    def __init__(self):
        self.texts = []

    def encode(self, texts):
        self.texts.extend(texts)
        return [[1.0]]


class Store:
    def search_dense(self, vector, limit):
        assert vector == [1.0]
        return ["real evidence"]


def context():
    return ExecutionContext("run", "medical-answer", {})


def test_hyde_uses_hypothesis_only_for_dense_search():
    model = Model(json.dumps({"hypothetical": "假设文本"}))
    embedder = Embedder()
    result = retrieval.hyde({"question": "原问题"}, {}, context(), Store(), embedder, model, "qwen", 20)
    assert result == {"evidence": ["real evidence"]}
    assert embedder.texts == ["假设文本"]
    assert model.timeout == 10


@pytest.mark.parametrize("reply", ["{}", TimeoutError(), RuntimeError("secret response")])
def test_hyde_failure_returns_no_duplicate_branch(reply):
    ctx = context()
    embedder = Embedder()
    result = retrieval.hyde({"question": "原问题"}, {}, ctx, Store(), embedder, Model(reply), "qwen", 20)
    assert result == {"evidence": []}
    assert embedder.texts == []
    assert ctx.node_metadata == {"degraded": True, "reason": "hyde_unavailable"}


def test_hyde_can_be_disabled_for_comparable_baseline():
    ctx = context()
    embedder = Embedder()
    model = Model(RuntimeError("must not call"))
    result = retrieval.hyde({"question": "原问题"}, {}, ctx, Store(), embedder, model, "qwen", 20,
                            enabled=False)
    assert result == {"evidence": []}
    assert embedder.texts == []
    assert ctx.node_metadata == {"reason": "hyde_disabled"}


def test_hyde_uses_configured_timeout():
    model = Model(json.dumps({"hypothetical": "假设文本"}))
    retrieval.hyde({"question": "原问题"}, {}, context(), Store(), Embedder(), model, "qwen", 20,
                   timeout_seconds=3)
    assert model.timeout == 3


def test_hyde_model_wait_does_not_hold_shared_retrieval_lock():
    lock = threading.RLock()
    started, release = threading.Event(), threading.Event()
    errors = []

    class SlowModel(Model):
        def complete_with_timeout(self, provider, system, user, timeout_seconds):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            return json.dumps({"hypothetical": "假设文本"})

    def run():
        try:
            retrieval.hyde({"question": "原问题"}, {}, context(), Store(), Embedder(),
                           SlowModel(""), "qwen", 20, resource_lock=lock)
        except Exception as exc:  # noqa: BLE001 - propagate thread failures to the test
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(2)
        assert lock.acquire(timeout=1)
        lock.release()
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []


def test_default_flow_fuses_three_retrieval_branches_and_reranks_original():
    graph = json.loads((Path(__file__).parents[1] / "workflows/medical-answer.v3.json").read_text(encoding="utf-8"))
    nodes = {node["id"]: node["type"] for node in graph["nodes"]}
    assert nodes["hyde"] == "retrieval.hyde"
    edges = {(e["source"], e["target"], e["target_port"]) for e in graph["edges"]}
    assert {("dense", "fusion", "sources"), ("bm25", "fusion", "sources"),
            ("hyde", "fusion", "sources"), ("query", "rerank", "question")} <= edges
    assert not any(e["source"] == "hyde" and e["target"] in {"llm", "citation"} for e in graph["edges"])


@pytest.mark.asyncio
async def test_complete_answer_chain_only_passes_retrieved_chunks_to_generator():
    from medical_rag.config import Settings
    from medical_rag.service import RagService
    from medical_rag.types import Chunk, Evidence

    chunk = Chunk("chunk-1", "doc", "source", "真实资料", "guide.md", "", None, 0)
    evidence = Evidence(chunk, 0.8)

    class SearchStore:
        def search_dense(self, vector, limit):
            return [evidence]

        def search_bm25(self, question, limit):
            assert question == "原问题"
            return [evidence]

    class Reranker:
        def score(self, question, texts):
            assert question == "原问题"
            assert texts == ["真实资料"]
            return [0.9]

    class Generator:
        def __init__(self):
            self.transport = Model(json.dumps({"hypothetical": "假设文本"}))

        def generate(self, question, retrieved, provider, **kwargs):
            assert question == "原问题"
            assert [item.chunk.text for item in retrieved] == ["真实资料"]
            return {"draft": "draft", "evidence": retrieved}

        def validate(self, draft, retrieved, **kwargs):
            return {"answer": "有引用的回答", "citations": [{"chunk_id": retrieved[0].chunk.id}], "abstained": False}

    embedder = Embedder()
    service = RagService(Settings(), SearchStore(), embedder, Reranker(), Generator())
    assert service.registry.get("retrieval.hyde").worker_lock is None
    result = await service.run_workflow("medical-answer", {"question": "原问题"})
    assert result.status == "success", [(t.node_id, t.error) for t in result.trace if t.status == "failed"]
    assert result.output["citations"][0]["chunk_id"] == "chunk-1"
    assert embedder.texts == ["原问题", "假设文本"]
