"""Validation and loading for the versioned medical-health evaluation set."""

import json
from pathlib import Path
from typing import Any

ANSWERABILITIES = {"answerable", "insufficient_evidence"}
QUESTION_TYPES = {"fact", "procedure", "numeric", "scenario", "comparison", "multi_evidence"}
DIFFICULTIES = {"easy", "medium", "hard"}
VALID_RELEVANCES = {0, 1, 2}


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_chunk_list(values: Any, line_number: int, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(values, list) or (not values and not allow_empty):
        raise ValueError(f"第 {line_number} 行题目或相关块 ID 无效")
    if any(not _nonempty(item) for item in values) or len(values) != len(set(values)):
        raise ValueError(f"第 {line_number} 行题目或相关块 ID 无效")
    return values


def _validate_source_refs(refs: Any, answerability: str, line_number: int) -> None:
    if not isinstance(refs, list) or (answerability == "answerable" and not refs):
        raise ValueError(f"第 {line_number} 行来源引用无效")
    for ref in refs:
        if not isinstance(ref, dict) or not _nonempty(ref.get("source_id")) or not _nonempty(ref.get("quote")):
            raise ValueError(f"第 {line_number} 行来源引用无效")
        locator = ref.get("locator")
        if not isinstance(locator, dict) or not locator or not any(locator.values()):
            raise ValueError(f"第 {line_number} 行来源定位无效")
        if not _nonempty(ref.get("anchor_id")):
            raise ValueError(f"第 {line_number} 行稳定锚点无效")
        if "relevance" in ref:
            rel = ref["relevance"]
            if type(rel) is not int or rel not in VALID_RELEVANCES:
                raise ValueError(f"第 {line_number} 行相关度分级无效")
            if answerability == "insufficient_evidence" and rel != 0:
                raise ValueError(f"第 {line_number} 行拒答题来源相关度必须为 0")
    if answerability == "answerable" and not any(ref.get("relevance", 2) == 2 for ref in refs):
        raise ValueError(f"第 {line_number} 行可回答题至少需 1 条核心来源引用")


def _validate_chunk_grades(grades: Any, line_number: int) -> None:
    if not isinstance(grades, dict):
        raise ValueError(f"第 {line_number} 行 chunk_grades 无效")
    for key, value in grades.items():
        if not _nonempty(key) or type(value) is not int or value not in {1, 2}:
            raise ValueError(f"第 {line_number} 行 chunk_grades 无效")


def validate_evaluation_row(row: dict, line_number: int, *, allow_empty_relevant: bool = True) -> dict:
    """Validate and normalize a legacy or rich evaluation row."""
    if not isinstance(row, dict):
        raise ValueError(f"第 {line_number} 行字段不符合要求")
    required = {"question", "relevant_chunk_ids"}
    if not required <= row.keys():
        raise ValueError(f"第 {line_number} 行字段不符合要求")
    identifier = row.get("id", str(line_number))
    if not _nonempty(identifier) or not _nonempty(row.get("question")):
        raise ValueError(f"第 {line_number} 行题目或相关块 ID 无效")
    relevant = _validate_chunk_list(row["relevant_chunk_ids"], line_number, allow_empty=allow_empty_relevant)

    rich_fields = {"reference_answer", "key_points", "answerability", "category",
                   "question_type", "difficulty", "source_refs"}
    is_rich = bool(rich_fields & row.keys())
    if not is_rich:
        allowed = {"id", "question", "relevant_chunk_ids"}
        if set(row) - allowed:
            raise ValueError(f"第 {line_number} 行字段不符合要求")
        return {"id": str(identifier).strip(), "question": row["question"].strip(),
                "relevant_chunk_ids": relevant}

    if not rich_fields <= row.keys():
        raise ValueError(f"第 {line_number} 行富题字段不完整")
    if not _nonempty(row["reference_answer"]) or not _nonempty(row["category"]):
        raise ValueError(f"第 {line_number} 行富题文本无效")
    if row["answerability"] not in ANSWERABILITIES:
        raise ValueError(f"第 {line_number} 行 answerability 无效")
    if row["question_type"] not in QUESTION_TYPES or row["difficulty"] not in DIFFICULTIES:
        raise ValueError(f"第 {line_number} 行题型或难度无效")
    points = row["key_points"]
    if (not isinstance(points, list) or not points or any(not _nonempty(x) for x in points)
            or len(points) != len(set(points))):
        raise ValueError(f"第 {line_number} 行关键要点无效")
    _validate_source_refs(row["source_refs"], row["answerability"], line_number)
    if "chunk_grades" in row:
        _validate_chunk_grades(row["chunk_grades"], line_number)
    if "context_chunk_ids" in row:
        _validate_chunk_list(row["context_chunk_ids"], line_number, allow_empty=True)
    result = dict(row)
    result["id"] = str(identifier).strip()
    result["question"] = row["question"].strip()
    return result


def load_evaluation_dataset(path: Path, *, expected_count: int | None = None) -> list[dict]:
    """Load JSONL, validating every row before evaluation."""
    cases, ids = [], set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            raw = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {line_number} 行不是有效 JSON") from exc
        row = validate_evaluation_row(raw, line_number, allow_empty_relevant=True)
        if row["id"] in ids:
            raise ValueError(f"第 {line_number} 行 ID 重复")
        ids.add(row["id"])
        cases.append(row)
    if not cases:
        raise ValueError("题集为空")
    if expected_count is not None and len(cases) != expected_count:
        raise ValueError(f"题目数量应为 {expected_count}，实际为 {len(cases)}")
    return cases


def validate_quotas(cases: list[dict], manifest: dict) -> None:
    """Validate optional release quotas declared by a manifest."""
    expected = manifest.get("question_count", manifest.get("expected_count"))
    if expected is not None and len(cases) != expected:
        raise ValueError(f"题目数量应为 {expected}，实际为 {len(cases)}")
    quotas = manifest.get("quotas", {})
    legacy_answers = manifest.get("answerability_counts")
    if legacy_answers:
        quotas = {**quotas, "answerability": legacy_answers}
    for dimension, expected_values in quotas.items():
        if dimension not in {"category", "question_type", "difficulty", "answerability"}:
            continue
        if not isinstance(expected_values, dict):
            raise ValueError(f"{dimension} 配额格式无效")
        actual_counts: dict[str, int] = {}
        for item in cases:
            value = item.get(dimension)
            actual_counts[value] = actual_counts.get(value, 0) + 1
        actual = dict(sorted(actual_counts.items()))
        expected_values = dict(sorted(expected_values.items()))
        if actual != expected_values:
            raise ValueError(f"{dimension} 配额不符：{actual} != {expected_values}")
