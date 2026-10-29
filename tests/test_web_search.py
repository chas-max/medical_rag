import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.config import Settings
from medical_rag.tools import TAVILY_TOOL, ToolRegistry, build_tools, tavily_search_tool
from medical_rag.types import ORIGIN_WEB, ApiError, Chunk, Evidence
from medical_rag.web_search import TavilySearch, WebPage

URL = "https://api.tavily.com/search"
REQUEST = httpx.Request("POST", URL)
PAGE = WebPage("https://example.org/a", "随访指引", "高血压患者应每月随访一次。", 0.8)


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("upstream private body", request=REQUEST,
                                        response=httpx.Response(self.status_code, request=REQUEST))

    def json(self):
        if self.payload is None:
            raise ValueError("not json")
        return self.payload


class Client:
    """Stands in for httpx.Client and records the outbound request."""

    def __init__(self, payload=None, status_code=200, error=None):
        self.calls = []
        self.payload = payload
        self.status_code = status_code
        self.error = error

    def __call__(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json, headers):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.error:
            raise self.error
        return Response(self.payload, self.status_code)


def make_search(client, **overrides):
    return TavilySearch(Settings(tavily_api_key="tv-key", **overrides), client_factory=client)


def test_missing_key_stops_before_network():
    client = Client(payload={"results": []})
    with pytest.raises(ApiError) as raised:
        TavilySearch(Settings(tavily_api_key=""), client_factory=client).search("高血压如何随访")
    assert (raised.value.code, raised.value.status) == ("WEB_SEARCH_NOT_CONFIGURED", 503)
    assert client.calls == []


@pytest.mark.parametrize("query", ["", "   ", None])
def test_blank_query_is_rejected_before_network(query):
    client = Client(payload={"results": []})
    with pytest.raises(ApiError) as raised:
        make_search(client).search(query)
    assert raised.value.code == "INVALID_QUERY"
    assert client.calls == []


def test_request_uses_bearer_token_and_configured_depth():
    client = Client(payload={"results": []})
    instance = make_search(client, tavily_search_depth="advanced", tavily_max_results=3)
    assert instance.search("  高血压如何随访  ") == []
    call = client.calls[0]
    assert call["url"] == URL
    assert call["headers"]["Authorization"] == "Bearer tv-key"
    assert call["json"] == {"query": "高血压如何随访", "search_depth": "advanced", "max_results": 3,
                            "include_answer": False, "include_raw_content": False}


def test_hits_become_web_evidence_with_url_and_length_cap():
    payload = {"results": [
        {"url": "https://example.org/a", "title": "随访指引", "content": "规" * 400, "score": 0.82},
        {"url": "https://example.org/a", "title": "同一页面", "content": "重复来源", "score": 0.4},
        {"url": "https://example.org/b", "title": "  ", "content": "   ", "score": 0.2},
        {"url": "https://example.org/c", "content": "没有标题也有正文", "score": "bad"},
    ]}
    instance = make_search(Client(payload=payload), web_content_max_chars=200, tavily_max_results=3)
    evidence = instance.to_evidence(instance.search("高血压"))

    assert len(evidence) == 2
    first = evidence[0]
    assert (first.origin, first.score) == (ORIGIN_WEB, 0.82)
    assert first.chunk.id.startswith("web-")
    assert first.chunk.id == instance.to_evidence(instance.search("高血压"))[0].chunk.id
    assert first.chunk.source_id == "https://example.org/a"
    assert first.chunk.filename == "随访指引"
    assert first.chunk.section == "https://example.org/a"
    assert len(first.chunk.text) == 200
    assert (evidence[1].chunk.filename, evidence[1].score) == ("example.org", 0.0)


@pytest.mark.parametrize("status,code,http_status", [
    (401, "WEB_SEARCH_AUTH_FAILED", 503),
    (403, "WEB_SEARCH_AUTH_FAILED", 503),
    (429, "WEB_SEARCH_RATE_LIMITED", 429),
    (400, "WEB_SEARCH_INVALID_REQUEST", 422),
    (500, "WEB_SEARCH_UPSTREAM_ERROR", 502),
])
def test_upstream_status_maps_without_exposing_body(status, code, http_status):
    instance = make_search(Client(payload={"detail": "secret-token"}, status_code=status))
    with pytest.raises(ApiError) as raised:
        instance.search("高血压")
    assert (raised.value.code, raised.value.status) == (code, http_status)
    assert "secret" not in raised.value.message


