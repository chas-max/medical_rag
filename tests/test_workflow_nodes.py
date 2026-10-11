import asyncio
import json
from pathlib import Path

import pytest

from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.tools import ToolRegistry, tavily_search_tool
from medical_rag.types import ApiError, Chunk, Evidence
from medical_rag.web_search import WebPage
from medical_rag.workflow.bootstrap import build_registry
from medical_rag.workflow.contracts import (
    CAPABILITY_WEB_SEARCH,
    ExecutionContext,
    node_inputs,
)
from medical_rag.workflow.engine import WorkflowEngine
from medical_rag.workflow.nodes import generation as generation_node
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
    with pytest.raises(ApiError, match="不支持的生成模型厂商") as error:
        query_input({"question": "高血压", "provider": "deepseek"}, {}, context)
    # 入参错误由节点自己声明成请求类错误，服务层才能不靠工作流 ID 白名单回 422。
    assert (error.value.code, error.value.status) == ("INVALID_PROVIDER", 422)


def test_default_graphs_are_valid():
    _, _, registry = setup()
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    for name in ("medical-answer", "medical-search", "document-ingestion"):
        graph = repository.get(name)
        assert graph.status == "published"
        assert validate_workflow(graph, registry) == []


def test_history_graph_is_rejected_for_splitting_draft_and_evidence():
    """v1 的接线让门控证据绕过生成节点：引用校验看不到联网补充的块，现在被校验器直接拒绝。"""
    _, _, registry = setup()
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    legacy = repository.get("medical-answer", 1)

    assert [issue.code for issue in validate_workflow(legacy, registry)] == ["PROVENANCE_MISMATCH"]


def test_the_three_pre_retrieval_shapes_all_publish():
    """同一套节点表下，路由／静态单路／不做提前检索三种形态都能通过发布校验。"""
    _, _, registry = setup()
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    for workflow_id in ("medical-answer-expanded", "medical-answer-decompose",
                        "medical-answer-direct", "medical-answer", "medical-search"):
        graph = repository.get(workflow_id)
        assert graph.status == "published", workflow_id
        assert validate_workflow(graph, registry) == [], workflow_id


def test_decompose_graph_retrieves_for_the_original_and_every_sub_question():
    """静态单路形态实际跑通：分解出的子问题各自参与检索，不经过任何路由节点。"""
    store, _, _ = setup()
    store.chunks = [Chunk("c1", "d1", "guide.md", "高血压患者应规律随访。", "guide.md", "", 1, 0)]

    class Transport2:
        def complete(self, provider, system, user):
            if "evidence" in json.loads(user):
                return json.dumps({"claims": [{"text": "应规律随访", "citations": [
                    {"chunk_id": "c1", "excerpt": "规律随访"}]}]})
            return json.dumps({"questions": ["高血压随访频率？", "高血压随访内容？"]})

    registry = build_registry(store, Embedder(), Reranker(), GroundedGenerator(Transport2()),
                              Settings())
    graph = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows").get(
        "medical-answer-decompose")

    result = asyncio.run(WorkflowEngine(registry).run(graph, {"question": "高血压", "top_k": 5}))

    assert result.status == "success"
    assert result.output["abstained"] is False
    assert result.output["citations"][0]["excerpt"] == "规律随访"
    # 原问题 + 两个子问题各走一次向量检索；连线数就是检索路数。
    assert store.dense_calls == 3
    assert result.trace_by_node("decompose").status == "success"


def test_direct_graph_answers_without_rerank_or_fusion():
    """不做提前检索的最小形态：dense 直接喂门控，不再有"门控证据必须来自重排"的限制。"""
    store, transport, registry = setup()
    store.chunks = [Chunk("c1", "d1", "guide.md", "高血压患者应规律随访。", "guide.md", "", 1, 0)]
    graph = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows").get(
        "medical-answer-direct")

    result = asyncio.run(WorkflowEngine(registry).run(graph, {"question": "高血压", "top_k": 5}))

    assert result.status == "success"
    assert result.output["abstained"] is False
    assert store.dense_calls == 1
    assert transport.calls == 1


