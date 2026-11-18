"""Token counting and token-aware splitting for retrieval-sized chunks.

The embedding model (``BAAI/bge-small-zh-v1.5``) and the reranker both cap their
input at 512 tokens. Character-based chunking cannot guarantee that bound for
Chinese text, so ingestion measures the real tokenizer and keeps every child
chunk well below it. Parent blocks are split with the same counter and are only
used as generation context; they are never embedded.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from threading import Lock

from loguru import logger

EMBEDDER_MODEL = "BAAI/bge-small-zh-v1.5"

# Hard ceiling shared with the store: a 512-token input window still has room for
# the query and special tokens, so an indexed chunk must stay at or below this.
MAX_CHUNK_TOKENS = 480
DEFAULT_CHUNK_TOKENS = 400
# 15% of the default child size.
DEFAULT_OVERLAP_TOKENS = 60
DEFAULT_PARENT_TOKENS = 900

# CJK ideographs and kana/hangul are normally one token per character for the
# BGE Chinese tokenizer; counting them individually is both accurate and safe.
_CJK = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)
_NON_SPACE = re.compile(r"\S+")
# Split after sentence terminators but keep the terminator on the left part. The
# English alternative requires trailing whitespace so decimals such as "1.5" stay
# inside one unit.
_SENTENCE_BREAK = re.compile(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def estimate_tokens(text: str) -> int:
    """Conservative token estimate used when the real tokenizer is unavailable.

    Chinese characters count as one token each; every other non-space run is
    estimated at one token per three characters (the BERT WordPiece average is
    closer to one token per four characters for English). Over-estimating only
    makes chunks smaller, so this never hides an over-long chunk.
    """

    if not text:
        return 0
    tokens = len(_CJK.findall(text))
    remainder = _CJK.sub(" ", text)
    for run in _NON_SPACE.findall(remainder):
        tokens += max(1, math.ceil(len(run) / 3))
    return tokens


class TokenCounter:
    """Count tokens with the embedding model's tokenizer, falling back safely.

    The tokenizer is loaded lazily and only from the local cache: ingestion must
    not download models. A missing cache or a tokenizer-only deployment failure
    is not fatal; the conservative estimate keeps the 512-token bound intact.
    """

    def __init__(self, model_name: str = EMBEDDER_MODEL, tokenizer: object | None = None) -> None:
        self.model_name = model_name
        self._tokenizer = tokenizer
        self._load_attempted = tokenizer is not None
        self._lock = Lock()

    def _ensure(self) -> None:
        if self._load_attempted:
            return
        with self._lock:
            if self._load_attempted:
                return
            self._load_attempted = True
            try:
                from transformers import AutoTokenizer

                self._tokenizer = AutoTokenizer.from_pretrained(
                    self.model_name, local_files_only=True
                )
            except Exception:  # noqa: BLE001 - any loader failure means fallback
                logger.info("未加载分词器，使用保守 token 估算")
                self._tokenizer = None

    def count(self, text: str) -> int:
        if not text:
            return 0
        self._ensure()
        tokenizer = self._tokenizer
        if tokenizer is not None:
            try:
                return len(tokenizer.encode(text, add_special_tokens=False))
            except Exception:  # noqa: BLE001 - a broken tokenizer is a fallback
                logger.warning("分词器计数失败，改用保守估算")
                self._tokenizer = None
        return estimate_tokens(text)


@lru_cache(maxsize=1)
def default_token_counter() -> TokenCounter:
    """Process-wide counter so direct callers do not reload the tokenizer per document."""
    return TokenCounter()


def _join_units(units: list[str]) -> str:
    """Join sentence pieces, restoring a space only between ASCII word edges."""

    result = ""
    for unit in units:
        if (
            result
            and result[-1].isascii()
            and result[-1].isalnum()
            and unit
            and unit[0].isascii()
            and unit[0].isalnum()
        ):
            result += " "
        result += unit
    return result


def _sentence_units(paragraph: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_BREAK.split(paragraph) if part.strip()]


def _paragraph_units(text: str) -> list[str]:
    units: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(text):
        paragraph = paragraph.strip()
        if paragraph:
            units.extend(_sentence_units(paragraph))
    return units


def _hard_pieces(text: str, limit: int, counter: TokenCounter) -> list[str]:
    """Split an oversized sentence by the largest prefix that fits the limit."""

    pieces: list[str] = []
    start = 0
    end_of_text = len(text)
    while start < end_of_text:
        lo, hi = start + 1, end_of_text
        while lo < hi:
            middle = (lo + hi + 1) // 2
            if counter.count(text[start:middle]) <= limit:
                lo = middle
            else:
                hi = middle - 1
        end = max(start + 1, lo)
        part = text[start:end].strip()
        if part:
            pieces.append(part)
        start = end
    return pieces


def split_text(
    text: str,
    max_tokens: int,
    overlap_tokens: int,
    counter: TokenCounter,
) -> list[str]:
    """Split text into sentence-aligned windows no longer than ``max_tokens``.

    The splitter never cuts a sentence unless a single sentence exceeds the
    limit. Windows overlap by up to ``overlap_tokens`` trailing sentences so a
    fact near a boundary is still visible to retrieval.
    """

    if max_tokens <= 0:
        raise ValueError("分块 token 上限必须大于零")
    if overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("分块重叠 token 数必须小于分块上限")
    text = text.strip()
    if not text:
        return []

    units: list[str] = []
    for unit in _paragraph_units(text):
        if counter.count(unit) > max_tokens:
            units.extend(_hard_pieces(unit, max_tokens, counter))
        else:
            units.append(unit)
    if not units:
        return []

    counts = [counter.count(unit) for unit in units]
    chunks: list[str] = []
    index = 0
    total_units = len(units)
    while index < total_units:
        total = 0
        end = index
        while end < total_units:
            if total + counts[end] > max_tokens and end > index:
                break
            total += counts[end]
            end += 1
        if end == index:
            # Defensive: a unit somehow larger than the limit must still progress.
            end = index + 1
        part = _join_units(units[index:end])
        if part:
            if counter.count(part) > max_tokens:
                # Joining can add separators; keep the hard bound authoritative.
                chunks.extend(_hard_pieces(part, max_tokens, counter))
            else:
                chunks.append(part)
        if end >= total_units:
            break
        overlap_start = end
        overlap_count = 0
        while overlap_start > index:
            if overlap_count + counts[overlap_start - 1] > overlap_tokens:
                break
            overlap_count += counts[overlap_start - 1]
            overlap_start -= 1
        # Never repeat the whole window: that would make no forward progress.
        index = overlap_start if overlap_start > index else end
    return chunks
