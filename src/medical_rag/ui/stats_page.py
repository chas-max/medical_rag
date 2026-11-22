"""数据统计页：多版本检索指标、问答延迟/吞吐与文档任务可视化。"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd
import streamlit as st

from ..config import Settings
from . import data, stats
from .api_client import BackendError, RagApiClient
from .stats import QueryMetricsStore

SINCE_OPTIONS: dict[str, float | None] = {
    "最近 1 小时": 3_600.0,
    "最近 24 小时": 86_400.0,
    "最近 7 天": 604_800.0,
    "全部": None,
}
JOB_STATUS_LABELS = {
    "queued": "排队中",
    "running": "执行中",
    "retrying": "重试中",
    "succeeded": "成功",
    "failed": "失败",
}


def _metric_display(key: str, value: float) -> str:
    if key.endswith("_rate"):
        return f"{value * 100:.1f}%"
    if key == "judge_failures":
        return str(int(value))
    return f"{value:.4f}"


def _delta_display(key: str, current: float, previous: float | None) -> str | None:
    if previous is None:
        return None
    if key.endswith("_rate"):
        return f"{(current - previous) * 100:+.1f}pp"
    return f"{current - previous:+.4f}"


def _render_reports(settings: Settings) -> None:
    reports = data.scan_reports(settings.ui_report_dirs)
    if not reports:
        st.info("报告目录中还没有可识别的评测报告 JSON。")
        st.code(
            "python -m medical_rag.evaluate --dataset data/evaluations/medical-health-v2/"
            "questions.jsonl --output outputs/reports/medical-health-v2.json",
            language=None,
        )
        st.caption(f"扫描目录：{settings.ui_report_dirs}（可用 UI_REPORT_DIRS 覆盖，逗号分隔）")
        return

    available = stats.available_report_metrics(reports)
    if not available:
        st.warning("评测报告存在，但没有可展示的数值型 summary 指标。")
        st.caption(f"已扫描 {len(reports)} 份报告。")
        return

    labels = {key: label for key, label in available}
    keys = [key for key, _ in available]
    selected_keys = st.multiselect(
        "对比指标", keys, default=keys[:4],
        format_func=lambda item: labels[item], key="stats-metric-select",
    )
    if not selected_keys:
        st.caption("请选择至少一个指标。")
        return

    report_labels: list[str] = []
    label_map: dict[str, Any] = {}
    for report in reports:
        label = (report.dataset_version.strip() if isinstance(report.dataset_version, str)
                 and report.dataset_version.strip() else report.path.stem)
        unique = label if label not in label_map else f"{label} · {data.format_timestamp(report.modified)}"
        report_labels.append(unique)
        label_map[unique] = report

    default_reports = report_labels[:6]
    chosen = st.multiselect("报告版本", report_labels, default=default_reports,
                            key="stats-report-select")
    if not chosen:
        st.caption("请选择至少一份报告。")
        return

    rows = stats.aggregate_report_metrics([label_map[item] for item in chosen], selected_keys)
    frame = pd.DataFrame(rows)
    chart_frame = frame.set_index("label")[selected_keys]
    chart_frame.columns = [labels[key] for key in selected_keys]
    st.bar_chart(chart_frame, height=340)

    newest = rows[0]
    previous = rows[1] if len(rows) > 1 else None
    cards = st.columns(min(4, len(selected_keys)))
    for index, key in enumerate(selected_keys[:4]):
        value = newest.get(key)
        if value is None:
            continue
        cards[index % len(cards)].metric(
            labels[key], _metric_display(key, value),
            delta=_delta_display(key, value, previous.get(key) if previous else None),
        )

    st.caption(
        f"最新报告：{newest['label']} · {data.format_timestamp(newest['modified'])} · "
        f"{newest['case_count']} 题 · 回答模型 {newest['answer_provider'] or '—'} · "
        f"裁判模型 {newest['judge_model'] or '—'}"
    )
    with st.expander("明细表格"):
        display = frame.copy()
        for key in selected_keys:
            display[labels[key]] = [
                _metric_display(key, item) if isinstance(item, (int, float)) else "—"
                for item in display[key]
            ]
        st.dataframe(
            display[["label", "dataset_version", "case_count", "modified", "answer_provider",
                     "judge_model"] + [labels[key] for key in selected_keys]],
            hide_index=True,
        )
    with st.expander("报告 summary 原文"):
        options = list(label_map)
        picked = st.selectbox("查看报告", options, key="stats-report-raw")
        st.json(label_map[picked].summary)


def _render_latency(metrics_store: QueryMetricsStore, tenant: dict[str, Any]) -> None:
    since_label = st.selectbox("时间范围", list(SINCE_OPTIONS), index=1,
                               key="stats-since")
    since = SINCE_OPTIONS[since_label]
    since_epoch = None if since is None else time.time() - since
    tenant_id = tenant["tenant_id"]
    summary = metrics_store.summary(tenant_id, since_epoch)
    if not summary["count"]:
        st.info("该时间范围内还没有问答指标。到「智能问答」页提问后，这里会显示延迟与吞吐。")
        return

    cards = st.columns(6)
    cards[0].metric("请求数", summary["count"])
    cards[1].metric("成功率", f"{summary['success_rate'] * 100:.1f}%"
                    if summary["success_rate"] is not None else "—")
    cards[2].metric("P50 延迟", f"{summary['p50_latency_ms']:.0f} ms"
                    if summary["p50_latency_ms"] is not None else "—")
    cards[3].metric("P95 延迟", f"{summary['p95_latency_ms']:.0f} ms"
                    if summary["p95_latency_ms"] is not None else "—")
    cards[4].metric("平均首事件", f"{summary['avg_first_event_ms']:.0f} ms"
                    if summary["avg_first_event_ms"] is not None else "—")
    cards[5].metric("吞吐（/分钟）", f"{summary['throughput_per_minute']:.2f}")

    bucket_seconds = 60.0
    if since is None:
        bucket_seconds = 3_600.0
    elif since >= 604_800:
        bucket_seconds = 3_600.0
    elif since >= 86_400:
        bucket_seconds = 900.0
    elif since >= 3_600:
        bucket_seconds = 300.0

    latency_series = metrics_store.latency_series(tenant_id, since_epoch,
                                                  bucket_seconds=bucket_seconds)
    if latency_series:
        latency_frame = pd.DataFrame(latency_series)
        latency_frame["时间"] = pd.to_datetime(latency_frame["bucket_start"], unit="s")
        st.markdown("**延迟分位趋势**")
        st.line_chart(
            latency_frame.set_index("时间")[["p50_ms", "p95_ms"]],
            height=300,
        )
    throughput_series = metrics_store.throughput_series(tenant_id, since_epoch,
                                                        bucket_seconds=bucket_seconds)
    if throughput_series:
        throughput_frame = pd.DataFrame(throughput_series)
        throughput_frame["时间"] = pd.to_datetime(throughput_frame["bucket_start"], unit="s")
        st.markdown("**吞吐（每桶请求数）**")
        st.bar_chart(throughput_frame.set_index("时间")[["count"]], height=240)

    node_rows = metrics_store.node_latency(tenant_id, since_epoch)
    if node_rows:
        st.markdown("**各节点耗时（P95）**")
        node_frame = pd.DataFrame(node_rows)[["node_id", "runs", "p50_ms", "p95_ms"]]
        node_frame = node_frame.sort_values("p95_ms", ascending=False)
        st.bar_chart(node_frame.set_index("node_id")[["p95_ms"]], height=280)
        with st.expander("节点耗时明细"):
            st.dataframe(node_frame, hide_index=True)

    error_rows = metrics_store.error_counts(tenant_id, since_epoch)
    if error_rows:
        st.markdown("**失败错误码分布**")
        error_frame = pd.DataFrame(error_rows)
        st.bar_chart(error_frame.set_index("error_code")[["count"]], height=220)
        with st.expander("错误码表格"):
            st.dataframe(error_frame, hide_index=True)

    with st.expander("最近请求明细"):
        recent = metrics_store.recent(tenant_id, since_epoch, limit=100)
        recent_frame = pd.DataFrame(recent)
        if not recent_frame.empty:
            for column in ("created_at", "first_event_ms"):
                if column in recent_frame:
                    recent_frame[column] = recent_frame[column].apply(
                        lambda value: data.format_timestamp(value)
                        if column == "created_at" and value is not None
                        else value)
            keep = [column for column in
                    ("created_at", "latency_ms", "first_event_ms", "status", "abstained",
                     "citation_count", "answer_chars", "error_code", "conversation_id")
                    if column in recent_frame]
            st.dataframe(recent_frame[keep], hide_index=True)


def _render_jobs(client: RagApiClient) -> None:
    try:
        jobs = client.document_jobs(limit=100)
    except BackendError as error:
        st.warning(f"无法读取文档任务：{error.message}")
        hint = ("请先启动后端与 worker：.\\start.ps1 -WithWorkers" if error.code == "BACKEND_UNREACHABLE"
                else None)
        if hint:
            st.caption(hint)
        return
    if not jobs:
        st.info("还没有文档入库任务。启动 worker 后上传 Markdown/TXT/PDF，任务会异步入库。")
        st.code(".\\start.ps1 -WithWorkers", language=None)
        return

    frame = pd.DataFrame(jobs)
    counts = frame["status"].value_counts()
    active = sum(count for status, count in counts.items()
                 if status not in {"succeeded", "failed"})
    cards = st.columns(4)
    cards[0].metric("任务总数", len(frame))
    cards[1].metric("运行中", int(active))
    cards[2].metric("成功", int(counts.get("succeeded", 0)))
    cards[3].metric("失败", int(counts.get("failed", 0)))

    status_frame = pd.DataFrame({
        "状态": [JOB_STATUS_LABELS.get(status, status) for status in counts.index],
        "数量": counts.values,
    }).set_index("状态")
    st.markdown("**任务状态分布**")
    st.bar_chart(status_frame, height=220)

    if "created_at" in frame:
        timeline = frame.copy()
        timeline["日期"] = pd.to_datetime(timeline["created_at"], unit="s").dt.date
        per_day = timeline.groupby("日期").size().reset_index(name="任务数")
        st.markdown("**每日入库任务量**")
        st.bar_chart(per_day.set_index("日期"), height=220)

    with st.expander("任务明细"):
        keep = [column for column in
                ("job_id", "filename", "source_id", "status", "stage", "attempts",
                 "created_at", "updated_at") if column in frame]
        display = frame[keep].copy()
        for column in ("created_at", "updated_at"):
            if column in display:
                display[column] = display[column].apply(data.format_timestamp)
        st.dataframe(display, hide_index=True)


def render_stats_page(*, settings: Settings, client: RagApiClient,
                      metrics_store: QueryMetricsStore, tenant: dict[str, Any]) -> None:
    st.markdown("#### 数据统计")
    st.caption(
        "检索质量来自评测报告（项目级共享）；延迟与吞吐来自本界面最近的真实问答请求"
        f"（租户 {tenant['display_name']}）。"
    )
    reports_tab, latency_tab, jobs_tab = st.tabs(["多版本检索指标", "问答延迟与吞吐", "任务与入库"])
    with reports_tab:
        _render_reports(settings)
    with latency_tab:
        _render_latency(metrics_store, tenant)
    with jobs_tab:
        _render_jobs(client)
