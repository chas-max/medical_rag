import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.generation import ABSTENTION, GroundedGenerator
from medical_rag.selfcheck import SelfCheckPolicy, assess
from medical_rag.tools import TAVILY_TOOL, ToolCall, ToolRegistry, tavily_search_tool
from medical_rag.types import ORIGIN_WEB, ApiError, Chunk, Evidence
from medical_rag.web_search import WebPage

WEB_PAGE = WebPage("https://example.org/guide", "随访指引", "高血压患者应每月随访一次。", 0.8)
POLICY = SelfCheckPolicy(clear_threshold=0.75, min_clear_evidence=2)


def kb(block_id="k1", score=0.9, text="高血压患者应规律随访。"):
    return Evidence(Chunk(block_id, "doc", "guide.md", text, "guide.md", "管理", 2, 0), score)


def web_evidence(text=WEB_PAGE.content, block_id="web-1"):
    return Evidence(Chunk(block_id, ORIGIN_WEB, WEB_PAGE.url, text, WEB_PAGE.title, WEB_PAGE.url, None, 0),
                    WEB_PAGE.score, origin=ORIGIN_WEB)


def claims(*citations):
    return json.dumps({"claims": [{"text": "应每月随访", "citations": list(citations)}]})


class StubSearch:
    def __init__(self, pages=(WEB_PAGE,), error=None):
        self.pages = list(pages)
        self.error = error
        self.calls = []

    def search(self, query, max_results=None):
        self.calls.append(query)
        if self.error:
            raise self.error
        return self.pages

    def to_evidence(self, pages):
        return [web_evidence()] if pages else []


def tools_for(stub):
    return ToolRegistry([tavily_search_tool(stub)])


class Transport:
    """Serves queued agent turns and records both the tool and plain paths."""

    def __init__(self, turns=(), final=None):
        self.turns = list(turns)
        self.final = final if final is not None else claims({"chunk_id": "web-1", "excerpt": "应每月随访一次"})
        self.chats = []
        self.completes = []

    def complete(self, provider, system, user):
        self.completes.append({"system": system, "user": user})
        return self.final

    def chat(self, provider, messages, tools=None):
        self.chats.append({"messages": messages, "tools": tools})
        if self.turns:
            return self.turns.pop(0)
        return {"content": self.final, "tool_calls": []}


def tool_call(call_id, query):
    return {"content": "", "tool_calls": [ToolCall(call_id, TAVILY_TOOL, json.dumps({"query": query}))]}


def generator(transport, stub=None):
    return GroundedGenerator(transport, tools_for(stub or StubSearch()), POLICY)


def test_assess_splits_retrieval_into_three_cases():
    assert assess([], POLICY, True).mode == "none"
    assert assess([kb(score=0.9), kb("k2", 0.8)], POLICY, True).mode == "clear"
    assert assess([kb(score=0.9)], POLICY, True).mode == "vague"
    assert assess([kb(score=0.6), kb("k2", 0.6)], POLICY, True).mode == "vague"


def test_assess_never_asks_for_network_when_search_is_unavailable():
    assert assess([], POLICY, False).search is False
    assert assess([kb("k1", 0.6)], POLICY, False).search is False
    assert assess([], POLICY, True).search is True
    assert assess([kb(), kb("k2", 0.8)], POLICY, True).search is False


def test_clear_retrieval_answers_from_knowledge_base_without_network():
    stub = StubSearch()
    transport = Transport(final=claims({"chunk_id": "k1", "excerpt": "应规律随访"}))
    evidence = [kb(), kb("k2", 0.8)]
    result = generator(transport, stub).generate("如何随访？", evidence, "qwen", web_search=True)

    assert (result["mode"], result["evidence"]) == ("clear", evidence)
    assert (transport.chats, stub.calls, len(transport.completes)) == ([], [], 1)


def test_disabled_web_search_keeps_weak_retrieval_offline():
    stub = StubSearch()
    transport = Transport(final=claims({"chunk_id": "k1", "excerpt": "应规律随访"}))
    result = generator(transport, stub).generate("如何随访？", [kb("k1", 0.6)], "qwen")

    assert (result["mode"], result["evidence"]) == ("vague", [kb("k1", 0.6)])
    assert (transport.chats, stub.calls) == ([], [])


