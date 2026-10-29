"""Milvus dense/BM25 collection and document persistence."""

from __future__ import annotations

import json
import math
import re

from pymilvus import DataType, Function, FunctionType, MilvusClient

from .tokenization import MAX_CHUNK_TOKENS, estimate_tokens
from .types import SCORE_BM25, SCORE_DENSE, Chunk, Document, Evidence


FIELDS = ["id", "document_id", "source_id", "filename", "sha256", "text", "section", "page",
          "ordinal", "chunk_count", "strategy", "reason", "parent_id", "parent_text",
          "parent_ordinal", "token_count"]
PARENT_FIELDS = frozenset({"parent_id", "parent_text", "parent_ordinal", "token_count"})
# Chunk ids are generated hashes (optionally prefixed, e.g. "web-"); anything
# outside this charset can never match and must not reach the filter expression.
CHUNK_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{4,128}")


class MilvusStore:
    def __init__(self, client: MilvusClient, collection: str = "medical_chunks") -> None:
        self.client = client
        self.collection = collection

    def ensure_schema(self) -> None:
        if self.client.has_collection(self.collection):
            existing = self.client.describe_collection(self.collection)
            fields = {field["name"]: field for field in existing["fields"]}
            required = set(FIELDS) | {"dense", "sparse"}
            missing = required - set(fields)
            if missing:
                if missing & PARENT_FIELDS:
                    raise RuntimeError(
                        "已有 Milvus 集合缺少父子分块字段，与当前版本不兼容；"
                        "请重建集合并重新入库")
                raise RuntimeError("已有 Milvus 集合 schema 与当前系统不兼容")
            varchar_lengths = {"id": 64, "document_id": 64, "source_id": 512,
                               "filename": 512, "sha256": 64, "section": 1024,
                               "strategy": 32, "reason": 512, "text": 65535,
                               "parent_id": 64, "parent_text": 65535}
            try:
                compatible = (not existing.get("auto_id", False)
                              and not existing.get("enable_dynamic_field", False)
                              and fields["id"].get("is_primary") is True)
                compatible = compatible and all(
                    int(fields[name]["type"]) == int(DataType.VARCHAR)
                    and int(fields[name]["params"]["max_length"]) >= length
                    for name, length in varchar_lengths.items())
                compatible = compatible and all(int(fields[name]["type"]) == int(DataType.INT64)
                                                for name in ("page", "ordinal", "chunk_count",
                                                             "parent_ordinal", "token_count"))
                compatible = compatible and int(fields["dense"]["type"]) == int(DataType.FLOAT_VECTOR)
                compatible = compatible and int(fields["sparse"]["type"]) == int(DataType.SPARSE_FLOAT_VECTOR)
            except (KeyError, TypeError, ValueError):
                compatible = False
            if not compatible:
                raise RuntimeError("已有 Milvus 集合字段定义不兼容")
            if fields["dense"].get("params", {}).get("dim") not in (512, "512"):
                raise RuntimeError("已有 Milvus 集合向量维度不兼容")
            analyzer = fields["text"].get("params", {}).get("analyzer_params", {})
            if isinstance(analyzer, str):
                analyzer = json.loads(analyzer)
            functions = existing.get("functions", [])
            if (analyzer.get("type") != "chinese"
                    or fields["text"].get("params", {}).get("enable_analyzer") not in (True, "true", "True")
                    or not any(int(fn["type"]) == int(FunctionType.BM25)
                               and fn["input_field_names"] == ["text"]
                               and fn["output_field_names"] == ["sparse"] for fn in functions)):
                raise RuntimeError("已有 Milvus 全文检索配置不兼容")
            dense_index = self.client.describe_index(self.collection, "dense_index")
            sparse_index = self.client.describe_index(self.collection, "sparse_index")
            if (dense_index.get("index_type") != "HNSW_PQ"
                    or dense_index.get("metric_type") != "COSINE"
                    or dense_index.get("field_name") != "dense"
                    or sparse_index.get("index_type") != "SPARSE_INVERTED_INDEX"
                    or sparse_index.get("metric_type") != "BM25"
                    or sparse_index.get("field_name") != "sparse"
                    or dense_index.get("state") == "Failed"
                    or sparse_index.get("state") == "Failed"
                    or any(int(dense_index.get(name, -1)) != expected for name, expected in
                           (("M", 16), ("efConstruction", 200), ("m", 64), ("nbits", 4)))):
                raise RuntimeError("已有 Milvus 索引与 HNSW_PQ/BM25 要求不兼容")
            self.client.load_collection(self.collection)
            return
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        for name, max_length in (("document_id", 64), ("source_id", 512), ("filename", 512),
                                 ("sha256", 64), ("section", 1024), ("strategy", 32),
                                 ("reason", 512), ("parent_id", 64),
                                 ("parent_text", 65535)):
            schema.add_field(name, DataType.VARCHAR, max_length=max_length)
        schema.add_field("text", DataType.VARCHAR, max_length=65535, enable_analyzer=True,
                         analyzer_params={"type": "chinese"})
        for name in ("page", "ordinal", "chunk_count", "parent_ordinal", "token_count"):
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
        self.client.create_collection(collection_name=self.collection, schema=schema,
                                      index_params=indices, consistency_level="Strong")
        self.client.load_collection(self.collection)

    def _iter_rows(self, expression: str, fields: list[str] | None = None):
        iterator = self.client.query_iterator(collection_name=self.collection, filter=expression,
                                              output_fields=fields or FIELDS, batch_size=1000,
                                              consistency_level="Strong")
        try:
            while batch := iterator.next():
                for row in batch:
                    yield row.to_dict() if hasattr(row, "to_dict") else row
        finally:
            iterator.close()

    def _source_rows(self, source_id: str) -> list[dict]:
        expression = f"source_id == {json.dumps(source_id, ensure_ascii=False)}"
        # Replacement only needs identity and source hash; parent_text can be
        # several kilobytes per child and must not be transferred for no reason.
        return list(self._iter_rows(expression, fields=["id", "sha256"]))

    def replace(self, doc: Document, chunks: list[Chunk], vectors: list[list[float]]) -> bool:
        if len(chunks) != len(vectors) or not chunks:
            raise ValueError("分块与向量数量不一致")
        if any(len(vector) != 512 or not all(math.isfinite(x) for x in vector) for vector in vectors):
            raise ValueError("向量必须为 512 维有限数值")
        rows = []
        for chunk, vector in zip(chunks, vectors):
            # The ingestion path records the tokenizer's count; the estimate is a
            # backstop only for callers that construct chunks directly (count 0).
            token_count = chunk.token_count or estimate_tokens(chunk.text)
            if token_count > MAX_CHUNK_TOKENS:
                raise ValueError(f"分块 {chunk.id} 超过 {MAX_CHUNK_TOKENS} token 上限")
            if len(chunk.parent_id.encode("utf-8")) > 64:
                raise ValueError("父块标识超过 64 字节限制")
            if len(chunk.parent_text.encode("utf-8")) > 65535:
                raise ValueError("父块正文超过 65535 字节限制")
            rows.append({"id": chunk.id, "document_id": doc.id, "source_id": doc.source_id,
                         "filename": doc.filename, "sha256": doc.sha256, "text": chunk.text,
                         "section": chunk.section, "page": chunk.page or 0,
                         "ordinal": chunk.ordinal, "chunk_count": doc.chunk_count,
                         "strategy": doc.strategy, "reason": doc.reason,
                         "parent_id": chunk.parent_id, "parent_text": chunk.parent_text,
                         "parent_ordinal": chunk.parent_ordinal, "token_count": token_count,
                         "dense": vector})
        data = rows
        previous = self._source_rows(doc.source_id)
        new_ids = {row["id"] for row in data}
        present_ids = {row["id"] for row in previous
                       if row["id"] in new_ids and row["sha256"] == doc.sha256}
        old_ids = {row["id"] for row in previous if row["id"] not in new_ids or row["sha256"] != doc.sha256}
        if present_ids == new_ids and not old_ids:
            return True
        missing = [row for row in data if row["id"] not in present_ids]
        if missing:
            try:
                self.client.insert(collection_name=self.collection, data=missing)
            except Exception:
                try:
                    self.client.delete(collection_name=self.collection, ids=[row["id"] for row in missing])
                except Exception:
                    pass
                raise
            visible = {row["id"] for row in self._source_rows(doc.source_id) if row["sha256"] == doc.sha256}
            if not new_ids.issubset(visible):
                raise RuntimeError("新版本尚不可见，旧版本已保留")
        if old_ids:
            self.client.delete(collection_name=self.collection, ids=list(old_ids))
            if any(row["id"] in old_ids for row in self._source_rows(doc.source_id)):
                raise RuntimeError("旧版本清理未完成，可重试同一上传")
        return False

    def list_documents(self) -> list[Document]:
        documents = {}
        fields = ["document_id", "source_id", "filename", "sha256", "chunk_count",
                  "strategy", "reason"]
        for row in self._iter_rows("", fields=fields):
            documents[row["document_id"]] = Document(row["document_id"], row["source_id"],
                row["filename"], row["sha256"], row["chunk_count"], row["strategy"], row["reason"])
        return list(documents.values())

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        """Fetch one stored block by id for source display; vectors are never read."""
        if not isinstance(chunk_id, str) or not CHUNK_ID_PATTERN.fullmatch(chunk_id):
            return None
        expression = f"id == {json.dumps(chunk_id, ensure_ascii=False)}"
        for row in self._iter_rows(expression, fields=FIELDS):
            return Chunk(row["id"], row["document_id"], row["source_id"], row["text"],
                         row["filename"], row["section"], row["page"] or None,
                         row["ordinal"], parent_id=row.get("parent_id", ""),
                         parent_text=row.get("parent_text", ""),
                         parent_ordinal=int(row.get("parent_ordinal", 0) or 0),
                         token_count=int(row.get("token_count", 0) or 0))
        return None

    def _search(self, data: list, field: str, limit: int) -> list[Evidence]:
        hits = self.client.search(collection_name=self.collection, data=data, anns_field=field,
                                  limit=limit, output_fields=FIELDS,
                                  consistency_level="Strong",
                                  search_params={"metric_type": "COSINE", "params": {"ef": 64}} if field == "dense" else {"metric_type": "BM25"})
        result = []
        kind = SCORE_DENSE if field == "dense" else SCORE_BM25
        for hit in hits[0]:
            entity = hit["entity"]
            chunk = Chunk(hit["id"], entity["document_id"], entity["source_id"], entity["text"],
                          entity["filename"], entity["section"], entity["page"] or None,
                          entity["ordinal"], parent_id=entity.get("parent_id", ""),
                          parent_text=entity.get("parent_text", ""),
                          parent_ordinal=int(entity.get("parent_ordinal", 0) or 0),
                          token_count=int(entity.get("token_count", 0) or 0))
            result.append(Evidence(chunk, float(hit["distance"]), score_kind=kind))
        return result

    def search_dense(self, vector: list[float], limit: int) -> list[Evidence]:
        return self._search([vector], "dense", limit)

    def search_bm25(self, question: str, limit: int) -> list[Evidence]:
        return self._search([question], "sparse", limit)

    def ping(self) -> bool:
        return bool(self.client.has_collection(self.collection))
