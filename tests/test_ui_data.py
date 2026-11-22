"""Read-only artifact access: conversations, jobs, reports and health probing."""

import json
import os
import sqlite3

import pytest

from medical_rag.ui import data


def _build_history(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                legacy TEXT NOT NULL DEFAULT '[]',
                failed_runs TEXT NOT NULL DEFAULT '[]', updated INTEGER NOT NULL);
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL, question TEXT NOT NULL,
                answer TEXT, failure TEXT);
            CREATE TABLE document_jobs (
                job_order INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL, source_id TEXT NOT NULL, upload_path TEXT NOT NULL,
                snapshot TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, dispatch_seq INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0, token TEXT,
                result TEXT, error TEXT, trace TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE worker_heartbeats (
                role TEXT PRIMARY KEY, updated_at REAL NOT NULL, broker TEXT NOT NULL);
        """)
        connection.execute(
            "INSERT INTO conversations(id,title,legacy,failed_runs,updated) VALUES (?,?,?,?,?)",
            ("conv-new", "糖尿病饮食", json.dumps([
                {"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧回答", "citations": [{"chunk_id": "c-1"}]},
            ], ensure_ascii=False), "[]", 2_000),
        )
        connection.execute(
            "INSERT INTO conversations(id,title,legacy,failed_runs,updated) VALUES (?,?,?,?,?)",
            ("conv-old", "高血压", "[]", json.dumps([{"runId": "r-failed"}]), 1_000),
        )
        connection.execute(
            "INSERT INTO turns(conversation_id,question,answer,failure) VALUES (?,?,?,?)",
            ("conv-new", "怎么吃？", json.dumps({
                "role": "assistant", "content": "控制总热量。",
                "citations": [{"chunk_id": "c-2", "title": "指南"}],
                "abstained": False, "runId": "run-1", "answerType": "answer",
            }, ensure_ascii=False), None),
        )
        connection.execute(
            "INSERT INTO turns(conversation_id,question,answer,failure) VALUES (?,?,?,?)",
            ("conv-new", "失败问题", None,
             json.dumps({"runId": "run-2", "trace": []}, ensure_ascii=False)),
        )
        connection.execute(
            "INSERT INTO document_jobs(job_id,filename,source_id,upload_path,snapshot,status,"
            "stage,error,available_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("job-1", "指南.md", "src-1", "x", "{}", "failed", "embed",
             "INVALID_DOCUMENT", 0.0, 10.0, 11.0),
        )
        connection.execute(
            "INSERT INTO worker_heartbeats(role,updated_at,broker) VALUES (?,?,?)",
            ("ingest", 12.0, "ok"),
        )
        connection.commit()


def test_conversation_index_orders_by_update_and_counts(tmp_path):
    path = tmp_path / "chat-history.sqlite3"
    _build_history(path)
    index = data.load_conversation_index(path)
    assert [row.conversation_id for row in index] == ["conv-new", "conv-old"]
    assert index[0].turn_count == 2
    assert index[0].answered_count == 1
    assert index[0].last_question == "失败问题"
    assert index[1].last_question == ""
    assert data.format_timestamp(2_000) != "—"


def test_conversation_merges_legacy_messages_turns_and_failures(tmp_path):
    path = tmp_path / "chat-history.sqlite3"
    _build_history(path)
    conversation = data.load_conversation(path, "conv-new")
    assert conversation is not None
    assert conversation.title == "糖尿病饮食"
    assert [message.role for message in conversation.messages] == [
        "user", "assistant", "user", "assistant", "user"]
    assert conversation.messages[1].citations[0]["chunk_id"] == "c-1"
    assert conversation.messages[3].content == "控制总热量。"
    assert conversation.messages[3].citations[0]["title"] == "指南"
    assert conversation.messages[3].run_id == "run-1"
    assert len(conversation.failed_runs) == 1
    assert conversation.failed_runs[0]["runId"] == "run-2"


def test_conversation_reads_are_read_only_and_missing_files_stay_absent(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    assert data.load_conversation_index(missing) == []
    assert data.load_conversation(missing, "any") is None
    assert data.load_document_jobs(missing) == []
    assert data.load_worker_heartbeats(missing) == []
    overview = data.load_database_overview(missing)
    assert overview.exists is False
    assert overview.conversation_count == 0
    assert not missing.exists(), "只读访问不得创建数据库文件"


def test_overview_jobs_and_heartbeats_are_reported(tmp_path):
    path = tmp_path / "chat-history.sqlite3"
    _build_history(path)
    overview = data.load_database_overview(path)
    assert overview.exists is True
    assert overview.conversation_count == 2
    assert overview.turn_count == 2
    assert overview.answered_count == 1
    assert overview.document_job_count == 1
    assert overview.pending_job_count == 0

    jobs = data.load_document_jobs(path)
    assert jobs == [{"job_id": "job-1", "filename": "指南.md", "source_id": "src-1",
                     "status": "failed", "stage": "embed", "attempts": 0,
                     "error": "INVALID_DOCUMENT", "created_at": 10.0, "updated_at": 11.0}]
    assert data.load_worker_heartbeats(path) == [
        {"role": "ingest", "updated_at": 12.0, "broker": "ok"}]


def test_malformed_stored_answers_are_skipped(tmp_path):
    path = tmp_path / "chat-history.sqlite3"
    _build_history(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO turns(conversation_id,question,answer,failure) VALUES (?,?,?,?)",
            ("conv-new", "坏数据", "{not-json", None),
        )
        connection.commit()
    conversation = data.load_conversation(path, "conv-new")
    assert conversation is not None
    assert conversation.messages[-1].role == "user"
    assert conversation.messages[-1].content == "坏数据"


def test_report_scan_parses_only_real_reports_newest_first(tmp_path):
    report_dir = tmp_path / "reports"
    nested = report_dir / "nested"
    nested.mkdir(parents=True)
    older = report_dir / "old.json"
    newer = nested / "new.json"
    not_a_report = report_dir / "notes.json"
    broken = report_dir / "broken.json"
    older.write_text(json.dumps({"summary": {"precision": 0.1}, "cases": []}), encoding="utf-8")
    newer.write_text(json.dumps({
        "dataset_version": "v2", "summary": {"recall": 0.8, "abstention_rate": 0.1},
        "config": {"answer_provider": "qwen", "judge_model": "qwen-max"},
        "cases": [{"id": "q1", "question": "问题", "abstained": False}],
    }, ensure_ascii=False), encoding="utf-8")
    not_a_report.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    broken.write_text("{oops", encoding="utf-8")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))

    reports = data.scan_reports(str(report_dir))
    assert [report.path.name for report in reports] == ["new.json", "old.json"]
    assert reports[0].dataset_version == "v2"
    assert reports[0].case_count == 1
    assert reports[0].answer_provider == "qwen"
    assert reports[0].judge_model == "qwen-max"


def test_read_report_validates_and_report_rows_flatten(tmp_path):
    valid = tmp_path / "report.json"
    valid.write_text(json.dumps({
        "summary": {"precision": 0.5, "abstention_rate": 0.25, "judge_failures": 2,
                    "nan_like": True},
        "cases": [
            {"id": "q1", "question": "问题", "category": "diet",
             "answerability": "answerable", "abstained": False,
             "retrieval": {"recall": 0.5, "ndcg": 0.4, "rr": 1.0},
             "answer": {"answer": "答案"}},
            "not-a-dict",
        ],
    }, ensure_ascii=False), encoding="utf-8")
    report = data.read_report(valid)
    metrics = data.report_metrics(report)
    assert ("precision", "检索 Precision", 0.5) in metrics
    assert all(key != "nan_like" for key, _, _ in metrics)
    rows = data.report_case_rows(report)
    assert len(rows) == 1
    assert rows[0]["题目 ID"] == "q1"
    assert rows[0]["Recall"] == 0.5

    broken = tmp_path / "broken.json"
    broken.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError):
        data.read_report(broken)
    with pytest.raises(ValueError):
        data.read_report(tmp_path / "missing.json")
    with pytest.raises(ValueError):
        data.read_report(broken.parent)


def test_report_metrics_detects_rates_and_missing_values(tmp_path):
    assert data.report_metrics({"summary": {"abstention_rate": 0.125}}) == [
        ("abstention_rate", "拒答率", 0.125)]
    assert data.report_metrics({"summary": {"precision": True}}) == []
    assert data.report_metrics({}) == []
    assert data.report_metrics({"summary": "not-a-dict"}) == []


def test_parse_report_dirs_splits_dedupes_and_resolves(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    parsed = data.parse_report_dirs(f"{first}, {second};{first}")
    assert parsed == [first, second]
    relative = data.parse_report_dirs("outputs/reports")
    assert relative == [data.PROJECT_ROOT / "outputs" / "reports"]


def test_health_probe_rejects_non_loopback_without_network():
    result = data.probe_backend_health("http://example.com:8001")
    assert result["reachable"] is False
    assert "回环" in result["error"]
    result = data.probe_backend_health("http://127.0.0.1:1", timeout=0.2)
    assert result["reachable"] is False


def test_format_helpers():
    assert data.format_bytes(0) == "0 B"
    assert data.format_bytes(1536) == "1.5 KiB"
    assert data.format_bytes(2 * 1024 * 1024) == "2.0 MiB"
    assert data.format_timestamp(None) == "—"
