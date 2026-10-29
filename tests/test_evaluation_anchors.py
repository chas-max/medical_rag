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
    assert match_anchor({"source_id": "guide", "quote": "血压测量需静坐"}, chunks) == {
        "chunk_ids": ["c1", "c2"], "score": 1.0, "method": "containment_multi"
    }

    with pytest.raises(ValueError):
        match_anchor({"source_id": "guide", "quote": "完全不存在"}, [])


def test_match_anchor_requires_ordered_exact_content_not_token_overlap():
    chunks = [{"source_id": "guide", "chunk_id": "c1", "text": "测量血压前应安静休息五分钟并保持坐姿。"}]
    with pytest.raises(ValueError, match="exact"):
        match_anchor({"source_id": "guide", "quote": "血压测量前安静休息"}, chunks)
    with pytest.raises(ValueError, match="exact"):
        match_anchor({"source_id": "guide", "quote": "完全不同的内容"}, chunks)
    with pytest.raises(ValueError, match="exact"):
        match_anchor({"source_id": "guide", "quote": "甲乙丙"}, [
            {"source_id": "guide", "chunk_id": "c2", "text": "丙乙甲"}
        ])


def test_match_anchor_ignores_pdf_extraction_whitespace_without_reordering():
    result = match_anchor({"source_id": "guide", "quote": "血压测量需静坐"}, [
        {"source_id": "guide", "chunk_id": "c1", "text": "血压\n测量 需 静坐。"}
    ])
    assert result == {"chunk_id": "c1", "score": 1.0, "method": "containment"}


def test_match_anchor_restricts_candidates_to_page_or_heading():
    pdf = [{"source_id": "guide", "chunk_id": f"p{page}", "page": page,
            "section": "", "text": "每年随访四次。"} for page in (1, 2)]
    assert match_anchor({"source_id": "guide", "locator": {"page": 2},
                         "quote": "每年随访四次。"}, pdf)["chunk_id"] == "p2"
    markdown = [{"source_id": "guide", "chunk_id": name, "page": None,
                 "section": name, "text": "食物多样。"} for name in ("饮食", "运动")]
    assert match_anchor({"source_id": "guide", "locator": {"heading": "饮食"},
                         "quote": "食物多样。"}, markdown)["chunk_id"] == "饮食"


def test_match_anchor_uses_parent_context_when_the_quote_spans_children():
    chunks = [
        {"source_id": "guide", "chunk_id": "c1", "text": "第一段只讲测量前静坐。",
         "parent_text": "第一段只讲测量前静坐。第二段才是完整锚点：每年随访四次。"},
        {"source_id": "guide", "chunk_id": "c2", "text": "第二段才是完整锚点：每年随访四次。",
         "parent_text": "第一段只讲测量前静坐。第二段才是完整锚点：每年随访四次。"},
        {"source_id": "guide", "chunk_id": "c3", "text": "另一段无关内容。",
         "parent_text": "另一段无关内容。"},
    ]
    assert match_anchor({"source_id": "guide", "quote": "测量前静坐。第二段才是完整锚点"},
                        chunks) == {
        "chunk_ids": ["c1", "c2"], "score": 1.0, "method": "parent_containment_multi"}
