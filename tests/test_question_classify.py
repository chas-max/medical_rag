import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError, APITimeoutError

from medical_rag.config import Settings
from medical_rag.generation import OpenAITransport
from medical_rag.types import ApiError
from medical_rag.workflow.bootstrap import build_registry
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.definition import WorkflowDefinition, WorkflowEdge, WorkflowNode
from medical_rag.workflow.nodes.question import CLASSIFY_FALLBACK_ROUTE, classify_question
from medical_rag.workflow.validator import validate_workflow


class FakeCompleteTransport:
    def __init__(self, reply: object = "", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[dict[str, object]] = []

    def complete_with_timeout(
        self,
        provider: str,
        system: str,
        user: str,
        timeout_seconds: float,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        max_attempts: int = 2,
    ) -> object:
        self.calls.append(
            {
                "provider": provider,
                "system": system,
                "user": json.loads(user),
                "timeout_seconds": timeout_seconds,
                "json_mode": json_mode,
                "max_tokens": max_tokens,
                "max_attempts": max_attempts,
            }
        )
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.mark.parametrize(
    "route",
    [
        "direct_lookup",
        "numeric_exact",
        "multi_doc_compare",
        "procedure_ordered",
        "scenario_guarded",
    ],
)
def test_classify_question_accepts_all_five_routes(route: str) -> None:
    ctx = ExecutionContext("run-1", "medical-answer-routed", {})
    transport = FakeCompleteTransport(json.dumps({"route": route, "confidence": 0.9}))

    result = classify_question({"question": "高血压随访频率？"}, {}, ctx, transport, "qwen")

    assert result == {"route": route, "confidence": 0.9}
    assert ctx.node_metadata["route"] == route
    assert ctx.node_metadata["raw_route"] == route
    assert ctx.node_metadata["confidence"] == 0.9
    assert ctx.node_metadata["fallback_reason"] is None
    assert len(transport.calls) == 1
    assert transport.calls[0]["timeout_seconds"] == 3.0
    assert transport.calls[0]["json_mode"] is True
    assert transport.calls[0]["max_tokens"] == 64
    assert transport.calls[0]["max_attempts"] == 1


def test_classify_question_low_confidence_falls_back() -> None:
    ctx = ExecutionContext("run-2", "medical-answer-routed", {})
    transport = FakeCompleteTransport(json.dumps({"route": "direct_lookup", "confidence": 0.59}))

    result = classify_question({"question": "血压多少需要用药？"}, {}, ctx, transport, "qwen")

    assert result == {"route": CLASSIFY_FALLBACK_ROUTE, "confidence": 0.59}
    assert ctx.node_metadata["route"] == CLASSIFY_FALLBACK_ROUTE
    assert ctx.node_metadata["raw_route"] == "direct_lookup"
    assert ctx.node_metadata["fallback_reason"] == "low_confidence"


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        "{}",
        '{"route":"unknown","confidence":0.9}',
        '{"route":"direct_lookup"}',
        '{"route":"direct_lookup","confidence":true}',
        '{"route":"direct_lookup","confidence":1.2}',
        '{"route":"direct_lookup","confidence":-0.1}',
        '{"route":[],"confidence":0.8}',
    ],
)
def test_classify_question_invalid_outputs_fall_back_safely(reply: str) -> None:
    ctx = ExecutionContext("run-3", "medical-answer-routed", {})
    transport = FakeCompleteTransport(reply)

    result = classify_question({"question": "问题"}, {}, ctx, transport, "qwen")

    assert result == {"route": CLASSIFY_FALLBACK_ROUTE, "confidence": 0.0}
    assert ctx.node_metadata["fallback_reason"] == "invalid_output"


@pytest.mark.parametrize(
    ("error", "expected_reason"),
    [
        (TimeoutError("timed out"), "timeout"),
        (ApiError("MODEL_RATE_LIMITED", "限流", 429), "transport_error"),
    ],
)
def test_classify_question_transport_failures_fall_back_safely(
    error: Exception, expected_reason: str
) -> None:
    ctx = ExecutionContext("run-4", "medical-answer-routed", {})
    transport = FakeCompleteTransport(error=error)

    result = classify_question({"question": "问题"}, {}, ctx, transport, "qwen")

    assert result == {"route": CLASSIFY_FALLBACK_ROUTE, "confidence": 0.0}
    assert ctx.node_metadata["fallback_reason"] == expected_reason


