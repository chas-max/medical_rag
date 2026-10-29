"""Numeric medical facts must be grounded before any output is published."""

import json

import pytest

from medical_rag.generation import GroundedGenerator
from medical_rag.numeric_validation import numbers_supported, numeric_facts
from medical_rag.types import Chunk, Evidence


def evidence(text: str, block_id: str = "c1", **kwargs: object) -> Evidence:
    return Evidence(Chunk(block_id, "doc", "guide.md", text, "guide.md", "", 1, 0,
                          **kwargs), 0.9)


def claim(text: str, *ids: str) -> dict:
    return {"text": text, "citations": [{"chunk_id": key} for key in ids or ("c1",)]}


def validate(claims: list[dict], sources: list[Evidence], **extra: object) -> dict:
    return GroundedGenerator(None).validate(
        json.dumps({"claims": claims, **extra}, ensure_ascii=False), sources)


@pytest.mark.parametrize(("source", "statement"), [
    ("每日30 mg。", "每日300 mg。"),
    ("每日30 mg。", "每日30 g。"),
    ("每3个月复查。", "每3周复查。"),
    ("间隔3小时。", "间隔3分钟。"),
    ("不低于90 mmHg。", "不高于90 mmHg。"),
    ("低于90 mmHg。", "不高于90 mmHg。"),
    ("至少90 mmHg。", "90 mmHg。"),
    ("3至6个月。", "3至9个月。"),
    ("3至6个月。", "6至3个月。"),
    ("风险为5%。", "风险为0.5%。"),
    ("每日三十毫克。", "每日三百毫克。"),
    ("每日30mg/kg。", "每日30mg。"),
    ("每日30mg。", "每周30mg。"),
    ("每日2次。", "每日3次。"),
    ("血压140/90 mmHg。", "血压140/80 mmHg。"),
    ("血糖7 mmol/L。", "血糖7 mg/dL。"),
    ("变化为-3%。", "变化为3%。"),
    ("剂量300mg。", "剂量30mg。"),
    ("每3个月复查。", "每月复查。"),
    ("不得超过90mmHg。", "超过90mmHg。"),
    ("不应低于90mmHg。", "低于90mmHg。"),
    ("30mg/(kg·d)。", "30mg。"),
    ("30mg。", "30mg/(kg·d)。"),
    ("30mg/平方米。", "30mg。"),
    ("30mg。", "30mg，300mg。"),
    ("30mg。", "30mg,300mg。"),
    ("每日每次30mg。", "每周每次30mg。"),
    ("每日每次30mg。", "每次30mg。"),
    ("3mg/kg至6mg/kg。", "6mg/kg。"),
    ("3mg至6mg/kg。", "3至6mg/kg。"),
    ("30mg。", "30.0000000000000000000000000001mg。"),
])
def test_changed_numeric_fact_is_rejected(source: str, statement: str) -> None:
    result = validate([claim(statement)], [evidence(source)])

    assert result["abstained"] is True
    assert result["citations"] == []
    assert result["abstain_reason"] == "ABSTAIN_NUMERIC_MISMATCH"
    assert result["numeric_rejections"] == {"claims": 1}


@pytest.mark.parametrize(("source", "statement"), [
    ("每日30 mg。", "每日30 mg。"),
    ("每日30 mg。", "每天三十毫克。"),
    ("0.5 mg。", "零点五毫克。"),
    ("0.030 g。", "０．０３克。"),
    ("300 μg。", "三百微克。"),
    ("300 ug。", "300 µg。"),
    ("5 mL。", "五毫升。"),
    ("血糖7 mmol/L。", "血糖７毫摩尔/升。"),
    ("不低于90 mmHg。", "≥90毫米汞柱。"),
    ("90 mmHg以上。", "至少90 mmHg。"),
    ("不超过90 mmHg。", "≤90 mmHg。"),
    ("3至6个月。", "三～六个月。"),
    ("血压140/90 mmHg。", "血压１４０／９０毫米汞柱。"),
    ("每日2次。", "每天两次。"),
    ("每日30mg/kg。", "每天三十毫克/公斤。"),
    ("每3个月复查。", "每三个月复查。"),
    ("风险为5%。", "风险为百分之五。"),
    ("不得超过90mmHg。", "不高于90mmHg。"),
    ("不应低于90mmHg。", "不低于90mmHg。"),
    ("每个月复查。", "每月复查。"),
    ("3mg/kg至6mg/kg。", "3至6毫克/公斤。"),
    ("高血压患者应规律随访。", "应规律随访。"),
])
def test_equivalent_numeric_facts_and_plain_prose_survive(source: str, statement: str) -> None:
    result = validate([claim(statement)], [evidence(source)])

    assert result["abstained"] is False
    assert result["answer"] == f"{statement} [c1]"
    assert "numeric_rejections" not in result


