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



def test_medical_source_isolation_and_complex_notation_do_not_drop_valid_claims() -> None:
    chunk = (
        "静脉点滴期间每 15~20 分钟监测一次。空腹血糖 5.1 mmol/L≤X<7.0 mmol/L "
        "且 BMI≥32.5 kg/m2 时，血压应低于 130 mmHg。复方厄贝沙坦150mg/氢氯噻嗪12.5mg，"
        "超过1周及以上观察，儿童可用 30mg/(kg·d)。"
    )
    assert numbers_supported("血压低于 130 mmHg，静脉点滴期间监测。", [chunk]) is True
    assert numbers_supported("每天30mg/(kg·d)。", ["每日 30mg/(kg·d)。"]) is True
    assert numbers_supported("每日1次。", [chunk]) is False
    assert numbers_supported("30mg/(kg·d)。", ["每日 30mg/(kg·d)。", chunk]) is True
    assert numbers_supported("30mg。", ["30mg半。"]) is False
    assert numbers_supported("30mg/(kg·d)。", ["30mg。"]) is False


def test_pdf_spacing_chinese_bmi_units_and_age_order_do_not_drop_claims() -> None:
    bmi_src = "65 岁以上老年人的适宜BMI建议保持在20～26.9 千克/米2，80 岁以上建议保持在22～26.9 千克/米2。"
    assert numbers_supported(
        "65岁以上老年人BMI建议保持在20~26.9 kg/m²，80岁以上建议保持在22~26.9 kg/m²。",
        [bmi_src],
    ) is True
    assert numbers_supported(
        "65岁以上老年人BMI建议保持在20~26.9 kg/m2,80岁以上建议保持在22~26.9 kg/m2。",
        [bmi_src],
    ) is True
    assert numbers_supported("1,23mg。", ["1,23mg。"]) is False

    elder_src = "辖区内 65 岁及以上常住居民。每年为老年人提供 1 次健康管理服务。"
    assert numbers_supported("每年为65岁及以上老年人提供1次健康管理服务。", [elder_src]) is True

    peri_src = (
        "术前或术后可口服10~25 g碳水化合物；不能口服者静脉推注50%葡萄糖注射液20~50 ml，"
        "持续静脉点滴5%或10%葡萄糖注射液，每 15~20 分 钟 监 测1 次直至血\n糖≥5.6 mmol/L。"
    )
    assert numbers_supported(
        "术前或术后口服10~25 g快速吸收碳水化合物；不能口服者静推50%葡萄糖注射液20~50 ml，"
        "持续静滴5%或10%葡萄糖注射液，每15~20分钟监测1次直至血糖≥5.6 mmol/L。",
        [peri_src],
    ) is True

    wrapped_bmi = "18岁以上正常BMI在18.5～23.9 千\n克/米2，BMI≥28 千\n克/米2为肥胖，65岁以上建议20～26.9 千克/米2。"
    assert numbers_supported(
        "18岁以上正常BMI为18.5～23.9 kg/m²，BMI≥28 kg/m²为肥胖；65岁以上建议保持在20～26.9千克/米²。",
        [wrapped_bmi],
    ) is True

    gdm_src = "75 g OGTT：5.1 mmol/L≤空腹血糖<7.0 mmol/L，OGTT 1 h 血糖≥10.0 mmol/L，8.5 mmol/L≤OGTT 2 h 血糖<11.1 mmol/L。"
    assert numbers_supported(
        "75 g OGTT空腹血糖≥5.1 mmol/L且<7.0 mmol/L，1小时≥10.0 mmol/L，2小时≥8.5 mmol/L且<11.1 mmol/L。",
        [gdm_src],
    ) is True


def test_compound_slash_drugs_and_table_column_header_units_pass() -> None:
    compound_src = "单片复方制剂：氯沙坦钾50 mg/氢氯噻嗪12.5 mg（或100/12.5 mg），氨氯地平5 mg/贝那普利10 mg，1片/d。"
    assert numbers_supported("可选用氯沙坦钾50 mg/氢氯噻嗪12.5 mg（1片/d）。", [compound_src]) is True
    assert numbers_supported("氯沙坦钾100 mg/氢氯噻嗪12.5 mg。", [compound_src]) is True
    assert numbers_supported("氯沙坦钾50 mg/氢氯噻嗪25 mg。", [compound_src]) is False

    table_15 = (
        "表 15  常用 CCB 剂量与药代学\n"
        "药物 消除半衰期 (h) 剂量(起始剂量~足量)(mg/d) 每日次数\n"
        "氨氯地平 30~50 2.5~10 1"
    )
    assert numbers_supported("氨氯地平消除半衰期为30~50 h，起始剂量2.5~10 mg/d。", [table_15]) is True

    table_diet = (
        "表 2.6  食物交换份\n"
        "每份热量90kcal；每份质量（g） 钠含量（mg）\n"
        "米饭 80～100 面包 32"
    )
    assert numbers_supported("每份热量90 kcal，米饭每份80~100 g，面包32 g。", [table_diet]) is True


