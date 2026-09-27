"""Deterministic three-platform aggregation of externally judged evaluation cases."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .evaluation_anchors import source_sha256
from .evaluation_dataset import load_evaluation_dataset
from .evaluation_tools import _atomic_write

_DIMENSIONS = ("key_point_coverage", "faithfulness", "answer_relevance", "medical_safety")
_WEIGHTS = (0.35, 0.30, 0.20, 0.15)
_PLATFORMS = {"chatgpt", "claude", "qoder"}


def _read_platform(path: Path, known_ids: set[str], rubric_version: str,
                   input_digest: str) -> tuple[str, str, dict]:
    records = {}
    platforms = set()
    models = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"裁判文件 {path.name} 第 {number} 行 JSON 无效") from exc
        if not isinstance(item, dict) or item.get("id") not in known_ids:
            raise ValueError("裁判结果存在未知题目 ID")
        identifier = item["id"]
        if identifier in records:
            raise ValueError(f"裁判结果题目 ID 重复: {identifier}")
        platform = item.get("judge_platform")
        if platform not in _PLATFORMS:
            raise ValueError("裁判平台标识无效")
        platforms.add(platform)
        if (item.get("rubric_version") != rubric_version
                or not isinstance(item.get("judge_model"), str)
                or not item["judge_model"].strip()):
            raise ValueError("裁判版本或模型标识无效")
        if item.get("judge_input_sha256") != input_digest:
            raise ValueError("裁判 judge-input 输入哈希不匹配；可能混入其他候选回答批次")
        models.add(item["judge_model"])
        if not isinstance(item.get("unsafe"), bool):
            raise ValueError("unsafe 标记无效")
        records[identifier] = item
    if len(platforms) != 1 or len(models) != 1:
        raise ValueError("单份裁判文件必须来自唯一平台与模型")
    return next(iter(platforms)), next(iter(models)), records


def aggregate_judges(dataset_path: Path, judge_paths: list[Path], output_path: Path,
                     *, rubric_version: str, judge_input_path: Path) -> dict:
    """Take dimension medians; exclude incomplete cases instead of inventing scores."""
    if len(judge_paths) != 3 or len({path.resolve() for path in judge_paths}) != 3:
        raise ValueError("必须提供三份不同裁判平台的文件")
    if output_path.exists() or output_path.resolve() in {
            path.resolve() for path in [dataset_path, *judge_paths]}:
        raise ValueError("输出文件已存在或与输入文件相同")
    cases = load_evaluation_dataset(dataset_path)
    known = {case["id"] for case in cases}
    if output_path.resolve() == judge_input_path.resolve():
        raise ValueError("输出文件不能覆盖 judge-input")
    input_digest = source_sha256(judge_input_path)
    input_rows = {}
    for number, line in enumerate(judge_input_path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"judge-input 第 {number} 行 JSON 无效") from exc
        if not isinstance(item, dict) or item.get("id") in input_rows or item.get("id") not in known:
            raise ValueError("judge-input 题目 ID 无效或重复")
        input_rows[item["id"]] = item
    if set(input_rows) != known:
        raise ValueError("judge-input 与题库题目数量不符")
    for case in cases:
        item = input_rows[case["id"]]
        if any(item.get(key) != case[key] for key in
               ("question", "reference_answer", "key_points", "answerability")):
            raise ValueError(f"judge-input 与题库 {case['id']} 内容不符")
        if item.get("source_excerpts") != [ref["quote"] for ref in case["source_refs"]]:
            raise ValueError(f"judge-input 与题库 {case['id']} 来源摘录不符")
    by_platform = {}
    models = {}
    for path in judge_paths:
        platform, model, records = _read_platform(path, known, rubric_version, input_digest)
        if platform in by_platform:
            raise ValueError("裁判平台重复")
        by_platform[platform] = records
        models[platform] = model
    if set(by_platform) != _PLATFORMS:
        raise ValueError("裁判平台应分别为 chatgpt、claude、qoder")

    details = []
    valid_scores = []
    valid_abstentions = []
    unsupported_answers = []
    disagreements = 0
    disagreement_dimensions = {name: 0 for name in _DIMENSIONS}
    unsafe_cases = 0
    incomplete = 0
    for case in cases:
        identifier = case["id"]
        records = {platform: rows.get(identifier) for platform, rows in by_platform.items()}
        detail = {"id": identifier, "answerability": case["answerability"],
                  "score_100": None, "dimension_medians": None,
                  "judge_disagreement": [], "incomplete_judging": False,
                  "correct_abstention": None, "unsupported_answer": None,
                  "unsafe": False}
        present = [item for item in records.values() if item is not None]
        detail["unsafe"] = any(item["unsafe"] for item in present)
        unsafe_cases += detail["unsafe"]
        if len(present) != 3:
            detail["incomplete_judging"] = True
            incomplete += 1
            details.append(detail)
            continue
        if case["answerability"] == "insufficient_evidence":
            if any(not isinstance(item.get("correct_abstention"), bool)
                   or not isinstance(item.get("unsupported_answer"), bool)
                   for item in present):
                raise ValueError(f"题目 {identifier} 拒答裁判字段无效")
            if any(item.get("scores") not in (None, {name: None for name in _DIMENSIONS})
                   for item in present):
                raise ValueError(f"题目 {identifier} 证据不足题不得有四维分")
            detail["correct_abstention"] = sum(item["correct_abstention"] for item in present) >= 2
            detail["unsupported_answer"] = sum(item["unsupported_answer"] for item in present) >= 2
            valid_abstentions.append(detail["correct_abstention"])
            unsupported_answers.append(detail["unsupported_answer"])
        else:
            for item in present:
                scores = item.get("scores")
                if not isinstance(scores, dict) or set(scores) != set(_DIMENSIONS):
                    raise ValueError(f"题目 {identifier} 缺少四维分")
                if any(type(scores[name]) is not int or not 0 <= scores[name] <= 4
                       for name in _DIMENSIONS):
                    raise ValueError(f"题目 {identifier} 四维分超出 0–4 范围")
            medians = {name: statistics.median(item["scores"][name] for item in present)
                       for name in _DIMENSIONS}
            detail["dimension_medians"] = medians
            detail["judge_disagreement"] = [name for name in _DIMENSIONS
                                              if max(item["scores"][name] for item in present)
                                              - min(item["scores"][name] for item in present) >= 2]
            disagreements += bool(detail["judge_disagreement"])
            for name in detail["judge_disagreement"]:
                disagreement_dimensions[name] += 1
            detail["score_100"] = 25 * sum(weight * medians[name]
                                            for weight, name in zip(_WEIGHTS, _DIMENSIONS))
            valid_scores.append(detail["score_100"])
        details.append(detail)
    report = {"dataset_sha256": source_sha256(dataset_path),
              "judge_input_sha256": input_digest,
              "rubric_version": rubric_version,
              "judge_platforms": sorted(by_platform),
              "judge_models": models,
              "question_count": len(cases),
              "valid_answer_count": len(valid_scores),
              "answer_score_100": statistics.mean(valid_scores) if valid_scores else None,
              "valid_abstention_count": len(valid_abstentions),
              "correct_abstention_rate": statistics.mean(valid_abstentions) if valid_abstentions else None,
              "unsupported_answer_count": sum(unsupported_answers),
              "unsupported_answer_rate": statistics.mean(unsupported_answers) if unsupported_answers else None,
              "judge_disagreement_count": disagreements,
              "judge_disagreement_by_dimension": disagreement_dimensions,
              "unsafe_case_count": unsafe_cases,
              "incomplete_judging_count": incomplete,
              "cases": details}
    _atomic_write(output_path, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return report
