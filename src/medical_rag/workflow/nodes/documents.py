"""Parse, chunk, embed, and persist uploaded public documents."""

import hashlib
from dataclasses import replace

from ...ingest import chunk_document, parse_document
from ...types import ApiError, Document
from ..contracts import ExecutionContext


def parse(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    try:
        return {"parsed": parse_document(inputs["filename"], inputs["content"])}
    except ValueError as exc:
        raise ApiError("INVALID_DOCUMENT", str(exc), 422) from exc


def chunk(inputs: dict, config: dict, context: ExecutionContext, embedder: object) -> dict:
    parsed = inputs["parsed"]
    source = inputs["source_id"] or parsed.filename
    if len(source.encode("utf-8")) > 512:
        raise ApiError("INVALID_SOURCE", "来源标识超过 512 字节限制", 422)
    if len(parsed.filename.encode("utf-8")) > 512:
        raise ApiError("INVALID_DOCUMENT", "文件名超过 512 字节限制", 422)
    strategy, reason, chunks = chunk_document(parsed, embedder)
    if any(len(item.section.encode("utf-8")) > 1024 for item in chunks):
        raise ApiError("INVALID_DOCUMENT", "章节标题超过 1024 字节限制", 422)
    doc_id = hashlib.sha256(f"{source}:{parsed.sha256}".encode()).hexdigest()
    chunks = [replace(item, document_id=doc_id, source_id=source,
                      id=hashlib.sha256(f"{doc_id}:{item.ordinal}:{item.text}".encode()).hexdigest())
              for item in chunks]
    return {"document": Document(doc_id, source, parsed.filename, parsed.sha256,
                                  len(chunks), strategy, reason), "chunks": chunks}


def embed(inputs: dict, config: dict, context: ExecutionContext, embedder: object) -> dict:
    return {"vectors": embedder.encode([item.text for item in inputs["chunks"]])}


def store(inputs: dict, config: dict, context: ExecutionContext, database: object) -> dict:
    document = inputs["document"]
    duplicate = database.replace(document, inputs["chunks"], inputs["vectors"])
    return {"result": {"document_id": document.id, "chunk_count": document.chunk_count,
                       "strategy": document.strategy, "reason": document.reason,
                       "duplicate": duplicate}}