@pytest.mark.parametrize("error", [
    httpx.ConnectError("dns failed", request=REQUEST),
    httpx.ReadTimeout("slow", request=REQUEST),
])
def test_network_failure_reports_unavailable(error):
    instance = make_search(Client(error=error))
    with pytest.raises(ApiError) as raised:
        instance.search("高血压")
    assert (raised.value.code, raised.value.status) == ("WEB_SEARCH_UNAVAILABLE", 503)


@pytest.mark.parametrize("payload", [None, {"results": "not a list"}, {"code": 200}])
def test_unusable_payload_reports_upstream_error(payload):
    instance = make_search(Client(payload=payload))
    with pytest.raises(ApiError) as raised:
        instance.search("高血压")
    assert raised.value.code == "WEB_SEARCH_UPSTREAM_ERROR"


class StubSearch:
    def __init__(self, pages=(), error=None):
        self.pages = list(pages)
        self.error = error
        self.calls = []

    def search(self, query, max_results=None):
        self.calls.append(query)
        if self.error:
            raise self.error
        return self.pages

    def to_evidence(self, pages):
        return [Evidence(Chunk(f"web-{index}", "web", page.url, page.content, page.title, page.url, None, index),
                         page.score, origin=ORIGIN_WEB) for index, page in enumerate(pages)]


def registry_for(stub):
    return ToolRegistry([tavily_search_tool(stub)])


def test_tool_spec_exposes_only_a_query_parameter():
    registry = registry_for(StubSearch())
    assert registry.names() == [TAVILY_TOOL]
    assert bool(registry) is True
    assert bool(ToolRegistry()) is False
    function = registry.specs()[0]["function"]
    assert function["name"] == TAVILY_TOOL
    assert function["parameters"]["required"] == ["query"]
    assert function["parameters"]["additionalProperties"] is False


def test_tool_returns_citable_payload_and_evidence():
    stub = StubSearch([PAGE])
    outcome = registry_for(stub).execute(TAVILY_TOOL, json.dumps({"query": " 高血压随访 "}))

    assert (outcome.failed, outcome.query, stub.calls) == (False, "高血压随访", ["高血压随访"])
    payload = json.loads(outcome.content)
    assert payload["query"] == "高血压随访"
    assert payload["results"][0]["origin"] == ORIGIN_WEB
    assert payload["results"][0]["chunk_id"] == outcome.evidence[0].chunk.id
    assert payload["results"][0]["content"] == PAGE.content


def test_search_failure_comes_back_to_the_model_as_data():
    registry = registry_for(StubSearch(error=ApiError("WEB_SEARCH_UNAVAILABLE", "联网搜索服务不可用，请稍后重试", 503)))
    outcome = registry.execute(TAVILY_TOOL, {"query": "高血压"})

    assert outcome.failed is True
    assert outcome.evidence == ()
    assert json.loads(outcome.content) == {"error": "WEB_SEARCH_UNAVAILABLE",
                                           "message": "联网搜索服务不可用，请稍后重试"}


@pytest.mark.parametrize("arguments", ["not json", "", {"question": "高血压"}])
def test_tool_rejects_unusable_arguments_without_calling_search(arguments):
    stub = StubSearch([PAGE])
    outcome = registry_for(stub).execute(TAVILY_TOOL, arguments)
    assert outcome.failed is True
    assert json.loads(outcome.content)["error"] == "INVALID_TOOL_ARGUMENTS"
    assert stub.calls == []


def test_unknown_tool_is_reported_to_the_model():
    outcome = registry_for(StubSearch()).execute("calculator", "{}")
    assert outcome.failed is True
    assert json.loads(outcome.content)["error"] == "UNKNOWN_TOOL"


def test_tools_are_absent_without_a_key():
    assert build_tools(Settings(tavily_api_key="")) is None
    assert bool(build_tools(Settings(tavily_api_key="tv-key"), StubSearch())) is True
