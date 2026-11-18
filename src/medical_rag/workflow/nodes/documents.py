"""Parse, chunk, embed, and persist uploaded public documents."""

import hashlib
from dataclasses import replace

from ...ingest import chunk_document, parse_document
from ...tokenization import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_PARENT_TOKENS,
    MAX_CHUNK_TOKENS,
)
from ...types import ApiError, Document
from ..contracts import ExecutionContext


def parse(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    try:
        return {"parsed": parse_document(inputs["filename"], inputs["content"])}
    except ValueError as exc:
        raise ApiError("INVALID_DOCUMENT", str(exc), 422) from exc


def chunk(
    inputs: dict,
    config: dict,
    context: ExecutionContext,
    embedder: object,
    token_counter: object | None = None,
    max_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    parent_max_tokens: int = DEFAULT_PARENT_TOKENS,
) -> dict:
    parsed = inputs["parsed"]
    source = inputs["source_id"] or parsed.filename
    if len(source.encode("utf-8")) > 512:
        raise ApiError("INVALID_SOURCE", "来源标识超过 512 字节限制", 422)
    if len(parsed.filename.encode("utf-8")) > 512:
        raise ApiError("INVALID_DOCUMENT", "文件名超过 512 字节限制", 422)
    try:
        strategy, reason, chunks = chunk_document(
            parsed,
            embedder,
            max_tokens=int(config.get("max_tokens", max_tokens)),
            overlap_tokens=int(config.get("overlap_tokens", overlap_tokens)),
            parent_max_tokens=int(config.get("parent_max_tokens", parent_max_tokens)),
            token_counter=token_counter,
        )
    except (TypeError, ValueError) as exc:
        raise ApiError("INVALID_DOCUMENT", str(exc), 422) from exc
    if any(len(item.section.encode("utf-8")) > 1024 for item in chunks):
        raise ApiError("INVALID_DOCUMENT", "章节标题超过 1024 字节限制", 422)
    if any(item.token_count > MAX_CHUNK_TOKENS for item in chunks):
        raise ApiError("INVALID_DOCUMENT", f"分块超过 {MAX_CHUNK_TOKENS} token 上限", 422)
    if any(len(item.parent_text.encode("utf-8")) > 65535 for item in chunks):
        raise ApiError("INVALID_DOCUMENT", "父块正文超过存储上限", 422)
    doc_id = hashlib.sha256(f"{source}:{parsed.sha256}".encode()).hexdigest()
    chunks = [replace(
        item,
        document_id=doc_id,
        source_id=source,
        id=hashlib.sha256(
            f"{doc_id}:{item.ordinal}:{item.parent_id}:{item.text}".encode()).hexdigest(),
        # Recompute the parent id from the per-source document id: the same file
        # uploaded under two sources must not collapse into one prompt context.
        parent_id=(hashlib.sha256(
            f"{doc_id}:parent:{item.parent_ordinal}:{item.parent_text}".encode()).hexdigest()
            if item.parent_id else ""),
    ) for item in chunks]
    return {"document": Document(doc_id, source, parsed.filename, parsed.sha256,
                                  len(chunks), strategy, reason), "chunks": chunks}


def embed(inputs: dict, config: dict, context: ExecutionContext, embedder: object) -> dict:
    from ...pdf_sections import format_chunk_for_index

    include_metadata = bool(config.get("include_metadata", False))
    texts = [
        format_chunk_for_index(item.filename, item.section, item.text)
        if include_metadata else item.text
        for item in inputs["chunks"]
    ]
    return {"vectors": embedder.encode(texts)}


def store(inputs: dict, config: dict, context: ExecutionContext, database: object) -> dict:
    document = inputs["document"]
    duplicate = database.replace(document, inputs["chunks"], inputs["vectors"])
    return {"result": {"document_id": document.id, "chunk_count": document.chunk_count,
                       "strategy": document.strategy, "reason": document.reason,
                       "duplicate": duplicate}}
