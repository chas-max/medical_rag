"""Orchestrates document ingestion, retrieval and grounded answers."""

import hashlib
from dataclasses import replace

from .config import Settings
from .ingest import chunk_document, parse_document
from .retrieval import Retriever
from .types import ApiError, Document


class RagService:
    def __init__(self, settings: Settings, store: object, embedder: object,
                 reranker: object, generator: object) -> None:
        self.settings = settings
        self.store = store
        self.embedder = embedder
        self.generator = generator
        self.retriever = Retriever(store, embedder, reranker, settings.candidate_k, settings.evidence_threshold)

    def ingest(self, filename: str, content: bytes, source_id: str | None = None) -> dict:
        source = source_id or filename
        if len(source.encode("utf-8")) > 512:
            raise ApiError("INVALID_SOURCE", "来源标识超过 512 字节限制", 422)
        if len(filename.encode("utf-8")) > 512:
            raise ApiError("INVALID_DOCUMENT", "文件名超过 512 字节限制", 422)
        try:
            parsed = parse_document(filename, content)
        except ValueError as exc:
            raise ApiError("INVALID_DOCUMENT", str(exc), 422) from exc
        strategy, reason, chunks = chunk_document(parsed, self.embedder)
        if any(len(chunk.section.encode("utf-8")) > 1024 for chunk in chunks):
            raise ApiError("INVALID_DOCUMENT", "章节标题超过 1024 字节限制", 422)
        doc_id = hashlib.sha256(f"{source}:{parsed.sha256}".encode()).hexdigest()
        chunks = [replace(chunk, document_id=doc_id, source_id=source,
                          id=hashlib.sha256(f"{doc_id}:{chunk.ordinal}:{chunk.text}".encode()).hexdigest())
                  for chunk in chunks]
        doc = Document(doc_id, source, filename, parsed.sha256, len(chunks), strategy, reason)
        vectors = self.embedder.encode([chunk.text for chunk in chunks])
        duplicate = self.store.replace(doc, chunks, vectors)
        return {"document_id": doc.id, "chunk_count": doc.chunk_count,
                "strategy": strategy, "reason": reason, "duplicate": duplicate}

    def list_documents(self) -> list[Document]:
        return self.store.list_documents()

    def search(self, question: str, top_k: int) -> list:
        if not question.strip():
            raise ApiError("INVALID_QUERY", "问题不能为空", 422)
        return self.retriever.search(question, top_k)

    def answer(self, question: str, provider: str, top_k: int) -> dict:
        return self.generator.answer(question, self.search(question, top_k), provider)

    def health(self) -> dict:
        return {"milvus": self.store.ping()}