def test_numbers_cannot_be_borrowed_from_uncited_evidence_or_fabricated_excerpt() -> None:
    entry = claim("每日300mg。")
    entry["citations"][0]["excerpt"] = "每日300mg。"
    result = validate([entry], [evidence("每日30mg。"), evidence("每日300mg。", "c2")])

    assert result["abstained"] is True
    assert result["abstain_reason"] == "ABSTAIN_NUMERIC_MISMATCH"


def test_multiple_references_support_separate_complete_numeric_facts() -> None:
    result = validate([claim("每日30mg，每3个月复查。", "c1", "c2")],
                      [evidence("每日30mg。"), evidence("每3个月复查。", "c2")])

    assert result["abstained"] is False
    assert [item["chunk_id"] for item in result["citations"]] == ["c1", "c2"]


def test_range_endpoints_cannot_be_assembled_from_different_blocks() -> None:
    result = validate([claim("3至6个月。", "c1", "c2")],
                      [evidence("3个月。"), evidence("6个月。", "c2")])

    assert result["abstained"] is True


def test_rejected_claim_leaves_no_citations_or_analysis_anchors_behind() -> None:
    result = validate([claim("应规律随访。"), claim("每日300mg。", "c2")],
                      [evidence("应规律随访。"), evidence("每日30mg。", "c2")],
                      analysis={"summary": "随访有助于管理。", "based_on": ["c2"]})

    assert result["answer"] == "应规律随访。 [c1]"
    assert [item["chunk_id"] for item in result["citations"]] == ["c1"]
    assert result["analysis"]["based_on"] == ["c1"]
    assert result["numeric_rejections"] == {"claims": 1}


def test_numeric_fact_is_verified_against_parent_context_seen_by_generator() -> None:
    result = validate([claim("每日30mg。")], [evidence(
        "局部摘要。", parent_id="p1", parent_text="完整原文：每日30mg。")])

    assert result["abstained"] is False


@pytest.mark.parametrize("summary", ["每日300mg。", "每周30mg。", "至少30mg。"])
def test_analysis_summary_cannot_introduce_or_change_verified_numeric_facts(summary: str) -> None:
    # 原文含有额外数字也不能成为分析段新增数值的依据。
    result = validate([claim("每日30mg。")], [evidence("每日30mg。另有方案每日300mg。")],
                      analysis={"summary": summary, "based_on": ["c1"]})

    assert result["abstained"] is False
    assert "analysis" not in result
    assert result["numeric_rejections"] == {"analysis": 1}


def test_analysis_numeric_sources_must_also_be_covered_by_based_on() -> None:
    result = validate([claim("每日30mg。"), claim("每3个月复查。", "c2")],
                      [evidence("每日30mg。"), evidence("每3个月复查。", "c2")],
                      analysis={"summary": "每3个月复查。", "based_on": ["c1"]})

    assert "analysis" not in result


def test_safe_analysis_survives_but_unsupported_extensions_and_gaps_are_removed() -> None:
    result = validate([claim("每日30mg。")], [evidence("每日30mg。")],
                      analysis={"summary": "每天三十毫克。", "based_on": ["c1"],
                                "extensions": ["每日300mg。", "可带记录就医。", "每周30mg。"]},
                      gaps=["儿童适用条件", "可能需要每日300mg。"])

    assert result["analysis"]["summary"] == "每天三十毫克。"
    assert result["analysis"]["extensions"] == ["可带记录就医。"]
    assert result["gaps"] == ["儿童适用条件"]
    assert "300mg" not in result["answer"]
    assert result["numeric_rejections"] == {"analysis": 2, "gaps": 1}


