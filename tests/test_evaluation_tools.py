import json
from pathlib import Path

import pytest

from medical_rag.evaluation_tools import (
    export_judge_cases,
    remap_release,
    validate_release,
)


def valid_row(identifier="MHE-0001", **overrides):
    row = {
        "id": identifier,
        "question": "问题",
        "reference_answer": "答案",
        "key_points": ["要点"],
        "answerability": "answerable",
        "category": "基层高血压管理",
        "question_type": "procedure",
        "difficulty": "medium",
        "source_refs": [{
            "source_id": "src",
            "locator": {"page": 1},
            "quote": "原文",
            "anchor_id": "src#q",
        }],
        "relevant_chunk_ids": [],
    }
    row.update(overrides)
    return row


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_validate_release_enforces_count_and_quotas(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    manifest = tmp_path / "manifest.json"
    registry = tmp_path / "source-registry.json"
    dataset.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    write_json(manifest, {"question_count": 1, "quotas": {
        "category": {"基层高血压管理": 1}, "question_type": {"procedure": 1},
        "difficulty": {"medium": 1}, "answerability": {"answerable": 1}
    }})
    write_json(registry, {"sources": [{"source_id": "src"}]})
    result = validate_release(dataset, manifest, registry)
    assert result["question_count"] == 1
    assert result["source_ids"] == ["src"]

    write_json(manifest, {"question_count": 2})
    with pytest.raises(ValueError, match="题目数量"):
        validate_release(dataset, manifest, registry)


def test_validate_release_rejects_duplicate_normalized_question(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text("\n".join([
        json.dumps(valid_row("MHE-0001", question="问题？"), ensure_ascii=False),
        json.dumps(valid_row("MHE-0002", question="问题?"), ensure_ascii=False),
    ]) + "\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"question_count": 2})
    registry = tmp_path / "source-registry.json"
    write_json(registry, {"sources": [{"source_id": "src"}]})
    with pytest.raises(ValueError, match="问题重复"):
        validate_release(dataset, manifest, registry)


def test_remap_fails_on_ambiguous_anchor(tmp_path):
    dataset_path = tmp_path / "questions.jsonl"
    chunks_path = tmp_path / "chunks.json"
    registry_path = tmp_path / "source-registry.json"
    dataset_path.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    write_json(chunks_path, [
        {"id": "c1", "source_id": "src", "text": "原文", "page": 1, "section": "x"},
        {"id": "c2", "source_id": "src", "text": "原文", "page": 1, "section": "x"},
    ])
    write_json(registry_path, {"sources": [{"source_id": "src"}]})
    with pytest.raises(ValueError, match="ambiguous"):
        remap_release(dataset_path, chunks_path, registry_path, tmp_path / "out.jsonl")
    assert not (tmp_path / "out.jsonl").exists()


def test_remap_writes_runtime_jsonl_and_sidecar(tmp_path):
    dataset_path = tmp_path / "questions.jsonl"
    chunks_path = tmp_path / "chunks.json"
    registry_path = tmp_path / "source-registry.json"
    output_path = tmp_path / "out.jsonl"
    dataset_path.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    write_json(chunks_path, [{"id": "c1", "source_id": "src", "text": "原文", "page": 1, "section": "x"}])
    write_json(registry_path, {"sources": [{"source_id": "src"}]})
    result = remap_release(dataset_path, chunks_path, registry_path, output_path)
    assert result["question_count"] == 1
    runtime = json.loads(output_path.read_text(encoding="utf-8").strip())
    assert runtime["relevant_chunk_ids"] == ["c1"]
    sidecar = output_path.with_suffix(output_path.suffix + ".mapping.json")
    report = json.loads(sidecar.read_text(encoding="utf-8"))
    assert report[0]["chunk_id"] == "c1"


def test_export_judge_cases_omits_provider_errors_and_secrets(tmp_path):
    report_path = tmp_path / "report.json"
    dataset_path = tmp_path / "questions.jsonl"
    output_path = tmp_path / "judge.jsonl"
    row = valid_row()
    dataset_path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    write_json(report_path, {"config": {"rubric_version": "medical-health-v1"}, "cases": [{
        "id": row["id"], "question": row["question"], "reference_answer": row["reference_answer"],
        "key_points": row["key_points"], "answerability": "answerable",
        "evidence": [{"chunk_id": "c1", "text": "证据"}],
        "answer": {"answer": "候选答案", "abstained": False},
        "faithfulness": {"score": 0.8, "reason": "ok", "model": "judge"},
        "provider_error": "SECRET API KEY",
    }]})
    assert export_judge_cases(report_path, dataset_path, output_path) == 1
    payload = json.loads(output_path.read_text(encoding="utf-8").strip())
    assert payload["candidate_answer"] == "候选答案"
    assert payload["evidence_excerpts"] == ["证据"]
    assert "SECRET" not in output_path.read_text(encoding="utf-8")
    assert set(payload) == {
        "id", "question", "reference_answer", "key_points", "evidence_excerpts",
        "candidate_answer", "answerability", "rubric_version", "scores",
    }
