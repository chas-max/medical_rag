"""Offline integration tests for the editable bounded correction graph."""

import asyncio
import json
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_service import Embedder, MemoryStore

from medical_rag.api import create_app
from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.service import RagService
from medical_rag.types import ApiError, Chunk, Evidence
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.definition import WorkflowEdge, WorkflowNode
from medical_rag.workflow.validator import validate_workflow


def item(key):
    return Evidence(
        Chunk(key, "doc", "source", "original useful text", "guide", "", None, 0), 0.9
    )


class Script:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.payloads = []

    def complete(self, provider, system, user):
        self.payloads.append(json.loads(user))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response)


def assessment(sufficient=False, ids=()):
    return {
        "sufficient": sufficient,
        "missing": [] if sufficient else ["gap"],
        "supporting_chunk_ids": list(ids),
    }


class Search(MemoryStore):
    def __init__(self, batches):
        super().__init__()
        self.batches = iter(batches)
        self.questions = []

    def search_bm25(self, question, limit):
        self.questions.append(question)
        return next(self.batches)


class Rank:
    def __init__(self):
        self.questions = []

    def score(self, question, texts):
        self.questions.append(question)
        return [0.9] * len(texts)


def setup(tmp_path, responses=(), batches=()):
    transport, store, rank = Script(responses), Search(batches), Rank()
    service = RagService(
        Settings(),
        store,
        Embedder(),
        rank,
        GroundedGenerator(transport),
        workflow_root=tmp_path,
    )
    # The real persisted child graph is editable; choose BM25 for deterministic search.
    child = service.repository.get("local-retrieval-correction", 1)
    child = child.model_copy(deep=True, update={"id": "test-child"})
    next(node for node in child.nodes if node.type == "retrieval.multi").config[
        "strategy"
    ] = "bm25"
    service.repository.save(child)
    return service, transport, store, rank


async def correction(service, evidence, config=None, events=None):
    context = ExecutionContext(
        "run",
        "outer",
        {},
        emit=events.append if events is not None else None,
        node_id="custom-wrapper",
    )
    return await service.registry.get("retrieval.agentic").execute(
        {"question": "Original Question", "evidence": evidence, "top_k": 5},
        {"workflow_id": "test-child", "workflow_version": 1, **(config or {})},
        context,
    )


