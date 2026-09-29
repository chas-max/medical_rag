import asyncio
import json
from pathlib import Path

import pytest

from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.tools import ToolRegistry, tavily_search_tool
from medical_rag.types import Chunk, Evidence
from medical_rag.web_search import WebPage
from medical_rag.workflow.bootstrap import build_registry
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.engine import WorkflowEngine
from medical_rag.workflow.nodes.input import query_input
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


WEB_PAGE = WebPage("https://example.org/guide", "随访指引", "高血压患者应每月随访一次。", 0.8)


class WebSearch:
    def __init__(self):
        self.calls = []

    def search(self, query, max_results=None):
        self.calls.append(query)
        return [WEB_PAGE]

    def to_evidence(self, pages):
        return [Evidence(Chunk("web-x", "web", WEB_PAGE.url, WEB_PAGE.content, WEB_PAGE.title,
                               WEB_PAGE.url, None, 0), WEB_PAGE.score, origin="web")]


class WebTransport:
    """Drafts against the block the web tool handed back, on either call path."""

    def __init__(self):
        self.complete_calls = 0
        self.chat_calls = 0

    def complete(self, provider, system, user):
        self.complete_calls += 1
        return self._claims()

    def chat(self, provider, messages, tools=None):
        self.chat_calls += 1
        return {"content": self._claims(), "tool_calls": []}

    @staticmethod
    def _claims():
        return json.dumps({"claims": [{"text": "应每月随访", "citations": [
            {"chunk_id": "web-x", "excerpt": "应每月随访一次"}]}]})


def test_query_input_defaults_to_qwen_and_rejects_deepseek():
    context = ExecutionContext("run", "medical-answer", {})
    assert query_input({"question": "高血压"}, {}, context)["provider"] == "qwen"
    with pytest.raises(ValueError, match="不支持的生成模型厂商"):
        query_input({"question": "高血压", "provider": "deepseek"}, {}, context)


def test_default_graphs_are_valid():
    _, _, registry = setup()
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    for name in ("medical-answer", "medical-search", "document-ingestion"):
        graph = repository.get(name)
        assert graph.status == "published"
        assert validate_workflow(graph, registry) == []


def test_expanded_graph_registers_and_validates_without_changing_defaults():
    _, _, registry = setup()
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    graph = repository.get("medical-answer-expanded")
    assert graph.status == "published"
    assert validate_workflow(graph, registry) == []
    assert {"question.router", "question.rewrite", "question.decompose",
            "question.hyde", "question.stepback", "question.select",
            "retrieval.expanded"}.issubset({item["type"] for item in registry.metadata()})
    assert repository.get("medical-answer").id == "medical-answer"


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
        "question": "未知", "provider": "qwen", "top_k": 5}))
    assert result.output["abstained"] is True
    assert result.output["answer"].startswith("现有资料不足")
    assert result.trace_by_node("llm").status == "skipped"
    assert transport.calls == 0


def test_default_graph_answers_from_web_when_retrieval_misses():
    store = Store()
    store.chunks = []
    search = WebSearch()
    transport = WebTransport()
    generator = GroundedGenerator(transport, ToolRegistry([tavily_search_tool(search)]))
    registry = build_registry(store, Embedder(), Reranker(), generator, Settings())
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    graph = repository.get("medical-answer")

    assert (graph.version, graph.status) == (2, "published")
    assert validate_workflow(graph, registry) == []
    result = asyncio.run(WorkflowEngine(registry).run(graph, {
        "question": "未知", "provider": "qwen", "top_k": 5}))

    assert search.calls == ["未知"]
    assert result.trace_by_node("gate").status == "success"
    assert result.trace_by_node("citation").status == "success"
    assert result.output["abstained"] is False
    assert "[web-x]" in result.output["answer"]
    assert result.output["citations"] == [{"chunk_id": "web-x", "filename": "随访指引",
                                           "section": "https://example.org/guide", "page": None,
                                           "excerpt": "应每月随访一次", "origin": "web"}]
    assert (transport.complete_calls, transport.chat_calls) == (0, 1)


def test_published_history_still_refuses_without_network():
    store = Store()
    store.chunks = []
    search = WebSearch()
    generator = GroundedGenerator(WebTransport(), ToolRegistry([tavily_search_tool(search)]))
    registry = build_registry(store, Embedder(), Reranker(), generator, Settings())
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    result = asyncio.run(WorkflowEngine(registry).run(repository.get("medical-answer", 1), {
        "question": "未知", "provider": "qwen", "top_k": 5}))

    assert (search.calls, result.output["abstained"], result.trace_by_node("llm").status) == (
        [], True, "skipped")


def test_invalid_citation_from_workflow_abstains():
    store, _, registry = setup()
    store.chunks = [Chunk("c1", "d1", "guide.md", "高血压应规律随访。", "guide.md", "", 1, 0)]
    class MaliciousGenerator:
        def generate(self, question, evidence, provider, web_search=False):
            return {"draft": json.dumps({"claims": [{"text": "伪造", "citations": [
                {"chunk_id": "c1", "excerpt": "不存在的摘录"}]}]}), "evidence": evidence}
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
