import json
from pathlib import Path

import pytest

from medical_rag.evaluation_tools import (
    export_judge_cases,
    remap_release,
    validate_release,
)
from medical_rag.evaluation_anchors import source_sha256


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


def write_registry(tmp_path: Path, *, source_id: str = "src", content: str = "原文") -> Path:
    source = tmp_path / "src.txt"
    source.write_text(content, encoding="utf-8")
    registry = tmp_path / "source-registry.json"
    write_json(registry, {"sources": [{"source_id": source_id, "filename": source.name,
                                         "sha256": source_sha256(source)}]})
    return registry


def test_validate_release_enforces_count_and_quotas(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    manifest = tmp_path / "manifest.json"
    registry = write_registry(tmp_path)
    dataset.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    write_json(manifest, {"question_count": 1, "quotas": {
        "category": {"基层高血压管理": 1}, "question_type": {"procedure": 1},
        "difficulty": {"medium": 1}, "answerability": {"answerable": 1}
    }})
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
    registry = write_registry(tmp_path)
    with pytest.raises(ValueError, match="问题重复"):
        validate_release(dataset, manifest, registry)


def test_remap_fails_on_ambiguous_anchor(tmp_path):
    dataset_path = tmp_path / "questions.jsonl"
    chunks_path = tmp_path / "chunks.json"
    registry_path = write_registry(tmp_path)
    dataset_path.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    write_json(chunks_path, [
        {"id": "c1", "source_id": "src", "text": "原文", "page": 1, "section": "x",
         "sha256": source_sha256(tmp_path / "src.txt")},
        {"id": "c2", "source_id": "src", "text": "原文", "page": 1, "section": "x",
         "sha256": source_sha256(tmp_path / "src.txt")},
    ])
    result = remap_release(dataset_path, chunks_path, registry_path, tmp_path / "out.jsonl")
    assert result["mapping_count"] == 2
    assert json.loads((tmp_path / "out.jsonl").read_text(encoding="utf-8"))["relevant_chunk_ids"] == ["c1", "c2"]


def test_remap_writes_runtime_jsonl_and_sidecar(tmp_path):
    dataset_path = tmp_path / "questions.jsonl"
    chunks_path = tmp_path / "chunks.json"
    registry_path = write_registry(tmp_path)
    output_path = tmp_path / "out.jsonl"
    dataset_path.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    write_json(chunks_path, [{"id": "c1", "source_id": "src", "text": "原文", "page": 1, "section": "x",
                              "sha256": source_sha256(tmp_path / "src.txt")}])
    result = remap_release(dataset_path, chunks_path, registry_path, output_path)
    assert result["question_count"] == 1
    runtime = json.loads(output_path.read_text(encoding="utf-8").strip())
    assert runtime["relevant_chunk_ids"] == ["c1"]
    sidecar = output_path.with_suffix(output_path.suffix + ".mapping.json")
    report = json.loads(sidecar.read_text(encoding="utf-8"))
    assert report[0]["chunk_id"] == "c1"


def test_remap_rejects_missing_or_stale_index_source_hash(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    registry = write_registry(tmp_path)
    inventory = tmp_path / "chunks.json"
    chunk = {"id": "c1", "source_id": "src", "text": "原文", "page": 1, "section": "x"}
    for digest in (None, "0" * 64):
        write_json(inventory, [{**chunk, **({"sha256": digest} if digest else {})}])
        with pytest.raises(ValueError, match="sha256"):
            remap_release(dataset, inventory, registry, tmp_path / "runtime.jsonl")
    assert not (tmp_path / "runtime.jsonl").exists()


def test_remap_accepts_milvus_markdown_page_zero(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(json.dumps(valid_row(source_refs=[{
        "source_id": "src", "locator": {"heading": "标题"}, "quote": "原文", "anchor_id": "src#q"
    }]), ensure_ascii=False) + "\n", encoding="utf-8")
    registry = write_registry(tmp_path)
    inventory = tmp_path / "chunks.json"
    write_json(inventory, [{"id": "c1", "source_id": "src", "text": "原文", "page": 0,
                            "section": "标题", "sha256": source_sha256(tmp_path / "src.txt")}])
    assert remap_release(dataset, inventory, registry, tmp_path / "runtime.jsonl")["mapping_count"] == 1


def test_remap_does_not_overwrite_existing_runtime_output(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    chunks = tmp_path / "chunks.json"
    write_json(chunks, [{"id": "c1", "source_id": "src", "text": "原文", "page": None,
                         "section": "Markdown section"}])
    output = tmp_path / "runtime.jsonl"
    output.write_text("previous release\n", encoding="utf-8")
    with pytest.raises(ValueError, match="已存在"):
        remap_release(dataset, chunks, write_registry(tmp_path), output)
    assert output.read_text(encoding="utf-8") == "previous release\n"


def test_validate_release_rejects_extra_quota_values(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text("\n".join([
        json.dumps(valid_row("MHE-0001"), ensure_ascii=False),
        json.dumps(valid_row("MHE-0002", question="另一个问题", category="其他"), ensure_ascii=False),
    ]) + "\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"question_count": 2, "quotas": {"category": {"基层高血压管理": 1}}})
    with pytest.raises(ValueError, match="category 配额"):
        validate_release(dataset, manifest, write_registry(tmp_path))


def test_validate_release_fails_closed_for_missing_source_hash_metadata(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"question_count": 1})
    registry = tmp_path / "source-registry.json"
    write_json(registry, {"sources": [{"source_id": "src"}]})
    with pytest.raises(ValueError, match="sha256"):
        validate_release(dataset, manifest, registry)


def test_validate_release_rejects_dataset_and_rubric_hash_mismatch(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    rubric = tmp_path / "rubric.md"
    rubric.write_text("裁判协议", encoding="utf-8")
    registry = write_registry(tmp_path)
    write_json(manifest, {"question_count": 1, "dataset_sha256": "0" * 64})
    with pytest.raises(ValueError, match="题库哈希"):
        validate_release(dataset, manifest, registry)
    write_json(manifest, {"question_count": 1, "dataset_sha256": source_sha256(dataset),
                          "rubric_sha256": "0" * 64})
    with pytest.raises(ValueError, match="rubric 哈希"):
        validate_release(dataset, manifest, registry)


def test_validate_release_rechecks_quote_locator_and_anchor_against_source(tmp_path):
    from medical_rag.evaluation_anchors import make_anchor_id

    source = tmp_path / "guide.md"
    source.write_text("## 正确章节\n每年随访四次。\n## 其他章节\n按规范测量。\n", encoding="utf-8")
    registry = tmp_path / "source-registry.json"
    write_json(registry, {"sources": [{"source_id": "src", "filename": source.name,
                                        "sha256": source_sha256(source)}]})
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"question_count": 1})
    dataset = tmp_path / "questions.jsonl"
    row = valid_row(source_refs=[{"source_id": "src", "locator": {"heading": "正确章节"},
                                  "quote": "每年随访四次。",
                                  "anchor_id": make_anchor_id("src", ["正确章节"], "每年随访四次。")}])
    dataset.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    assert validate_release(dataset, manifest, registry)["question_count"] == 1
    row["source_refs"][0]["locator"]["heading"] = "其他章节"
    dataset.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="摘录"):
        validate_release(dataset, manifest, registry)


def test_remap_rejects_malformed_chunk_inventory(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    chunks = tmp_path / "chunks.json"
    write_json(chunks, [{"id": "c1", "source_id": "src", "text": "原文", "page": 1}])
    with pytest.raises(ValueError, match="page/section"):
        remap_release(dataset, chunks, write_registry(tmp_path), tmp_path / "out.jsonl")


def test_export_rejects_nonfinite_and_out_of_range_scores(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(json.dumps(valid_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    report = tmp_path / "report.json"
    base = {"id": "MHE-0001", "evidence": [], "answer": {"answer": "候选", "abstained": False}}
    for value in (True, 1.1, float("nan"), float("inf")):
        item = {**base, "faithfulness": {"score": value}}
        write_json(report, {"cases": [item]})
        with pytest.raises(ValueError, match="score"):
            export_judge_cases(report, dataset, tmp_path / "judge.jsonl")


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
    assert payload["source_excerpts"] == ["原文"]
    assert payload["question_type"] == "procedure"
    assert payload["difficulty"] == "medium"
    assert "SECRET" not in output_path.read_text(encoding="utf-8")
    assert set(payload) == {
        "id", "question", "reference_answer", "key_points", "source_excerpts", "evidence_excerpts",
        "candidate_answer", "answerability", "question_type", "difficulty",
        "rubric_version", "scores",
    }


def test_remap_builds_chunk_grades_and_context_chunk_ids(tmp_path):
    from medical_rag.evaluation_anchors import make_anchor_id

    source = tmp_path / "guide.md"
    source.write_text("## 章节一\n核心证据A。\n补充证据B。\n干扰背景C。\n", encoding="utf-8")
    digest = source_sha256(source)
    registry = tmp_path / "source-registry.json"
    write_json(registry, {"sources": [{"source_id": "src", "filename": source.name, "sha256": digest}]})
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"question_count": 3})

    dataset = tmp_path / "questions.jsonl"
    ans_row = valid_row(
        "MHE2-0001",
        question="可回答问题",
        source_refs=[
            {"source_id": "src", "locator": {"heading": "章节一"}, "quote": "核心证据A。",
             "anchor_id": make_anchor_id("src", ["章节一"], "核心证据A。"), "relevance": 2},
            {"source_id": "src", "locator": {"heading": "章节一"}, "quote": "补充证据B。",
             "anchor_id": make_anchor_id("src", ["章节一"], "补充证据B。"), "relevance": 1},
        ],
    )
    ins_row = valid_row(
        "MHE2-0002",
        question="拒答问题带干扰",
        answerability="insufficient_evidence",
        source_refs=[
            {"source_id": "src", "locator": {"heading": "章节一"}, "quote": "干扰背景C。",
             "anchor_id": make_anchor_id("src", ["章节一"], "干扰背景C。"), "relevance": 0},
        ],
    )
    ins_empty = valid_row(
        "MHE2-0003",
        question="纯空引用拒答问题",
        answerability="insufficient_evidence",
        source_refs=[],
    )
    dataset.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in (ans_row, ins_row, ins_empty)) + "\n",
        encoding="utf-8",
    )
    assert validate_release(dataset, manifest, registry)["question_count"] == 3

    chunks = tmp_path / "chunks.json"
    write_json(chunks, [
        {"id": "c1", "source_id": "src", "text": "核心证据A。补充证据B。", "page": 0, "section": "章节一", "sha256": digest},
        {"id": "c2", "source_id": "src", "text": "补充证据B。", "page": 0, "section": "章节一", "sha256": digest},
        {"id": "c3", "source_id": "src", "text": "干扰背景C。", "page": 0, "section": "章节一", "sha256": digest},
    ])
    output = tmp_path / "runtime.jsonl"
    remap_release(dataset, chunks, registry, output)
    runtime_rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert runtime_rows[0]["relevant_chunk_ids"] == ["c1", "c2"]
    assert runtime_rows[0]["chunk_grades"] == {"c1": 2, "c2": 1}
    assert runtime_rows[1]["relevant_chunk_ids"] == []
    assert runtime_rows[1]["context_chunk_ids"] == ["c3"]
    assert runtime_rows[2]["relevant_chunk_ids"] == []
    sidecar = json.loads(output.with_suffix(".jsonl.mapping.json").read_text(encoding="utf-8"))
    assert any(item["method"] == "context_only" and item["chunk_id"] == "c3" for item in sidecar)
