"""Opt-in real Milvus round trip. Never touches an existing collection."""

import os
import math
import random
import sys
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.store import MilvusStore
from medical_rag.types import Chunk, Document


def _wait_for_index(client, collection: str, index_name: str, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        index = client.describe_index(collection, index_name)
        if index["state"] == "Finished":
            return index
        if index["state"] == "Failed":
            raise AssertionError(f"Milvus index {index_name} failed: {index}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"Timed out waiting for Milvus index {index_name}: {index}")
        time.sleep(min(0.5, remaining))


def test_wait_for_index_rechecks_until_index_is_finished(monkeypatch):
    states = iter(("InProgress", "InProgress", "Finished"))
    calls = []

    class Client:
        def describe_index(self, collection_name, index_name):
            calls.append((collection_name, index_name))
            return {"state": next(states)}

    monkeypatch.setattr("time.sleep", lambda _: None)
    result = _wait_for_index(Client(), "collection", "dense_index", timeout=1)

    assert result["state"] == "Finished"
    assert calls == [("collection", "dense_index")] * 3


@pytest.mark.integration
def test_real_dense_and_chinese_bm25_indices():
    if os.getenv("RUN_MILVUS_INTEGRATION") != "1":
        pytest.skip("requires RUN_MILVUS_INTEGRATION=1 and a running Milvus Standalone")
    from pymilvus import MilvusClient

    collection = "rag_it_" + uuid4().hex
    client = MilvusClient(uri=os.getenv("MILVUS_URI", "http://localhost:19530"))
    store = MilvusStore(client, collection)
    try:
        store.ensure_schema()
        store.ensure_schema()  # verify startup accepts its own persisted schema
        count = 2048  # Exceeds the default per-segment index threshold (1,024).
        doc = Document("testdoc", "test-source", "guide.txt", "hash", count, "recursive", "test")
        terms = {0: "高血压患者需要定期测量血压。", 1: "阿司匹林用药需参考适应证和禁忌证。",
                 2: "糖尿病随访应关注血糖指标。"}
        chunks = [Chunk(f"block{i}", "testdoc", "test-source", terms.get(i,
                        f"测试医学术语语料第{i}段，关注诊断指标和健康随访。"), "guide.txt", "", None, i)
                  for i in range(count)]
        vectors = []
        for i in range(count):
            rng = random.Random(i)
            values = [rng.random() for _ in range(512)]
            norm = math.sqrt(sum(value * value for value in values))
            vectors.append([value / norm for value in values])
        assert len({tuple(vector) for vector in vectors}) == count
        assert store.replace(doc, chunks, vectors) is False
        client.flush(collection)
        client.load_collection(collection)
        assert store.replace(doc, chunks, vectors) is True
        dense_index = _wait_for_index(client, collection, "dense_index")
        sparse_index = _wait_for_index(client, collection, "sparse_index")
        assert dense_index["index_type"] == "HNSW_PQ" and dense_index["metric_type"] == "COSINE"
        assert sparse_index["index_type"] == "SPARSE_INVERTED_INDEX" and sparse_index["metric_type"] == "BM25"
        assert dense_index["state"] == sparse_index["state"] == "Finished"
        assert dense_index["indexed_rows"] >= 1024
        for term, block_id in (("高血压", "block0"), ("阿司匹林", "block1"), ("糖尿病", "block2")):
            assert any(item.chunk.id == block_id for item in store.search_bm25(term, 10))
        assert any(item.chunk.id == "block0" for item in store.search_dense(vectors[0], 10))
        assert store.list_documents()[0].filename == "guide.txt"
        updated = Document("testdoc2", "test-source", "guide.txt", "hash2", 1, "recursive", "test")
        updated_chunks = [Chunk("newblock0", "testdoc2", "test-source", "新版医学语料，关注健康随访。",
                                "guide.txt", "", None, 0)]
        original_delete = client.delete
        failed = False

        def delete_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("injected delete failure")
            return original_delete(*args, **kwargs)

        with patch.object(client, "delete", side_effect=delete_once):
            with pytest.raises(RuntimeError, match="injected delete failure"):
                store.replace(updated, updated_chunks, vectors[:1])
        assert store.replace(updated, updated_chunks, vectors[:1]) is False
        assert {row["id"] for row in store._source_rows("test-source")} == {chunk.id for chunk in updated_chunks}
    finally:
        if collection.startswith("rag_it_") and client.has_collection(collection):
            client.drop_collection(collection)
