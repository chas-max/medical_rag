import json
import sys
from pathlib import Path

from document_helpers import upload_and_process
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_service import Embedder, MemoryStore, Reranker

from medical_rag.api import create_app
from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.profile_models import IntentDecision
from medical_rag.service import RagService
from medical_rag.types import ApiError


class QueryAnalyzer:
    def analyze(self, question, snapshot):
        return IntentDecision(needs_answer=True)


def chat_post(api, question, stream=True):
    return api.post("/chat/runs" + ("/stream" if stream else ""), json={
        "request_id": "request-1", "conversation_id": "chat-1",
        "inputs": {"question": question, "provider": "qwen", "top_k": 5},
    })


def answer_document(user, excerpt="规律随访"):
    block = json.loads(user)["evidence"][0]
    return json.dumps({"claims": [
        {"text": "应规律随访", "citations": [{"chunk_id": block["chunk_id"], "excerpt": excerpt}]},
        {"text": "需要长期管理", "citations": [{"chunk_id": block["chunk_id"], "excerpt": excerpt}]},
    ]}, ensure_ascii=False)


def answer_document_with_analysis(user, excerpt="规律随访"):
    """先给结论，再给一段挂在已验证块上的延伸解读。"""
    document = json.loads(answer_document(user, excerpt))
    block = json.loads(user)["evidence"][0]["chunk_id"]
    document["analysis"] = {"summary": "把结论串起来看，随访和长期管理是同一件事。",
                            "extensions": ["复诊时带上既往检查资料。"], "based_on": [block]}
    return json.dumps(document, ensure_ascii=False)


def plan_reply() -> str:
    """回答规划节点的调用只看结构与主题：给一份可解析的结构即可。"""
    return json.dumps({"sections": ["基本概念解释", "常见处理方式"]}, ensure_ascii=False)


class StreamTransport:
    """Serves the same document through both the streamed and the plain call."""

    def __init__(self, size=6, excerpt="规律随访"):
        self.size = size
        self.excerpt = excerpt
        self.streams = 0
        self.completes = 0

    def complete(self, provider, system, user):
        self.completes += 1
        if "evidence" not in json.loads(user):
            return plan_reply()
        return answer_document(user, self.excerpt)

    def stream_chat(self, provider, messages, tools=None, on_content=None):
        self.streams += 1
        text = answer_document(messages[-1]["content"], self.excerpt)
        for index in range(0, len(text), self.size):
            if on_content is not None:
                on_content(text[index:index + self.size])
        return {"content": text, "tool_calls": []}


def client(tmp_path: Path, transport=None):
    transport = transport or StreamTransport()
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(transport), workflow_root=tmp_path)
    api = TestClient(create_app(service=service, profile_analyzer=QueryAnalyzer()))
    upload_and_process(api, files={"file": ("guide.md", "# 高血压\n\n高血压患者应规律随访。".encode())})
    return api, service, transport


def read_events(body):
    events = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        events.append((name, data))
    return events


def test_stream_route_publishes_verified_claims_before_the_final_result(tmp_path):
    api, service, transport = client(tmp_path)
    response = chat_post(api, "高血压")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = read_events(response.text)
    names = [name for name, _ in events]
    assert names[-1] == "done"
    assert names.count("claim") == 2
    assert names.index("status") < names.index("claim") < names.index("done")
    # 生成开始前先告知检索到多少证据，长等待期间界面才不是空的。
    assert ("status", {"stage": "generating", "evidence": 1, "web_search": False}) in events

    published = [data for name, data in events if name == "claim"]
    done = events[-1][1]
    assert done["status"] == "success"
    assert done["output"]["answer"] == "\n".join(item["text"] for item in published)
    assert done["output"]["citations"][0]["filename"] == "guide.md"
    # 流式请求不得退化成整段同步生成；HyDE、纠错重写与回答规划各走一次 complete，
    # 生成本体走流式。
    assert (transport.streams, transport.completes) == (1, 3)
    assert api.get(f"/runs/{done['run_id']}").json()["data"]["run_id"] == done["run_id"]
    assert service.last_workflow_id == "medical-answer"


def test_stream_route_publishes_the_analysis_section_as_its_own_event(tmp_path):
    """分析段必须以 analysis 事件到达线上；塞进 claim 通道会让前端解析崩溃并丢掉整条回答。"""
    class AnalysisTransport(StreamTransport):
        def complete(self, provider, system, user):
            self.completes += 1
            if "evidence" not in json.loads(user):
                return plan_reply()
            return answer_document_with_analysis(user, self.excerpt)

        def stream_chat(self, provider, messages, tools=None, on_content=None):
            self.streams += 1
            text = answer_document_with_analysis(messages[-1]["content"], self.excerpt)
            for index in range(0, len(text), self.size):
                if on_content is not None:
                    on_content(text[index:index + self.size])
            return {"content": text, "tool_calls": []}

    api, _, _ = client(tmp_path, AnalysisTransport())
    events = read_events(chat_post(api, "高血压").text)
    names = [name for name, _ in events]

    assert names.count("claim") == 2
    assert names.count("analysis") == 1
    # 分析段固定在所有结论之后、done 之前。
    assert names.index("analysis") == len(names) - 2

    # claim 事件必须始终带着 text/citations，前端直接按这个形状读取。
    for _, data in (item for item in events if item[0] == "claim"):
        assert isinstance(data.get("text"), str)
        assert isinstance(data.get("citations"), list)

    analysis = next(data for name, data in events if name == "analysis")
    assert analysis["extensions"] == ["复诊时带上既往检查资料。"]
    assert analysis["based_on"]
    assert events[-1][1]["output"]["analysis"] == analysis


