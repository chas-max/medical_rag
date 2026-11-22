"""问答界面的延迟/吞吐/多版本指标统计。

每次问答请求可以往共享界面状态库（``UI_STATE_FILE``）的 ``ui_query_metrics`` 表
追加一行，记录耗时、首事件耗时、状态、引用数、节点 trace 等。这里再把明细行汇总成
延迟百分位、吞吐分桶、节点耗时和错误码分布，供界面的指标页展示。

本模块只使用标准库：Streamlit 进程可能没有安装 pandas 或整套 RAG 依赖，评测报告的
多版本聚合也必须能在最小依赖下被测试。这里的指标是本地演示用的轻量遥测，不是生产
监控后端；数据与界面状态共用同一个 SQLite 文件，由操作员调用 :meth:`prune` 清理。
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .data import REPORT_METRICS, ReportFile
from .storage import UiSqliteStore

# 与 state.py 保持一致的标识长度上限，避免界面把超长文本写进状态库。
MAX_ID_CHARS = 128
MAX_STATUS_CHARS = 64
MAX_ERROR_CODE_CHARS = 128
MAX_TRACE_BYTES = 256 * 1024
SUCCESS_STATUS = "success"
MIN_COVERAGE_SECONDS = 60.0
UNKNOWN_ERROR_CODE = "UNKNOWN"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ui_query_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    conversation_id TEXT,
    request_id TEXT,
    created_at REAL NOT NULL,
    latency_ms REAL NOT NULL,
    first_event_ms REAL,
    status TEXT NOT NULL,
    abstained INTEGER,
    answer_chars INTEGER NOT NULL DEFAULT 0,
    citation_count INTEGER NOT NULL DEFAULT 0,
    stream INTEGER NOT NULL DEFAULT 1,
    question_chars INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    trace_json TEXT
);
CREATE INDEX IF NOT EXISTS ui_query_metrics_tenant_created
    ON ui_query_metrics(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS ui_query_metrics_created
    ON ui_query_metrics(created_at);
"""


# -- 纯函数：百分位与分桶 --------------------------------------------------
def percentile(values: Iterable[float], p: float) -> float | None:
    """返回 ``values`` 的线性插值百分位；空序列返回 ``None``。

    ``p`` 取 0..100，允许小数。``bool`` 会被忽略（它在 Python 里是 ``int``，
    但不是耗时数值），非数值输入抛 ``ValueError``。
    """
    if isinstance(p, bool) or not isinstance(p, (int, float)):
        raise ValueError("百分位 p 必须是数值")
    rank = float(p)
    if not 0.0 <= rank <= 100.0:
        raise ValueError("百分位 p 必须在 0 到 100 之间")
    numbers: list[float] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("百分位输入必须是数值") from error
        if not math.isfinite(number):
            raise ValueError("百分位输入必须是有限数值")
        numbers.append(number)
    if not numbers:
        return None
    numbers.sort()
    position = (len(numbers) - 1) * rank / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return numbers[lower]
    weight = position - lower
    return numbers[lower] + (numbers[upper] - numbers[lower]) * weight


def bucket_start(ts: float, bucket_seconds: float) -> float:
    """把时间戳对齐到 ``bucket_seconds`` 宽度的桶起点（向下取整）。"""
    moment = _finite_float(ts, "时间戳")
    seconds = _finite_float(bucket_seconds, "分桶秒数")
    if seconds <= 0:
        raise ValueError("分桶秒数必须大于 0")
    return math.floor(moment / seconds) * seconds


def _now() -> float:
    """当前时间；测试通过 monkeypatch 它来固定吞吐的覆盖跨度。"""
    return time.time()


