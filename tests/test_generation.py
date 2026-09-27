import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import (APIConnectionError, APIStatusError, APITimeoutError,
                    AuthenticationError, PermissionDeniedError, RateLimitError)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator, OpenAITransport
from medical_rag.types import ApiError, Chunk, Evidence


def test_settings_expose_only_qwen_provider_fields():
    assert {name for name in Settings.model_fields if name.startswith(("qwen_", "deepseek_"))} == {
        "qwen_api_key", "qwen_base_url", "qwen_model"
    }


def evidence():
    return Evidence(Chunk("block1", "doc", "guide.md", "高血压患者应规律随访。", "guide.md", "管理", 2, 0), 0.9)


class Transport:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete(self, provider, system, user):
        self.calls.append((provider, user))
        return self.result


def test_no_evidence_abstains_without_calling_model():
    transport = Transport("invalid")
    result = GroundedGenerator(transport).answer("问题", [], "qwen")
    assert result["abstained"] is True
    assert transport.calls == []


def test_every_claim_receives_a_verified_quote_and_source():
    transport = Transport(json.dumps({"claims": [{"text": "应规律随访", "citations": [{"chunk_id": "block1", "excerpt": "应规律随访"}]}]}))
    result = GroundedGenerator(transport).answer("如何管理？", [evidence()], "qwen")
    assert result["abstained"] is False
    assert "[block1]" in result["answer"]
    assert result["citations"][0]["filename"] == "guide.md"
    assert result["citations"][0]["page"] == 2


@pytest.mark.parametrize("response", [
    "not json",
    '{"claims":[{"text":"未经支持的内容","citations":[]}]}',
    '{"claims":[{"text":"内容","citations":[{"chunk_id":"other","excerpt":"应规律随访"}]}]}',
    '{"claims":[{"text":"内容","citations":[{"chunk_id":"block1","excerpt":"不存在的原文"}]}]}',
])
def test_invalid_citation_or_response_abstains(response):
    result = GroundedGenerator(Transport(response)).answer("问题", [evidence()], "qwen")
    assert result["abstained"] is True
    assert result["citations"] == []


def test_transport_requires_provider_key_before_network():
    transport = OpenAITransport(Settings(qwen_api_key=""))
    with pytest.raises(ApiError, match="API Key"):
        transport.complete("qwen", "system", "user")


def test_transport_uses_only_configured_qwen_model():
    models = []

    class Completions:
        def create(self, **kwargs):
            models.append(kwargs["model"])
            return type("Response", (), {"choices": [type("Choice", (), {
                "message": type("Message", (), {"content": "{}"})()})()]})()

    class Client:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": Completions()})()

    transport = OpenAITransport(Settings(qwen_api_key="key"), client_factory=Client)
    assert transport.complete("qwen", "system", "user") == "{}"
    assert models == ["qwen3.8-flash"]
    with pytest.raises(ApiError) as rejected:
        transport.complete("deepseek", "system", "user")
    assert rejected.value.code == "INVALID_PROVIDER"


@pytest.mark.parametrize("error_type,status,code,http_status", [
    (AuthenticationError, 401, "MODEL_AUTH_FAILED", 503),
    (PermissionDeniedError, 403, "MODEL_AUTH_FAILED", 503),
    (RateLimitError, 429, "MODEL_RATE_LIMITED", 429),
    (APIStatusError, 500, "MODEL_UPSTREAM_ERROR", 502),
])
def test_transport_maps_upstream_status_without_exposing_body(error_type, status, code, http_status):
    request = httpx.Request("POST", "https://example.invalid/chat")
    upstream = httpx.Response(status, request=request)

    class Client:
        def __init__(self, **kwargs):
            def fail(**kwargs):
                raise error_type("secret upstream", response=upstream, body={"secret": "private"})
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=fail))

    transport = OpenAITransport(Settings(qwen_api_key="key"), client_factory=Client)
    with pytest.raises(ApiError) as raised:
        transport.complete("qwen", "system", "question")
    assert (raised.value.code, raised.value.status) == (code, http_status)
    assert "secret" not in raised.value.message
    assert "private" not in raised.value.message


@pytest.mark.parametrize("error", [
    APITimeoutError(request=httpx.Request("POST", "https://example.invalid/chat")),
    APIConnectionError(request=httpx.Request("POST", "https://example.invalid/chat")),
])
def test_transport_reports_network_failure_without_raw_error(error):
    class Client:
        def __init__(self, **kwargs):
            def fail(**kwargs):
                raise error
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=fail))

    with pytest.raises(ApiError) as raised:
        OpenAITransport(Settings(qwen_api_key="key"), client_factory=Client).complete("qwen", "s", "u")
    assert (raised.value.code, raised.value.status) == ("MODEL_UNAVAILABLE", 503)


def test_draft_is_not_public_until_separate_citation_validation():
    transport = Transport(json.dumps({"claims": [{"text": "应规律随访", "citations": [
        {"chunk_id": "block1", "excerpt": "规律随访"}]}]}))
    generator = GroundedGenerator(transport)
    draft = generator.draft("如何管理？", [evidence()], "qwen")
    assert isinstance(draft, str)
    assert "claims" in draft
    result = generator.validate(draft, [evidence()])
    assert result["abstained"] is False
    assert result["citations"][0]["chunk_id"] == "block1"


def test_generation_prompt_answers_core_before_details():
    class PromptTransport:
        def __init__(self):
            self.system = ""

        def complete(self, provider, system, user):
            self.system = system
            return '{"claims":[]}'

    transport = PromptTransport()
    GroundedGenerator(transport).draft("问题", [evidence()], "qwen")
    assert "优先 3–5 条" in transport.system
    assert "先回答问题" in transport.system