@pytest.mark.asyncio
async def test_sufficient_seed_skips_search(tmp_path):
    service, model, store, _ = setup(tmp_path, [assessment(True, ["a"])])
    events = []
    result = await correction(service, [item("a")], events=events)
    assert result.outputs["evidence"] == [item("a")]
    assert result.outputs["report"]["stop_reason"] == "sufficient"
    assert result.outputs["report"]["retrieval_rounds"] == 0
    assert result.outputs["report"]["iterations"][0]["assessment"] == assessment(
        True, ["a"]
    )
    assert len(model.payloads) == 1 and store.questions == []
    assert {event["data"]["node_id"] for event in events} == {"custom-wrapper"}
    assert [event["data"]["stage"] for event in events] == ["assessing", "stopped"]
    assert result.metadata["agentic"] == result.outputs["report"]
    assert "original useful text" not in json.dumps(result.metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("rounds", [1, 2])
async def test_repairs_then_assesses_without_overwriting_original(tmp_path, rounds):
    responses, batches = [], []
    for index in range(rounds):
        responses += [
            assessment(),
            {
                "queries": [
                    f" New {index} ",
                    f"new {index}",
                    f"second {index}",
                    "ignored",
                ]
            },
        ]
        batches += [[item(f"new-{index}")], [item(f"new-{index}")]]
    responses += [assessment(True, [f"new-{rounds - 1}"])]
    service, model, store, rank = setup(tmp_path, responses, batches)
    result = await correction(service, [])
    report = result.outputs["report"]
    assert (
        report["stop_reason"] == "sufficient" and report["retrieval_rounds"] == rounds
    )
    assert report["model_calls"] == 2 * rounds + 1
    assert len(result.outputs["evidence"]) == rounds
    assert store.questions == [
        query
        for index in range(rounds)
        for query in [f"New {index}", f"second {index}"]
    ]
    assert rank.questions == ["Original Question"] * rounds
    assert all(payload["question"] == "Original Question" for payload in model.payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize("rerank_k", [3, 15])
@pytest.mark.parametrize("fail_second_rank", [False, True])
async def test_correction_reranks_fresh_hits_and_retains_useful_evidence(
    tmp_path, rerank_k, fail_second_rank
):
    def hit(key):
        evidence = item(key)
        return replace(evidence, chunk=replace(evidence.chunk, text=key))

    batches = [
        [hit(f"round-{round_number}-{index}") for index in range(20)]
        for round_number in (1, 2)
    ]
    # A repeated hit must consume only one slot even across different rounds.
    batches[1].append(batches[0][0])
    service, model, _, rank = setup(
        tmp_path,
        [
            assessment(),
            {"queries": ["first gap"]},
            assessment(),
            {"queries": ["second gap"]},
            assessment(True, ["round-2-0"]),
        ],
        batches,
    )
    service.registry.get("retrieval.rerank").handler.keywords["rerank_k"] = rerank_k
    pools = []

    def score(question, texts):
        rank.questions.append(question)
        pools.append(texts)
        if fail_second_rank and len(pools) == 2:
            raise ValueError("offline reranker failure")
        return [1.0 if text == "round-2-0" else 0.9 for text in texts]

    rank.score = score
    result = await correction(service, [])
    report = result.outputs["report"]
    assert len(pools) == 2
    assert "round-2-0" in pools[1]
    assert set(pools[0][:5]).issubset(pools[1])
    assert all(len(pool) == len(set(pool)) for pool in pools)
    assert all(len(pool) <= max(rerank_k, 5) + 5 for pool in pools)
    assert rank.questions == ["Original Question"] * 2
    assert all(payload["question"] == "Original Question" for payload in model.payloads)
    assert report["retrieval_rounds"] == 2
    if fail_second_rank:
        assert report["stop_reason"] == "retrieval_failed"
        assert [e.chunk.id for e in result.outputs["evidence"]] == pools[0][:5]
    else:
        assert report["stop_reason"] == "sufficient"
        assert result.outputs["evidence"][0].chunk.id == "round-2-0"
        assert "round-1-0" in {e.chunk.id for e in result.outputs["evidence"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses,batches,reason",
    [
        ([assessment(), {"queries": [" original question "]}], [], "repeated_query"),
        ([assessment(), {"queries": ["new"]}], [[]], "no_new_evidence"),
        ([ValueError("secret")], [], "assessment_failed"),
        (
            [{"sufficient": True, "missing": [], "supporting_chunk_ids": ["fake"]}],
            [],
            "assessment_failed",
        ),
        ([assessment(), {"queries": "not-list"}], [], "planning_failed"),
        ([assessment(), {"queries": ["new"], "extra": True}], [], "planning_failed"),
    ],
)
async def test_failure_and_stopping_return_last_valid_evidence(
    tmp_path, responses, batches, reason
):
    service, _, _, _ = setup(tmp_path, responses, batches)
    result = await correction(service, [item("seed")])
    assert result.status == "success"
    assert result.outputs["evidence"] == [item("seed")]
    assert result.outputs["report"]["stop_reason"] == reason
    assert "secret" not in json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_budget_zero_assesses_only_and_empty_cannot_be_sufficient(tmp_path):
    service, model, store, _ = setup(tmp_path, [assessment(True)])
    result = await correction(service, [], {"max_rounds": 0})
    assert result.outputs["report"]["stop_reason"] == "assessment_failed"
    assert store.questions == [] and len(model.payloads) == 1


@pytest.mark.asyncio
async def test_max_rounds_has_final_assessment_only(tmp_path):
    service, model, store, _ = setup(
        tmp_path, [assessment(), {"queries": ["new"]}, assessment()], [[item("b")]]
    )
    result = await correction(service, [item("a")], {"max_rounds": 1})
    assert result.outputs["report"]["stop_reason"] == "max_rounds"
    assert store.questions == ["new"] and len(model.payloads) == 3


@pytest.mark.asyncio
async def test_timeout_does_not_wait_for_late_model_or_emit_after_stop(tmp_path):
    service, model, _, _ = setup(tmp_path)

    def slow(*args):
        time.sleep(0.15)
        return json.dumps(assessment(True, ["a"]))

    model.complete = slow
    events = []
    started = time.monotonic()
    result = await correction(service, [item("a")], {"timeout_seconds": 0.02}, events)
    assert time.monotonic() - started < 0.1
    assert result.outputs["report"]["stop_reason"] == "timeout"
    trace = result.outputs["report"]["iterations"][0]["trace"]
    assert any(
        entry["node_id"] == "assess"
        and entry["status"] == "failed"
        and entry["error"]["code"] == "TIMEOUT"
        for entry in trace
    )
    event_count = len(events)
    await asyncio.sleep(0.18)
    assert len(events) == event_count
    assert result.outputs["evidence"] == [item("a")]


def test_editor_and_iteration_run_are_not_public(tmp_path):
    service, _, _, _ = setup(tmp_path)
    client = TestClient(create_app(service=service))
    assert client.get("/nodes").status_code == 404
    assert client.get("/workflows").status_code == 404
    assert client.post("/workflows/local-retrieval-correction/runs", json={"inputs": {}}).status_code == 404


def test_default_and_new_graphs_validate(tmp_path):
    service, _, _, _ = setup(tmp_path)
    for graph in service.repository.list_published():
        assert not validate_workflow(graph, service.registry), graph.id
    assert service.repository.get("medical-answer").version == 3


@pytest.mark.parametrize(
    "change,expected",
    [
        ("missing", "INVALID_AGENT_REFERENCE"),
        ("draft", "INVALID_AGENT_REFERENCE"),
        ("boundary", "INVALID_AGENT_BOUNDARY"),
        ("nested", "INVALID_NODE_KIND"),
        ("cycle", "CYCLE"),
        ("unsafe", "UNSAFE_AGENT_EVIDENCE"),
    ],
)
def test_invalid_child_rejected_at_save_and_run(tmp_path, change, expected):
    service, _, _, _ = setup(tmp_path)
    outer = service.repository.get("medical-answer-agentic", 1).model_copy(deep=True)
    wrapper = next(node for node in outer.nodes if node.type == "retrieval.agentic")
    wrapper.config["workflow_id"] = "bad-child"
    child = service.repository.get("test-child", 1).model_copy(
        deep=True, update={"id": "bad-child"}
    )
    if change == "draft":
        child.status = "draft"
    elif change == "boundary":
        child.nodes[0].type = "query.input"
    elif change == "nested":
        child.nodes[1].type = "retrieval.agentic"
    elif change == "cycle":
        child.edges[0].source = "output"
    elif change == "unsafe":
        # A fabricated evidence output declared on a model producer is not a source.
        service.registry.get("evidence.assess").outputs["fabricated"] = "evidence_list"
        next(
            edge
            for edge in child.edges
            if edge.target == "output" and edge.target_port == "evidence"
        ).source = "assess"
        next(
            edge
            for edge in child.edges
            if edge.target == "output" and edge.target_port == "evidence"
        ).source_port = "fabricated"
    if change != "missing":
        service.repository.save(child)
    issues = validate_workflow(outer, service.registry)
    assert expected in {issue.code for issue in issues}
    outer.id = "invalid-outer"
    with pytest.raises(ApiError, match="校验"):
        service.save_workflow(outer)
    service.repository.save(outer)
    with pytest.raises(ApiError, match="校验"):
        asyncio.run(service.run_workflow(outer.id, {"question": "original"}))


@pytest.mark.asyncio
async def test_retrieval_failure_counts_attempted_round_and_keeps_seed(tmp_path):
    service, _, store, _ = setup(tmp_path, [assessment(), {"queries": ["new"]}])

    def broken(*args):
        raise ValueError("private failure")

    store.search_bm25 = broken
    result = await correction(service, [item("a")])
    assert result.outputs["report"]["stop_reason"] == "retrieval_failed"
    assert result.outputs["report"]["retrieval_rounds"] == 1
    assert result.outputs["evidence"] == [item("a")]


@pytest.mark.asyncio
async def test_edited_child_output_is_the_effective_evidence(tmp_path):
    service, _, _, _ = setup(
        tmp_path,
        [assessment(), {"queries": ["new"]}, assessment(True, ["b"])],
        [[item("b")]],
    )
    child = service.repository.get("test-child", 1).model_copy(
        deep=True, update={"id": "direct-child"}
    )
    # Replace cumulative output wiring with the real retrieval result. The controller
    # must honor this edit, not substitute its internal accumulator's result.
    child.nodes = [
        node
        for node in child.nodes
        if node.type not in {"evidence.accumulate", "retrieval.rerank"}
    ]
    child.edges = [
        edge
        for edge in child.edges
        if edge.source not in {"accumulate", "rerank"}
        and edge.target not in {"accumulate", "rerank"}
    ]
    child.edges.append(
        WorkflowEdge(
            id="direct",
            source="retrieve",
            source_port="evidence",
            target="output",
            target_port="evidence",
        )
    )
    service.repository.save(child)
    assert not validate_workflow(child, service.registry)
    result = await correction(service, [item("a")], {"workflow_id": "direct-child"})
    assert result.outputs["report"]["stop_reason"] == "sufficient"
    assert [e.chunk.id for e in result.outputs["evidence"]] == ["b"]


@pytest.mark.asyncio
async def test_duplicate_edited_retrievers_do_not_repeat_dispatch(tmp_path):
    service, _, store, _ = setup(
        tmp_path,
        [assessment(), {"queries": ["one", "two"]}, assessment(True, ["b"])],
        [[item("b")], [item("b")]],
    )
    child = service.repository.get("test-child", 1).model_copy(
        deep=True, update={"id": "branched-child"}
    )
    child.nodes.append(
        WorkflowNode(
            id="duplicate", type="retrieval.multi", config={"strategy": "bm25"}
        )
    )
    child.edges.extend(
        [
            WorkflowEdge(
                id="duplicate-q",
                source="refine",
                source_port="queries",
                target="duplicate",
                target_port="queries",
            ),
            WorkflowEdge(
                id="duplicate-e",
                source="duplicate",
                source_port="evidence",
                target="accumulate",
                target_port="sources",
            ),
        ]
    )
    service.repository.save(child)
    assert not validate_workflow(child, service.registry)
    result = await correction(service, [], {"workflow_id": "branched-child"})
    assert result.outputs["report"]["stop_reason"] == "sufficient"
    assert store.questions == ["one", "two"]


@pytest.mark.asyncio
async def test_queries_are_seen_across_rounds_and_max_queries_is_enforced(tmp_path):
    service, _, store, _ = setup(
        tmp_path,
        [
            assessment(),
            {"queries": ["one", "two"]},
            assessment(),
            {"queries": ["ONE", "third"]},
            assessment(True, ["c"]),
        ],
        [[item("b")], [item("c")]],
    )
    result = await correction(service, [], {"max_queries": 1})
    assert result.outputs["report"]["stop_reason"] == "sufficient"
    assert store.questions == ["one", "third"]


@pytest.mark.asyncio
async def test_exact_published_version_pinning_and_missing_version_rejection(tmp_path):
    service, _, _, _ = setup(tmp_path, batches=[[]])
    old = service.repository.get("medical-answer", 3)
    new = old.model_copy(
        deep=True, update={"id": "pin", "version": 1, "status": "published"}
    )
    service.repository.save(new)
    service.repository.save(new.model_copy(update={"version": 2, "status": "draft"}))
    assert (
        await service.run_workflow("pin", {"question": "original"}, version=1)
    ).status == "success"
    with pytest.raises(ApiError, match="已发布"):
        await service.run_workflow("pin", {"question": "original"}, version=2)
    context = ExecutionContext("run", "outer", {})
    with pytest.raises(ApiError):
        await service.registry.get("retrieval.agentic").execute(
            {"question": "q", "evidence": []}, {"workflow_id": "test-child"}, context
        )


@pytest.mark.asyncio
async def test_sync_and_stream_public_runs_share_answer_citations_and_progress(
    tmp_path,
):
    from test_service import Transport

    class Model(Transport):
        def complete(self, provider, system, user):
            payload = json.loads(user)
            if "supporting_chunk_ids" in system:
                return json.dumps(
                    assessment(True, [payload["evidence"][0]["chunk_id"]])
                )
            return super().complete(provider, system, user)

        def stream_chat(self, provider, messages, tools=None, on_content=None):
            raw = self.complete(
                provider, messages[0]["content"], messages[-1]["content"]
            )
            if on_content is not None:
                on_content(raw)
            return {"content": raw, "tool_calls": []}

    store = MemoryStore()
    service = RagService(
        Settings(),
        store,
        Embedder(),
        Rank(),
        GroundedGenerator(Model()),
        workflow_root=tmp_path,
    )
    await service.run_workflow(
        "document-ingestion", {"filename": "guide.md", "content": "应规律随访".encode()}
    )
    inputs = {"question": "Original Question", "provider": "qwen", "top_k": 5}
    sync = await service.run_workflow("medical-answer-agentic", inputs)
    events = []
    streamed = await service.run_workflow(
        "medical-answer-agentic", inputs, events.append
    )
    assert sync.output == streamed.output
    assert sync.evidence == streamed.evidence
    assert sync.output["citations"][0]["chunk_id"] == sync.evidence[0].chunk.id
    assert (
        streamed.trace_by_node("agentic").metadata["agentic"]["stop_reason"]
        == "sufficient"
    )
    assert any(event["event"] == "node.progress" for event in events)


def test_duplicate_model_protocol_nodes_are_rejected(tmp_path):
    service, _, _, _ = setup(tmp_path)
    child = service.repository.get("test-child", 1).model_copy(deep=True)
    child.nodes.append(WorkflowNode(id="second-planner", type="question.refine"))
    assert "INVALID_AGENT_OPERATION" in {
        issue.code for issue in validate_workflow(child, service.registry)
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, 61, True])
async def test_invalid_deadlines_are_graph_errors_not_fallback(tmp_path, value):
    service, _, _, _ = setup(tmp_path)
    with pytest.raises(ApiError):
        await correction(service, [], {"timeout_seconds": value})


@pytest.mark.asyncio
async def test_container_pins_child_v1_even_when_v2_is_published(tmp_path):
    service, _, store, _ = setup(
        tmp_path,
        [assessment(), {"queries": ["new"]}, assessment(True, ["b"])],
        [[item("b")]],
    )
    v2 = service.repository.get("test-child", 1).model_copy(
        deep=True, update={"version": 2}
    )
    next(node for node in v2.nodes if node.type == "retrieval.multi").config[
        "strategy"
    ] = "dense"
    service.repository.save(v2)
    assert service.repository.get("test-child").version == 2
    result = await correction(service, [])
    assert result.outputs["report"]["stop_reason"] == "sufficient"
    assert store.questions == ["new"]


@pytest.mark.asyncio
async def test_split_dense_bm25_edited_child_is_a_real_search_graph(tmp_path):
    service, _, store, _ = setup(
        tmp_path,
        [assessment(), {"queries": ["new"]}, assessment(True, ["b"])],
        [[item("b")]],
    )
    store.search_dense = lambda vector, limit: [item("dense")]
    child = service.repository.get("test-child", 1).model_copy(
        deep=True, update={"id": "split-child"}
    )
    child.nodes.append(
        WorkflowNode(
            id="dense-branch", type="retrieval.multi", config={"strategy": "dense"}
        )
    )
    child.edges.extend(
        [
            WorkflowEdge(
                id="split-q",
                source="refine",
                source_port="queries",
                target="dense-branch",
                target_port="queries",
            ),
            WorkflowEdge(
                id="split-e",
                source="dense-branch",
                source_port="evidence",
                target="accumulate",
                target_port="sources",
            ),
        ]
    )
    service.repository.save(child)
    result = await correction(service, [], {"workflow_id": "split-child"})
    assert result.outputs["report"]["stop_reason"] == "sufficient"
    assert {e.chunk.id for e in result.outputs["evidence"]} == {"b", "dense"}


@pytest.mark.asyncio
async def test_final_answer_evidence_not_overwritten_by_unrelated_late_branch(tmp_path):
    from medical_rag.workflow.nodes.base import BuiltinNode

    service, _, _, _ = setup(tmp_path)
    graph = service.repository.get("medical-answer-agentic", 1).model_copy(deep=True)
    # Exercise the actual engine using adapters without external generation.
    service.registry.register(
        BuiltinNode(
            "test.answer",
            "answer",
            "generation",
            {},
            {"answer": "answer"},
            lambda inputs, config, context: {
                "answer": {"answer": "ok", "citations": [], "abstained": False}
            },
        )
    )
    service.registry.register(
        BuiltinNode(
            "test.cite",
            "cite",
            "citation",
            {"answer": "answer", "evidence": "evidence_list"},
            {"answer": "answer"},
            lambda inputs, config, context: {"answer": inputs["answer"]},
        )
    )
    service.registry.register(
        BuiltinNode(
            "test.evidence",
            "evidence",
            "retrieval",
            {},
            {"evidence": "evidence_list"},
            lambda inputs, config, context: {
                "evidence": [item(config.get("id", "web"))]
            },
            {"id": {"type": "string"}},
        )
    )
    graph.nodes = [
        WorkflowNode(id="answer", type="test.answer"),
        WorkflowNode(id="evidence", type="test.evidence"),
        WorkflowNode(id="cite", type="test.cite"),
        WorkflowNode(id="output", type="answer.output"),
        WorkflowNode(id="late", type="test.evidence", config={"id": "unrelated"}),
    ]
    graph.edges = [
        WorkflowEdge(
            id="a-c",
            source="answer",
            source_port="answer",
            target="cite",
            target_port="answer",
        ),
        WorkflowEdge(
            id="e-c",
            source="evidence",
            source_port="evidence",
            target="cite",
            target_port="evidence",
        ),
        WorkflowEdge(
            id="c-o",
            source="cite",
            source_port="answer",
            target="output",
            target_port="answer",
        ),
        # An ordering dependency ensures the unrelated evidence is last.
        WorkflowEdge(
            id="o-l",
            source="output",
            source_port="answer",
            target="late",
            target_port="ignored",
        ),
    ]
    result = await service.engine.run(graph, {})
    assert [e.chunk.id for e in result.evidence] == ["web"]


@pytest.mark.asyncio
async def test_timeout_retrieval_does_not_mutate_fallback_with_late_hits(tmp_path):
    service, _, store, _ = setup(tmp_path, [assessment(), {"queries": ["new"]}])

    def slow(*args):
        time.sleep(0.15)
        return [item("late")]

    store.search_bm25 = slow
    events = []
    started = time.monotonic()
    result = await correction(service, [item("a")], {"timeout_seconds": 0.03}, events)
    assert time.monotonic() - started < 0.12
    assert result.outputs["report"]["stop_reason"] == "timeout"
    assert result.outputs["report"]["retrieval_rounds"] == 1
    count = len(events)
    await asyncio.sleep(0.18)
    assert len(events) == count
    assert result.outputs["evidence"] == [item("a")]


def test_transport_remaining_deadline_preserves_generation_default():
    from types import SimpleNamespace

    from medical_rag.generation import OpenAITransport

    timeouts = []

    def factory(**kwargs):
        timeouts.append(kwargs["timeout"])
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
        )
        return SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_: response)
            )
        )

    transport = OpenAITransport(
        Settings(_env_file=None, qwen_api_key="offline"), client_factory=factory
    )
    assert transport.complete_with_timeout("qwen", "system", "user", 0.25) == "{}"
    assert transport.complete_with_timeout("qwen", "system", "user", 0.0001) == "{}"
    assert transport.complete("qwen", "system", "user") == "{}"
    assert timeouts == [0.25, 0.0001, 120.0]


