"""Metrics store and report aggregation for the statistics page."""

import json

import pytest

from medical_rag.ui import stats
from medical_rag.ui.data import ReportFile
from medical_rag.ui.stats import QueryMetricsStore, aggregate_report_metrics, available_report_metrics


@pytest.fixture
def store(tmp_path):
    return QueryMetricsStore(tmp_path / "ui-state.sqlite3")


def test_percentile_and_bucket_start():
    assert stats.percentile([], 50) is None
    assert stats.percentile([1.0], 95) == 1.0
    assert stats.percentile([0.0, 10.0], 50) == 5.0
    assert stats.percentile([0.0, 10.0], 0) == 0.0
    assert stats.percentile([0.0, 10.0], 100) == 10.0
    assert stats.percentile([True, 2.0], 50) == 2.0
    with pytest.raises(ValueError):
        stats.percentile([1.0], 101)
    with pytest.raises(ValueError):
        stats.percentile([1.0], True)
    with pytest.raises(ValueError):
        stats.percentile([float("nan")], 50)
    assert stats.bucket_start(125.9, 60) == 120
    with pytest.raises(ValueError):
        stats.bucket_start(100, 0)


def test_record_and_recent_roundtrip(store):
    trace = [{"node_id": "retrieval.dense", "duration_ms": 12.5, "status": "success"}]
    first = store.record("tenant-a", 250.5, conversation_id="c1", request_id="r1",
                         status="success", abstained=False, answer_chars=42,
                         citation_count=3, stream=True, question_chars=10,
                         first_event_ms=80.0, trace=trace, created_at=1_000.0)
    second = store.record("tenant-a", 100.0, status="failed", error_code="BACKEND_TIMEOUT",
                          created_at=1_010.0)
    assert first > 0 and second > first
    recent = store.recent("tenant-a")
    assert [row["latency_ms"] for row in recent] == [100.0, 250.5]
    newest, oldest = recent[0], recent[1]
    assert newest["status"] == "failed"
    assert newest["error_code"] == "BACKEND_TIMEOUT"
    assert oldest["trace"] == trace
    assert oldest["abstained"] is False
    assert oldest["answer_chars"] == 42
    assert oldest["citation_count"] == 3


def test_record_validation(store):
    with pytest.raises(ValueError):
        store.record("", 1.0)
    with pytest.raises(ValueError):
        store.record("tenant-a", -1.0)
    with pytest.raises(ValueError):
        store.record("tenant-a", 1.0, stream=1)
    with pytest.raises(ValueError):
        store.record("tenant-a", 1.0, abstained="yes")
    with pytest.raises(ValueError):
        store.record("tenant-a", 1.0, answer_chars=-1)
    with pytest.raises(ValueError):
        store.record("tenant-a", 1.0, trace=["not-a-dict"])
    with pytest.raises(ValueError):
        store.record("tenant-a", 1.0, status="")


def test_trace_corruption_degrades_to_empty(store):
    store.record("tenant-a", 10.0, created_at=1.0)
    with store.connect() as connection:
        connection.execute("UPDATE ui_query_metrics SET trace_json = '{broken'")
        connection.commit()
    assert store.recent("tenant-a")[0]["trace"] == []
    assert store.node_latency("tenant-a") == []


def test_summary_percentiles_success_and_throughput(store, monkeypatch):
    monkeypatch.setattr(stats, "_now", lambda: 1_060.0)
    for index, latency in enumerate([100.0, 200.0, 300.0, 400.0]):
        store.record("tenant-a", latency, status="success", created_at=float(index * 10))
    store.record("tenant-a", 500.0, status="failed", error_code="MODEL_RATE_LIMITED",
                 created_at=40.0)
    store.record("tenant-b", 999.0, status="success", created_at=40.0)
    summary = store.summary("tenant-a")
    assert summary["count"] == 5
    assert summary["success_count"] == 4
    assert summary["failure_count"] == 1
    assert summary["success_rate"] == pytest.approx(0.8)
    assert summary["avg_latency_ms"] == pytest.approx(300.0)
    assert summary["p50_latency_ms"] == pytest.approx(300.0)
    assert summary["p95_latency_ms"] > 400.0
    assert summary["min_latency_ms"] == 100.0
    assert summary["max_latency_ms"] == 500.0
    assert summary["throughput_per_minute"] > 0
    assert store.summary("tenant-missing")["count"] == 0
    assert store.summary("tenant-missing")["success_rate"] is None
    assert store.summary("tenant-missing")["throughput_per_minute"] == 0


def test_summary_since_uses_now_for_coverage(store, monkeypatch):
    monkeypatch.setattr(stats, "_now", lambda: 1_000.0)
    store.record("tenant-a", 100.0, created_at=700.0)
    store.record("tenant-a", 200.0, created_at=900.0)
    summary = store.summary("tenant-a", since=400.0)
    # 覆盖跨度为 since..now = 600 秒 = 10 分钟，吞吐 = 2/10。
    assert summary["count"] == 2
    assert summary["throughput_per_minute"] == pytest.approx(0.2)
    assert summary["first_at"] == 700.0
    assert store.summary("tenant-a", since=950.0)["count"] == 0


