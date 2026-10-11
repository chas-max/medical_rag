import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.config import Settings
from medical_rag.generation import MAX_CLAIMS, GroundedGenerator, OpenAITransport
from medical_rag.streaming import ClaimStreamParser
from medical_rag.types import Chunk, Evidence


def evidence(chunk_id="block1", text="高血压患者应规律随访。"):
    return Evidence(Chunk(chunk_id, "doc", "guide.md", text, "guide.md", "管理", 2, 0), 0.9)


def document(claims):
    return json.dumps({"claims": claims}, ensure_ascii=False)


def feed_every(text, size):
    parser = ClaimStreamParser()
    found = []
    for index in range(0, len(text), size):
        found.extend(parser.feed(text[index:index + size]))
    return parser, found


def claim(text, chunk_id="block1", excerpt=None):
    return {"text": text, "citations": [{"chunk_id": chunk_id, "excerpt": excerpt or text}]}


def delta_chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def call_delta(index=0, call_id=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=call_id,
                           function=SimpleNamespace(name=name, arguments=arguments))


class StreamingClient:
    """Stands in for the OpenAI SDK's streamed completion call."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.request = {}

        def create(**kwargs):
            self.request = kwargs
            return iter(self.chunks)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def transport_for(chunks):
    client = StreamingClient(chunks)
    transport = OpenAITransport(Settings(qwen_api_key="key"),
                               client_factory=lambda **kwargs: client)
    return transport, client


def test_stream_chat_forwards_every_delta_of_a_final_answer_turn():
    transport, client = transport_for([delta_chunk('{"claims":['), delta_chunk("]}")])
    seen = []
    message = transport.stream_chat("qwen", [{"role": "user", "content": "问题"}], None,
                                   on_content=seen.append)
    assert "".join(seen) == '{"claims":[]}'
    assert message == {"content": '{"claims":[]}', "tool_calls": []}
    assert client.request["stream"] is True
    assert client.request["extra_body"] == {"enable_thinking": False}


def test_stream_chat_withholds_content_once_a_tool_call_appears():
    transport, _ = transport_for([
        delta_chunk('{"claims":['),
        delta_chunk(None, [call_delta(0, "call-1", "tavily_search", '{"query":')]),
        delta_chunk("工具轮的文字", [call_delta(0, arguments='"退热"')]),
    ])
    seen = []
    message = transport.stream_chat("qwen", [{"role": "user", "content": "问题"}],
                                   [{"type": "function"}], on_content=seen.append)
    assert "".join(seen) == '{"claims":['
    assert message["content"] == '{"claims":[工具轮的文字'
    assert [(call.id, call.name, call.arguments) for call in message["tool_calls"]] == [
        ("call-1", "tavily_search", '{"query":"退热"')]


@pytest.mark.parametrize("size", [1, 2, 7, 64, 4096])
def test_parser_reports_every_claim_once_in_order(size):
    text = document([claim("应规律随访"), claim("血压需长期监测", excerpt="长期监测")])
    parser, found = feed_every(text, size)
    assert [item["text"] for item in found] == ["应规律随访", "血压需长期监测"]
    assert parser.claims_complete is True
    assert parser.buffer == text


def test_parser_keeps_braces_quotes_and_the_claims_word_inside_text():
    text = document([claim('包含 { 和 [ 以及 "引号" 与 claims 字样的结论', excerpt="随访")])
    parser, found = feed_every(text, 3)
    assert found == [{"text": '包含 { 和 [ 以及 "引号" 与 claims 字样的结论',
                      "citations": [{"chunk_id": "block1", "excerpt": "随访"}]}]
    assert parser.claims_complete is True


def test_parser_ignores_an_unfinished_or_invalid_element():
    parser = ClaimStreamParser()
    assert parser.feed('{"claims":[{"text":"未写完') == []
    assert parser.feed('的结论","citations":[]}') == [
        {"text": "未写完的结论", "citations": []}]
    broken = ClaimStreamParser()
    assert broken.feed('{"claims":[{"text":},') == []
    assert broken.claims_complete is False


def test_parser_handles_a_preceding_object_and_an_empty_array():
    parser = ClaimStreamParser()
    found = parser.feed('{"meta":{"page":[1,2]},"claims":[{"text":"结论","citations":[]}]}')
    assert [item["text"] for item in found] == ["结论"]
    empty = ClaimStreamParser()
    assert empty.feed('{"claims":[]}') == []
    assert empty.claims_complete is True


class StreamTransport:
    """Serves one answer document in fixed-size fragments."""

    def __init__(self, text, size=4):
        self.text = text
        self.size = size
        self.tools = "unset"

    def stream_chat(self, provider, messages, tools=None, on_content=None):
        self.tools = tools
        for index in range(0, len(self.text), self.size):
            if on_content is not None:
                on_content(self.text[index:index + self.size])
        return {"content": self.text, "tool_calls": []}


def test_streamed_claims_match_the_final_answer_line_for_line():
    text = document([claim("应规律随访"), claim("血压需长期监测", excerpt="规律随访")])
    transport = StreamTransport(text)
    published = []
    generator = GroundedGenerator(transport)
    result = generator.generate_stream("如何随访？", [evidence()], "qwen",
                                       on_claim=published.append)

    final = generator.validate(result["draft"], result["evidence"])
    assert result["draft"] == text
    assert final["abstained"] is False
    assert [item["text"] for item in published] == final["answer"].split("\n")
    assert published[0]["text"].startswith("应规律随访 [block1]")
    assert published[0]["citations"][0]["excerpt"] == "应规律随访"
    assert [item["chunk_id"] for item in published[1]["citations"]] == ["block1"]
    assert published[1]["text"].startswith("血压需长期监测 [block1]")


def test_stream_never_publishes_a_claim_that_fails_excerpt_verification():
    text = document([
        claim("无依据的结论", excerpt="证据里没有这句话"),
        claim("有依据的结论", excerpt="规律随访"),
    ])
    generator = GroundedGenerator(StreamTransport(text))
    published = []
    result = generator.generate_stream("如何随访？", [evidence()], "qwen",
                                       on_claim=published.append)

    final = generator.validate(result["draft"], result["evidence"])
    assert [item["text"] for item in published] == ["有依据的结论 [block1]"]
    assert [item["text"] for item in published] == final["answer"].split("\n")


def test_stream_accepts_a_whitespace_only_difference_like_the_final_check():
    text = document([claim("应规律随访", excerpt="应规律\n随访")])
    generator = GroundedGenerator(StreamTransport(text))
    published = []
    result = generator.generate_stream("如何随访？", [evidence()], "qwen",
                                       on_claim=published.append)

    final = generator.validate(result["draft"], result["evidence"])
    assert published[0]["citations"][0]["excerpt"] == "应规律随访"
    assert [item["text"] for item in published] == final["answer"].split("\n")


def test_stream_truncates_like_the_final_answer_before_filtering():
    claims = [claim(f"结论{index}", excerpt="规律随访") for index in range(MAX_CLAIMS + 3)]
    generator = GroundedGenerator(StreamTransport(document(claims), size=9))
    published = []
    result = generator.generate_stream("如何随访？", [evidence()], "qwen",
                                       on_claim=published.append)

    final = generator.validate(result["draft"], result["evidence"])
    assert len(published) == MAX_CLAIMS
    assert [item["text"] for item in published] == final["answer"].split("\n")


def test_stream_without_evidence_never_calls_the_model():
    transport = StreamTransport("unused")
    published = []
    result = GroundedGenerator(transport).generate_stream("如何随访？", [], "qwen",
                                                          on_claim=published.append)
    assert (result["draft"], published) == ("", [])


class AgentStreamTransport:
    """Serves queued agent turns; every turn reaches the streamed call."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.tools_seen = []

    def stream_chat(self, provider, messages, tools=None, on_content=None):
        content, calls = self.turns.pop(0)
        self.tools_seen.append(tools)
        if on_content is not None and not calls:
            on_content(content)
        return {"content": content, "tool_calls": calls}


