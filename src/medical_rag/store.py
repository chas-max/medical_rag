"""Milvus dense/BM25 collection and document persistence."""

from __future__ import annotations

import json
import math

from pymilvus import DataType, Function, FunctionType, MilvusClient

from .types import Chunk, Document, Evidence


FIELDS = ["id", "document_id", "source_id", "filename", "sha256", "text", "section", "page", "ordinal", "chunk_count", "strategy", "reason"]


class MilvusStore:
    def __init__(self, client: MilvusClient, collection: str = "medical_chunks") -> None:
        self.client = client
        self.collection = collection

    def ensure_schema(self) -> None:
        if self.client.has_collection(self.collection):
            existing = self.client.describe_collection(self.collection)
            fields = {field["name"]: field for field in existing["fields"]}
            required = {"id", "text", "dense", "sparse", "source_id", "document_id"}
            if not required.issubset(fields) or fields["dense"].get("params", {}).get("dim") not in (512, "512"):
                raise RuntimeError("已有 Milvus 集合 schema 与当前系统不兼容")
            indices = {self.client.describe_index(self.collection, name)["index_type"]
                       for name in ("dense_index", "sparse_index")}
            if indices != {"HNSW_PQ", "SPARSE_INVERTED_INDEX"}:
                raise RuntimeError("已有 Milvus 索引与 HNSW_PQ/BM25 要求不兼容")
            self.client.load_collection(self.collection)
            return
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        for name, max_length in (("document_id", 64), ("source_id", 512), ("filename", 512),
                                 ("sha256", 64), ("section", 1024), ("strategy", 32), ("reason", 512)):
            schema.add_field(name, DataType.VARCHAR, max_length=max_length)
        schema.add_field("text", DataType.VARCHAR, max_length=65535, enable_analyzer=True,
                         analyzer_params={"type": "chinese"})
        for name in ("page", "ordinal", "chunk_count"):
            schema.add_field(name, DataType.INT64)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=512)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(Function(name="text_bm25", function_type=FunctionType.BM25,
                                     input_field_names=["text"], output_field_names=["sparse"]))
        indices = MilvusClient.prepare_index_params()
        indices.add_index(field_name="dense", index_type="HNSW_PQ", index_name="dense_index",
                          metric_type="COSINE", params={"M": 16, "efConstruction": 200, "m": 64, "nbits": 4})
        indices.add_index(field_name="sparse", index_type="SPARSE_INVERTED_INDEX",
                          index_name="sparse_index", metric_type="BM25", params={})
        self.client.create_collection(collection_name=self.collection, schema=schema, index_params=indices)
        self.client.load_collection(self.collection)

    def _source_rows(self, source_id: str) -> list[dict]:
        expression = f"source_id == {json.dumps(source_id, ensure_ascii=False)}"
        return self.client.query(collection_name=self.collection, filter=expression, output_fields=FIELDS, limit=16384)

    def replace(self, doc: Document, chunks: list[Chunk], vectors: list[list[float]]) -> bool:
        if len(chunks) != len(vectors) or not chunks:
            raise ValueError("分块与向量数量不一致")
        if any(len(vector) != 512 or not all(math.isfinite(x) for x in vector) for vector in vectors):
            raise ValueError("向量必须为 512 维有限数值")
        previous = self._source_rows(doc.source_id)
        if previous and {row["sha256"] for row in previous} == {doc.sha256}:
            return True
        data = [{"id": chunk.id, "document_id": doc.id, "source_id": doc.source_id,
                 "filename": doc.filename, "sha256": doc.sha256, "text": chunk.text,
                 "section": chunk.section, "page": chunk.page or 0, "ordinal": chunk.ordinal,
                 "chunk_count": doc.chunk_count, "strategy": doc.strategy, "reason": doc.reason,
                 "dense": vector} for chunk, vector in zip(chunks, vectors)]
        new_ids = [row["id"] for row in data]
        try:
            self.client.insert(collection_name=self.collection, data=data)
        except Exception:
            try:
                self.client.delete(collection_name=self.collection, ids=new_ids)
            except Exception:
                pass
            raise
        if previous:
            self.client.delete(collection_name=self.collection, ids=[row["id"] for row in previous])
        return False

    def list_documents(self) -> list[Document]:
        rows = self.client.query(collection_name=self.collection, filter="", output_fields=FIELDS, limit=16384)
        documents = {}
        for row in rows:
            documents[row["document_id"]] = Document(row["document_id"], row["source_id"],
                row["filename"], row["sha256"], row["chunk_count"], row["strategy"], row["reason"])
        return list(documents.values())

    def _search(self, data: list, field: str, limit: int) -> list[Evidence]:
        hits = self.client.search(collection_name=self.collection, data=data, anns_field=field,
                                  limit=limit, output_fields=FIELDS,
                                  search_params={"metric_type": "COSINE", "params": {"ef": 64}} if field == "dense" else {"metric_type": "BM25"})
        result = []
        for hit in hits[0]:
            entity = hit["entity"]
            chunk = Chunk(hit["id"], entity["document_id"], entity["source_id"], entity["text"],
                          entity["filename"], entity["section"], entity["page"] or None, entity["ordinal"])
            result.append(Evidence(chunk, float(hit["distance"])))
        return result

    def search_dense(self, vector: list[float], limit: int) -> list[Evidence]:
        return self._search([vector], "dense", limit)

    def search_bm25(self, question: str, limit: int) -> list[Evidence]:
        return self._search([question], "sparse", limit)

    def ping(self) -> bool:
        return bool(self.client.has_collection(self.collection))
