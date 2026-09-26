"""Opt-in real Milvus round trip. Never touches an existing collection."""

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.store import MilvusStore
from medical_rag.types import Chunk, Document


@pytest.mark.integration
def test_real_dense_and_chinese_bm25_indices():
    if os.getenv("RUN_MILVUS_INTEGRATION") != "1":
        pytest.skip("requires RUN_MILVUS_INTEGRATION=1 and a running Milvus Standalone")
    from pymilvus import MilvusClient

    collection = "rag_it_" + uuid4().hex
    client = MilvusClient(uri=os.getenv("MILVUS_URI", "http://localhost:19530"))
    store = MilvusStore(client, collection)
    created = False
    try:
        store.ensure_schema()
        created = True
        doc = Document("testdoc", "test-source", "guide.txt", "hash", 512, "recursive", "test")
        chunks = [Chunk(f"block{i}", "testdoc", "test-source", "高血压患者需要定期测量血压。" if i == 0
                        else f"测试医学术语语料第{i}段，关注诊断指标和健康随访。", "guide.txt", "", None, i)
                  for i in range(512)]
        vectors = [[float((i + offset) % 17) / 17 for offset in range(512)] for i in range(512)]
        store.replace(doc, chunks, vectors)
        client.flush(collection)
        client.load_collection(collection)
        assert any(item.chunk.id == "block0" for item in store.search_bm25("高血压", 10))
        assert store.search_dense(vectors[0], 10)
        assert store.list_documents()[0].filename == "guide.txt"
    finally:
        if created and collection.startswith("rag_it_") and client.has_collection(collection):
            client.drop_collection(collection)
