import json

import pytest

from medical_rag.evaluation_release_build import compile_release


def test_compile_release_refuses_incomplete_draft_without_output(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("## 准则\n- 每天吃蔬菜。\n", encoding="utf-8")
    draft = tmp_path / "draft.psv"
    draft.write_text(
        "id|question_type|difficulty|answerability|question|reference_answer|key_points|source_quote\n"
        "MHE-0001|fact|easy|answerable|每天吃什么？|每天吃蔬菜。|每天；蔬菜|每天吃蔬菜。\n",
        encoding="utf-8",
    )
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"drafts": [{"draft": "draft.psv", "source_id": "guide",
                                         "source_file": "guide.md", "category": "膳食指南"}]},
                                ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "questions.jsonl"
    with pytest.raises(ValueError, match="500"):
        compile_release(index, output)
    assert not output.exists()
    assert compile_release(index, output, expected_count=1) == 1
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["id"] == "MHE-0001"
    assert row["source_refs"][0]["locator"]["heading"] == "准则"


def test_compile_release_composite_requires_two_distinct_sources(tmp_path):
    for name, quote in (("a", "膳食多样。"), ("b", "规律运动。")):
        (tmp_path / f"{name}.md").write_text(f"## 指导\n{quote}\n", encoding="utf-8")
        (tmp_path / f"{name}.psv").write_text(
            "id|question_type|difficulty|answerability|question|reference_answer|key_points|source_quote\n"
            f"MHE-000{1 if name == 'a' else 2}|fact|easy|answerable|{name}是什么？|{quote}|{quote}|{quote}\n",
            encoding="utf-8",
        )
    composite = tmp_path / "cross.psv"
    composite.write_text(
        "id|question_type|difficulty|answerability|question|reference_answer|key_points|evidence_ids\n"
        "MHE-0003|multi_evidence|hard|answerable|怎样保持健康？|膳食多样且规律运动。|膳食多样；规律运动|MHE-0001；MHE-0002\n",
        encoding="utf-8",
    )
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"drafts": [
        {"draft": f"{name}.psv", "source_id": name, "source_file": f"{name}.md", "category": name}
        for name in ("a", "b")
    ], "composites": [{"draft": "cross.psv", "category": "综合"}]}, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "release.jsonl"
    assert compile_release(index, output, expected_count=3) == 3
    row = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()][2]
    assert {ref["source_id"] for ref in row["source_refs"]} == {"a", "b"}

    composite.write_text(composite.read_text(encoding="utf-8").replace("MHE-0001；MHE-0002", "MHE-0001；MHE-0001"), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        compile_release(index, tmp_path / "another.jsonl", expected_count=3)