class StreamTransport:
    def __init__(self, document: str, size: int) -> None:
        self.document = document
        self.size = size

    def stream_chat(self, provider, messages, tools=None, on_content=None):
        for index in range(0, len(self.document), self.size):
            on_content(self.document[index:index + self.size])
        return {"content": self.document, "tool_calls": []}


@pytest.mark.parametrize("size", [1, 7, 4096])
def test_stream_never_publishes_rejected_numeric_claims_or_analysis(size: int) -> None:
    document = json.dumps({"claims": [claim("每日300mg。"), claim("每日30mg。")],
                           "analysis": {"summary": "每日300mg。", "based_on": ["c1"]}},
                          ensure_ascii=False)
    generator = GroundedGenerator(StreamTransport(document, size))
    emitted = []
    drafted = generator.draft("用量？", [evidence("每日30mg。")], "qwen", on_claim=emitted.append)
    final = generator.validate(drafted["draft"], drafted["evidence"])

    assert all("text" in event for event in emitted)
    assert [event["text"] for event in emitted] == ["每日30mg。 [c1]"]
    assert final["answer"] == emitted[0]["text"]
    assert "analysis" not in final


@pytest.mark.parametrize(("source", "statement"), [
    ("每次三十二毫克。", "每次32mg。"),
    ("每次一千零二毫克。", "每次1002mg。"),
    ("一万国际单位。", "10000 IU。"),
    ("二亿国际单位。", "200000000 IU。"),
    ("1万IU。", "10000 IU。"),
    ("1,000 mg。", "1000mg。"),
    ("1e3 mg。", "1000mg。"),
    ("半年。", "0.5年。"),
    ("一年半。", "1.5年。"),
    (".5 mg。", "0.5mg。"),
    ("负3%。", "-3%。"),
    ("每2天30mg。", "每两天三十毫克。"),
    ("30mg/天。", "每日30毫克。"),
    ("每次30mg。", "30mg/次。"),
    ("3个月至6个月。", "3-6个月。"),
])
def test_numeric_normalization(source: str, statement: str) -> None:
    assert numbers_supported(statement, [source]) is True
    assert numeric_facts(statement)


@pytest.mark.parametrize("statement", [
    "不低于90mmHg以下。", "6至3个月。", "30mg半。",
    "每日30mg/周。", "零点十毫克。",
])
def test_malformed_recognized_quantities_fail_closed_even_if_repeated(statement: str) -> None:
    assert numbers_supported(statement, [statement]) is False


def test_sources_with_malformed_numeric_facts_cannot_support_a_dose() -> None:
    assert numbers_supported("30mg。", ["30mg半。"]) is False


def test_analysis_does_not_treat_unverified_chinese_count_as_plain_prose() -> None:
    result = validate([claim("应规律随访。")], [evidence("应规律随访。")],
                      analysis={"summary": "每月复查一次。", "based_on": ["c1"]})
    assert result["abstained"] is False
    assert "analysis" not in result


def test_numeric_dimensions_are_not_invented_when_there_are_no_sources() -> None:
    assert numbers_supported("每月复查。", []) is False
    assert numbers_supported("30mg。", []) is False
    assert numbers_supported("没有医学数值的通俗说明。", []) is True


def test_large_scientific_number_never_raises_or_matches_a_normal_dose() -> None:
    assert numbers_supported("1e1000000mg。", ["30mg。"]) is False


@pytest.mark.parametrize("statement", ["1e1000000年半。", "9" * 257 + "mg。"])
def test_excessive_numeric_representation_fails_closed(statement: str) -> None:
    assert numbers_supported(statement, [statement]) is False


@pytest.mark.parametrize("field", ["summary", "extension", "gap"])
def test_comma_separated_dose_cannot_bypass_secondary_text_checks(field: str) -> None:
    secondary = "30mg，300mg。"
    analysis = {"summary": secondary if field == "summary" else "请核对原文。",
                "extensions": [secondary] if field == "extension" else [], "based_on": ["c1"]}
    result = validate([claim("30mg。")], [evidence("30mg。")], analysis=analysis,
                      gaps=[secondary] if field == "gap" else [])

    assert result["answer"] == "30mg。 [c1]"
    if field == "summary":
        assert "analysis" not in result
    else:
        assert result["analysis"]["extensions"] == []
    assert "gaps" not in result
