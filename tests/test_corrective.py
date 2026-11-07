"""Rule-2 feedback: the corrective round must be bounded, honest and never regress."""

import asyncio
import json

import pytest

from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.short_term import ShortTerm
from medical_rag.types import SCORE_DENSE, SCORE_RERANK, SCORE_UNKNOWN, Chunk, Evidence
from medical_rag.workflow.bootstrap import build_registry
from medical_rag.workflow.contracts import RERANK_DEGRADED_VARIABLE, ExecutionContext
from medical_rag.workflow.corrective import CorrectiveNode, _needs_correction

SHORT = ShortTerm(recent=({"turn": 1, "user": "医生给我开了氨氯地平",
                           "assistant": "氨氯地平是常见的降压药。"},))
QUESTION = "那它要吃多久？"


def _evidence(chunk_id: str, score: float, kind: str = SCORE_RERANK) -> Evidence:
    chunk = Chunk(chunk_id, "d1", "guide.md", f"文本 {chunk_id}", "guide.md", "用药", 1, 0)
    return Evidence(chunk, score, score_kind=kind)


class Store:
    def __init__(self, chunks):
        self.chunks = chunks
        self.bm25_queries = []

    def search_dense(self, vector, limit):
        return [Evidence(chunk, 0.8, score_kind=SCORE_DENSE) for chunk in self.chunks][:limit]

    def search_bm25(self, question, limit):
        self.bm25_queries.append(question)
        return [Evidence(chunk, 1.0, score_kind=SCORE_DENSE) for chunk in self.chunks
                if question in chunk.text][:limit]