def test_agent_turns_are_streamed_and_a_direct_answer_is_published():
    from medical_rag.selfcheck import SelfCheckPolicy
    from medical_rag.tools import ToolRegistry, tavily_search_tool
    from test_generation_agent import POLICY, StubSearch

    transport = AgentStreamTransport([(document([claim("应规律随访", chunk_id="k1")]), [])])
    generator = GroundedGenerator(transport, ToolRegistry([tavily_search_tool(StubSearch())]),
                                  SelfCheckPolicy(POLICY.clear_threshold, POLICY.min_clear_evidence))
    published = []
    result = generator.generate_stream("如何随访？", [evidence("k1", "高血压患者应规律随访。")],
                                       "qwen", web_search=True, on_claim=published.append)

    assert [item["text"] for item in published] == ["应规律随访 [k1]"]
    assert transport.tools_seen == [generator.tools.specs()]
    assert result["mode"] == "vague"
    assert result["draft"] == document([claim("应规律随访", chunk_id="k1")])


def test_a_tool_turn_never_reaches_the_streamed_answer():
    from medical_rag.tools import TAVILY_TOOL, ToolCall, ToolRegistry, tavily_search_tool
    from test_generation_agent import POLICY, StubSearch

    stub = StubSearch()
    transport = AgentStreamTransport([
        (document([claim("应每月随访", chunk_id="web-1", excerpt="应每月随访一次")]),
         [ToolCall("call-1", TAVILY_TOOL, json.dumps({"query": "随访频率"}))]),
        (document([claim("应每月随访", chunk_id="web-1", excerpt="应每月随访一次")]), []),
    ])
    generator = GroundedGenerator(transport, ToolRegistry([tavily_search_tool(stub)]), POLICY)
    published = []
    generator.generate_stream("如何随访？", [evidence("k1", "高血压患者应规律随访。")],
                              "qwen", web_search=True, on_claim=published.append)

    # 调用工具的那一轮内容不展示，只有最终轮网页证据上的结论被推送。
    assert len(transport.tools_seen) == 2
    assert transport.tools_seen[0] is not None and transport.tools_seen[1] is not None
    # 自检先用原问题预取，随后那一轮才是模型自己发起的工具调用。
    assert stub.calls == ["如何随访？", "随访频率"]
    assert [item["text"] for item in published] == ["应每月随访 [web-1]"]
    assert published[0]["citations"][0]["origin"] == "web"
