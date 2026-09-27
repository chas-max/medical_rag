"""Stable source anchors and deterministic chunk remapping helpers."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any


_PUNCTUATION = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "；": ";",
        "：": ":",
        "！": "!",
        "？": "?",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "、": ",",
        "—": "-",
        "–": "-",
        "－": "-",
    }
)


def normalize_source_text(text: str) -> str:
    """Canonicalize source text while preserving Chinese characters and digits."""
    normalized = unicodedata.normalize("NFKC", text).translate(_PUNCTUATION)
    normalized = re.sub(r"\s+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s*([,.;:!?()\[\]'])\s*", r"\1", normalized)
    return normalized.strip()


def source_sha256(path: Path) -> str:
    """Return SHA-256 of a source file's normalized UTF-8 text."""
    text = path.read_text(encoding="utf-8")
    return hashlib.sha256(normalize_source_text(text).encode("utf-8")).hexdigest()


def make_anchor_id(source_id: str, heading_path: list[str], quote: str) -> str:
    """Create an ID independent of chunking, based on canonical source metadata."""
    canonical_heading = [normalize_source_text(item) for item in heading_path]
    canonical_quote = normalize_source_text(quote)
    payload = "\x1f".join([source_id, *canonical_heading, canonical_quote]).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"{source_id}#{'/'.join(canonical_heading)}#{digest}"


def _tokens(text: str) -> set[str]:
    # Keep CJK characters individually and group Latin/digit runs for deterministic overlap.
    return set(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+", normalize_source_text(text).lower()))


def match_anchor(anchor: dict[str, Any], chunks: list[dict[str, Any]], *, min_score: float = 0.92) -> dict[str, Any]:
    """Resolve an anchor to exactly one chunk, failing on ambiguity or weak evidence."""
    source_id = anchor.get("source_id")
    quote = normalize_source_text(str(anchor.get("quote", "")))
    candidates = [chunk for chunk in chunks if chunk.get("source_id") == source_id]
    if not candidates:
        raise ValueError("no chunks match anchor source_id")

    containment = [chunk for chunk in candidates if quote and quote in normalize_source_text(str(chunk.get("text", chunk.get("content", ""))))]
    if containment:
        if len(containment) != 1:
            raise ValueError("ambiguous anchor: multiple containment matches")
        return {"chunk_id": containment[0]["chunk_id"], "score": 1.0, "method": "containment"}

    quote_tokens = _tokens(quote)
    if not quote_tokens:
        raise ValueError("anchor quote is empty")
    scored = []
    for chunk in candidates:
        chunk_tokens = _tokens(str(chunk.get("text", chunk.get("content", ""))))
        score = len(quote_tokens & chunk_tokens) / len(quote_tokens)
        scored.append((score, str(chunk["chunk_id"]), chunk))
    best_score = max(item[0] for item in scored)
    best = [item for item in scored if item[0] == best_score]
    if len(best) != 1:
        raise ValueError("ambiguous anchor: equal best token-overlap matches")
    if best_score < min_score:
        raise ValueError(f"anchor match score {best_score:.3f} below minimum {min_score:.3f}")
    return {"chunk_id": best[0][2]["chunk_id"], "score": best_score, "method": "token_overlap"}
