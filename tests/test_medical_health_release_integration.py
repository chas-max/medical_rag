"""End-to-end offline smoke test for the pinned medical-health release."""

import json
from pathlib import Path

import pytest

from medical_rag.evaluation_tools import remap_release, validate_release
from medical_rag.ingest import chunk_document, parse_document


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "data" / "evaluations" / "medical-health-v1"


class ConstantEmbedder:
    def encode(self, texts):
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.pinned_release
def test_pinned_sources_remap_all_500_questions_with_default_chunking(tmp_path):
    dataset = RELEASE / "questions.jsonl"
    registry = RELEASE / "source-registry.json"
    manifest = RELEASE / "manifest.json"
    assert validate_release(dataset, manifest, registry)["question_count"] == 500
    sources = json.loads(registry.read_text(encoding="utf-8"))["sources"]
    inventory = []
    for entry in sources:
        path = RELEASE / entry["filename"]
        document = parse_document(path.name, path.read_bytes())
        _, _, chunks = chunk_document(document, ConstantEmbedder())
        inventory.extend({"id": chunk.id, "source_id": entry["source_id"],
                          "sha256": document.sha256, "text": chunk.text,
                          "parent_text": chunk.parent_text,
                          "page": chunk.page or 0, "section": chunk.section} for chunk in chunks)
    chunks_path = tmp_path / "inventory.json"
    chunks_path.write_text(json.dumps(inventory, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "runtime.jsonl"
    result = remap_release(dataset, chunks_path, registry, output)
    assert result["question_count"] == 500
    runtime = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert all(bool(row["relevant_chunk_ids"]) == (row["answerability"] == "answerable")
               for row in runtime)
    methods = {entry["method"] for entry in json.loads(
        output.with_suffix(".jsonl.mapping.json").read_text(encoding="utf-8"))}
    assert methods <= {"containment", "containment_multi", "parent_containment",
                       "parent_containment_multi", "skipped"}
