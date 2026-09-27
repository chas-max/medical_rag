"""Compile source-checked human-authored drafts into a release JSONL."""

import json
from pathlib import Path

from .evaluation_anchors import normalize_source_text
from .evaluation_authoring import materialize_draft
from .evaluation_dataset import validate_evaluation_row
from .evaluation_tools import _atomic_write


def _materialize_composites(draft_path: Path, category: str, base_rows: list[dict]) -> list[dict]:
    """Combine independently sourced, already verified excerpts without inventing anchors."""
    lines = draft_path.read_text(encoding="utf-8").splitlines()
    expected = "id|question_type|difficulty|answerability|question|reference_answer|key_points|evidence_ids"
    if not lines or lines[0] != expected:
        raise ValueError("综合题草稿表头无效")
    by_id = {row["id"]: row for row in base_rows}
    result = []
    for line_number, line in enumerate(lines[1:], 2):
        cells = line.split("|")
        if len(cells) != 8:
            raise ValueError(f"综合题第 {line_number} 行列数无效")
        identifier, question_type, difficulty, answerability, question, answer, points, evidence = cells
        ids = [item.strip() for item in evidence.split("；")]
        if len(ids) < 2 or len(set(ids)) != len(ids) or any(item not in by_id for item in ids):
            raise ValueError(f"题目 {identifier} 的证据题目 ID 缺失或重复")
        refs = [ref for item in ids for ref in by_id[item]["source_refs"]]
        if len({ref["source_id"] for ref in refs}) < 2:
            raise ValueError(f"题目 {identifier} 需要至少两个不同来源")
        row = {"id": identifier, "question": question, "reference_answer": answer,
               "key_points": [point.strip() for point in points.split("；")],
               "answerability": answerability, "category": category,
               "question_type": question_type, "difficulty": difficulty,
               "source_refs": refs, "relevant_chunk_ids": []}
        result.append(validate_evaluation_row(row, line_number))
    return result


def compile_release(index_path: Path, output_path: Path, *, expected_count: int = 500) -> int:
    """Write only a complete, unique, consecutively identified question bank."""
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(index, dict) or not isinstance(index.get("drafts"), list):
        raise ValueError("草稿索引格式无效")
    rows = []
    for entry in index["drafts"]:
        if not isinstance(entry, dict) or set(entry) != {"draft", "source_id", "source_file", "category"}:
            raise ValueError("草稿来源配置无效")
        rows.extend(materialize_draft(
            index_path.parent / entry["draft"], entry["source_id"],
            index_path.parent / entry["source_file"], entry["category"],
        ))
    for entry in index.get("composites", []):
        if not isinstance(entry, dict) or set(entry) != {"draft", "category"}:
            raise ValueError("综合题草稿配置无效")
        rows.extend(_materialize_composites(index_path.parent / entry["draft"],
                                             entry["category"], rows))
    if len(rows) != expected_count:
        raise ValueError(f"正式题库须有 {expected_count} 题，当前只有 {len(rows)} 题")
    rows.sort(key=lambda item: item["id"])
    expected_ids = [f"MHE-{number:04d}" for number in range(1, expected_count + 1)]
    if [row["id"] for row in rows] != expected_ids:
        raise ValueError("题目 ID 缺失、重复或不连续")
    normalized_questions = [normalize_source_text(row["question"]).casefold() for row in rows]
    if len(set(normalized_questions)) != len(rows):
        raise ValueError("题目问题重复")
    if output_path.exists():
        raise ValueError("输出文件已存在，请勿覆盖正式题库")
    _atomic_write(output_path, "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                                       + "\n" for row in rows))
    return len(rows)
