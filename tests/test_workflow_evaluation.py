"""Offline workflow evaluation through the real repository, graph and correction loop."""

import json

import pytest
from test_evaluation import Judge, evidence
from test_service import Embedder, MemoryStore

from medical_rag import evaluation
from medical_rag.config import Settings
from medical_rag.service import RagService


class Model:
    def complete(self, provider, system, user):
        payload = json.loads(user)
        if "supporting_chunk_ids" in system:
            ids = [item["chunk_id"] for item in payload["evidence"]]
            return json.dumps({"sufficient": "supplement" in ids,
                               "missing": [] if "supplement" in ids else ["gap"],
                               "supporting_chunk_ids": ["supplement"] if "supplement" in ids else []})
        return json.dumps({"queries": ["补充"]})


class Store(MemoryStore):
    def search_dense(self, vector, limit):
        return [evidence("seed")]

    def search_bm25(self, question, limit):
        return [evidence("supplement")] if question == "补充" else [evidence("seed")]


class Rank:
    def score(self, question, texts):
        return [0.9] * len(texts)


class Generator:
    transport = Model()
    tools = ()

    def __init__(self):
        self.on_generate = None
        self.fail = False

    def generate(self, question, items, provider, **kwargs):
        if self.on_generate:
            self.on_generate()
        if self.fail:
            raise RuntimeError("secret provider response")
        return {"draft": "回答", "evidence": items + [evidence("web-1"), evidence("web-2")]}

    def validate(self, draft, items, truncated=False):
        return {"answer": draft, "citations": [], "abstained": False}


def setup(tmp_path):
    generator = Generator()
    service = RagService(Settings(_env_file=None), Store(), Embedder(), Rank(), generator,
                         workflow_root=tmp_path)
    return service, generator


def cases(count=1, insufficient=False):
    return [{"id": str(index), "question": "问题", "relevant_chunk_ids": [] if insufficient else ["web-2"],
             **({"answerability": "insufficient_evidence"} if insufficient else {})}
            for index in range(count)]


async def evaluate(service, judge=None, rows=None, **kwargs):
    function = getattr(evaluation, "evaluate_workflow_cases", None)
    assert callable(function), "Missing async workflow-aware evaluator"
    return await function(rows or cases(), service, judge or Judge(), 5, "qwen", "judge",
                          "medical-answer-agentic", **kwargs)


@pytest.mark.asyncio
async def test_executes_correction_graph_and_judges_all_final_generation_evidence(tmp_path):
    service, _ = setup(tmp_path)

    class EvidenceJudge(Judge):
        def score_faithfulness(self, question, answer, contexts):
            assert set(contexts) == {"证据 seed", "证据 supplement", "证据 web-1", "证据 web-2"}
            return super().score_faithfulness(question, answer, contexts)

    report = await evaluate(service, EvidenceJudge())
    row = report["cases"][0]
    assert {item["chunk_id"] for item in row["evidence"]} == {"seed", "supplement", "web-1", "web-2"}
    assert row["retrieval"]["recall"] == 1
    assert len(row["context_relevance"]["items"]) == 4
    assert row["retrieval_rounds"] == 1
    assert row["agent_model_calls"] == 3
    assert row["latency_ms"] >= 0
    assert row["workflow_id"] == "medical-answer-agentic" and row["workflow_version"] == 1
    assert row["agentic"][0]["stop_reason"] == "sufficient"
    assert "model_calls" not in row
    assert report["config"]["workflow_version"] == 1
    assert service.runs[row["run_id"]].trace_by_node("agentic").status == "success"


@pytest.mark.asyncio
async def test_resolves_published_once_not_latest_draft_and_pins_during_cases(tmp_path):
    service, generator = setup(tmp_path)
    first = service.repository.get("medical-answer-agentic", 1)
    draft = first.model_copy(deep=True, update={"version": 3, "status": "draft"})
    service.repository.save(draft)

    def publish():
        generator.on_generate = None
        second = first.model_copy(deep=True, update={"version": 2})
        next(node for node in second.nodes if node.type == "evidence.gate").config["threshold"] = 1
        service.repository.save(second)

    generator.on_generate = publish
    report = await evaluate(service, rows=cases(2))
    assert service.repository.get_published(first.id).version == 2
    assert all(row["workflow_version"] == 1 and row["abstained"] is False for row in report["cases"])


@pytest.mark.asyncio
async def test_preserves_judge_errors_without_exposing_provider_response(tmp_path):
    service, _ = setup(tmp_path)
    report = await evaluate(service, Judge(fail="web-2"))
    assert report["summary"]["judge_failures"] == 1
    assert report["cases"][0]["context_relevance"]["score"] is None
    assert report["cases"][0]["faithfulness"]["score"] == 1
    assert "private-key" not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("insufficient", [False, True])
