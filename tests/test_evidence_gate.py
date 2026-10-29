"""Regression tests for reranker degradation and the evidence gate.

The original failure mode: when the cross-encoder could not load, the lexical
fallback scored any chunk sharing one character above the reranker's 0.5 gate
threshold, so unrelated evidence produced a cited answer instead of an abstention.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.embeddings import BgeReranker
from medical_rag.generation import ABSTENTION, DEGRADED_ABSTENTION
from medical_rag.types import (
    SCORE_LEXICAL,
    SCORE_RERANK,
    Chunk,
    Evidence,
)
from medical_rag.workflow.contracts import CAPABILITY_WEB_SEARCH, ExecutionContext
from medical_rag.workflow.nodes import evidence as evidence_node


class FailingCrossEncoder:
    def predict(self, pairs):
        raise OSError("本地模型缓存缺失")


def block(block_id="c1", text="糖尿病患者的血糖管理"):
    return Chunk(block_id, "doc", "guide.md", text, "guide.md", "管理", 1, 0)


def item(score, kind, text="糖尿病患者的血糖管理"):
    return Evidence(block(text=text), score, score_kind=kind)


def run_gate(items, capabilities=None, fail_closed=True):
    context = ExecutionContext("run", "w", {}, capabilities or {})
    result = evidence_node.gate({"evidence": items}, {}, context,
                                threshold=0.5, fail_closed=fail_closed)
    return result, context


def test_lexical_fallback_scores_a_single_shared_character_as_zero():
    reranker = BgeReranker(model=FailingCrossEncoder())
    scores, kind = reranker.score_with_kind("高血压如何随访", ["糖尿病患者的血糖管理"])

    # 只共享「血」一个字：既不够 2 个实词，也不能够到 0.5 的重排门槛。
    assert kind == SCORE_LEXICAL
    assert scores == [0.0]


def test_lexical_fallback_requires_most_query_content_words():
    reranker = BgeReranker(model=FailingCrossEncoder())
    scores, kind = reranker.score_with_kind(
        "高血压如何随访", ["高血压患者应规律随访", "糖尿病患者的血糖管理"])

    assert kind == SCORE_LEXICAL
    assert scores[0] >= 0.6
    assert scores[1] == 0.0


def test_gate_fails_closed_on_lexical_scores_even_when_they_look_high():
    result, context = run_gate([item(0.9, SCORE_LEXICAL)])

    assert result.outputs["abstained"] is True
    assert result.outputs["answer"] == DEGRADED_ABSTENTION
    assert result.outputs["citations"] == []
    assert result.outputs["degraded"] is True
    assert result.outputs["degraded_reason"] == "reranker_unavailable"
    assert context.node_metadata["degraded"] is True


def test_gate_only_uses_the_conservative_lexical_rule_when_fail_closed_is_off():
    result, _ = run_gate([item(0.7, SCORE_LEXICAL), item(0.4, SCORE_LEXICAL, "无关块")],
                         fail_closed=False)

    assert result["abstained"] is False
    assert [entry.score for entry in result["evidence"]] == [0.7]
    assert result["degraded"] is True


def test_gate_rejects_evidence_whose_score_scale_is_unknown():
    result, _ = run_gate([item(0.99, "unknown")])

    assert result.outputs["abstained"] is True
    assert result.outputs["answer"] == ABSTENTION


def test_gate_accepts_rerank_scores_and_rejects_below_the_cutoff():
    result, _ = run_gate([item(0.6, SCORE_RERANK), item(0.4, SCORE_RERANK, "弱相关块")])

    assert result["abstained"] is False
    assert [entry.score for entry in result["evidence"]] == [0.6]


def test_gate_still_defers_to_web_search_when_local_reranking_is_down():
    result, _ = run_gate([item(0.9, SCORE_LEXICAL)], {CAPABILITY_WEB_SEARCH: True})

    assert result["abstained"] is False
    assert result["evidence"] == []
    assert result["degraded"] is True