@pytest.mark.asyncio
async def test_runtime_state_is_not_shared_between_runs(tmp_path):
    service, _, store, _ = setup(
        tmp_path,
        [
            assessment(),
            {"queries": ["new"]},
            assessment(True, ["b"]),
            assessment(),
            {"queries": ["new"]},
            assessment(True, ["b"]),
        ],
        [[item("b")], [item("b")]],
    )
    first = await correction(service, [])
    second = await correction(service, [])
    assert (
        first.outputs["report"]["stop_reason"]
        == second.outputs["report"]["stop_reason"]
        == "sufficient"
    )
    assert store.questions == ["new", "new"]


@pytest.mark.asyncio
async def test_public_json_cannot_impersonate_private_runtime(tmp_path):
    service, _, _, _ = setup(tmp_path, batches=[[]])
    result = await service.run_workflow(
        "medical-search",
        {"question": "original", "_agent_runtime": {"allow_repair": True}},
    )
    assert result.status == "success"


@pytest.mark.asyncio
async def test_ordinary_lock_wait_does_not_block_heartbeat_or_correction_deadline(
    tmp_path,
):
    service, model, store, _ = setup(tmp_path, [assessment(), {"queries": ["new"]}])
    entered = threading.Event()

    def late_search(*args):
        entered.set()
        time.sleep(0.20)
        return [item("late")]

    store.search_bm25 = late_search
    first = await correction(service, [item("a")], {"timeout_seconds": 0.03})
    assert first.outputs["report"]["stop_reason"] == "timeout" and entered.is_set()

    def late_assessment(*args):
        time.sleep(0.15)
        return json.dumps(assessment(True, ["a"]))

    model.complete = late_assessment
    ordinary = asyncio.create_task(
        service.registry.get("retrieval.dense").execute(
            {"question": "ordinary"}, {}, ExecutionContext("ordinary", "search", {})
        )
    )
    started = time.monotonic()
    second = asyncio.create_task(
        correction(service, [item("a")], {"timeout_seconds": 0.03})
    )
    await asyncio.sleep(0.01)
    heartbeat_ms = (time.monotonic() - started) * 1000
    result = await second
    elapsed = time.monotonic() - started
    await ordinary
    await asyncio.sleep(0.18)
    assert heartbeat_ms < 70
    assert elapsed < 0.10
    assert result.outputs["report"]["stop_reason"] == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy,slow_operation",
    [
        ("bm25", "bm25"),
        ("hybrid", "embed"),
        ("hybrid", "dense"),
        ("hybrid", "bm25"),
    ],
)
async def test_multi_never_dispatches_another_heavy_leg_after_timeout(
    tmp_path, strategy, slow_operation
):
    service, _, store, _ = setup(tmp_path, [assessment(), {"queries": ["one", "two"]}])
    child = service.repository.get("test-child", 1).model_copy(
        deep=True, update={"id": "timeout-child"}
    )
    next(node for node in child.nodes if node.type == "retrieval.multi").config[
        "strategy"
    ] = strategy
    service.repository.save(child)
    calls = []

    def work(operation):
        def invoke(*args):
            calls.append((operation, time.monotonic()))
            if operation == slow_operation:
                time.sleep(0.12)
            return [[1.0] + [0.0] * 511] if operation == "embed" else [item("late")]

        return invoke

    store.search_bm25 = work("bm25")
    store.search_dense = work("dense")
    service.registry.get("retrieval.multi").handler.keywords["embedder"].encode = work(
        "embed"
    )
    events = []
    result = await correction(
        service,
        [item("a")],
        {"workflow_id": "timeout-child", "timeout_seconds": 0.04},
        events,
    )
    stopped = time.monotonic()
    count = len(events)
    await asyncio.sleep(0.30)
    assert result.outputs["report"]["stop_reason"] == "timeout"
    assert not [(name, began) for name, began in calls if began > stopped]
    assert len(events) == count and result.outputs["evidence"] == [item("a")]


@pytest.mark.asyncio
async def test_cancelled_multi_does_not_start_next_query_before_deadline(tmp_path):
    service, _, store, _ = setup(tmp_path, [assessment(), {"queries": ["one", "two"]}])
    entered = threading.Event()
    calls = []

    def slow(query, limit):
        calls.append(query)
        entered.set()
        time.sleep(0.12)
        return [item("late")]

    store.search_bm25 = slow
    task = asyncio.create_task(correction(service, [], {"timeout_seconds": 60}))
    while not entered.is_set():
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.30)
    assert calls == ["one"]
