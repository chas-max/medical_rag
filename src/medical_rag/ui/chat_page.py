"""DeepSeek-style chat page: streaming answers, citations and run trace."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

import streamlit as st

from ..config import Settings
from .api_client import BackendError, RagApiClient
from .stats import QueryMetricsStore
from .state import UiStateStore, new_conversation_id

CONVERSATION_KEY = "conversation"
MAX_QUESTION_CHARS = 20_000
STAGE_LABELS = {
    "intent": "理解问题…",
    "profile": "检查画像与记忆…",
    "answer": "检索资料并生成回答…",
    "assessing": "评估问题复杂度…",
    "planning": "规划检索路径…",
    "retrieving": "召回资料中…",
    "stopped": "检索完成",
}


def _progress_label(event: str, payload: dict[str, Any]) -> str:
    if event == "chat.progress":
        return STAGE_LABELS.get(str(payload.get("stage")), "处理中…")
    if event == "node.progress":
        base = STAGE_LABELS.get(str(payload.get("stage")), "处理中…")
        evidence = payload.get("evidence")
        if isinstance(evidence, int) and evidence > 0:
            return f"{base}（已召回 {evidence} 条）"
        return base
    if event == "status":
        evidence = payload.get("evidence")
        if isinstance(evidence, int):
            return f"检索资料中（已召回 {evidence} 条）…"
        return "检索资料中…"
    return "处理中…"


def _citation_rows(citations: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        excerpt = citation.get("excerpt") or citation.get("text") or ""
        rows.append({
            "资料": citation.get("filename") or citation.get("source")
            or citation.get("title") or citation.get("chunk_id") or "未知资料",
            "页码": citation.get("page"),
            "相关度": citation.get("score"),
            "摘录": str(excerpt)[:240],
        })
    return rows


def _render_citations(citations: list[Any]) -> None:
    rows = _citation_rows(citations)
    if not rows:
        return
    with st.expander(f"引用资料（{len(rows)} 条）"):
        st.dataframe(rows, hide_index=True)
        chunk_ids = [str(item.get("chunk_id")) for item in citations
                     if isinstance(item, dict) and item.get("chunk_id")]
        if chunk_ids:
            st.caption("资料块 ID：" + "、".join(chunk_ids[:8]))


def _trace_rows(trace: list[Any], total_ms: float | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in trace:
        if not isinstance(entry, dict):
            continue
        duration = entry.get("duration_ms")
        rows.append({
            "节点": entry.get("node_id"),
            "状态": entry.get("status"),
            "耗时 (ms)": round(float(duration), 1) if isinstance(duration, (int, float)) else None,
        })
    rows.sort(key=lambda item: item["耗时 (ms)"] or 0, reverse=True)
    if total_ms is not None:
        rows.append({"节点": "客户端总耗时", "状态": "—", "耗时 (ms)": round(total_ms, 1)})
    return rows


def _render_trace(trace: list[Any], total_ms: float | None = None) -> None:
    rows = _trace_rows(trace, total_ms)
    if not rows:
        return
    with st.expander("运行轨迹 / 延迟明细", expanded=False):
        st.dataframe(rows, hide_index=True)


def _render_stored_message(message: dict[str, Any]) -> None:
    role = "user" if message.get("role") == "user" else "assistant"
    content = message.get("content")
    if not isinstance(content, str):
        return
    with st.chat_message(role):
        st.markdown(content)
        if role != "assistant":
            return
        if message.get("answerType") == "clarification":
            st.info("需要补充信息才能继续回答。")
        if message.get("abstained"):
            reason = message.get("abstainReason")
            st.warning("证据不足：本次未形成可引用的医学结论" + (f"（{reason}）" if reason else "。"))
        citations = message.get("citations")
        if isinstance(citations, list) and citations:
            _render_citations(citations)
        trace = message.get("trace")
        if isinstance(trace, list) and trace:
            _render_trace(trace)
        run_id = message.get("runId")
        if isinstance(run_id, str) and run_id:
            st.caption(f"run_id: {run_id}")


def _render_history(conversation: dict[str, Any]) -> None:
    messages = conversation.get("messages")
    if not isinstance(messages, list) or not messages:
        st.caption("开始提问吧：回答只基于本机公开医疗资料，引用可展开核对。")
        return
    for message in messages:
        if isinstance(message, dict):
            _render_stored_message(message)
    failures = conversation.get("failedRuns")
    if isinstance(failures, list) and failures:
        with st.expander(f"失败运行（{len(failures)} 次）"):
            st.json(failures)


def _error_hint(code: str) -> str | None:
    return {
        "BACKEND_UNREACHABLE": "请先启动 RAG 后端：在仓库根运行 .\\start.ps1（本机 8001）。",
        "BACKEND_TIMEOUT": "生成超时：可在 .env 调大 UI_REQUEST_TIMEOUT_SECONDS。",
        "SECURITY_NOT_READY": "后端处于 locked 模式：请用 start.ps1 以 unsafe-local-demo 启动。",
        "LOCAL_DEMO_ONLY": "后端拒绝本机以外的来源；界面必须与后端在同一台机器上运行。",
        "DEPENDENCY_ERROR": "检索或模型依赖不可用：检查 docker compose（Milvus）与 .env 中的模型 Key。",
        "STREAM_INTERRUPTED": "流式响应中断：可重试，或在界面关闭流式（UI_STREAM_ENABLED=false）。",
    }.get(code)


def _record_turn(metrics_store: QueryMetricsStore, tenant_id: str, conversation_id: str,
                 question: str, *, total_ms: float, first_event_ms: float | None,
                 status: str, stream: bool = True, answer: str = "",
                 citation_count: int = 0, abstained: bool | None = None,
                 trace: list[Any] | None = None,
                 error_code: str | None = None) -> None:
    try:
        metrics_store.record(
            tenant_id, total_ms,
            conversation_id=conversation_id,
            status=status,
            abstained=abstained,
            answer_chars=len(answer),
            citation_count=citation_count,
            stream=stream,
            question_chars=len(question),
            error_code=error_code,
            first_event_ms=first_event_ms,
            trace=trace,
        )
    except Exception:  # noqa: BLE001 - metrics must never lose an answer
        st.caption("指标写入失败：本次问答不会出现在数据统计页。")


def _stream_turn(*, client: RagApiClient, settings: Settings,
                 metrics_store: QueryMetricsStore, state_store: UiStateStore,
                 tenant: dict[str, Any], conversation_id: str, question: str) -> None:
    """Run one turn in a worker thread while the script thread renders progress."""
    events: queue.Queue[tuple[str, Any, float]] = queue.Queue()
    started = time.perf_counter()
    first_event_ms: float | None = None

    def on_event(name: str, payload: dict[str, Any]) -> None:
        events.put((name, payload, time.perf_counter()))

    def worker() -> None:
        try:
            result = client.ask(question, conversation_id, stream=settings.ui_stream_enabled,
                                top_k=settings.ui_top_k, on_event=on_event)
        except BackendError as error:
            events.put(("__error__", error, time.perf_counter()))
        except Exception as error:  # noqa: BLE001 - keep the interface alive
            events.put(("__error__", BackendError(
                "UI_ERROR", f"界面处理失败：{type(error).__name__}"), time.perf_counter()))
        else:
            events.put(("__done__", result, time.perf_counter()))

    thread = threading.Thread(target=worker, daemon=True, name="rag-chat-stream")
    thread.start()

    with st.chat_message("assistant"):
        status_slot = st.empty()
        claims_slot = st.empty()
        status_text = "正在提交问题…"
        status_slot.caption(f"⏳ {status_text}")
        claims: list[str] = []
        analysis: dict[str, Any] | None = None
        final: dict[str, Any] | None = None
        failure: BackendError | None = None
        finished_at = started

        while True:
            try:
                name, payload, stamp = events.get(timeout=0.2)
            except queue.Empty:
                if not thread.is_alive() and events.empty():
                    break
                status_slot.caption(f"⏳ {status_text}（{time.perf_counter() - started:.1f}s）")
                continue
            if first_event_ms is None:
                first_event_ms = (stamp - started) * 1000.0
            if name == "__done__":
                final = payload
                finished_at = stamp
                break
            if name == "__error__":
                failure = payload
                finished_at = stamp
                break
            if name == "claim":
                text = str(payload.get("text") or "").strip()
                if text:
                    claims.append(text)
                    bullets = "\n".join(f"- {item}" for item in claims)
                    claims_slot.markdown(f"**已核验的结论（流式）**\n\n{bullets}")
            elif name == "analysis":
                analysis = payload
            else:
                status_text = _progress_label(name, payload)
            status_slot.caption(f"⏳ {status_text}（{stamp - started:.1f}s）")

        total_ms = (finished_at - started) * 1000.0
        status_slot.empty()
        claims_slot.empty()

        if failure is not None:
            st.error(failure.message)
            hint = _error_hint(failure.code)
            if hint:
                st.caption(hint)
            _record_turn(metrics_store, tenant["tenant_id"], conversation_id, question,
                         total_ms=total_ms, first_event_ms=first_event_ms,
                         status="failed", stream=settings.ui_stream_enabled,
                         error_code=failure.code)
            _remember_title(client, state_store, tenant, conversation_id, question)
            return

        output = final.get("output") if isinstance(final, dict) else {}
        output = output if isinstance(output, dict) else {}
        answer = output.get("answer") if isinstance(output.get("answer"), str) else ""
        citations = output.get("citations")
        citations = citations if isinstance(citations, list) else output.get("evidence")
        citations = citations if isinstance(citations, list) else []
        abstained = bool(output.get("abstained"))
        final_status = str(final.get("status") or "success") if isinstance(final, dict) else "failed"
        error_payload = final.get("error") if isinstance(final, dict) else None

        if answer:
            st.markdown(answer)
        if output.get("answerType") == "clarification":
            st.info("需要补充信息才能继续回答。")
        if abstained:
            reason = output.get("abstain_reason")
            st.warning("证据不足：本次未形成可引用的医学结论" + (f"（{reason}）" if reason else "。"))
        if citations:
            _render_citations(citations)
        if isinstance(analysis, dict):
            summary = analysis.get("summary")
            if isinstance(summary, str) and summary:
                with st.expander("解读与延伸"):
                    st.markdown(summary)
                    extensions = analysis.get("extensions")
                    if isinstance(extensions, list):
                        for item in extensions:
                            st.markdown(f"- {item}")
        trace = final.get("trace") if isinstance(final, dict) else None
        trace = trace if isinstance(trace, list) else []
        _render_trace(trace, total_ms)
        if final_status != "success":
            message = (error_payload.get("message") if isinstance(error_payload, dict)
                       else None) or "本次回答未成功生成，请重试。"
            st.error(str(message))

        _record_turn(
            metrics_store, tenant["tenant_id"], conversation_id, question,
            total_ms=total_ms, first_event_ms=first_event_ms,
            status="success" if final_status == "success" else "failed",
            stream=settings.ui_stream_enabled,
            answer=answer, citation_count=len(citations), abstained=abstained,
            trace=trace,
            error_code=(str(error_payload.get("code"))
                        if isinstance(error_payload, dict) and error_payload.get("code")
                        else None),
        )
        _remember_title(client, state_store, tenant, conversation_id, question)
        st.rerun()


def _remember_title(client: RagApiClient, state_store: UiStateStore, tenant: dict[str, Any],
                    conversation_id: str, question: str) -> None:
    """Persist the tenant session title; the backend supplies the canonical one."""
    title = question.strip()[:24] or "新对话"
    try:
        conversation = client.conversation(conversation_id)
        backend_title = conversation.get("title")
        if isinstance(backend_title, str) and backend_title.strip():
            title = backend_title.strip()
    except BackendError:
        pass
    try:
        state_store.remember_session(tenant["tenant_id"], conversation_id, title)
        state_store.set_last_conversation(tenant["tenant_id"], conversation_id)
    except Exception:  # noqa: BLE001 - session list persistence is best effort
        pass


def _ensure_conversation(tenant: dict[str, Any], state_store: UiStateStore) -> str:
    """Pick the active conversation, creating and persisting one when needed."""
    tenant_id = tenant["tenant_id"]
    sessions = state_store.list_sessions(tenant_id, limit=50)
    owned = {str(item["conversation_id"]) for item in sessions}
    current = st.session_state.get(CONVERSATION_KEY)
    if isinstance(current, str) and current in owned:
        state_store.set_last_conversation(tenant_id, current)
        return current
    last = state_store.get_last_conversation(tenant_id)
    if last and last in owned:
        st.session_state[CONVERSATION_KEY] = last
        return last
    if sessions:
        picked = str(sessions[0]["conversation_id"])
        st.session_state[CONVERSATION_KEY] = picked
        return picked
    created = new_conversation_id(tenant_id)
    state_store.remember_session(tenant_id, created, "新对话")
    state_store.set_last_conversation(tenant_id, created)
    st.session_state[CONVERSATION_KEY] = created
    st.rerun()
    return created  # pragma: no cover - st.rerun never returns


def render_chat_page(*, settings: Settings, client: RagApiClient,
                     state_store: UiStateStore, metrics_store: QueryMetricsStore,
                     tenant: dict[str, Any]) -> None:
    tenant_id = tenant["tenant_id"]
    conversation_id = _ensure_conversation(tenant, state_store)

    backend_error: BackendError | None = None
    conversation: dict[str, Any] | None = None
    try:
        conversation = client.conversation(conversation_id)
    except BackendError as error:
        backend_error = error

    title = "新对话"
    if isinstance(conversation, dict) and isinstance(conversation.get("title"), str):
        title = conversation["title"] or title
    elif state_store.conversation_title(tenant_id, conversation_id):
        title = str(state_store.conversation_title(tenant_id, conversation_id))

    header_left, header_right = st.columns([6, 1])
    with header_left:
        st.markdown(f"#### {title}")
        st.caption("基于公开医疗资料 · 回答不构成医疗建议 · 引用可展开核对")
    with header_right:
        with st.expander("会话管理", expanded=False):
            confirm = st.checkbox("确认删除当前会话", key=f"confirm-delete-{conversation_id}")
            if st.button("删除", disabled=not confirm, key=f"delete-{conversation_id}",
                         use_container_width=True):
                try:
                    client.delete_conversation(conversation_id)
                except BackendError as error:
                    st.error(f"删除失败：{error.message}")
                else:
                    state_store.forget_session(tenant_id, conversation_id)
                    st.session_state.pop(CONVERSATION_KEY, None)
                    st.rerun()

    st.divider()
    if backend_error is not None:
        st.warning(f"无法读取会话历史：{backend_error.message}")
        hint = _error_hint(backend_error.code)
        if hint:
            st.caption(hint)
    elif isinstance(conversation, dict):
        _render_history(conversation)

    st.divider()
    prompt = st.chat_input("输入你的问题，例如：糖尿病患者如何安排主食？")
    if not isinstance(prompt, str) or not prompt.strip():
        return
    if len(prompt.strip()) > MAX_QUESTION_CHARS:
        st.error(f"问题不能超过 {MAX_QUESTION_CHARS} 字符。")
        return
    with st.chat_message("user"):
        st.markdown(prompt.strip())
    _stream_turn(client=client, settings=settings, metrics_store=metrics_store,
                 state_store=state_store, tenant=tenant,
                 conversation_id=conversation_id, question=prompt.strip())