def test_vague_retrieval_is_supplemented_by_model_queries():
    stub = StubSearch()
    transport = Transport([tool_call("call-1", "儿童退热剂量")])
    weak = [kb("k1", 0.6)]
    result = generator(transport, stub).generate("如何随访？", weak, "qwen", web_search=True)

    assert result["mode"] == "vague"
    assert stub.calls == ["如何随访？", "儿童退热剂量"]
    assert result["queries"] == ["如何随访？", "儿童退热剂量"]
    assert [item.chunk.id for item in result["evidence"]] == ["k1", "web-1"]
    history = transport.chats[-1]["messages"]
    assert [item["role"] for item in history] == ["system", "user", "assistant", "tool"]
    assert history[0]["content"].startswith("你是公开医疗资料检索助手")
    assert "tavily_search" in history[0]["content"]
    assert json.loads(history[1]["content"])["self_check"] == {"mode": "vague", "reason": "evidence_weak"}
    assert json.loads(history[3]["content"])["results"][0]["chunk_id"] == "web-1"


def test_missing_retrieval_falls_back_to_web_evidence():
    stub = StubSearch()
    transport = Transport()
    result = generator(transport, stub).answer("如何随访？", [], "qwen", web_search=True)

    assert stub.calls == ["如何随访？"]
    assert result["abstained"] is False
    assert "[web-1]" in result["answer"]
    assert result["citations"] == [{"chunk_id": "web-1", "filename": "随访指引",
                                    "section": "https://example.org/guide", "page": None,
                                    "excerpt": "应每月随访一次", "origin": ORIGIN_WEB}]


def test_failed_search_still_abstains_instead_of_answering_from_nothing():
    stub = StubSearch(error=ApiError("WEB_SEARCH_UNAVAILABLE", "联网搜索服务不可用，请稍后重试", 503))
    transport = Transport()
    result = generator(transport, stub).answer("如何随访？", [], "qwen", web_search=True)

    assert result == {"answer": ABSTENTION, "citations": [], "abstained": True}
    assert (transport.chats, transport.completes) == ([], [])


def test_failed_search_keeps_weak_retrieval_answerable():
    stub = StubSearch(pages=[])
    transport = Transport(final=claims({"chunk_id": "k1", "excerpt": "应规律随访"}))
    result = generator(transport, stub).answer("如何随访？", [kb("k1", 0.6)], "qwen", web_search=True)

    assert result["abstained"] is False
    assert result["citations"][0]["origin"] == "knowledge_base"


def test_web_excerpt_is_verified_exactly_like_local_evidence():
    transport = Transport(final=claims({"chunk_id": "web-1", "excerpt": "不存在的摘录"}))
    result = generator(transport, StubSearch()).answer("如何随访？", [], "qwen", web_search=True)

    assert result["abstained"] is True
    assert result["citations"] == []


def test_excerpt_may_differ_only_in_whitespace_and_is_returned_as_source_slice():
    source = "血压达标患者，至少每 3 个月随访 1 次；\n未达标患者，每月至少随访 1 次。"
    block = Evidence(Chunk("k1", "doc", "guide.md", source, "guide.md", "管理", 2, 0), 0.9)
    result = GroundedGenerator(Transport()).validate(claims({"chunk_id": "k1",
                                                             "excerpt": "血压达标患者，至少每3个月随访1次；未达标患者"}),
                                                     [block])

    assert result["abstained"] is False
    assert result["citations"][0]["excerpt"] in source
    assert result["citations"][0]["origin"] == "knowledge_base"


def test_unsupported_claim_is_dropped_while_verified_claims_survive():
    block = kb()
    draft = json.dumps({"claims": [
        {"text": "伪造结论", "citations": [{"chunk_id": "k1", "excerpt": "从不存在的表述"}]},
        {"text": "应规律随访", "citations": [{"chunk_id": "k1", "excerpt": "应规律随访"}]},
    ]})
    result = GroundedGenerator(Transport()).validate(draft, [block])

    assert result["abstained"] is False
    assert result["answer"] == "应规律随访 [k1]"
    assert len(result["citations"]) == 1


def test_tool_loop_stops_at_the_configured_budget():
    stub = StubSearch()
    transport = Transport([tool_call("call-1", "血压目标值"), tool_call("call-2", "用药方案")])
    agent = GroundedGenerator(transport, tools_for(stub), POLICY, max_tool_calls=1)
    result = agent.generate("如何随访？", [kb("k1", 0.6)], "qwen", web_search=True)

    assert stub.calls == ["如何随访？", "血压目标值"]
    assert len(transport.chats) == 2
    assert transport.chats[1]["tools"] is None
    assert result["draft"] == ""


def test_generator_without_tools_behaves_like_the_local_only_pipeline():
    transport = Transport(final=claims({"chunk_id": "k1", "excerpt": "应规律随访"}))
    result = GroundedGenerator(transport).generate("如何随访？", [kb("k1", 0.6)], "qwen", web_search=True)

    assert result["mode"] == "vague"
    assert (transport.chats, len(transport.completes)) == ([], 1)
    assert "tavily_search" not in transport.completes[0]["system"]
    assert result["draft"] == transport.final