def test_routing_is_a_graph_property_not_a_node_implementation():
    _, _, registry = setup()
    types = {item["type"] for item in registry.metadata()}
    assert {"question.router", "question.rewrite", "question.decompose", "question.hyde",
            "question.stepback", "merge.collect", "retrieval.multi"}.issubset(types)
    assert not {"question.select", "retrieval.expanded"} & types

    # 被路由的节点只暴露一个可不接的 selector 端口；节点自己不再认识路由。
    rewrite = registry.get("question.rewrite")
    route = node_inputs(rewrite)["route"]
    assert (route.dtype, route.optional) == ("selector", True)

    routed = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows").get(
        "medical-answer-expanded")
    assert [node.activation.equals for node in routed.nodes if node.activation] == [
        "rewrite", "decompose", "hyde", "stepback"]
    assert routed.nodes[2].activation.port == "route"


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
    engine = WorkflowEngine(registry, {CAPABILITY_WEB_SEARCH: True})
    result = asyncio.run(engine.run(graph, {"question": "未知", "provider": "qwen", "top_k": 5}))

    assert search.calls == ["未知"]
    assert result.trace_by_node("gate").status == "success"
    assert result.trace_by_node("citation").status == "success"
    assert result.output["abstained"] is False
    assert "[web-x]" in result.output["answer"]
    assert result.output["citations"] == [{"chunk_id": "web-x", "filename": "随访指引",
                                           "section": "https://example.org/guide", "page": None,
                                           "excerpt": "应每月随访一次", "origin": "web"}]
    assert (transport.complete_calls, transport.chat_calls) == (0, 1)


def test_published_history_keeps_its_own_evidence_wiring():
    """v1 把门控证据接给引用校验：全局联网能发起检索，但补充证据对它不可见，结论仍被拒。"""
    store = Store()
    store.chunks = []
    search = WebSearch()
    generator = GroundedGenerator(WebTransport(), ToolRegistry([tavily_search_tool(search)]))
    registry = build_registry(store, Embedder(), Reranker(), generator, Settings())
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    engine = WorkflowEngine(registry, {CAPABILITY_WEB_SEARCH: True})
    result = asyncio.run(engine.run(repository.get("medical-answer", 1), {
        "question": "未知", "provider": "qwen", "top_k": 5}))

    assert (search.calls, result.output["abstained"], result.trace_by_node("llm").status) == (
        ["未知"], True, "success")


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


def test_workflow_output_exposes_analysis_without_changing_the_verified_answer():
    """answer.output 只做透传：analysis 必须原样到达公开结果，answer 格式不受影响。"""
    document = json.dumps({
        "claims": [{"text": "应规律随访", "citations": [
            {"chunk_id": "c1", "excerpt": "规律随访"}]}],
        "analysis": {"summary": "规律随访属于长期管理的一部分，便于医生及时调整方案。",
                     "extensions": ["复诊时带上既往检查资料。"], "based_on": ["c1"]},
    }, ensure_ascii=False)

    class AnalysisTransport:
        def complete(self, provider, system, user):
            return document

    store = Store()
    store.chunks = [Chunk("c1", "d1", "guide.md", "高血压患者应规律随访。", "guide.md", "", 1, 0)]
    registry = build_registry(store, Embedder(), Reranker(),
                              GroundedGenerator(AnalysisTransport()), Settings())
    repository = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows")
    result = asyncio.run(WorkflowEngine(registry).run(repository.get("medical-answer"), {
        "question": "高血压", "provider": "qwen", "top_k": 5}))

    assert result.status == "success"
    assert result.output["abstained"] is False
    assert result.output["answer"] == "应规律随访 [c1]"
    assert result.output["analysis"]["based_on"] == ["c1"]
    assert result.output["analysis"]["extensions"] == ["复诊时带上既往检查资料。"]


def test_draft_node_gives_the_analysis_its_own_event_and_wire_shape():
    """节点必须把生成器的两种载荷分流：分析段走 analysis 事件，且 data 直接是分析对象。

    这里刻意从节点层进入（而不是直接调 generator.draft），因为把分析段塞进 claim 通道的
    缺陷正好藏在节点这一层的包装里；回归测试只有在这一层才拦得住。
    """
    document = json.dumps({
        "claims": [{"text": "应规律随访", "citations": [
            {"chunk_id": "c1", "excerpt": "规律随访"}]}],
        "analysis": {"summary": "把结论串起来看，随访与长期管理是同一件事。",
                     "extensions": ["复诊时带上既往检查资料。"], "based_on": ["c1"]},
    }, ensure_ascii=False)

    class FakeGenerator:
        def generate_stream(self, *args, **kwargs):
            raise AssertionError("节点应优先走逐条推送的路径")

        def draft(self, question, evidence, provider, web_search=False, on_claim=None):
            # 节点必须把联网能力原样传进来，漏传会让检索落空时不再兜底。
            assert web_search is False
            if on_claim is not None:
                on_claim({"text": "应规律随访 [c1]", "citations": [{"chunk_id": "c1"}]})
                on_claim({"event": "analysis", "analysis": {
                    "summary": "把结论串起来看，随访与长期管理是同一件事。",
                    "extensions": ["复诊时带上既往检查资料。"], "based_on": ["c1"]}})
            return {"draft": document, "evidence": evidence}

    emitted = []
    chunk = Chunk("c1", "d1", "guide.md", "高血压患者应规律随访。", "guide.md", "", 1, 0)
    context = ExecutionContext("run", "w", {}, emit=emitted.append)
    result = generation_node.draft(
        {"question": "高血压", "provider": "qwen", "evidence": [Evidence(chunk, 0.9)]},
        {}, context, FakeGenerator(), "qwen")

    assert result["draft"] == document
    assert [item["event"] for item in emitted] == ["status", "claim", "analysis"]
    # claim 事件必须带 text/citations，前端直接按这两个键读取。
    assert emitted[1]["data"] == {"text": "应规律随访 [c1]", "citations": [{"chunk_id": "c1"}]}
    # analysis 事件的 data 就是分析对象本身，而不是再包一层的 {"analysis": ...}。
    assert emitted[2]["data"]["extensions"] == ["复诊时带上既往检查资料。"]
    assert emitted[2]["data"]["based_on"] == ["c1"]