def test_stream_route_uses_the_web_capability_when_retrieval_is_empty(tmp_path):
    """联网能力必须一路传到生成器：漏传时 gate 不再拒答，生成阶段却不会联网，用户只看到拒答。"""
    from medical_rag.tools import ToolRegistry, tavily_search_tool
    from medical_rag.web_search import WebPage

    class EmptyStore(MemoryStore):
        def search_dense(self, vector, limit):
            return []

        def search_bm25(self, question, limit):
            return []

    class WebSearch:
        def __init__(self):
            self.calls = []

        def search(self, query, max_results=None):
            self.calls.append(query)
            return [WebPage("https://example.org/guide", "随访指引", "高血压患者应规律随访。", 0.8)]

        def to_evidence(self, pages):
            from medical_rag.types import Chunk, Evidence
            page = pages[0]
            block = Chunk("web-x", "web", page.url, page.content, page.title, page.url, None, 0)
            return [Evidence(block, page.score, origin="web")]

    class AgentTransport(StreamTransport):
        """证据为空时生成节点会先发起工具轮次，再由第二轮给出最终 JSON。"""

        def __init__(self):
            super().__init__()
            self.chat_rounds = 0

        def _document(self, messages):
            return json.dumps({"claims": [{"text": "应规律随访", "citations": [
                {"chunk_id": "web-x", "excerpt": "高血压患者应规律随访。"}]}]}, ensure_ascii=False)

        def chat(self, provider, messages, tools=None):
            self.chat_rounds += 1
            return {"content": self._document(messages), "tool_calls": []}

        def stream_chat(self, provider, messages, tools=None, on_content=None):
            self.streams += 1
            text = self._document(messages)
            if on_content is not None and not tools:
                on_content(text)
            return {"content": text, "tool_calls": []}

    search = WebSearch()
    transport = AgentTransport()
    generator = GroundedGenerator(transport, ToolRegistry([tavily_search_tool(search)]))
    service = RagService(Settings(tavily_api_key=""), EmptyStore(), Embedder(), Reranker(),
                         generator, workflow_root=tmp_path)
    api = TestClient(create_app(service=service, profile_analyzer=QueryAnalyzer()))

    response = chat_post(api, "高血压")
    events = read_events(response.text)

    # 状态帧宣称联网，行为就必须真的联网。
    assert ("status", {"stage": "generating", "evidence": 0, "web_search": True}) in events
    assert search.calls == ["高血压"]
    done = events[-1][1]
    assert done["status"] == "success"
    assert done["output"]["abstained"] is False
    assert done["output"]["citations"][0]["origin"] == "web"


def test_stream_route_keeps_the_synchronous_route_on_the_plain_call(tmp_path):
    api, _, transport = client(tmp_path)
    run = chat_post(api, "高血压", stream=False)
    assert run.json()["data"]["status"] == "success"
    # 同步路由：HyDE、纠错重写、回答规划与生成本体各走一次 complete。
    assert (transport.streams, transport.completes) == (0, 4)


def test_stream_route_reports_rejected_inputs_as_an_error_event(tmp_path):
    api, _, _ = client(tmp_path)
    assert chat_post(api, "   ").status_code == 422
    response = api.post("/chat/runs/stream", json={"request_id": "r", "conversation_id": "c",
                        "workflow_id": "missing", "inputs": {"question": "高血压"}})
    assert response.status_code == 422


def test_stream_route_keeps_model_failures_inside_the_final_event(tmp_path):
    class Limited(StreamTransport):
        def stream_chat(self, provider, messages, tools=None, on_content=None):
            raise ApiError("MODEL_RATE_LIMITED", "生成模型请求过于频繁，请稍后重试", 429)

    api, _, _ = client(tmp_path, Limited())
    events = read_events(chat_post(api, "高血压").text)
    assert [name for name, _ in events if name != "chat.progress"] == ["status", "done"]
    failed = next(item for item in events[-1][1]["trace"] if item["status"] == "failed")
    assert failed["error"]["code"] == "MODEL_RATE_LIMITED"


def test_stream_route_reports_abstention_without_publishing_claims(tmp_path):
    api, _, transport = client(tmp_path)
    events = read_events(chat_post(api, "糖尿病").text)
    assert [name for name, _ in events if name != "chat.progress"] == ["done"]
    assert events[-1][1]["output"]["abstained"] is True
    assert transport.streams == 0
