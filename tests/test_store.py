import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.store import MilvusStore
from medical_rag.types import Chunk, Document


class RecordingClient:
    def __init__(self):
        self.created = None
        self.rows = []
        self.fail_insert = False

    def has_collection(self, collection_name):
        return self.created is not None

    def create_collection(self, **kwargs):
        self.created = kwargs

    def load_collection(self, collection_name):
        pass

    def query(self, collection_name, filter, output_fields, **kwargs):
        if not filter:
            return list(self.rows)
        if "source_id" in filter:
            return [r for r in self.rows if r["source_id"] in filter]
        return []

    def insert(self, collection_name, data):
        if self.fail_insert:
            raise RuntimeError("write failed")
        self.rows.extend(data)

    def delete(self, collection_name, ids):
        self.rows = [r for r in self.rows if r["id"] not in ids]


def sample():
    doc = Document("doc1", "guide.md", "guide.md", "hash1", 1, "markdown", "heading")
    chunk = Chunk("block1", "doc1", "guide.md", "高血压应规律随访", "guide.md", "高血压", None, 0)
    return doc, chunk


def test_collection_contains_hnsw_pq_and_chinese_bm25():
    client = RecordingClient()
    MilvusStore(client).ensure_schema()
    schema = client.created["schema"].to_dict()
    indices = [index.to_dict() for index in client.created["index_params"]]
    assert any(f["name"] == "text" and 'chinese' in f["params"]["analyzer_params"] for f in schema["fields"])
    assert schema["functions"][0]["input_field_names"] == ["text"]
    assert any(i["index_type"] == "HNSW_PQ" and i["metric_type"] == "COSINE" and i["m"] == 64 for i in indices)
    assert any(i["index_type"] == "SPARSE_INVERTED_INDEX" and i["metric_type"] == "BM25" for i in indices)


def test_same_file_does_not_insert_duplicate_chunks():
    client = RecordingClient()
    store = MilvusStore(client)
    doc, chunk = sample()
    vector = [1.0] + [0.0] * 511
    assert store.replace(doc, [chunk], [vector]) is False
    assert store.replace(doc, [chunk], [vector]) is True
    assert len(client.rows) == 1


def test_failed_replacement_keeps_previous_document():
    client = RecordingClient()
    store = MilvusStore(client)
    doc, chunk = sample()
    store.replace(doc, [chunk], [[1.0] + [0.0] * 511])
    client.fail_insert = True
    updated = Document("doc2", "guide.md", "guide.md", "hash2", 1, "markdown", "heading")
    new_chunk = Chunk("block2", "doc2", "guide.md", "新内容", "guide.md", "管理", None, 0)
    with pytest.raises(RuntimeError, match="write failed"):
        store.replace(updated, [new_chunk], [[1.0] + [0.0] * 511])
    assert client.rows[0]["id"] == "block1"


def test_invalid_vector_cannot_be_written():
    store = MilvusStore(RecordingClient())
    doc, chunk = sample()
    with pytest.raises(ValueError, match="512"):
        store.replace(doc, [chunk], [[1.0]])
