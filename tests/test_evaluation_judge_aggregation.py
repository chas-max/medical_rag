import json

import pytest

from medical_rag.evaluation_judge_aggregation import aggregate_judges


def _row(identifier, answerability):
    return {"id": identifier, "question": identifier, "reference_answer": "参考答案",
            "key_points": ["一点", "二点"], "answerability": answerability,
            "category": "测试", "question_type": "fact", "difficulty": "easy",
            "source_refs": [{"source_id": "src", "locator": {"page": 1},
                             "quote": "参考答案", "anchor_id": "src#1"}],
            "relevant_chunk_ids": []}


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def test_aggregate_three_platform_medians_and_abstention(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    _write_jsonl(dataset, [_row("MHE-0001", "answerable"),
                           _row("MHE-0002", "insufficient_evidence")])
    paths = []
    for platform, score, abstain in (("chatgpt", 4, True), ("claude", 3, True),
                                     ("qoder", 1, False)):
        path = tmp_path / f"{platform}.jsonl"
        _write_jsonl(path, [
            {"id": "MHE-0001", "judge_platform": platform, "judge_model": platform + "-model",
             "rubric_version": "r1", "scores": {name: score for name in
                ("key_point_coverage", "faithfulness", "answer_relevance", "medical_safety")},
             "unsafe": False},
            {"id": "MHE-0002", "judge_platform": platform, "judge_model": platform + "-model",
             "rubric_version": "r1", "scores": None, "correct_abstention": abstain,
             "unsupported_answer": not abstain, "unsafe": not abstain},
        ])
        paths.append(path)
    output = tmp_path / "summary.json"
    report = aggregate_judges(dataset, paths, output, rubric_version="r1")
    assert report["answer_score_100"] == 75
    assert report["valid_answer_count"] == 1
    assert report["judge_disagreement_count"] == 1
    assert report["correct_abstention_rate"] == 1
    assert report["unsupported_answer_count"] == 0
    assert report["unsafe_case_count"] == 1
    assert report["judge_models"] == {name: name + "-model" for name in
                                      ("chatgpt", "claude", "qoder")}
    assert json.loads(output.read_text(encoding="utf-8"))["cases"][0]["score_100"] == 75


def test_aggregate_rejects_duplicate_platform_and_invalid_score(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    _write_jsonl(dataset, [_row("MHE-0001", "answerable")])
    paths = []
    for i in range(3):
        path = tmp_path / f"judge-{i}.jsonl"
        _write_jsonl(path, [{"id": "MHE-0001", "judge_platform": "chatgpt",
                             "judge_model": "model", "rubric_version": "r1",
                             "scores": {name: 5 if i == 0 else 3 for name in
                                 ("key_point_coverage", "faithfulness", "answer_relevance", "medical_safety")},
                             "unsafe": False}])
        paths.append(path)
    with pytest.raises(ValueError, match="平台"):
        aggregate_judges(dataset, paths, tmp_path / "out.json", rubric_version="r1")

    for path, platform in zip(paths, ("chatgpt", "claude", "qoder")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        entry["judge_platform"] = platform
        _write_jsonl(path, [entry])
    with pytest.raises(ValueError, match="四维分"):
        aggregate_judges(dataset, paths, tmp_path / "out.json", rubric_version="r1")
    assert not (tmp_path / "out.json").exists()


def test_aggregate_reports_missing_judge_without_scoring(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    _write_jsonl(dataset, [_row("MHE-0001", "answerable"), _row("MHE-0002", "answerable")])
    paths = []
    for platform in ("chatgpt", "claude", "qoder"):
        path = tmp_path / f"{platform}.jsonl"
        rows = [{"id": identifier, "judge_platform": platform, "judge_model": "model",
                 "rubric_version": "r1", "scores": {name: 3 for name in
                     ("key_point_coverage", "faithfulness", "answer_relevance", "medical_safety")},
                 "unsafe": False} for identifier in
                (["MHE-0001"] if platform == "qoder" else ["MHE-0001", "MHE-0002"])]
        _write_jsonl(path, rows)
        paths.append(path)
    report = aggregate_judges(dataset, paths, tmp_path / "out.json", rubric_version="r1")
    assert report["valid_answer_count"] == 1
    assert report["incomplete_judging_count"] == 1
    assert report["cases"][1]["score_100"] is None