def test_pdf_line_wrap_comma_cadence_reset_table_counts_and_scaled_denominators() -> None:
    weight_src = (
        "超重患者每 天 可 减 少\n250～500kcal，一般每周减轻0.5~1kg，每月减轻2~4kg，"
        "避免能量密度≥400kcal/100g的超加工食品。"
    )
    assert numbers_supported(
        "每天减少250~500 kcal，每周减轻0.5~1 kg，每月减轻2~4 kg，避免≥400 kcal/(100 g)食品。",
        [weight_src],
    ) is True
    assert numbers_supported("每月减轻0.5~1 kg。", [weight_src]) is False
    assert numbers_supported("能量密度≥400 kcal/1 g。", [weight_src]) is False

    assert numbers_supported(
        "每日1次，每次30mg。",
        ["每日1次，每次30 mg。"],
    ) is True
    assert numbers_supported("每次30mg。", ["每日1次，每次30 mg。"]) is False

    table_16 = (
        "表 16 常用 ARB 与 ARNI 药物\n"
        "药物 消除半衰期(h) 剂量(起始剂量~足量)(mg/d) 每天服药 次数\n"
        "沙库巴曲缬沙坦钠 11.5/9.9 100~200 2"
    )
    assert numbers_supported(
        "沙库巴曲缬沙坦钠半衰期分别为11.5 h和9.9 h，起始100 mg/d，足量200 mg/d，每天服药2次。",
        [table_16],
    ) is True
    assert numbers_supported("沙库巴曲缬沙坦钠每天服药3次。", [table_16]) is False


def test_negation_linewrap_list_prefix_space_unit_and_time_cadence_symmetry() -> None:
    # 1. 不宜超过否定前缀
    assert numbers_supported("每日食盐不超过5g。", ["每日食盐不宜超过5g。"]) is True
    assert numbers_supported("每日食盐超过5g。", ["每日食盐不宜超过5g。"]) is False

    # 2. PDF 中文断行保留前置 cadence，且不破坏表格换行解析
    wrapped_diet = "建议每日烹调油使用\n量宜控制在 25g 以内、食盐用量每日不宜超过 5g。"
    assert numbers_supported("每日烹调油使用量宜控制在25g以内，食盐用量每日不宜超过5g。", [wrapped_diet]) is True
    assert numbers_supported("每周烹调油25g以内。", [wrapped_diet]) is False

    # 3. 列表编号不误判为小数
    list_src = "（一）筛查策略。1.20～39岁 （1）每月1次检查。2.40～69岁 （1）每年1次。3.70岁以上"
    assert numbers_supported("20～39岁每月1次，40～69岁每年1次，70岁以上。", [list_src]) is True
    assert numbers_supported("20～49岁。", [list_src]) is False

    # 4. 单位空格断行 kg/m 2
    assert numbers_supported("BMI为18.5～23.9kg/m²。", ["BMI在 18.5～23.9kg/m 2。"]) is True
    assert numbers_supported("BMI为18.5～24.9kg/m²。", ["BMI在 18.5～23.9kg/m 2。"]) is False

    # 5. 「两次…间隔」不被误判为给药频次 & 时间单位周期对称性
    blood_src = "全血每次200～400毫升，两次捐献间隔不少于6个月。"
    assert numbers_supported("全血每次捐献200～400毫升，两次捐献间隔期不少于6个月。", [blood_src]) is True
    assert numbers_supported("全血每次捐献200～400毫升，两次捐献间隔不少于3个月。", [blood_src]) is False

    assert numbers_supported("每周至少250分钟。", ["至少250分钟/周。"]) is True
    assert numbers_supported("至少250分钟/周。", ["每周至少250分钟。"]) is True
    assert numbers_supported("每天250分钟。", ["每周250分钟。"]) is False