def test_draft_node_without_a_streaming_generator_stays_off_the_wire():
    """没有流式能力的生成器只能走整段生成：除状态事件外不得推送任何结论。"""
    class PlainGenerator:
        def generate(self, question, evidence, provider, web_search=False):
            return {"draft": "{}", "evidence": evidence}

    emitted = []
    context = ExecutionContext("run", "w", {}, emit=emitted.append)
    generation_node.draft({"question": "高血压", "provider": "qwen", "evidence": []}, {}, context,
                          PlainGenerator(), "qwen")

    assert [item["event"] for item in emitted] == ["status"]


def test_rrf_fuses_duplicate_chunks():
    store, _, registry = setup()
    chunk = Chunk("same", "d", "guide.md", "高血压", "guide.md", "", None, 0)
    result = asyncio.run(registry.get("retrieval.rrf").execute({
        "sources": [[Evidence(chunk, 0.7)], [Evidence(chunk, 0.9)]]},
        {"k": 60}, ExecutionContext("run", "w", {})))
    assert len(result.outputs["evidence"]) == 1
    assert result.outputs["evidence"][0].fused_score == 2 / 61


def test_streaming_draft_pushes_claims_before_the_analysis_section():
    """前端按事件顺序排版：结论必须全部先到，解释段固定在最后一条。"""
    document = json.dumps({
        "claims": [{"text": "应规律随访", "citations": [
            {"chunk_id": "c1", "excerpt": "规律随访"}]}],
        "analysis": {"summary": "规律随访是长期管理的一部分，便于医生及时调整方案。",
                     "extensions": ["复诊时带上既往检查资料。"], "based_on": ["c1"]},
    }, ensure_ascii=False)

    class StreamTransport:
        def __init__(self, payload):
            self.payload = payload

        def complete(self, provider, system, user):
            return self.payload

        def stream_chat(self, provider, messages, tools=None, on_content=None):
            step = 7
            for index in range(0, len(self.payload), step):
                if on_content is not None:
                    on_content(self.payload[index:index + step])
            return {"content": self.payload, "tool_calls": []}

    emitted = []
    chunk = Chunk("c1", "d1", "guide.md", "高血压患者应规律随访。", "guide.md", "", 1, 0)
    generator = GroundedGenerator(StreamTransport(document))
    result = generator.draft("高血压", [Evidence(chunk, 0.9)], "qwen", on_claim=emitted.append)

    assert result["draft"] == document
    assert "analysis" not in result
    assert [item.get("event", "claim") for item in emitted] == ["claim", "analysis"]
    assert emitted[0]["text"] == "应规律随访 [c1]"
    assert emitted[1]["analysis"]["based_on"] == ["c1"]


def test_analysis_never_reaches_a_client_when_no_claim_verifies():
    """解释段读起来再合理，只要结论没通过逐字校验，流上就不能出现 analysis 事件。"""
    document = json.dumps({
        "claims": [{"text": "伪造结论", "citations": [
            {"chunk_id": "c1", "excerpt": "不存在的摘录"}]}],
        "analysis": {"summary": "这段解释本身读起来很合理。", "extensions": [],
                     "based_on": ["c1"]},
    }, ensure_ascii=False)

    class StreamTransport:
        def complete(self, provider, system, user):
            return document

        def stream_chat(self, provider, messages, tools=None, on_content=None):
            if on_content is not None:
                on_content(document)
            return {"content": document, "tool_calls": []}

    emitted = []
    chunk = Chunk("c1", "d1", "guide.md", "高血压患者应规律随访。", "guide.md", "", 1, 0)
    GroundedGenerator(StreamTransport()).draft(
        "高血压", [Evidence(chunk, 0.9)], "qwen", on_claim=emitted.append)

    assert emitted == []
