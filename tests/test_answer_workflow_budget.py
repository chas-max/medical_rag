"""Regression guard for the v11 answer-workflow retrieval budget.

`medical-health-v2` labels 3–6+ chunks per answerable question (mean 4.4).  A budget
of 8 slots with ``max_per_parent=1`` cannot represent those labels: the child-level
recall ceiling is 0.863 and the precision ceiling is 0.549.  These assertions pin the
budget that removes that structural cap while keeping the corrective re-rank in step.
"""

import json
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


def _nodes(workflow_id: str) -> dict[str, dict]:
    path = WORKFLOWS / f"{workflow_id}.v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["status"] == "published"
    return {node["id"]: node for node in raw["nodes"]}


def test_v11_budget_can_cover_the_label_set():
    nodes = _nodes("medical-answer-v11")
    rerank = nodes["rerank"]["config"]
    assert rerank["top_k"] >= 12
    assert rerank["max_per_parent"] == 0


def test_v11_corrective_does_not_truncate_back_to_eight():
    nodes = _nodes("medical-answer-v11")
    rerank = nodes["rerank"]["config"]
    corrective = nodes["corrective"]["config"]
    assert corrective["top_k"] == rerank["top_k"]
    assert corrective["max_per_parent"] == rerank["max_per_parent"]


def test_v11_gate_keeps_calibrated_evidence_without_tail_pruning():
    nodes = _nodes("medical-answer-v11")
    gate = nodes["gate"]["config"]
    assert gate["relative_floor_ratio"] == 0
    assert gate["hard_floor"] == 0