# -- 参数校验 --------------------------------------------------------------
def _finite_float(value: object, name: str) -> float:
    """要求一个有限数值；``bool`` 不算数值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数值")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数值")
    return number


def _non_negative_float(value: object, name: str) -> float:
    number = _finite_float(value, name)
    if number < 0:
        raise ValueError(f"{name} 不能为负数")
    return number


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是非负整数")
    if value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


def _required_text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 不能为空")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{name} 超过 {limit} 字符上限")
    return text


def _optional_text(value: object, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是文本")
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        raise ValueError(f"{name} 超过 {limit} 字符上限")
    return text


def _optional_flag(value: object, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{name} 必须是布尔值或空值")
    return int(value)


def _encode_trace(trace: object) -> str | None:
    """把 ``list[dict]`` 形式的节点 trace 序列化为 JSON 文本。"""
    if trace is None:
        return None
    if not isinstance(trace, list):
        raise ValueError("trace 必须是字典列表")
    for item in trace:
        if not isinstance(item, dict):
            raise ValueError("trace 必须是字典列表")
    try:
        payload = json.dumps(trace, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("trace 必须是可序列化的 JSON 数据") from error
    if len(payload.encode("utf-8")) > MAX_TRACE_BYTES:
        raise ValueError(f"trace 超过 {MAX_TRACE_BYTES} 字节上限")
    return payload


def _decode_trace(raw: object) -> list[dict[str, Any]]:
    """解析库里的 ``trace_json``；缺失或损坏时退化为空列表。"""
    if not isinstance(raw, str) or not raw:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _filter_conditions(
    tenant_id: str | None, since: float | None
) -> tuple[list[str], list[object]]:
    """构造可复用的 ``AND`` 条件列表与参数列表。"""
    clauses: list[str] = []
    params: list[object] = []
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(_required_text(tenant_id, "租户 ID", MAX_ID_CHARS))
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(_finite_float(since, "起始时间"))
    return clauses, params


def _filter_clause(
    tenant_id: str | None, since: float | None
) -> tuple[str, list[object]]:
    """构造可复用的 ``WHERE`` 片段（含前导空格），空过滤时返回空串。"""
    clauses, params = _filter_conditions(tenant_id, since)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _row_to_record(row: Any) -> dict[str, Any]:
    abstained = row["abstained"]
    return {
        "id": int(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "conversation_id": row["conversation_id"],
        "request_id": row["request_id"],
        "created_at": float(row["created_at"]),
        "latency_ms": float(row["latency_ms"]),
        "first_event_ms": None if row["first_event_ms"] is None else float(row["first_event_ms"]),
        "status": str(row["status"]),
        "abstained": None if abstained is None else bool(abstained),
        "answer_chars": int(row["answer_chars"]),
        "citation_count": int(row["citation_count"]),
        "stream": bool(row["stream"]),
        "question_chars": int(row["question_chars"]),
        "error_code": row["error_code"],
        "trace": _decode_trace(row["trace_json"]),
    }


def _positive_bucket(bucket_seconds: float) -> float:
    seconds = _finite_float(bucket_seconds, "分桶秒数")
    if seconds <= 0:
        raise ValueError("分桶秒数必须大于 0")
    return seconds


# -- 明细存储 --------------------------------------------------------------
class QueryMetricsStore(UiSqliteStore):
    """问答遥测明细表，以及界面指标页需要的聚合读取接口。"""

    SCHEMA = SCHEMA

    def record(
        self,
        tenant_id: str,
        latency_ms: float,
        *,
        conversation_id: str | None = None,
        request_id: str | None = None,
        status: str = SUCCESS_STATUS,
        abstained: bool | None = None,
        answer_chars: int = 0,
        citation_count: int = 0,
        stream: bool = True,
        question_chars: int = 0,
        error_code: str | None = None,
        first_event_ms: float | None = None,
        trace: list[dict[str, Any]] | None = None,
        created_at: float | None = None,
    ) -> int:
        """追加一条问答指标，返回自增主键。

        ``latency_ms`` 必须是非负有限数值；``trace`` 是节点 trace 列表，
        每项可含 ``node_id`` / ``duration_ms`` / ``status``。非法参数抛
        ``ValueError``，数据库读写失败由基类抛 ``UiStoreError``。
        """
        tenant = _required_text(tenant_id, "租户 ID", MAX_ID_CHARS)
        latency = _non_negative_float(latency_ms, "耗时 latency_ms")
        first_event = (
            None if first_event_ms is None else _non_negative_float(first_event_ms, "首事件耗时")
        )
        state = _required_text(status, "状态 status", MAX_STATUS_CHARS)
        if not isinstance(stream, bool):
            raise ValueError("stream 必须是布尔值")
        timestamp = _now() if created_at is None else _finite_float(created_at, "记录时间")
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO ui_query_metrics("
                "tenant_id, conversation_id, request_id, created_at, latency_ms, "
                "first_event_ms, status, abstained, answer_chars, citation_count, "
                "stream, question_chars, error_code, trace_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant,
                    _optional_text(conversation_id, "会话 ID", MAX_ID_CHARS),
                    _optional_text(request_id, "请求 ID", MAX_ID_CHARS),
                    timestamp,
                    latency,
                    first_event,
                    state,
                    _optional_flag(abstained, "abstained"),
                    _non_negative_int(answer_chars, "answer_chars"),
                    _non_negative_int(citation_count, "citation_count"),
                    int(stream),
                    _non_negative_int(question_chars, "question_chars"),
                    _optional_text(error_code, "错误码 error_code", MAX_ERROR_CODE_CHARS),
                    _encode_trace(trace),
                ),
            )
            connection.commit()
        return int(cursor.lastrowid or 0)

    def recent(
        self,
        tenant_id: str | None = None,
        since: float | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """最近的指标明细（``created_at`` 倒序，同秒按 id 倒序）。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        where, params = _filter_clause(tenant_id, since)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ui_query_metrics" + where
                + " ORDER BY created_at DESC, id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    # -- 聚合读取 ----------------------------------------------------------
    def summary(
        self, tenant_id: str | None = None, since: float | None = None
    ) -> dict[str, Any]:
        """整体统计：计数、成功率、延迟百分位、覆盖率吞吐。

        无数据时计数为 0、``success_rate`` 与各类延迟为 ``None``、
        ``throughput_per_minute`` 为 0。给了 ``since`` 时覆盖跨度取
        ``since`` 到现在，否则取首尾记录的差值，两者都不少于 1 分钟。
        """
        where, params = _filter_clause(tenant_id, since)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT created_at, latency_ms, first_event_ms, status FROM ui_query_metrics"
                + where,
                params,
            ).fetchall()
        latencies = [float(row["latency_ms"]) for row in rows]
        first_events = [
            float(row["first_event_ms"]) for row in rows if row["first_event_ms"] is not None
        ]
        count = len(rows)
        success_count = sum(1 for row in rows if row["status"] == SUCCESS_STATUS)
        first_at = min((float(row["created_at"]) for row in rows), default=None)
        last_at = max((float(row["created_at"]) for row in rows), default=None)
        throughput = 0.0
        if count:
            if since is not None:
                coverage = _now() - _finite_float(since, "起始时间")
            else:
                coverage = float(last_at or 0.0) - float(first_at or 0.0)
            coverage = max(coverage, MIN_COVERAGE_SECONDS)
            throughput = count / (coverage / 60.0)
        return {
            "count": count,
            "success_count": success_count,
            "failure_count": count - success_count,
            "success_rate": None if not count else success_count / count,
            "avg_latency_ms": None if not latencies else sum(latencies) / len(latencies),
            "p50_latency_ms": percentile(latencies, 50),
            "p95_latency_ms": percentile(latencies, 95),
            "min_latency_ms": None if not latencies else min(latencies),
            "max_latency_ms": None if not latencies else max(latencies),
            "avg_first_event_ms": None
            if not first_events
            else sum(first_events) / len(first_events),
            "throughput_per_minute": throughput,
            "first_at": first_at,
            "last_at": last_at,
        }

    def latency_series(
        self,
        tenant_id: str | None = None,
        since: float | None = None,
        bucket_seconds: float = 60,
    ) -> list[dict[str, Any]]:
        """按时间桶统计延迟（升序）；每桶含 count / p50 / p95 / avg。"""
        seconds = _positive_bucket(bucket_seconds)
        where, params = _filter_clause(tenant_id, since)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT created_at, latency_ms FROM ui_query_metrics" + where,
                params,
            ).fetchall()
        buckets: dict[float, list[float]] = {}
        for row in rows:
            start = bucket_start(float(row["created_at"]), seconds)
            buckets.setdefault(start, []).append(float(row["latency_ms"]))
        return [
            {
                "bucket_start": start,
                "count": len(values),
                "p50_ms": percentile(values, 50),
                "p95_ms": percentile(values, 95),
                "avg_ms": sum(values) / len(values),
            }
            for start, values in sorted(buckets.items())
        ]

    def throughput_series(
        self,
        tenant_id: str | None = None,
        since: float | None = None,
        bucket_seconds: float = 60,
    ) -> list[dict[str, Any]]:
        """按时间桶统计请求量（升序）；每桶含 count / success / failure。"""
        seconds = _positive_bucket(bucket_seconds)
        where, params = _filter_clause(tenant_id, since)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT created_at, status FROM ui_query_metrics" + where,
                params,
            ).fetchall()
        buckets: dict[float, dict[str, int]] = {}
        for row in rows:
            start = bucket_start(float(row["created_at"]), seconds)
            bucket = buckets.setdefault(start, {"count": 0, "success": 0, "failure": 0})
            bucket["count"] += 1
            if row["status"] == SUCCESS_STATUS:
                bucket["success"] += 1
            else:
                bucket["failure"] += 1
        return [
            {"bucket_start": start, **buckets[start]}
            for start in sorted(buckets)
        ]

    def node_latency(
        self, tenant_id: str | None = None, since: float | None = None
    ) -> list[dict[str, Any]]:
        """从 trace 聚合每个节点的耗时，按 p95 从高到低排序。

        trace 项缺少合法 ``node_id`` 或非负 ``duration_ms`` 时跳过，坏 trace 不影响
        其它行的聚合。
        """
        where, params = _filter_clause(tenant_id, since)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT trace_json FROM ui_query_metrics" + where, params
            ).fetchall()
        durations: dict[str, list[float]] = {}
        for row in rows:
            for item in _decode_trace(row["trace_json"]):
                node_id = item.get("node_id")
                if not isinstance(node_id, str) or not node_id.strip():
                    continue
                duration = item.get("duration_ms")
                if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                    continue
                value = float(duration)
                if not math.isfinite(value) or value < 0:
                    continue
                durations.setdefault(node_id.strip(), []).append(value)
        result = [
            {
                "node_id": node_id,
                "runs": len(values),
                "p50_ms": percentile(values, 50),
                "p95_ms": percentile(values, 95),
                "avg_ms": sum(values) / len(values),
            }
            for node_id, values in durations.items()
        ]
        result.sort(key=lambda item: (-(item["p95_ms"] or 0.0), item["node_id"]))
        return result

    def error_counts(
        self, tenant_id: str | None = None, since: float | None = None
    ) -> list[dict[str, Any]]:
        """失败请求的错误码分布（次数倒序）；错误码为空/缺失归 ``UNKNOWN``。

        成功请求不参与错误分布，否则每个成功请求都会被误记成 ``UNKNOWN``。
        """
        where, params = _filter_clause(tenant_id, since)
        condition = "status <> ?"
        if where:
            condition = f"{condition} AND{where.replace(' WHERE ', ' ', 1)}"
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT COALESCE(NULLIF(TRIM(error_code), ''), ?) AS code, "
                "COUNT(*) AS total FROM ui_query_metrics WHERE "
                + condition
                + " GROUP BY code ORDER BY total DESC, code ASC",
                [UNKNOWN_ERROR_CODE, SUCCESS_STATUS, *params],
            ).fetchall()
        return [
            {"error_code": str(row["code"]), "count": int(row["total"])}
            for row in rows
        ]

    def prune(self, before: float) -> int:
        """删除 ``created_at < before`` 的明细行，返回删除行数。"""
        cutoff = _finite_float(before, "删除截止时间")
        with self.connect() as connection:
            deleted = connection.execute(
                "DELETE FROM ui_query_metrics WHERE created_at < ?", (cutoff,)
            ).rowcount
            connection.commit()
        return int(deleted)


