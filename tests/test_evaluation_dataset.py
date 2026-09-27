import json

import pytest

from medical_rag.evaluation_dataset import (
    load_evaluation_dataset,
    validate_evaluation_row,
    validate_quotas,
)


def rich_row(**overrides):
    row = {
        "id": "MHE-0001",
        "question": "高血压患者如何测量血压？",
        "reference_answer": "应按规范测量并记录。",
        "key_points": ["测量前静坐", "正确坐姿"],
        "answerability": "answerable",
        "category": "高血压",
        "question_type": "procedure",
        "difficulty": "medium",
        "source_refs": [{
            "source_id": "guide",
            "locator": {"page": 1, "heading": "测量"},
            "quote": "测量前静坐。",
            "anchor_id": "guide#测量#abc123",
        }],
        "relevant_chunk_ids": ["chunk-1"],
    }
    row.update(overrides)
    return row


def test_rich_dataset_preserves_metadata(tmp_path):
    path = tmp_path / "rich.jsonl"
    path.write_text(json.dumps(rich_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    loaded = load_evaluation_dataset(path, expected_count=1)
    assert loaded[0]["key_points"] == ["测量前静坐", "正确坐姿"]
    assert loaded[0]["source_refs"][0]["anchor_id"] == "guide#测量#abc123"


@pytest.mark.parametrize("field,value", [
    ("answerability", "unknown"),
    ("question_type", "essay"),
    ("difficulty", "expert"),
    ("key_points", ["重复", "重复"]),
    ("source_refs", []),
    ("relevant_chunk_ids", ["chunk-1", "chunk-1"]),
])
def test_invalid_rich_rows_are_rejected(field, value):
    with pytest.raises(ValueError):
        validate_evaluation_row(rich_row(**{field: value}), 1)


def test_source_form_and_insufficient_evidence_allow_empty_chunks():
    assert validate_evaluation_row(rich_row(relevant_chunk_ids=[]), 1)["id"] == "MHE-0001"
    row = rich_row(answerability="insufficient_evidence", relevant_chunk_ids=[])
    assert validate_evaluation_row(row, 1)["answerability"] == "insufficient_evidence"


def test_validate_quotas_checks_declared_counts():
    rows = [rich_row(id="MHE-0001"), rich_row(id="MHE-0002", answerability="insufficient_evidence")]
    manifest = {"expected_count": 2, "answerability_counts": {"answerable": 1, "insufficient_evidence": 1}}
    validate_quotas(rows, manifest)
    with pytest.raises(ValueError):
        validate_quotas(rows, {"expected_count": 3})
