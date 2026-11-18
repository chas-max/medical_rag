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


def test_validate_quotas_checks_all_declared_dimensions():
    rows = [rich_row()]
    manifest = {"question_count": 1, "quotas": {
        "category": {"高血压": 1}, "question_type": {"procedure": 1},
        "difficulty": {"medium": 1}, "answerability": {"answerable": 1},
    }}
    validate_quotas(rows, manifest)
    for dimension, value in (("category", "其他"), ("question_type", "fact"),
                             ("difficulty", "easy"), ("answerability", "insufficient_evidence")):
        bad = {"question_count": 1, "quotas": dict(manifest["quotas"])}
        bad["quotas"][dimension] = {value: 1}
        with pytest.raises(ValueError):
            validate_quotas(rows, bad)


def test_legacy_empty_relevant_reports_actual_line_number(tmp_path):
    from medical_rag.evaluation import load_dataset
    path = tmp_path / "legacy.jsonl"
    path.write_text('{"question":"ok","relevant_chunk_ids":["a"]}\n'
                    '{"question":"bad","relevant_chunk_ids":[]}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="第 2 行"):
        load_dataset(path)


def test_graded_relevance_and_insufficient_context_only_refs():
    answerable = rich_row(
        source_refs=[
            {"source_id": "g1", "locator": {"page": 1}, "quote": "核心句", "anchor_id": "g1#p1#1", "relevance": 2},
            {"source_id": "g2", "locator": {"page": 2}, "quote": "补充句", "anchor_id": "g2#p2#2", "relevance": 1},
        ],
        chunk_grades={"chunk-1": 2},
    )
    assert validate_evaluation_row(answerable, 1)["chunk_grades"] == {"chunk-1": 2}

    insuff_empty = rich_row(answerability="insufficient_evidence", source_refs=[], relevant_chunk_ids=[])
    assert validate_evaluation_row(insuff_empty, 1)["source_refs"] == []

    insuff_decoy = rich_row(
        answerability="insufficient_evidence",
        source_refs=[{"source_id": "g1", "locator": {"page": 1}, "quote": "干扰句", "anchor_id": "g1#p1#0", "relevance": 0}],
        relevant_chunk_ids=[],
        context_chunk_ids=["decoy-1"],
    )
    assert validate_evaluation_row(insuff_decoy, 1)["context_chunk_ids"] == ["decoy-1"]


@pytest.mark.parametrize("overrides", [
    {"source_refs": [{"source_id": "g1", "locator": {"page": 1}, "quote": "q", "anchor_id": "a", "relevance": 3}]},
    {"source_refs": [{"source_id": "g1", "locator": {"page": 1}, "quote": "q", "anchor_id": "a", "relevance": True}]},
    {"source_refs": [{"source_id": "g1", "locator": {"page": 1}, "quote": "q", "anchor_id": "a", "relevance": 0}]},
    {"source_refs": [
        {"source_id": "g1", "locator": {"page": 1}, "quote": "q1", "anchor_id": "a1", "relevance": 1},
        {"source_id": "g2", "locator": {"page": 2}, "quote": "q2", "anchor_id": "a2", "relevance": 1},
    ]},
    {"answerability": "insufficient_evidence",
     "source_refs": [{"source_id": "g1", "locator": {"page": 1}, "quote": "q", "anchor_id": "a", "relevance": 2}]},
    {"chunk_grades": {"chunk-1": 0}},
    {"chunk_grades": {"": 2}},
    {"context_chunk_ids": ["d1", "d1"]},
])
def test_invalid_graded_fields_are_rejected(overrides):
    with pytest.raises(ValueError):
        validate_evaluation_row(rich_row(**overrides), 1)
