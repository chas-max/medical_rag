"""Offline dataset validation and end-to-end RAG evaluation reports."""

import json
from pathlib import Path

from .evaluation_dataset import validate_evaluation_row
from .evaluation_report import build_report, new_case, score_answer, score_evidence
from .evaluation_workflow import evaluate_workflow_cases

__all__ = ["evaluate_cases", "evaluate_workflow_cases", "load_dataset"]


def load_dataset(path: Path) -> list[dict]:
    """Read and validate every JSONL row before permitting any model calls."""
    cases, ids = [], set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            raw = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {line_number} 行不是有效 JSON") from exc
        rich = isinstance(raw, dict) and "answerability" in raw
        case = validate_evaluation_row(raw, line_number, allow_empty_relevant=rich)
        if case["id"] in ids:
            raise ValueError(f"第 {line_number} 行 ID 重复")
        ids.add(case["id"])
        cases.append(case)
    if not cases:
        raise ValueError("题集为空")
    return cases


def evaluate_cases(cases: list[dict], service: object, judge: object, top_k: int,
                   answer_provider: str, judge_provider: str) -> dict:
    """Reuse each search result for both scoring and generation (legacy mode)."""
    if not 1 <= top_k <= 20 or not cases:
        raise ValueError("top_k 必须在 1 到 20 之间，题集不能为空")
    results = []
    for case in cases:
        question = case["question"]
        result = new_case(case)
        results.append(result)
        try:
            evidence = service.search(question, top_k)
            score_evidence(result, evidence, top_k)
        except Exception:  # noqa: BLE001 -- isolate and sanitize a per-case provider failure
            result["error"] = "SEARCH_FAILED"
            continue
        try:
            answer = service.generator.answer(question, evidence, answer_provider)
            result["answer"] = answer
            result["abstained"] = answer["abstained"]
        except Exception:  # noqa: BLE001 -- isolate and sanitize a per-case provider failure
            result["error"] = "ANSWER_FAILED"
            continue
        score_answer(result, answer, evidence, judge)
    return build_report(results, judge, top_k, answer_provider, judge_provider)