def test_latency_and_throughput_series_are_bucketed_and_sorted(store):
    store.record("tenant-a", 100.0, status="success", created_at=121.0)
    store.record("tenant-a", 300.0, status="success", created_at=150.0)
    store.record("tenant-a", 500.0, status="failed", created_at=185.0)
    store.record("tenant-a", 700.0, status="success", created_at=245.0)
    latency = store.latency_series("tenant-a", bucket_seconds=60)
    assert [row["bucket_start"] for row in latency] == [120.0, 180.0, 240.0]
    assert [row["count"] for row in latency] == [2, 1, 1]
    assert latency[0]["p50_ms"] == pytest.approx(200.0)
    throughput = store.throughput_series("tenant-a", bucket_seconds=60)
    assert [row["count"] for row in throughput] == [2, 1, 1]
    assert [row["success"] for row in throughput] == [2, 0, 1]
    assert [row["failure"] for row in throughput] == [0, 1, 0]
    assert store.latency_series("tenant-missing") == []
    with pytest.raises(ValueError):
        store.latency_series("tenant-a", bucket_seconds=0)


def test_node_latency_aggregates_trace_and_skips_bad_entries(store):
    store.record("tenant-a", 100.0, trace=[
        {"node_id": "retrieval.dense", "duration_ms": 10.0},
        {"node_id": "generation.llm", "duration_ms": 100.0},
        {"node_id": "bad", "duration_ms": -5.0},
        {"duration_ms": 3.0},
    ])
    store.record("tenant-a", 200.0, trace=[
        {"node_id": "retrieval.dense", "duration_ms": 30.0},
        {"node_id": "generation.llm", "duration_ms": 300.0},
    ])
    nodes = store.node_latency("tenant-a")
    assert [row["node_id"] for row in nodes] == ["generation.llm", "retrieval.dense"]
    llm = nodes[0]
    assert llm["runs"] == 2
    assert llm["p50_ms"] == pytest.approx(200.0)
    assert llm["p95_ms"] == pytest.approx(290.0)


def test_error_counts_excludes_success_and_groups_unknown(store):
    store.record("tenant-a", 1.0, status="success")
    store.record("tenant-a", 1.0, status="failed", error_code="BACKEND_TIMEOUT")
    store.record("tenant-a", 1.0, status="failed", error_code="BACKEND_TIMEOUT")
    store.record("tenant-a", 1.0, status="failed")
    store.record("tenant-b", 1.0, status="failed", error_code="MODEL_RATE_LIMITED")
    counts = store.error_counts("tenant-a")
    assert counts == [{"error_code": "BACKEND_TIMEOUT", "count": 2},
                      {"error_code": "UNKNOWN", "count": 1}]
    assert store.error_counts("tenant-b") == [
        {"error_code": "MODEL_RATE_LIMITED", "count": 1}]


def test_prune_removes_only_old_rows(store):
    store.record("tenant-a", 1.0, created_at=100.0)
    store.record("tenant-a", 1.0, created_at=300.0)
    assert store.prune(200.0) == 1
    assert [row["created_at"] for row in store.recent("tenant-a")] == [300.0]
    with pytest.raises(ValueError):
        store.prune("yesterday")


def _report(tmp_path, name, summary, *, version=None, modified=1.0, case_count=10):
    path = tmp_path / name
    path.write_text(json.dumps({"summary": summary}), encoding="utf-8")
    return ReportFile(path=path, modified=modified, case_count=case_count,
                      dataset_version=version, answer_provider="qwen",
                      judge_model="qwen-max", summary=summary)


def test_aggregate_report_metrics_and_available_metrics(tmp_path):
    older = _report(tmp_path, "old.json", {"precision": 0.5, "recall": 0.4},
                    version="v1", modified=10.0)
    newer = _report(tmp_path, "new.json", {"precision": 0.7, "ndcg": 0.6},
                    version="v2", modified=20.0)
    rows = aggregate_report_metrics([older, newer], ["precision", "recall", "ndcg"])
    assert [row["label"] for row in rows] == ["v2", "v1"]
    assert rows[0]["precision"] == pytest.approx(0.7)
    assert rows[0]["recall"] is None
    assert rows[1]["ndcg"] is None
    assert rows[0]["answer_provider"] == "qwen"
    assert rows[0]["judge_model"] == "qwen-max"
    assert rows[1]["case_count"] == 10

    available = available_report_metrics([older, newer])
    available_keys = [key for key, _ in available]
    assert available_keys == ["precision", "recall", "ndcg"]
    assert ("precision", "检索 Precision") in available


def test_aggregate_report_metrics_defaults_labels_and_validates(tmp_path):
    report = _report(tmp_path, "report-x.json", {"precision": 0.1}, modified=1.0)
    row = aggregate_report_metrics([report])[0]
    assert row["label"] == "report-x"
    assert row["path"].endswith("report-x.json")
    assert row["precision"] == pytest.approx(0.1)
    with pytest.raises(ValueError):
        aggregate_report_metrics([report], [1])
    assert available_report_metrics([]) == []
