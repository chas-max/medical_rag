"""Stable source anchors and deterministic chunk remapping helpers."""

from __future__ import annotations

import hashlib
import json
import re
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
    normalized = text.translate(_PUNCTUATION)
    normalized = re.sub(r"\s+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s*([,.;:!?()\[\]'])\s*", r"\1", normalized)
    return normalized.strip()


def source_sha256(path: Path) -> str:
    """Return SHA-256 of the exact source file bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_anchor_id(source_id: str, heading_path: list[str], quote: str) -> str:
    """Create an ID independent of chunking, based on canonical source metadata."""
    canonical_heading = [normalize_source_text(item) for item in heading_path]
    canonical_quote = normalize_source_text(quote)
    payload = json.dumps(
        [source_id, canonical_heading, canonical_quote],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"{source_id}#{'/'.join(canonical_heading)}#{digest}"


def _comparable(text: str) -> str:
    # PDF extraction can insert layout whitespace inside words, but cannot reorder them.
    return "".join(normalize_source_text(text).split())


def match_anchor(anchor: dict[str, Any], chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve an exact quote to the chunks that surface it at its source locator.

    Child text is checked first. With small-to-big chunking a long quote can span
    children while still sitting in one parent, so the parent text is the second
    containment target and maps to every child under that parent.
    """
    source_id = anchor.get("source_id")
    quote = _comparable(str(anchor.get("quote", "")))
    candidates = [chunk for chunk in chunks if chunk.get("source_id") == source_id]
    if not candidates:
        raise ValueError("no chunks match anchor source_id")
    locator = anchor.get("locator") or {}
    if "page" in locator:
        candidates = [chunk for chunk in candidates if chunk.get("page") == locator["page"]]
    if "heading" in locator:
        heading = normalize_source_text(str(locator["heading"]))
        candidates = [chunk for chunk in candidates
                      if normalize_source_text(str(chunk.get("section", ""))) == heading
                      or normalize_source_text(str(chunk.get("section", ""))).endswith(" / " + heading)]
    if not candidates:
        raise ValueError("no chunks match anchor locator")

    if not quote:
        raise ValueError("anchor quote is empty")
    containment = [chunk for chunk in candidates
                   if quote in _comparable(str(chunk.get("text", chunk.get("content", ""))))]
    if containment:
        if len(containment) == 1:
            return {"chunk_id": containment[0]["chunk_id"], "score": 1.0,
                    "method": "containment"}
        return {"chunk_ids": [str(chunk["chunk_id"]) for chunk in containment],
                "score": 1.0, "method": "containment_multi"}
    # Small-to-big chunking indexes child blocks but hands the enclosing parent to
    # the generator. An anchor that spans children is still present in the context
    # those children retrieve, so map it to the children of the containing parent.
    parents = [chunk for chunk in candidates
               if quote in _comparable(str(chunk.get("parent_text", "")))]
    if not parents:
        raise ValueError("no exact ordered quote match at anchor locator")
    ids = list(dict.fromkeys(str(chunk["chunk_id"]) for chunk in parents))
    if len(ids) == 1:
        return {"chunk_id": ids[0], "score": 1.0, "method": "parent_containment"}
    return {"chunk_ids": ids, "score": 1.0, "method": "parent_containment_multi"}
