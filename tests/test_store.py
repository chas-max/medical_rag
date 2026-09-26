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
        self.fail_delete_once = False
        self.insert_count = 0
        self.read_kwargs = []

    def has_collection(self, collection_name):
        return self.created is not None

    def create_collection(self, **kwargs):
        self.created = kwargs

    def load_collection(self, collection_name):
        pass

    def describe_collection(self, collection_name):
        return self.created["schema"].to_dict()

    def describe_index(self, collection_name, index_name):
        return next(index.to_dict() for index in self.created["index_params"]
                    if index.index_name == index_name)

    def query(self, collection_name, filter, output_fields, **kwargs):
        assert kwargs.get("limit", 0) < 16384
        self.read_kwargs.append(kwargs)
        if not filter:
            return list(self.rows)
        if "source_id" in filter:
            return [r for r in self.rows if r["source_id"] in filter]
        return []

    def insert(self, collection_name, data):
        if self.fail_insert:
            raise RuntimeError("write failed")
        self.insert_count += 1
        self.rows.extend(data)

    def delete(self, collection_name, ids):
        if self.fail_delete_once:
            self.fail_delete_once = False
            raise RuntimeError("delete failed")
        self.rows = [r for r in self.rows if r["id"] not in ids]

    def query_iterator(self, collection_name, filter, output_fields, batch_size, **kwargs):
        self.read_kwargs.append(kwargs)
        rows = self.query(collection_name, filter, output_fields, limit=1000,
                          consistency_level=kwargs.get("consistency_level"))

        class Iterator:
            def __init__(self):
                self.done = False

            def next(self):
                if self.done:
                    return []
                self.done = True
                return rows

            def close(self):
                pass

        return Iterator()


def sample():
    doc = Document("doc1", "guide.md", "guide.md", "hash1", 1, "markdown", "heading")
    chunk = Chunk("block1", "doc1", "guide.md", "高血压应规律随访", "guide.md", "高血压", None, 0)
    return doc, chunk


def test_collection_contains_hnsw_pq_and_chinese_bm25():
    client = RecordingClient()
    store = MilvusStore(client)
    store.ensure_schema()
    store.ensure_schema()
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


def test_delete_failure_retry_keeps_new_version_and_removes_only_old():
    client = RecordingClient()
    store = MilvusStore(client)
    doc, chunk = sample()
    vector = [1.0] + [0.0] * 511
    store.replace(doc, [chunk], [vector])
    updated = Document("doc2", "guide.md", "guide.md", "hash2", 1, "markdown", "heading")
    new_chunk = Chunk("block2", "doc2", "guide.md", "新内容", "guide.md", "管理", None, 0)
    client.fail_delete_once = True
    with pytest.raises(RuntimeError, match="delete failed"):
        store.replace(updated, [new_chunk], [vector])
    assert client.insert_count == 2
    assert store.replace(updated, [new_chunk], [vector]) is False
    assert client.insert_count == 2
    assert [row["id"] for row in client.rows] == ["block2"]


def test_store_queries_use_strong_consistency():
    client = RecordingClient()
    store = MilvusStore(client)
    doc, chunk = sample()
    store.replace(doc, [chunk], [[1.0] + [0.0] * 511])
    store.list_documents()
    assert client.read_kwargs
    assert all(kwargs.get("consistency_level") == "Strong" for kwargs in client.read_kwargs)


def test_existing_collection_without_bm25_function_is_rejected():
    client = RecordingClient()
    store = MilvusStore(client)
    store.ensure_schema()
    original = client.describe_collection

    def incompatible(name):
        schema = original(name)
        schema["functions"] = []
        return schema

    client.describe_collection = incompatible
    with pytest.raises(RuntimeError, match="不兼容"):
        store.ensure_schema()


def test_existing_collection_with_wrong_dense_metric_is_rejected():
    client = RecordingClient()
    store = MilvusStore(client)
    store.ensure_schema()
    original = client.describe_index

    def wrong_metric(collection_name, index_name):
        result = original(collection_name, index_name)
        if index_name == "dense_index":
            result["metric_type"] = "L2"
        return result

    client.describe_index = wrong_metric
    with pytest.raises(RuntimeError, match="不兼容"):
        store.ensure_schema()
