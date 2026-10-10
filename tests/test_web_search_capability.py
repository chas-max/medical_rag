"""联网检索是全局内置能力：工作流不声明、节点不配置，运行时对所有工作流一视同仁。"""

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.service import RagService, global_capabilities
from medical_rag.tools import ToolRegistry, tavily_search_tool
from medical_rag.types import Chunk, Evidence
from medical_rag.web_search import WebPage
from medical_rag.workflow.bootstrap import build_registry
from medical_rag.workflow.contracts import CAPABILITY_WEB_SEARCH, ExecutionContext, supports
from medical_rag.workflow.definition import WorkflowDefinition, WorkflowNode
from medical_rag.workflow.engine import WorkflowEngine
from medical_rag.workflow.repository import WorkflowRepository
from medical_rag.workflow.validator import validate_workflow

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
WEB_PAGE = WebPage("https://example.org/guide", "随访指引", "高血压患者应每月随访一次。", 0.8)
CLAIMS = ('{"claims":[{"text":"应每月随访","citations":['
          '{"chunk_id":"web-x","excerpt":"应每月随访一次"}]}]}')


class Store:
    """Empty retrieval: every question has to be supplemented or refused."""

    def search_dense(self, vector, limit):
        return []

    def search_bm25(self, question, limit):
        return []

    def replace(self, document, chunks, vectors):
        return False


class Embedder:
    def encode(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


class Reranker:
    def score(self, question, texts):
        return [0.1 for _ in texts]


class WebTransport:
    """Answers from the web block the tool handed back."""

    def __init__(self):
        self.chat_calls = 0

    def complete(self, provider, system, user):
        raise AssertionError("检索落空时应走联网工具轮次")

    def chat(self, provider, messages, tools=None):
        self.chat_calls += 1
        return {"content": CLAIMS, "tool_calls": []}


class AnswerTransport(WebTransport):
    """问题路由节点也会问模型一次，这里按结构化协议回答。"""

    def complete(self, provider, system, user):
        return '{"route":"direct"}' if "route" in system else CLAIMS


class WebSearch:
    def __init__(self):
        self.calls = []

    def search(self, query, max_results=None):
        self.calls.append(query)
        return [WEB_PAGE]

    def to_evidence(self, pages):
        block = Chunk("web-x", "web", WEB_PAGE.url, WEB_PAGE.content, WEB_PAGE.title,
                      WEB_PAGE.url, None, 0)
        return [Evidence(block, WEB_PAGE.score, origin="web")] if pages else []


def registry_for(transport, search):
    """生成器持有 Tavily 工具时才具备联网能力，能力本身与工作流定义无关。"""
    generator = GroundedGenerator(transport, ToolRegistry([tavily_search_tool(search)]))
    store = Store()
    return build_registry(store, Embedder(), Reranker(), generator, Settings(tavily_api_key=""))


def test_no_published_node_exposes_a_web_search_switch():
    repository = WorkflowRepository(WORKFLOWS)
    switches = ("allow_web_search", "web_search")
    for definition in repository.list():
        for node in definition.nodes:
            assert not set(node.config) & set(switches), f"{definition.id}:{node.id}"


def test_gate_and_generation_metadata_have_no_web_search_config():
    metadata = {item["type"]: item["config_schema"]
                for item in registry_for(WebTransport(), WebSearch()).metadata()}

    assert set(metadata["evidence.gate"]) == {"threshold"}
    assert metadata["generation.llm"] == {}


def test_validator_rejects_a_node_level_web_search_switch():
    definition = WorkflowDefinition(
        id="medical-answer", name="问答", version=9, status="draft",
        nodes=[WorkflowNode(id="gate", type="evidence.gate", config={"allow_web_search": True})],
        edges=[],
    )
    issues = validate_workflow(definition, registry_for(WebTransport(), WebSearch()))

    assert any(issue.code == "INVALID_CONFIG" and issue.node_id == "gate" for issue in issues)


def test_capability_helper_treats_missing_flags_as_off():
    assert supports({CAPABILITY_WEB_SEARCH: True}, CAPABILITY_WEB_SEARCH) is True
    assert supports({CAPABILITY_WEB_SEARCH: False}, CAPABILITY_WEB_SEARCH) is False
    assert supports({}, CAPABILITY_WEB_SEARCH) is False
    assert supports(None, CAPABILITY_WEB_SEARCH) is False


def test_every_published_answer_workflow_reaches_the_web_without_node_config():
    repository = WorkflowRepository(WORKFLOWS)
    for workflow_id in ("medical-answer", "medical-answer-expanded"):
        transport, search = AnswerTransport(), WebSearch()
        registry = registry_for(transport, search)

        result = asyncio.run(WorkflowEngine(registry, {CAPABILITY_WEB_SEARCH: True}).run(
            repository.get(workflow_id), {"question": "未知", "provider": "qwen", "top_k": 5}))

        assert search.calls == ["未知"], workflow_id
        assert transport.chat_calls == 1, workflow_id
        assert result.output["abstained"] is False, workflow_id
        assert result.output["citations"][0]["origin"] == "web", workflow_id


def test_every_answer_workflow_validates_the_evidence_generation_saw():
    """联网补充只存在于生成节点的证据里：引用校验必须接生成节点，否则网页证据永远进不了引用。"""
    registry = registry_for(WebTransport(), WebSearch())
    for definition in WorkflowRepository(WORKFLOWS).list():
        types = {node.id: node.type for node in definition.nodes}
        if "generation.llm" not in types.values():
            continue
        sources = {edge.source for edge in definition.edges
                   if edge.target_port == "evidence" and types.get(edge.target) == "citation.validate"}
        assert {types[node_id] for node_id in sources} == {"generation.llm"}, definition.id
        assert validate_workflow(definition, registry) == [], definition.id


def test_gate_defers_to_generation_only_while_the_capability_is_active():
    gate = registry_for(WebTransport(), WebSearch()).get("evidence.gate")

    def run(capabilities):
        context = ExecutionContext("run", "w", {}, capabilities)
        return asyncio.run(gate.execute({"evidence": []}, {}, context)).outputs

    assert run({CAPABILITY_WEB_SEARCH: True})["abstained"] is False
    assert run({CAPABILITY_WEB_SEARCH: False})["abstained"] is True
    assert run({})["abstained"] is True


def test_service_declares_the_capability_from_tool_availability(tmp_path):
    transport, search = WebTransport(), WebSearch()
    generator = GroundedGenerator(transport, ToolRegistry([tavily_search_tool(search)]))
    service = RagService(Settings(tavily_api_key=""), Store(), Embedder(), Reranker(),
                         generator, workflow_root=tmp_path)

    assert service.capabilities == {CAPABILITY_WEB_SEARCH: True}
    assert service.engine.capabilities == {CAPABILITY_WEB_SEARCH: True}
    assert global_capabilities(GroundedGenerator(transport)) == {CAPABILITY_WEB_SEARCH: False}
