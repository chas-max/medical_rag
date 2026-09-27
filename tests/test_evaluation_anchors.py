from pathlib import Path
import hashlib

import pytest

from medical_rag.evaluation_anchors import (
    make_anchor_id,
    match_anchor,
    normalize_source_text,
    source_sha256,
)


def test_normalization_makes_whitespace_and_punctuation_variants_equivalent():
    assert normalize_source_text("  血压\u00a0测量，需静坐。 ") == normalize_source_text(
        "血压 测量, 需静坐."
    )


def test_normalization_preserves_fullwidth_digits_and_compatibility_characters():
    assert normalize_source_text("１２３４５ ①") == "１２３４５ ①"


def test_anchor_id_is_stable_across_whitespace_and_punctuation():
    first = make_anchor_id("guide", ["第一章", "测量"], "血压\n测量，需静坐。")
    second = make_anchor_id("guide", ["第一章", "测量"], " 血压 测量, 需静坐. ")
    assert first == second
    assert first.startswith("guide#第一章/测量#")
    assert len(first.rsplit("#", 1)[1]) == 16


def test_anchor_id_uses_deterministic_canonical_hash():
    assert make_anchor_id("guide", ["第一章", "测量"], "血压测量") == "guide#第一章/测量#38ef9fe7c1d1b405"


def test_source_sha256_hashes_exact_source_bytes(tmp_path: Path):
    path = tmp_path / "source.txt"
    path.write_text("血压\n测量，需静坐。", encoding="utf-8")
    assert source_sha256(path) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_match_anchor_prefers_unique_quote_containment():
    anchor = {"source_id": "guide", "quote": "血压测量需静坐"}
    chunks = [
        {"source_id": "guide", "chunk_id": "c1", "text": "测量血压前应静坐五分钟。血压测量需静坐。"},
        {"source_id": "guide", "chunk_id": "c2", "text": "糖尿病患者应监测血糖。"},
    ]
    assert match_anchor(anchor, chunks) == {"chunk_id": "c1", "score": 1.0, "method": "containment"}


def test_match_anchor_rejects_source_mismatch_and_ambiguous_best_matches():
    with pytest.raises(ValueError):
        match_anchor({"source_id": "other", "quote": "血压测量"}, [{"source_id": "guide", "chunk_id": "c1", "text": "血压测量"}])

    chunks = [
        {"source_id": "guide", "chunk_id": "c1", "text": "血压测量需静坐。"},
        {"source_id": "guide", "chunk_id": "c2", "text": "血压测量需静坐。"},
    ]
    with pytest.raises(ValueError):
        match_anchor({"source_id": "guide", "quote": "血压测量需静坐"}, chunks)

    with pytest.raises(ValueError):
        match_anchor({"source_id": "guide", "quote": "完全不存在"}, [])


def test_match_anchor_uses_token_overlap_and_enforces_threshold():
    chunks = [{"source_id": "guide", "chunk_id": "c1", "text": "测量血压前应安静休息五分钟并保持坐姿。"}]
    result = match_anchor({"source_id": "guide", "quote": "血压测量前安静休息"}, chunks, min_score=0.5)
    assert result["chunk_id"] == "c1"
    assert result["method"] == "token_overlap"
    with pytest.raises(ValueError):
        match_anchor({"source_id": "guide", "quote": "完全不同的内容"}, chunks, min_score=0.9)
