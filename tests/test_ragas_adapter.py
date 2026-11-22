import pytest

from medical_rag.ragas_adapter import (
    DEFAULT_METRICS,
    RagasConfig,
    applicable_metrics,
    build_headline,
    build_sample,
    merge_ragas_report,
    normalize_metric_names,
)


def evidence(chunk_id, parent_id="", text="上下文"):
    return {"chunk_id": chunk_id, "parent_id": parent_id, "text": text}


def rich_case():
    return {
        "id": "MHE2-0001",
        "question": "高血压患者如何随访？",
        "reference_answer": "定期测量血压并随访。",
        "key_points": ["测量血压", "随访"],
        "answerability": "answerable",
        "category": "慢病",
        "question_type": "procedure",
        "difficulty": "easy",
        "source_refs": [
            {"source_id": "doc", "quote": "每年随访四次", "relevance": 2},
            {"source_id": "doc", "quote": "每次测量血压", "relevance": 1},
            {"source_id": "doc", "quote": "无关干扰", "relevance": 0},
        ],
        "relevant_chunk_ids": ["c1", "c3"],
        "evidence": [
            evidence("c1", "p1", "父窗口一"),
            evidence("c2", "p1", "父窗口一"),
            evidence("c3", "p2", "父窗口二"),
            evidence("c4", "", "网页片段"),
        ],
        "answer": {"answer": "应随访[c1]并测量[c2]", "citations": [], "abstained": False},
    }


def test_sample_keeps_dual_context_views():
    sample = build_sample(rich_case())
    assert sample.retrieval_hit_count == 4
    assert sample.semantic_context_count == 3
    assert sample.retrieved_contexts == ("父窗口一", "父窗口二", "网页片段")
    assert sample.retrieved_context_ids == ("c1", "c2", "c3", "c4")
    assert sample.reference_contexts == ("每年随访四次", "每次测量血压")
    assert sample.reference_context_ids == ("c1", "c3")
    assert sample.response == "应随访并测量"
    assert sample.warnings == ()


def test_legacy_report_dedupes_by_text():
    case = rich_case()
    case["evidence"] = [evidence("c1", "", "同一父窗口"), evidence("c2", "", "同一父窗口")]
    sample = build_sample(case)
    assert sample.retrieved_context_ids == ("c1", "c2")
    assert sample.retrieved_contexts == ("同一父窗口",)
    assert "LEGACY_TEXT_DEDUPE" in sample.warnings


def test_parent_text_mismatch_is_a_warning_not_a_crash():
    case = rich_case()
    case["evidence"] = [evidence("c1", "p1", "第一版"), evidence("c2", "p1", "第二版")]
    sample = build_sample(case)
    assert sample.retrieved_contexts == ("第一版",)
    assert "PARENT_TEXT_MISMATCH" in sample.warnings


def test_abstained_and_string_answers():
    case = rich_case()
    case["answer"] = {"answer": "资料不足", "citations": [], "abstained": True}
    assert build_sample(case).response is None

    case["answer"] = "字符串回答[c1]"
    assert build_sample(case).response == "字符串回答"

    case["answer"] = None
    assert build_sample(case).response is None


def test_applicability_matrix():
    sample = build_sample(rich_case())
    applicable, skipped = applicable_metrics(sample, DEFAULT_METRICS)
    assert applicable == list(DEFAULT_METRICS)
    assert skipped == {}

    abstained = rich_case()
    abstained["answer"] = {"answer": "资料不足", "citations": [], "abstained": True}
    applicable, skipped = applicable_metrics(build_sample(abstained), DEFAULT_METRICS)
    assert "faithfulness" in skipped and skipped["faithfulness"] == "NO_RESPONSE"
    assert "answer_relevancy" in skipped
    assert "id_based_context_precision" in applicable
    assert "context_precision" in applicable

    insufficient = rich_case()
    insufficient["answerability"] = "insufficient_evidence"
    applicable, skipped = applicable_metrics(build_sample(insufficient), DEFAULT_METRICS)
    assert applicable == []
    assert set(skipped.values()) == {"EXPECTED_ABSTENTION"}

    legacy = rich_case()
    legacy.pop("reference_answer")
    applicable, skipped = applicable_metrics(build_sample(legacy), DEFAULT_METRICS)
    assert skipped["context_precision"] == "NO_REFERENCE"
    assert skipped["context_recall"] == "NO_REFERENCE"
    assert "faithfulness" in applicable

    no_evidence = rich_case()
    no_evidence["evidence"] = []
    applicable, skipped = applicable_metrics(build_sample(no_evidence), DEFAULT_METRICS)
    assert skipped["id_based_context_precision"] == "NO_RETRIEVED_IDS"
    assert skipped["context_relevance"] == "NO_RETRIEVED_CONTEXTS"

    failed = rich_case()
    failed["error"] = "SEARCH_FAILED"
    applicable, skipped = applicable_metrics(build_sample(failed), DEFAULT_METRICS)
    assert applicable == []
    assert set(skipped.values()) == {"CASE_ERROR"}


