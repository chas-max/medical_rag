"""Recovery of model output that is valid JSON in substance but not in surface form."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.model_json import (
    REPAIR_CLAIMS_LIST,
    REPAIR_FENCE,
    REPAIR_PROSE,
    REPAIR_TRAILING_COMMA,
    REPAIR_TRUNCATED,
    parse_model_json,
)

CLAIM = '{"text":"应规律随访","citations":[{"chunk_id":"c1"}]}'
OBJECT = '{"claims":[' + CLAIM + "]}"


def test_clean_object_needs_no_repair():
    parsed = parse_model_json(OBJECT)

    assert parsed.payload == {"claims": [{"text": "应规律随访",
                                          "citations": [{"chunk_id": "c1"}]}]}
    assert parsed.repair is None
    assert parsed.recovered is True


def test_markdown_fence_is_stripped():
    parsed = parse_model_json("```json\n" + OBJECT + "\n```")

    assert parsed.payload["claims"][0]["text"] == "应规律随访"
    assert parsed.repair == REPAIR_FENCE


def test_unterminated_fence_is_still_read():
    parsed = parse_model_json("```json\n" + OBJECT)

    assert parsed.payload["claims"][0]["text"] == "应规律随访"
    assert parsed.repair == REPAIR_FENCE


def test_sentence_before_and_after_the_object_is_ignored():
    parsed = parse_model_json("好的，以下是回答：\n" + OBJECT + "\n希望对你有帮助。")

    assert parsed.payload["claims"][0]["text"] == "应规律随访"
    assert parsed.repair == REPAIR_PROSE


def test_trailing_comma_is_dropped_once():
    parsed = parse_model_json('{"claims":[{"text":"应规律随访",'
                              '"citations":[{"chunk_id":"c1"},]},],}')

    assert parsed.payload["claims"][0]["citations"] == [{"chunk_id": "c1"}]
    assert parsed.repair == REPAIR_TRAILING_COMMA


def test_comma_inside_a_string_is_not_a_trailing_comma():
    text = '{"claims":[{"text":"每日 1 次，随访，复查，","citations":[{"chunk_id":"c1"}]}]}'

    assert parse_model_json(text).payload["claims"][0]["text"] == "每日 1 次，随访，复查，"


def test_truncated_last_claim_keeps_every_finished_claim():
    parsed = parse_model_json('{"claims":[' + CLAIM + ',{"text":"第二条还没写完')

    assert parsed.payload == {"claims": [{"text": "应规律随访",
                                          "citations": [{"chunk_id": "c1"}]}]}
    assert parsed.repair == REPAIR_TRUNCATED


def test_truncation_inside_the_analysis_section_keeps_the_claims():
    parsed = parse_model_json('{"claims":[' + CLAIM + '],"analysis":{"summary":"解释写到一半')

    assert [claim["text"] for claim in parsed.payload["claims"]] == ["应规律随访"]
    assert parsed.repair == REPAIR_TRUNCATED
    assert "analysis" not in parsed.payload


def test_truncation_inside_the_first_citation_recovers_nothing():
    """半条结论没有锚点就没有价值：宁可返回空，也不替模型补 citations。"""
    parsed = parse_model_json('{"claims":[{"text":"应规律随访","citations":[{"chunk_id":"c')

    assert parsed.payload is None


def test_bare_claim_array_is_wrapped():
    parsed = parse_model_json("[" + CLAIM + "]")

    assert parsed.payload == {"claims": [{"text": "应规律随访",
                                          "citations": [{"chunk_id": "c1"}]}]}
    assert parsed.repair == REPAIR_CLAIMS_LIST


def test_braces_and_quotes_inside_the_prose_are_not_mistaken_for_structure():
    text = '{"claims":[{"text":"剂量写成 {每次 30mg}，他说 \\"按说明\\"","citations":[{"chunk_id":"c1"}]}]}'

    parsed = parse_model_json(text)

    assert parsed.payload["claims"][0]["text"] == '剂量写成 {每次 30mg}，他说 "按说明"'
    assert parsed.repair is None


def test_empty_claims_are_a_decoded_object_not_a_failure():
    parsed = parse_model_json('{"claims":[]}')

    assert parsed.payload == {"claims": []}
    assert parsed.repair is None


def test_a_raw_newline_inside_a_string_is_tolerated():
    """模型常在 text 里直接换行；严格 JSON 会拒绝，但这显然不是“资料不足”。"""
    parsed = parse_model_json('{"claims":[{"text":"第一行\n第二行","citations":[{"chunk_id":"c1"}]}]}')

    assert parsed.payload["claims"][0]["text"] == "第一行\n第二行"
    assert parsed.repair is None


def test_full_width_colon_after_the_claims_key_is_recovered():
    parsed = parse_model_json('{"claims"：[' + CLAIM + "]}")

    assert parsed.payload["claims"][0]["text"] == "应规律随访"


def test_unusable_text_returns_no_payload():
    for text in ("not json", "", "   ", None, 5, '{"claims":'):
        assert parse_model_json(text).payload is None
