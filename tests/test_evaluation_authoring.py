"""Source-backed draft materialization must reject unsupported entries."""

import json

import pytest

from medical_rag.evaluation_authoring import materialize_draft


def test_materialize_markdown_draft_with_stable_heading_and_anchor(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("# 指南\n## 准则一\n- 每天吃蔬菜。\n", encoding="utf-8")
    draft = tmp_path / "draft.psv"
    draft.write_text(
        "id|question_type|difficulty|answerability|question|reference_answer|key_points|source_quote\n"
        "MHE-0001|fact|easy|answerable|每天吃什么？|每天吃蔬菜。|每天；蔬菜|每天吃蔬菜。\n",
        encoding="utf-8",
    )
    rows = materialize_draft(draft, "guide", source, "膳食指南")
    assert rows[0]["source_refs"][0]["locator"] == {"heading": "准则一"}
    assert rows[0]["source_refs"][0]["anchor_id"].startswith("guide#准则一#")
    assert rows[0]["relevant_chunk_ids"] == []
    assert json.loads(json.dumps(rows, ensure_ascii=False))[0]["key_points"] == ["每天", "蔬菜"]


def test_materialize_rejects_quote_absent_from_source(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("## 准则一\n每天吃蔬菜。", encoding="utf-8")
    draft = tmp_path / "draft.psv"
    draft.write_text(
        "id|question_type|difficulty|answerability|question|reference_answer|key_points|source_quote\n"
        "MHE-0001|fact|easy|answerable|问题？|答案。|要点|源文没有的说法。\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="MHE-0001"):
        materialize_draft(draft, "guide", source, "膳食指南")


def test_materialize_repeated_quote_requires_explicit_heading(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("## 第一节\n共同建议。\n## 第二节\n共同建议。\n", encoding="utf-8")
    draft = tmp_path / "draft.psv"
    draft.write_text(
        "id|question_type|difficulty|answerability|question|reference_answer|key_points|source_quote\n"
        "MHE-0001|fact|easy|answerable|问题？|答案。|要点|@heading:第二节::共同建议。\n",
        encoding="utf-8",
    )
    rows = materialize_draft(draft, "guide", source, "膳食指南")
    assert rows[0]["source_refs"][0]["locator"] == {"heading": "第二节"}
    assert rows[0]["source_refs"][0]["quote"] == "共同建议。"
