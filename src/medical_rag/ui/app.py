"""Streamlit viewer for persisted local demonstration artifacts.

Run it with ``python -m medical_rag.ui`` (or ``streamlit run app.py``). The
viewer is strictly read-only towards the chat database: the only file it writes
is its own UI-state SQLite database, which keeps the current page, the last
selected conversation/report and operator notes across restarts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow ``streamlit run src/medical_rag/ui/app.py`` without an editable install.
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402 - bootstrap must happen before importing the project

from medical_rag.config import Settings  # noqa: E402
from medical_rag.ui import data  # noqa: E402
from medical_rag.ui.state import (  # noqa: E402
    CONVERSATION_KEY,
    NOTE_KINDS,
    PAGE_KEY,
    REPORT_KEY,
    UiStateError,
    UiStateStore,
)

PAGES = ("概览", "会话历史", "文档任务", "评测报告", "界面笔记")
DEFAULT_PAGE = PAGES[0]
REFRESH_SECONDS = 15


# -- cached data access -----------------------------------------------------
def _settings() -> Settings:
    """Read settings per rerun: cheap, and ``.env``/process env stay authoritative."""
    return Settings()


def _state_store(state_file: str) -> UiStateStore:
    """Construct per rerun; the store itself is stateless beyond its path."""
    return UiStateStore(state_file)


def _db_signature(history_file: str) -> str:
    path = data.project_path(history_file)
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    return f"{stat.st_mtime_ns}:{stat.st_size}"


@st.cache_data(show_spinner=False, ttl=REFRESH_SECONDS)
def _cached_overview(history_file: str, signature: str) -> data.DatabaseOverview:
    return data.load_database_overview(history_file)


@st.cache_data(show_spinner=False, ttl=REFRESH_SECONDS)
def _cached_conversation_index(history_file: str, signature: str) -> list[data.ConversationIndex]:
    return data.load_conversation_index(history_file)


@st.cache_data(show_spinner=False, ttl=REFRESH_SECONDS)
def _cached_conversation(history_file: str, conversation_id: str,
                         signature: str) -> data.Conversation | None:
    return data.load_conversation(history_file, conversation_id)


@st.cache_data(show_spinner=False, ttl=REFRESH_SECONDS)
def _cached_jobs(history_file: str, signature: str) -> list[dict]:
    return data.load_document_jobs(history_file)


@st.cache_data(show_spinner=False, ttl=REFRESH_SECONDS)
def _cached_heartbeats(history_file: str, signature: str) -> list[dict]:
    return data.load_worker_heartbeats(history_file)


@st.cache_data(show_spinner=False, ttl=REFRESH_SECONDS)
def _cached_reports(report_dirs: str) -> list[data.ReportFile]:
    return data.scan_reports(report_dirs)


@st.cache_data(show_spinner=False, ttl=REFRESH_SECONDS)
def _cached_report(path: str) -> dict:
    return data.read_report(path)


@st.cache_data(show_spinner=False, ttl=REFRESH_SECONDS)
def _cached_health(base_url: str) -> dict:
    return data.probe_backend_health(base_url)


def _refresh() -> None:
    """Drop cached reads so an operator sees files written by another process."""
    st.cache_data.clear()
    st.rerun()


# -- persistent UI state ----------------------------------------------------
def _state_error(message: str) -> None:
    if not st.session_state.get("_ui_state_error_shown"):
        st.sidebar.error(message)
        st.session_state["_ui_state_error_shown"] = True


def _read_state(store: UiStateStore, key: str, default: object) -> object:
    try:
        return store.get(key, default)
    except UiStateError as error:
        _state_error(str(error))
        return default


def _write_state(store: UiStateStore, key: str, value: object) -> bool:
    try:
        store.set(key, value)
        return True
    except (UiStateError, ValueError) as error:
        _state_error(str(error))
        return False


def _render_sidebar(store: UiStateStore, settings: Settings,
                    history_file: str) -> str:
    st.sidebar.title("Medical RAG")
    st.sidebar.caption("本地持久化展示界面 · 只读")

    persisted_page = _read_state(store, PAGE_KEY, DEFAULT_PAGE)
    if persisted_page not in PAGES:
        persisted_page = DEFAULT_PAGE
    st.session_state.setdefault("ui_page", persisted_page)
    page = st.sidebar.radio("页面", PAGES, key="ui_page")
    if page != persisted_page:
        _write_state(store, PAGE_KEY, page)

    st.sidebar.divider()
    st.sidebar.caption("数据源")
    st.sidebar.code(
        f"会话库(只读): {data.project_path(history_file)}\n"
        f"状态库: {data.project_path(settings.ui_state_file)}\n"
        f"报告目录: {settings.ui_report_dirs}",
        language=None,
    )
    if st.sidebar.button("刷新数据", help="重新读取磁盘上的会话、任务与评测报告"):
        _refresh()

    with st.sidebar.expander("持久化界面状态", expanded=False):
        try:
            snapshot = store.snapshot()
        except UiStateError as error:
            snapshot = {}
            st.error(str(error))
        if snapshot:
            st.dataframe(
                [{"键": key, "值": json.dumps(value, ensure_ascii=False)[:120]}
                 for key, value in snapshot.items()],
                hide_index=True,
            )
        else:
            st.caption("暂无已持久化的视图状态")
        st.caption("页面、上次选中的会话/报告与笔记在下一次启动时自动恢复。")

    if st.sidebar.button("恢复默认视图", help="清除页面与选择状态，笔记会保留"):
        try:
            store.reset_navigation()
        except UiStateError as error:
            _state_error(str(error))
        else:
            for key in ("ui_page", "ui_conversation", "ui_report"):
                st.session_state.pop(key, None)
            _refresh()

    st.sidebar.divider()
    st.sidebar.warning(
        "仅限本机虚构数据与公开资料。界面无登录与鉴权，只绑定 127.0.0.1；"
        "不得转发、反向代理或用于真实患者数据。"
    )
    return page


def _persisted_notes(store: UiStateStore, kind: str, target_id: str) -> list[dict]:
    try:
        return store.list_notes(kind, target_id)
    except (UiStateError, ValueError) as error:
        st.error(str(error))
        return []


def _reset_note_draft(draft_key: str) -> None:
    """Clear a draft before its widget is created; widgets cannot be reset afterwards."""
    if st.session_state.pop("_clear_note_draft_key", None) == draft_key:
        st.session_state.pop(draft_key, None)


def _render_note_editor(store: UiStateStore, kind: str, target_id: str,
                        title: str) -> None:
    """Notes are the one editable thing in this viewer; they live in ui-state."""
    st.subheader("操作员笔记")
    st.caption("笔记保存在界面状态数据库中，进程重启或容器重建后仍然保留。")
    notes = _persisted_notes(store, kind, target_id)
    if not notes:
        st.caption("该对象还没有笔记。")
    for note in notes:
        with st.container(border=True):
            st.markdown(note["body"])
            st.caption(f"更新于 {data.format_timestamp(note['updated'])}")
            if st.button("删除笔记", key=f"delete-note-{note['note_id']}"):
                try:
                    store.delete_note(note["note_id"])
                except (UiStateError, ValueError) as error:
                    st.error(str(error))
                else:
                    st.rerun()

    draft_key = f"note-draft-{kind}-{target_id}"
    _reset_note_draft(draft_key)
    body = st.text_area("新增笔记", key=draft_key, max_chars=4000,
                        placeholder=f"记录关于「{title}」的观察或待办…")
    if st.button("保存笔记", key=f"save-note-{kind}-{target_id}"):
        try:
            store.save_note(kind, target_id, body)
        except (UiStateError, ValueError) as error:
            st.error(str(error))
        else:
            st.session_state["_clear_note_draft_key"] = draft_key
            st.success("笔记已保存")
            st.rerun()


# -- pages ------------------------------------------------------------------
def _page_overview(settings: Settings, history_file: str, signature: str) -> None:
    st.title("概览")
    st.caption("展示本项目已经持久化到磁盘的会话、文档任务与评测结果；不触发任何模型调用。")
    overview = _cached_overview(history_file, signature)

    columns = st.columns(5)
    columns[0].metric("会话", overview.conversation_count)
    columns[1].metric("消息轮次", overview.turn_count)
    columns[2].metric("已回答", overview.answered_count)
    columns[3].metric("文档任务", overview.document_job_count)
    columns[4].metric("待处理任务", overview.pending_job_count)
    st.caption(
        f"会话库 {overview.path} · {data.format_bytes(overview.size_bytes)} · "
        f"更新于 {data.format_timestamp(overview.modified_at)}"
        if overview.exists else
        f"会话库不存在：{overview.path}（启动本机演示并提问后会自动创建）"
    )

    st.divider()
    left, right = st.columns(2)
    with left:
        st.subheader("后端健康探测")
        health = _cached_health(settings.ui_api_base_url)
        if health.get("reachable") and isinstance(health.get("payload"), dict):
            payload = health["payload"]
            st.success(f"RAG API 可达：{settings.ui_api_base_url}")
            status = payload.get("data", payload)
            if isinstance(status, dict):
                st.json({"status": status.get("status"),
                         "milvus": status.get("milvus"),
                         "messaging": status.get("messaging")})
        else:
            st.warning(f"RAG API 不可达或未就绪：{health.get('error') or '未知原因'}")
        st.caption("仅探测本机回环 /health；界面本身不依赖后端在线。")

    with right:
        st.subheader("最近会话")
        index = _cached_conversation_index(history_file, signature)
        if index:
            st.dataframe(
                [{"标题": row.title, "轮次": row.turn_count, "已回答": row.answered_count,
                  "更新时间": data.format_timestamp(row.updated)} for row in index[:8]],
                hide_index=True,
            )
        else:
            st.info("还没有持久化的会话。启动本机演示并提问后，这里会出现记录。")

    st.divider()
    st.subheader("最近评测报告")
    reports = _cached_reports(settings.ui_report_dirs)
    if reports:
        st.dataframe(
            [{"报告": report.path.name, "题目数": report.case_count,
              "数据版本": report.dataset_version or "—",
              "修改时间": data.format_timestamp(report.modified)} for report in reports[:8]],
            hide_index=True,
        )
        st.caption("在「评测报告」页面查看完整指标与逐题结果。")
    else:
        st.info("报告目录中还没有可识别的评测报告 JSON。")

    st.divider()
    st.subheader("后台 worker 心跳")
    heartbeats = _cached_heartbeats(history_file, signature)
    if heartbeats:
        st.dataframe(
            [{"角色": item.get("role"), "更新时间": data.format_timestamp(item.get("updated_at")),
              "Broker": item.get("broker")} for item in heartbeats],
            hide_index=True,
        )
    else:
        st.caption("没有 worker 心跳记录（单用户问答不需要 worker；文档入库与记忆抽取需要）。")


def _page_conversations(settings: Settings, store: UiStateStore,
                        history_file: str, signature: str) -> None:
    st.title("会话历史")
    st.caption("只读读取 data/chat-history.sqlite3；引用、拒答原因与失败运行都来自已持久化的记录。")
    index = _cached_conversation_index(history_file, signature)
    if not index:
        st.info("还没有会话记录。")
        st.code("python -m medical_rag.api_server --host 127.0.0.1 --port 8001", language=None)
        st.caption("在 unsafe-local-demo 模式下提问后，问答会落盘到 data/chat-history.sqlite3。")
        return

    search = st.text_input("筛选标题或最近问题", key="conversation-filter",
                           placeholder="输入关键词…")
    visible = [row for row in index
               if not search.strip()
               or search.strip().lower() in (row.title + " " + row.last_question).lower()]
    if not visible:
        st.warning("没有匹配的会话。")
        return

    ids = [row.conversation_id for row in visible]
    labels = {row.conversation_id: f"{row.title}（{row.turn_count} 轮 · "
                                   f"{data.format_timestamp(row.updated)}）" for row in visible}
    stored = _read_state(store, CONVERSATION_KEY, None)
    if st.session_state.get("ui_conversation") not in ids:
        st.session_state["ui_conversation"] = stored if stored in ids else ids[0]
    conversation_id = st.selectbox("选择会话", ids, format_func=labels.get, key="ui_conversation")
    if conversation_id and conversation_id != stored:
        _write_state(store, CONVERSATION_KEY, conversation_id)

    conversation = _cached_conversation(history_file, conversation_id, signature)
    if conversation is None:
        st.error("会话记录无法读取，可能已被删除。")
        return

    left, right = st.columns([3, 1])
    left.subheader(conversation.title)
    right.caption(f"{len(conversation.messages)} 条消息 · 更新于 "
                  f"{data.format_timestamp(conversation.updated)}")

    for message in conversation.messages:
        with st.chat_message("user" if message.role == "user" else "assistant"):
            st.markdown(message.content)
            if message.role != "assistant":
                continue
            tags = []
            if message.abstained:
                tags.append("⛔ 拒答/证据不足")
            if message.answer_type == "clarification":
                tags.append("❓ 追问澄清")
            if message.abstain_reason:
                tags.append(f"原因: {message.abstain_reason}")
            if tags:
                st.caption(" · ".join(tags))
            if message.citations:
                with st.expander(f"引用 {len(message.citations)} 条"):
                    st.json(message.citations)
            if message.run_id:
                st.caption(f"run_id: {message.run_id}")

    if conversation.failed_runs:
        with st.expander(f"失败运行 {len(conversation.failed_runs)} 条"):
            st.json(conversation.failed_runs)

    st.divider()
    _render_note_editor(store, "conversation", conversation_id, conversation.title)


def _page_documents(settings: Settings, history_file: str, signature: str) -> None:
    st.title("文档任务")
    st.caption("读取 document_jobs 表：文件先暂存，再由 relay/ingest worker 异步入库。")
    jobs = _cached_jobs(history_file, signature)
    if not jobs:
        st.info("没有文档任务记录。")
        st.caption("用 .\\start.ps1 -WithWorkers 启动 relay/ingest worker 后再上传文档，"
                   "任务会写入这里。")
        return

    statuses = sorted({str(job.get("status") or "unknown") for job in jobs})
    chosen = st.multiselect("状态筛选", statuses, default=statuses,
                            key="job-status-filter")
    visible = [job for job in jobs if str(job.get("status") or "unknown") in chosen]
    st.dataframe(
        [{"文件": job.get("filename"), "来源": job.get("source_id"),
          "状态": job.get("status"), "阶段": job.get("stage"),
          "尝试": job.get("attempts"), "创建": data.format_timestamp(job.get("created_at")),
          "更新": data.format_timestamp(job.get("updated_at")),
          "错误": job.get("error") or ""} for job in visible],
        hide_index=True,
    )
    with_error = [job for job in visible if job.get("error")]
    if with_error:
        st.subheader("失败详情")
        for job in with_error:
            with st.expander(f"{job.get('filename')} · {job.get('status')}"):
                st.code(str(job.get("error")), language=None)


def _page_reports(settings: Settings, store: UiStateStore) -> None:
    st.title("评测报告")
    st.caption("只读扫描 UI_REPORT_DIRS 下的评测报告 JSON；界面不会运行评估或调用模型。")
    reports = _cached_reports(settings.ui_report_dirs)
    if not reports:
        st.info("没有找到评测报告。")
        st.code(
            'python -m medical_rag.evaluate --dataset data/evaluations/medical-health-v2/'
            'questions.jsonl --output outputs/reports/medical-health-v2.json',
            language=None,
        )
        st.caption(f"当前报告目录：{settings.ui_report_dirs}（可用 UI_REPORT_DIRS 覆盖，逗号分隔多个目录）")
        return

    paths = [str(report.path) for report in reports]
    labels = {str(report.path): f"{report.path.name} · {report.case_count} 题 · "
                               f"{data.format_timestamp(report.modified)}" for report in reports}
    stored = _read_state(store, REPORT_KEY, None)
    if st.session_state.get("ui_report") not in paths:
        st.session_state["ui_report"] = stored if stored in paths else paths[0]
    chosen = st.selectbox("选择报告", paths, format_func=labels.get, key="ui_report")
    if chosen and chosen != stored:
        _write_state(store, REPORT_KEY, chosen)

    meta = next((report for report in reports if str(report.path) == chosen), None)
    if meta is None:  # pragma: no cover - selection and options are the same list
        st.error("报告已不存在，请刷新数据。")
        return

    st.subheader(meta.path.name)
    st.caption(
        f"数据版本: {meta.dataset_version or '—'} · 题目数: {meta.case_count} · "
        f"回答模型: {meta.answer_provider or '—'} · 裁判模型: {meta.judge_model or '—'} · "
        f"修改于 {data.format_timestamp(meta.modified)}"
    )

    metrics = data.report_metrics(meta)
    if metrics:
        columns = st.columns(4)
        for position, (key, label, value) in enumerate(metrics):
            rendered = (f"{value * 100:.1f}%" if key.endswith("_rate")
                        else str(int(value)) if key == "judge_failures"
                        else f"{value:.4f}")
            columns[position % 4].metric(label, rendered)
    else:
        st.info("该报告没有可展示的汇总指标（可能是中断的评估）。")

    with st.expander("完整 summary"):
        st.json(meta.summary)

    try:
        report = _cached_report(str(meta.path))
    except ValueError as error:
        st.error(f"报告无法解析：{error}")
        return

    st.subheader("逐题结果")
    rows = data.report_case_rows(report)
    if rows:
        categories = sorted({str(row.get("类别")) for row in rows if row.get("类别")})
        answerabilities = sorted({str(row.get("可答性")) for row in rows if row.get("可答性")})
        filters = st.columns(4)
        category = filters[0].selectbox("类别", ["全部", *categories], key="case-category")
        answerability = filters[1].selectbox("可答性", ["全部", *answerabilities],
                                             key="case-answerability")
        only_abstained = filters[2].checkbox("只看拒答", key="case-abstained")
        only_errors = filters[3].checkbox("只看失败", key="case-errors")
        filtered = [
            row for row in rows
            if (category == "全部" or str(row.get("类别")) == category)
            and (answerability == "全部" or str(row.get("可答性")) == answerability)
            and (not only_abstained or row.get("拒答") is True)
            and (not only_errors or row.get("错误"))
        ]
        st.caption(f"显示 {len(filtered)} / {len(rows)} 题")
        st.dataframe(filtered, hide_index=True)
    else:
        st.caption("该报告没有逐题 cases 数据。")

    st.download_button(
        "下载报告 JSON",
        data=json.dumps(report, ensure_ascii=False, indent=2),
        file_name=meta.path.name,
        mime="application/json",
        key="download-report",
    )

    st.divider()
    _render_note_editor(store, "report", str(meta.path.resolve()), meta.path.name)


def _page_notes(settings: Settings, store: UiStateStore, history_file: str,
                signature: str) -> None:
    st.title("界面笔记")
    st.caption("所有笔记都保存在界面状态数据库中，与问答库分离，可单独备份或删除。")
    try:
        notes = store.list_notes()
    except UiStateError as error:
        st.error(str(error))
        notes = []

    index = _cached_conversation_index(history_file, signature)
    conversation_titles = {row.conversation_id: row.title for row in index}
    reports = _cached_reports(settings.ui_report_dirs)
    report_names = {str(report.path.resolve()): report.path.name for report in reports}

    def target_label(note: dict) -> str:
        kind = note["target_kind"]
        target = note["target_id"]
        if kind == "conversation":
            return f"会话: {conversation_titles.get(target, target[:12])}"
        if kind == "report":
            return f"报告: {report_names.get(target, Path(target).name)}"
        return f"{kind}: {target[:24]}"

    if not notes:
        st.info("还没有笔记。可以在会话详情或评测报告页面添加，或使用下面的表单。")
    for note in notes:
        with st.container(border=True):
            st.caption(target_label(note))
            st.markdown(note["body"])
            st.caption(f"更新于 {data.format_timestamp(note['updated'])}")
            if st.button("删除", key=f"notes-page-delete-{note['note_id']}"):
                try:
                    store.delete_note(note["note_id"])
                except (UiStateError, ValueError) as error:
                    st.error(str(error))
                else:
                    st.rerun()

    st.divider()
    st.subheader("新增笔记")
    kind_label = st.radio("对象类型", ["会话", "评测报告"], horizontal=True,
                          key="note-target-kind")
    target_id = None
    if kind_label == "会话":
        if index:
            ids = [row.conversation_id for row in index]
            target_id = st.selectbox("会话", ids, key="note-conversation-target",
                                     format_func=lambda item: conversation_titles.get(item, item))
        else:
            st.info("没有会话可选。")
    else:
        if reports:
            paths = [str(report.path.resolve()) for report in reports]
            target_id = st.selectbox("报告", paths, key="note-report-target",
                                     format_func=lambda item: report_names.get(item, item))
        else:
            st.info("没有评测报告可选。")
    if target_id is not None:
        _reset_note_draft("notes-page-new-body")
        body = st.text_area("笔记内容", key="notes-page-new-body", max_chars=4000)
        if st.button("保存到界面状态库", key="notes-page-save"):
            try:
                store.save_note("conversation" if kind_label == "会话" else "report",
                                target_id, body)
            except (UiStateError, ValueError) as error:
                st.error(str(error))
            else:
                st.session_state["_clear_note_draft_key"] = "notes-page-new-body"
                st.success("笔记已保存")
                st.rerun()


# -- entry point ------------------------------------------------------------
def main() -> None:
    """Streamlit script entry point; every rerun rebuilds the page from state."""
    st.set_page_config(page_title="Medical RAG 展示界面", page_icon="📊", layout="wide")
    try:
        settings = _settings()
    except Exception as error:  # noqa: BLE001 - configuration errors must stay readable
        st.error(f"读取配置失败：{type(error).__name__}。请检查 .env 中的取值。")
        st.stop()
        return

    store = _state_store(settings.ui_state_file)
    history_file = settings.chat_history_file
    signature = _db_signature(history_file)
    page = _render_sidebar(store, settings, history_file)

    if page == "概览":
        _page_overview(settings, history_file, signature)
    elif page == "会话历史":
        _page_conversations(settings, store, history_file, signature)
    elif page == "文档任务":
        _page_documents(settings, history_file, signature)
    elif page == "评测报告":
        _page_reports(settings, store)
    else:
        _page_notes(settings, store, history_file, signature)

    st.divider()
    st.caption(
        "只读展示界面 · 不替代医生诊断 · 不处理真实患者数据 · "
        f"笔记类型: {', '.join(sorted(NOTE_KINDS))}"
    )


main()
