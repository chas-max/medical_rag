"""End-to-end offline integration test for the medical-health-v2 150-question release."""

import json
from pathlib import Path

import pytest

from medical_rag.evaluation_tools import remap_release, validate_release
from medical_rag.ingest import chunk_document, parse_document

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "data" / "evaluations" / "medical-health-v2"


class ConstantEmbedder:
    def encode(self, texts):
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.pinned_release
def test_v2_all_35_sources_remap_all_150_questions_with_default_chunking(tmp_path):
    dataset = RELEASE / "questions.jsonl"
    registry = RELEASE / "source-registry.json"
    manifest = RELEASE / "manifest.json"
    summary = validate_release(dataset, manifest, registry)
    assert summary["question_count"] == 150
    assert len(summary["source_ids"]) == 35

    sources = json.loads(registry.read_text(encoding="utf-8"))["sources"]
    assert len(sources) == 35
    inventory = []
    for entry in sources:
        path = RELEASE / entry["filename"]
        document = parse_document(path.name, path.read_bytes())
        _, _, chunks = chunk_document(document, ConstantEmbedder())
        inventory.extend({
            "id": chunk.id,
            "source_id": entry["source_id"],
            "sha256": document.sha256,
            "text": chunk.text,
            "parent_text": chunk.parent_text,
            "page": chunk.page or 0,
            "section": chunk.section,
        } for chunk in chunks)

    chunks_path = tmp_path / "inventory.json"
    chunks_path.write_text(json.dumps(inventory, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "runtime.jsonl"
    result = remap_release(dataset, chunks_path, registry, output)
    assert result["question_count"] == 150

    runtime = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    answerable = [row for row in runtime if row["answerability"] == "answerable"]
    insufficient = [row for row in runtime if row["answerability"] == "insufficient_evidence"]
    assert len(answerable) == 120
    assert len(insufficient) == 30
    assert all(len(row["source_refs"]) >= 3 for row in answerable)
    assert all(row["relevant_chunk_ids"] and 2 in row.get("chunk_grades", {}).values() for row in answerable)
    avg_chunks = sum(len(row["relevant_chunk_ids"]) for row in answerable) / len(answerable)
    assert avg_chunks >= 3.0
    assert all(not row["relevant_chunk_ids"] for row in insufficient)
    assert sum(bool(row.get("context_chunk_ids")) for row in insufficient) >= 5

    methods = {entry["method"] for entry in json.loads(
        output.with_suffix(".jsonl.mapping.json").read_text(encoding="utf-8")
    )}
    assert methods <= {
        "containment", "containment_multi",
        "parent_containment", "parent_containment_multi",
        "context_only", "skipped",
    }