def test_normalize_metrics_aliases_and_local_only_rejection():
    assert normalize_metric_names("precision,recall,faithfulness") == (
        "id_based_context_precision",
        "id_based_context_recall",
        "faithfulness",
    )
    assert normalize_metric_names("all") == DEFAULT_METRICS
    with pytest.raises(ValueError, match="本地指标"):
        normalize_metric_names("mrr")
    with pytest.raises(ValueError, match="未知"):
        normalize_metric_names("not-a-metric")


def test_config_validates_ranges():
    config = RagasConfig(metrics=("faithfulness",), concurrency=2, limit=3)
    assert config.embedding_model is None
    assert RagasConfig(metrics=("answer_relevancy",)).embedding_model
    with pytest.raises(ValueError):
        RagasConfig(concurrency=0)
    with pytest.raises(ValueError):
        RagasConfig(limit=0)


def _local_report():
    return {
        "config": {"top_k": 5},
        "summary": {
            "precision": 0.2,
            "recall": 0.3,
            "mrr": 0.8,
            "faithfulness": 0.5,
            "context_relevance": 0.4,
            "answer_relevance": 0.6,
            "valid_counts": {"faithfulness": 1},
        },
        "cases": [
            {
                "id": "a",
                "question": "q",
                "faithfulness": None,
                "context_relevance": None,
                "answer_relevance": None,
            },
            {
                "id": "b",
                "question": "q2",
                "faithfulness": None,
                "context_relevance": None,
                "answer_relevance": None,
            },
        ],
    }


def test_headline_prefers_ragas_and_keeps_zero_scores():
    headline, sources = build_headline(
        {"precision": 0.2, "recall": 0.3, "mrr": 0.8},
        {
            "id_based_context_precision": 0.0,
            "id_based_context_recall": 0.4,
            "faithfulness": 0.9,
        },
    )
    assert headline["precision"] == 0.0
    assert sources["precision"] == "ragas"
    assert headline["recall"] == 0.4
    assert headline["mrr"] == 0.8 and sources["mrr"] == "local"
    assert headline["faithfulness"] == 0.9


def test_merge_fills_legacy_judge_fields_and_summary():
    report = _local_report()
    block = {
        "config": {"model": "qwen3.8-flash", "metrics": ["faithfulness"]},
        "summary": {
            "id_based_context_precision": 0.1,
            "id_based_context_recall": 0.2,
            "faithfulness": 0.75,
            "context_relevance": 0.25,
            "answer_relevancy": 0.5,
            "failure_counts": {"METRIC_FAILED": 1},
        },
        "failure_counts": {"METRIC_FAILED": 1},
        "cases": [
            {
                "id": "a",
                "scores": {"faithfulness": 0.5, "context_relevance": 0.25,
                           "answer_relevancy": 0.5},
                "errors": {},
                "skipped": {},
            },
            {
                "id": "b",
                "scores": {"faithfulness": 1.0},
                "errors": {"context_recall": "METRIC_FAILED"},
                "skipped": {},
            },
        ],
    }
    merged = merge_ragas_report(report, block, model="qwen3.8-flash")
    assert merged["ragas"] is block
    assert merged["config"]["ragas"] is block["config"]
    assert merged["summary"]["headline"]["precision"] == 0.1
    assert merged["summary"]["metric_sources"]["mrr"] == "local"
    assert merged["summary"]["ragas"]["faithfulness"] == 0.75
    assert merged["cases"][0]["faithfulness"]["score"] == 0.5
    assert merged["cases"][0]["faithfulness"]["source"] == "ragas"
    assert merged["cases"][0]["context_relevance"]["items"] == []
    assert merged["cases"][1]["context_relevance"] is None
    assert merged["summary"]["faithfulness"] == pytest.approx(0.75)
    assert merged["summary"]["valid_counts"]["faithfulness"] == 2
    assert merged["summary"]["ragas_metric_failures"] == 1


def test_merge_can_keep_local_judge_fields():
    report = _local_report()
    report["cases"][0]["faithfulness"] = {"score": 0.5, "reason": "local", "model": "local"}
    block = {
        "config": {"model": "qwen3.8-flash"},
        "summary": {"faithfulness": 0.9},
        "cases": [
            {"id": "a", "scores": {"faithfulness": 0.9}, "errors": {}, "skipped": {}},
            {"id": "b", "scores": {}, "errors": {}, "skipped": {}},
        ],
    }
    merge_ragas_report(report, block, overwrite_judge_fields=False)
    assert report["cases"][0]["faithfulness"]["reason"] == "local"
    assert report["summary"]["headline"]["faithfulness"] == 0.9
    assert report["cases"][0]["ragas"]["scores"]["faithfulness"] == 0.9


def test_merge_rejects_unknown_block_ids():
    report = _local_report()
    block = {"cases": [{"id": "unknown", "scores": {}, "errors": {}, "skipped": {}}]}
    with pytest.raises(ValueError, match="报告之外"):
        merge_ragas_report(report, block)
