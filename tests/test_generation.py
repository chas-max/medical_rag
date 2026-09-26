import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator, OpenAITransport
from medical_rag.types import ApiError, Chunk, Evidence


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
    result = GroundedGenerator(transport).answer("问题", [], "deepseek")
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
    result = GroundedGenerator(Transport(response)).answer("问题", [evidence()], "deepseek")
    assert result["abstained"] is True
    assert result["citations"] == []


def test_transport_requires_provider_key_before_network():
    transport = OpenAITransport(Settings(deepseek_api_key="", qwen_api_key=""))
    with pytest.raises(ApiError, match="API Key"):
        transport.complete("deepseek", "system", "user")


def test_provider_models_are_selected_independently():
    called = []

    class Completions:
        def create(self, **kwargs):
            called.append(kwargs["model"])
            return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": "{}"})()})()]})()

    class Client:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": Completions()})()

    transport = OpenAITransport(Settings(deepseek_api_key="key", qwen_api_key="key"), client_factory=Client)
    transport.complete("deepseek", "system", "user")
    transport.complete("qwen", "system", "user")
    assert called == ["deepseek-flash", "qwen3.8-flash"]


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
