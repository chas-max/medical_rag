import asyncio
import json
from pathlib import Path

from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.types import Chunk, Evidence
from medical_rag.workflow.bootstrap import build_registry
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.engine import WorkflowEngine
from medical_rag.workflow.repository import WorkflowRepository
from medical_rag.workflow.validator import validate_workflow


class Store:
    def __init__(self):
        self.docs = []
        self.chunks = []
        self.dense_calls = 0

    def search_dense(self, vector, limit):
        self.dense_calls += 1
        return [Evidence(chunk, 0.8) for chunk in self.chunks][:limit]

    def search_bm25(self, question, limit):
        return [Evidence(chunk, 1.0) for chunk in self.chunks if question in chunk.text][:limit]

    def replace(self, document, chunks, vectors):
        duplicate = bool(self.docs and self.docs[-1].sha256 == document.sha256)
        if not duplicate:
            self.docs = [document]
            self.chunks = chunks
        return duplicate


class Embedder:
    def encode(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


class Reranker:
    def score(self, question, texts):
        return [0.9 if question in text else 0.1 for text in texts]


class Transport:
    def __init__(self):
        self.calls = 0

    def complete(self, provider, system, user):
        self.calls += 1
        evidence = json.loads(user)["evidence"][0]
        return json.dumps({"claims": [{"text": "应规律随访", "citations": [{
            "chunk_id": evidence["chunk_id"], "excerpt": "规律随访"}]}]})


def setup():
    store, transport = Store(), Transport()
    registry = build_registry(store, Embedder(), Reranker(), GroundedGenerator(transport), Settings())
    return store, transport, registry


def test_default_graphs_are_valid():
    _, _, registry = setup()
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    for name in ("medical-answer", "medical-search", "document-ingestion"):
        graph = repository.get(name)
        assert graph.status == "published"
        assert validate_workflow(graph, registry) == []


def test_document_to_answer_workflow_and_duplicate():
    store, transport, registry = setup()
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    engine = WorkflowEngine(registry)
    ingestion = repository.get("document-ingestion")
    inputs = {"filename": "guide.md", "content": "# 高血压\n\n高血压患者应规律随访。".encode()}
    first = asyncio.run(engine.run(ingestion, inputs))
    assert first.status == "success"
    assert first.output["duplicate"] is False
    second = asyncio.run(engine.run(ingestion, inputs))
    assert second.output["duplicate"] is True
    answer = asyncio.run(engine.run(repository.get("medical-answer"), {
        "question": "高血压", "provider": "qwen", "top_k": 5}))
    assert answer.output["abstained"] is False
    assert answer.output["citations"][0]["filename"] == "guide.md"
    assert transport.calls == 1


def test_no_evidence_skips_model_and_returns_abstention():
    _, transport, registry = setup()
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    result = asyncio.run(WorkflowEngine(registry).run(repository.get("medical-answer"), {
        "question": "未知", "provider": "deepseek", "top_k": 5}))
    assert result.output["abstained"] is True
    assert result.output["answer"].startswith("现有资料不足")
    assert result.trace_by_node("llm").status == "skipped"
    assert transport.calls == 0


def test_invalid_citation_from_workflow_abstains():
    store, _, registry = setup()
    store.chunks = [Chunk("c1", "d1", "guide.md", "高血压应规律随访。", "guide.md", "", 1, 0)]
    class MaliciousGenerator:
        def draft(self, question, evidence, provider):
            return json.dumps({"claims": [{"text": "伪造", "citations": [
                {"chunk_id": "c1", "excerpt": "不存在的摘录"}]}]})
        validate = GroundedGenerator(Transport()).validate
    registry = build_registry(store, Embedder(), Reranker(), MaliciousGenerator(), Settings())
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    result = asyncio.run(WorkflowEngine(registry).run(repository.get("medical-answer"), {
        "question": "高血压", "provider": "qwen", "top_k": 5}))
    assert result.output["abstained"] is True
    assert result.output["citations"] == []


def test_rrf_fuses_duplicate_chunks():
    store, _, registry = setup()
    chunk = Chunk("same", "d", "guide.md", "高血压", "guide.md", "", None, 0)
    result = asyncio.run(registry.get("retrieval.rrf").execute({
        "dense": [Evidence(chunk, 0.7)], "bm25": [Evidence(chunk, 0.9)]},
        {"k": 60}, ExecutionContext("run", "w", {})))
    assert len(result.outputs["evidence"]) == 1
    assert result.outputs["evidence"][0].fused_score == 2 / 61