@pytest.mark.parametrize("exc_kind", ["timeout", "server_503"])
def test_complete_with_timeout_max_attempts_one_does_not_retry(exc_kind: str) -> None:
    request = httpx.Request("POST", "https://example.invalid/chat/completions")
    if exc_kind == "timeout":
        err: Exception = APITimeoutError(request=request)
    else:
        err = APIStatusError("unavailable", response=httpx.Response(503, request=request), body={})

    calls_single: list[int] = []

    class SingleFailClient:
        def __init__(self, **kwargs):
            def create(**kw):
                calls_single.append(1)
                raise err

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    transport = OpenAITransport(Settings(_env_file=None, qwen_api_key="key"), client_factory=SingleFailClient)
    with pytest.raises(ApiError):
        transport.complete_with_timeout("qwen", "sys", "usr", 3.0, max_attempts=1)
    assert len(calls_single) == 1

    calls_default: list[int] = []

    class DefaultFailClient:
        def __init__(self, **kwargs):
            def create(**kw):
                calls_default.append(1)
                raise err

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    transport_default = OpenAITransport(
        Settings(_env_file=None, qwen_api_key="key"), client_factory=DefaultFailClient
    )
    with pytest.raises(ApiError):
        transport_default.complete_with_timeout("qwen", "sys", "usr", 3.0)
    assert len(calls_default) == 2


def test_question_classify_registered_and_validates_in_minimal_graph() -> None:
    registry = build_registry(None, None, None, None, Settings(_env_file=None))
    node = registry.get("question.classify")
    assert node.outputs == {"route": "selector", "confidence": "number"}

    definition = WorkflowDefinition(
        id="classify-smoke",
        name="Classify Smoke",
        version=1,
        status="draft",
        nodes=[
            WorkflowNode.model_validate({"id": "query", "type": "query.input"}),
            WorkflowNode.model_validate({"id": "classify", "type": "question.classify"}),
            WorkflowNode.model_validate(
                {
                    "id": "decompose",
                    "type": "question.decompose",
                    "activation": {"port": "route", "equals": "multi_doc_compare"},
                }
            ),
            WorkflowNode.model_validate({"id": "merge", "type": "merge.collect"}),
            WorkflowNode.model_validate({"id": "multi", "type": "retrieval.multi"}),
            WorkflowNode.model_validate({"id": "rerank", "type": "retrieval.rerank"}),
            WorkflowNode.model_validate({"id": "gate", "type": "evidence.gate"}),
            WorkflowNode.model_validate({"id": "llm", "type": "generation.llm"}),
            WorkflowNode.model_validate({"id": "citation", "type": "citation.validate"}),
            WorkflowNode.model_validate({"id": "output", "type": "answer.output"}),
        ],
        edges=[
            WorkflowEdge.model_validate({"id": "e1", "source": "query", "source_port": "question", "target": "classify", "target_port": "question"}),
            WorkflowEdge.model_validate({"id": "e2", "source": "query", "source_port": "question", "target": "decompose", "target_port": "question"}),
            WorkflowEdge.model_validate({"id": "e3", "source": "classify", "source_port": "route", "target": "decompose", "target_port": "route"}),
            WorkflowEdge.model_validate({"id": "e4", "source": "query", "source_port": "question", "target": "merge", "target_port": "items"}),
            WorkflowEdge.model_validate({"id": "e5", "source": "decompose", "source_port": "queries", "target": "merge", "target_port": "items"}),
            WorkflowEdge.model_validate({"id": "e6", "source": "merge", "source_port": "values", "target": "multi", "target_port": "queries"}),
            WorkflowEdge.model_validate({"id": "e7", "source": "multi", "source_port": "evidence", "target": "rerank", "target_port": "evidence"}),
            WorkflowEdge.model_validate({"id": "e8", "source": "query", "source_port": "question", "target": "rerank", "target_port": "question"}),
            WorkflowEdge.model_validate({"id": "e9", "source": "rerank", "source_port": "evidence", "target": "gate", "target_port": "evidence"}),
            WorkflowEdge.model_validate({"id": "e10", "source": "gate", "source_port": "evidence", "target": "llm", "target_port": "evidence"}),
            WorkflowEdge.model_validate({"id": "e11", "source": "query", "source_port": "question", "target": "llm", "target_port": "question"}),
            WorkflowEdge.model_validate({"id": "e12", "source": "llm", "source_port": "draft", "target": "citation", "target_port": "draft"}),
            WorkflowEdge.model_validate({"id": "e13", "source": "llm", "source_port": "evidence", "target": "citation", "target_port": "evidence"}),
            WorkflowEdge.model_validate({"id": "e14", "source": "citation", "source_port": "answer", "target": "output", "target_port": "answer"}),
        ],
    )
    assert validate_workflow(definition, registry) == []