async def test_abstention_and_insufficient_semantics(tmp_path, insufficient):
    service, _ = setup(tmp_path)
    graph = service.repository.get("medical-answer-agentic", 1).model_copy(deep=True, update={"version": 2})
    next(node for node in graph.nodes if node.type == "evidence.gate").config["threshold"] = 1
    service.repository.save(graph)
    judge = Judge()
    report = await evaluate(service, judge, cases(insufficient=insufficient), workflow_version=2)
    row = report["cases"][0]
    assert row["abstained"] is True and not judge.calls
    assert report["summary"]["abstention_count"] == 1
    if insufficient:
        assert row["correct_abstention"] is True and row["unsupported_answer"] is False
        assert row["retrieval"] is None and report["summary"]["recall"] is None
    else:
        assert row["retrieval"]["recall"] == 0


@pytest.mark.asyncio
async def test_insufficient_nonabstention_records_unsupported_answer(tmp_path):
    service, _ = setup(tmp_path)
    judge = Judge()
    report = await evaluate(service, judge, cases(insufficient=True))
    assert report["cases"][0]["unsupported_answer"] is True
    assert report["summary"]["unsupported_answer_rate"] == 1 and not judge.calls


@pytest.mark.asyncio
async def test_failed_run_preserves_final_snapshot_stats_and_continues(tmp_path):
    service, generator = setup(tmp_path)
    generator.fail = True
    report = await evaluate(service, rows=cases(2))
    assert len(report["cases"]) == 2
    for row in report["cases"]:
        assert row["error"] == "WORKFLOW_FAILED" and row["run_status"] == "failed"
        assert row["evidence"] == [] and row["answer"] is None
        assert service.runs[row["run_id"]].evidence == []
        assert row["retrieval"] is None
        assert row["agent_model_calls"] == 3 and row["retrieval_rounds"] == 1
    assert "secret" not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_id,version", [("local-retrieval-correction", 1),
                                                 ("missing", None), ("bad/path", 1),
                                                 ("medical-answer", 1), ("medical-answer", 0)])
async def test_invalid_selection_rejected_before_any_execution(tmp_path, workflow_id, version):
    service, _ = setup(tmp_path)
    function = getattr(evaluation, "evaluate_workflow_cases", None)
    assert callable(function), "Missing async workflow-aware evaluator"
    with pytest.raises(ValueError):
        await function(cases(), service, Judge(), 5, "qwen", "judge", workflow_id, version)
    assert not service.runs


@pytest.mark.asyncio
@pytest.mark.parametrize("rows,k,provider", [([], 5, "qwen"), (cases(), 0, "qwen"),
                                           ([{"question": " "}], 5, "qwen"),
                                           (cases(), 5, " ")])
async def test_invalid_inputs_rejected_before_graph(tmp_path, rows, k, provider):
    service, _ = setup(tmp_path)
    function = getattr(evaluation, "evaluate_workflow_cases", None)
    assert callable(function), "Missing async workflow-aware evaluator"
    with pytest.raises(ValueError):
        await function(rows, service, Judge(), k, provider, "judge", "medical-answer-agentic")
    assert not service.runs


@pytest.mark.asyncio
async def test_nonagent_workflow_has_unavailable_not_total_model_call_count(tmp_path):
    service, _ = setup(tmp_path)
    function = getattr(evaluation, "evaluate_workflow_cases", None)
    assert callable(function)
    report = await function(cases(), service, Judge(), 5, "qwen", "judge", "medical-answer")
    row = report["cases"][0]
    assert row["agent_model_calls"] is None and row["retrieval_rounds"] is None
    assert row["agentic"] == [] and row["error"] is None


@pytest.mark.asyncio
async def test_nonanswer_workflow_has_safe_answer_failure(tmp_path):
    service, _ = setup(tmp_path)
    function = getattr(evaluation, "evaluate_workflow_cases", None)
    assert callable(function)
    report = await function(cases(), service, Judge(), 5, "qwen", "judge", "medical-search")
    assert report["cases"][0]["error"] == "ANSWER_FAILED"
    assert report["cases"][0]["evidence"] and report["summary"]["judge_failures"] == 0


@pytest.mark.asyncio
async def test_raised_workflow_error_is_sanitized_and_later_case_still_runs(tmp_path, monkeypatch):
    service, _ = setup(tmp_path)
    original = service.run_workflow
    attempts = 0

    async def unavailable(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private response")
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "run_workflow", unavailable)
    report = await evaluate(service, rows=cases(2))
    assert report["cases"][0]["error"] == "WORKFLOW_FAILED"
    assert report["cases"][0]["latency_ms"] >= 0
    assert report["cases"][1]["error"] is None
    assert "private response" not in json.dumps(report)