# -- 评测报告多版本聚合 ----------------------------------------------------
def _report_number(summary: Mapping[str, Any], key: str) -> float | None:
    """读取一个数值型指标；缺失、``bool`` 或非数值一律视为缺失。"""
    value = summary.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _report_label(report: ReportFile) -> str:
    """优先用数据集版本做显示名，否则退回报告文件名。"""
    version = report.dataset_version
    if isinstance(version, str) and version.strip():
        return version.strip()
    stem = Path(report.path).stem
    return stem or str(report.path)


def aggregate_report_metrics(
    reports: list[ReportFile],
    metrics: list[str] | None = None,
) -> list[dict[str, Any]]:
    """把多份评测报告排成一行一份的对比表（按修改时间倒序）。

    每行固定包含 ``label`` / ``path`` / ``dataset_version`` / ``modified`` /
    ``case_count`` / ``answer_provider`` / ``judge_model``，以及 ``metrics`` 里
    每个 key 对应的数值（该报告没有时为 ``None``）。默认指标取
    :data:`~medical_rag.ui.data.REPORT_METRICS` 的顺序。
    """
    if metrics is None:
        keys = [key for key, _ in REPORT_METRICS]
    else:
        keys = list(metrics)
        if any(not isinstance(key, str) for key in keys):
            raise ValueError("指标键必须是字符串列表")
    rows: list[dict[str, Any]] = []
    for report in sorted(reports, key=lambda item: item.modified, reverse=True):
        row: dict[str, Any] = {
            "label": _report_label(report),
            "path": str(report.path),
            "dataset_version": report.dataset_version,
            "modified": report.modified,
            "case_count": report.case_count,
            "answer_provider": report.answer_provider,
            "judge_model": report.judge_model,
        }
        for key in keys:
            row[key] = _report_number(report.summary, key)
        rows.append(row)
    return rows


def available_report_metrics(reports: list[ReportFile]) -> list[tuple[str, str]]:
    """只返回至少一份报告有数值的 ``(key, label)``，保持 ``REPORT_METRICS`` 顺序。"""
    available: list[tuple[str, str]] = []
    for key, label in REPORT_METRICS:
        if any(_report_number(report.summary, key) is not None for report in reports):
            available.append((key, label))
    return available