class Embedder:
    def encode(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


class Reranker:
    def __init__(self, score: float):
        self.value = score

    def score(self, question, texts):
        return [self.value for _ in texts]


class Transport:
    def __init__(self, rewritten: str = "氨氯地平要吃多久"):
        self.rewritten = rewritten
        self.calls = []

    def complete(self, provider, system, user):
        payload = json.loads(user)
        self.calls.append(payload)
        if "recent" in payload:
            return json.dumps({"question": self.rewritten}, ensure_ascii=False)
        return json.dumps({"hypothetical": "假设性回答"}, ensure_ascii=False)


def _node(chunks, transport, reranker_score: float = 0.9, threshold: float = 0.5):
    store = Store(chunks)
    registry = build_registry(store, Embedder(), Reranker(reranker_score),
                              GroundedGenerator(transport), Settings())
    node = CorrectiveNode(registry, transport, "qwen", threshold=threshold, top_k=5)
    return node, store


def _run(node, evidence, config=None):
    context = ExecutionContext("run", "medical-answer", {}, short_term=SHORT)
    return asyncio.run(node.execute({"question": QUESTION, "evidence": evidence},
                                    config or {}, context))


# -- the rule ---------------------------------------------------------------

@pytest.mark.parametrize("evidence,degraded,expected", [
    ([], False, (True, "too_few")),
    ([_evidence("a", 0.9)], False, (True, "too_few")),
    ([_evidence("a", 0.9), _evidence("b", 0.9)], False, (False, "sufficient")),
    ([_evidence("a", 0.2), _evidence("b", 0.3)], False, (True, "low_score")),
    ([_evidence("a", 0.9, SCORE_DENSE), _evidence("b", 0.9, SCORE_DENSE)], False,
     (False, "sufficient")),
    ([_evidence("a", 0.9, SCORE_UNKNOWN), _evidence("b", 0.9, SCORE_UNKNOWN)], False,
     (True, "low_score")),
    ([_evidence("a", 0.9), _evidence("b", 0.9)], True, (False, "degraded")),
])
def test_rule_two_only_trusts_comparable_scores(evidence, degraded, expected):
    assert _needs_correction(evidence, 2, 0.5, degraded) == expected


# -- the round --------------------------------------------------------------

def test_weak_results_trigger_one_rewritten_retrieval():
    chunks = [
        Chunk("c1", "d1", "guide.md", "氨氯地平要吃多久 规律随访", "guide.md", "用药", 1, 0),
        Chunk("c2", "d1", "guide.md", "氨氯地平要吃多久 常见不良反应", "guide.md", "用药", 1, 1),
    ]
    transport = Transport()
    node, store = _node(chunks, transport)

    result = _run(node, [])

    report = result.outputs["report"]
    assert report["stop_reason"] == "retried"
    assert report["rounds"] == 1
    assert report["queries"] == ["氨氯地平要吃多久"]
    assert len(result.outputs["evidence"]) == 2
    # 重写拿到的是会话上下文，而不是裸问题。
    assert transport.calls[0]["recent"][0]["user"] == "医生给我开了氨氯地平"
    assert store.bm25_queries == ["氨氯地平要吃多久"]


def test_confident_results_never_call_the_model_again():
    transport = Transport()
    node, store = _node([], transport)

    result = _run(node, [_evidence("a", 0.9), _evidence("b", 0.8)])

    assert result.outputs["report"]["stop_reason"] == "sufficient"
    assert transport.calls == []
    assert store.bm25_queries == []


def test_degraded_reranker_does_not_retry():
    transport = Transport()
    node, _ = _node([], transport)
    context = ExecutionContext("run", "medical-answer",
                               {RERANK_DEGRADED_VARIABLE: {"reason": "reranker_unavailable"}})

    result = asyncio.run(node.execute({"question": QUESTION, "evidence": []}, {}, context))

    assert result.outputs["report"]["stop_reason"] == "degraded"
    assert transport.calls == []


def test_identical_rewrite_does_not_run_a_second_retrieval():
    transport = Transport(rewritten=QUESTION)
    node, store = _node([], transport)

    result = _run(node, [])

    assert result.outputs["report"]["stop_reason"] == "unchanged_query"
    assert store.bm25_queries == []


def test_retry_that_finds_nothing_keeps_the_first_pass_evidence():
    chunks = [
        Chunk("c1", "d1", "guide.md", "氨氯地平要吃多久 规律随访", "guide.md", "用药", 1, 0),
        Chunk("c2", "d1", "guide.md", "氨氯地平要吃多久 常见不良反应", "guide.md", "用药", 1, 1),
    ]
    transport = Transport()
    weak = [_evidence("a", 0.2)]
    # 第二轮检索能拿回候选，但重排仍全部低于门限。
    node, _ = _node(chunks, transport, reranker_score=0.1)

    result = _run(node, weak)

    assert result.outputs["report"]["stop_reason"] == "retry_empty"
    assert result.outputs["evidence"] == weak


def test_exhausted_budget_keeps_the_first_pass_without_a_model_call():
    transport = Transport()
    node, store = _node([], transport)

    result = _run(node, [], {"max_rounds": 0})

    assert result.outputs["report"]["stop_reason"] == "max_rounds"
    assert transport.calls == []
    assert store.bm25_queries == []


def test_corrective_reports_only_safe_fields():
    chunks = [
        Chunk("c1", "d1", "guide.md", "氨氯地平要吃多久 规律随访", "guide.md", "用药", 1, 0),
        Chunk("c2", "d1", "guide.md", "氨氯地平要吃多久 常见不良反应", "guide.md", "用药", 1, 1),
    ]
    node, _ = _node(chunks, Transport())

    report = _run(node, []).outputs["report"]

    assert set(report) <= {"stop_reason", "rounds", "queries", "before", "after",
                           "history_turns", "failed_node"}


# -- history recall (rule 2 only) -------------------------------------------

class History:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.calls = []

    def recall(self, conversation_id, seed):
        self.calls.append((conversation_id, seed))
        if self.error is not None:
            raise self.error
        return list(self.rows)


RECALLED = [{"turn": 2, "text": "问：医生开了氨氯地平\n答：每天一次"}]
WEAK_CHUNKS = [
    Chunk("c1", "d1", "guide.md", "氨氯地平要吃多久 规律随访", "guide.md", "用药", 1, 0),
    Chunk("c2", "d1", "guide.md", "氨氯地平要吃多久 常见不良反应", "guide.md", "用药", 1, 1),
]


def _node_with_history(history, collection=None):
    store = Store(collection if collection is not None else WEAK_CHUNKS)
    transport = Transport()
    registry = build_registry(store, Embedder(), Reranker(0.9), GroundedGenerator(transport),
                              Settings())
    return CorrectiveNode(registry, transport, "qwen", threshold=0.5, top_k=5,
                          history=history), transport, store


def _run_scoped(node, evidence, conversation_id="conversation-1"):
    context = ExecutionContext("run", "medical-answer", {}, short_term=SHORT,
                               conversation_id=conversation_id)
    return asyncio.run(node.execute({"question": QUESTION, "evidence": evidence}, {}, context))


def test_rule_two_recalls_history_and_feeds_it_to_the_rewrite():
    history = History(RECALLED)
    node, transport, _ = _node_with_history(history)

    result = _run_scoped(node, [])

    conversation_id, seed = history.calls[0]
    assert conversation_id == "conversation-1"
    # 种子包含上一轮用户原话，避免拿一个纯代词去检索。
    assert "氨氯地平" in seed and QUESTION in seed
    assert result.outputs["history"] == RECALLED
    assert result.outputs["report"]["history_turns"] == 1
    assert transport.calls[0]["history"] == RECALLED


def test_confident_results_never_touch_the_history_index():
    history = History(RECALLED)
    node, _, _ = _node_with_history(history)

    _run_scoped(node, [_evidence("a", 0.9), _evidence("b", 0.9)])

    assert history.calls == []


def test_history_recall_needs_a_conversation_id():
    history = History(RECALLED)
    node, _, _ = _node_with_history(history)

    result = _run_scoped(node, [], conversation_id="")

    assert history.calls == []
    assert "history" not in result.outputs


def test_history_recall_failure_does_not_break_the_correction():
    history = History(error=RuntimeError("index down"))
    node, _, _ = _node_with_history(history)

    result = _run_scoped(node, [])

    assert result.outputs["report"]["stop_reason"] == "retried"
    assert "history" not in result.outputs
    assert len(result.outputs["evidence"]) == 2
