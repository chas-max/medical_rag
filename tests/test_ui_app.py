"""Streamlit UI smoke tests: login gate, page routing and empty/offline states."""

import http.server
import json
import threading

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from medical_rag.ui import launcher  # noqa: E402
from medical_rag.ui.auth import TenantStore  # noqa: E402
from medical_rag.ui.state import UiStateStore  # noqa: E402
from medical_rag.ui.stats import QueryMetricsStore  # noqa: E402

PASSWORD = "demo-pass-123"


class _FakeBackend:
    """Minimal loopback RAG API: /health, conversations and SSE chat."""

    def __init__(self) -> None:
        self.conversations: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # noqa: D401 - silence test output
                return

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                outer.requests.append(("GET", self.path))
                path = self.path.split("?")[0]
                if path == "/health":
                    self._json(200, {"success": True, "data": {
                        "status": "ok", "milvus": True, "messaging": {"status": "ok"}},
                        "error": None})
                elif path == "/conversations":
                    self._json(200, {"success": True, "data": list(outer.conversations.values()),
                                     "error": None})
                elif path == "/document-jobs":
                    self._json(200, {"success": True, "data": [], "error": None})
                elif path.startswith("/conversations/"):
                    conversation = outer.conversations.get(path.rsplit("/", 1)[-1])
                    if conversation is None:
                        self._json(404, {"success": False, "data": None,
                                         "error": {"code": "CONVERSATION_NOT_FOUND",
                                                   "message": "对话记录不存在"}})
                    else:
                        self._json(200, {"success": True, "data": conversation,
                                         "error": None})
                else:
                    self._json(404, {"success": False, "data": None,
                                     "error": {"code": "NOT_FOUND", "message": "not found"}})

            def do_DELETE(self) -> None:  # noqa: N802
                outer.requests.append(("DELETE", self.path))
                self._json(200, {"success": True, "data": {"deleted": True}, "error": None})

            def do_POST(self) -> None:  # noqa: N802
                outer.requests.append(("POST", self.path))
                if self.path != "/chat/runs/stream":
                    self._json(404, {"success": False, "data": None,
                                     "error": {"code": "NOT_FOUND", "message": "not found"}})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length))
                question = payload["inputs"]["question"]
                conversation_id = payload["conversation_id"]
                conversation = outer.conversations.setdefault(conversation_id, {
                    "id": conversation_id, "title": question[:24],
                    "messages": [], "failedRuns": []})
                conversation["messages"].append({"role": "user", "content": question})
                citations = [{"chunk_id": "c1", "filename": "膳食指南.md",
                              "excerpt": "控制总热量并合理分餐。"}]
                conversation["messages"].append({
                    "role": "assistant", "content": "建议控制总热量并合理分餐。",
                    "citations": citations, "abstained": False, "runId": "run-1"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                frames = [
                    ("status", {"stage": "retrieving", "evidence": 2, "web_search": False}),
                    ("claim", {"text": "建议控制总热量。", "citations": citations}),
                    ("done", {
                        "run_id": "run-1", "status": "success",
                        "output": {"answer": "建议控制总热量并合理分餐。",
                                   "citations": citations, "abstained": False},
                        "trace": [
                            {"node_id": "retrieval.dense", "status": "success",
                             "duration_ms": 12.0},
                            {"node_id": "generation.llm", "status": "success",
                             "duration_ms": 30.0},
                        ],
                    }),
                ]
                for name, data in frames:
                    frame = f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                self.close_connection = True

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _app() -> AppTest:
    return AppTest.from_file(str(launcher.APP_PATH), default_timeout=60)


def _by_key(elements, key):
    return next(item for item in elements if item.key == key)


def _prepare(tmp_path, monkeypatch, *, username="demo", password=PASSWORD):
    state_file = tmp_path / "ui-state.sqlite3"
    monkeypatch.setenv("UI_STATE_FILE", str(state_file))
    monkeypatch.setenv("CHAT_HISTORY_FILE", str(tmp_path / "chat-history.sqlite3"))
    monkeypatch.setenv("UI_REPORT_DIRS", str(tmp_path / "reports"))
    tenant = TenantStore(state_file).create_tenant(username, password, display_name="演示租户")
    return state_file, tenant


def _login(at: AppTest, username="demo", password=PASSWORD) -> AppTest:
    at.run()
    assert not at.exception
    at.text_input[0].set_value(username)
    at.text_input[1].set_value(password)
    at.button[0].click().run()
    return at


def test_login_gate_shows_first_run_setup_when_no_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("UI_STATE_FILE", str(tmp_path / "ui-state.sqlite3"))
    at = _app()
    at.run()
    assert not at.exception
    assert any("医疗资料 RAG 智能问答" in item.value for item in at.markdown)
    assert any("尚未配置任何租户" in item.value for item in at.info)
    assert any(item.key == "create-demo-tenant" for item in at.button)


def test_first_run_button_creates_tenant_and_shows_password_once(tmp_path, monkeypatch):
    monkeypatch.setenv("UI_STATE_FILE", str(tmp_path / "ui-state.sqlite3"))
    at = _app()
    at.run()
    at.button(key="create-demo-tenant").click().run()
    assert not at.exception
    assert TenantStore(tmp_path / "ui-state.sqlite3").get_by_username("demo") is not None
    assert at.code, "创建后应一次性显示随机密码"
    generated = at.code[0].value
    created = TenantStore(tmp_path / "ui-state.sqlite3").get_by_username("demo")
    assert created is not None
    # The shown password really works.
    at.text_input[0].set_value("demo")
    at.text_input[1].set_value(generated)
    at.button[0].click().run()
    assert not at.exception
    assert any("租户：演示租户" in item.value for item in at.sidebar.caption)


def test_login_loads_chat_page_and_sidebar_navigation(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    at = _login(_app())
    assert not at.exception
    assert any("租户：演示租户" in item.value for item in at.sidebar.caption)
    assert any(item.key == "nav-chat" for item in at.sidebar.button)
    assert any(item.key == "nav-stats" for item in at.sidebar.button)
    # Backend is not running in this test: chat page must warn, not crash.
    assert any("无法读取会话历史" in item.value for item in at.warning)
    assert not at.exception


def test_buttons_switch_to_stats_page(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    at = _login(_app())
    _by_key(at.sidebar.button, "nav-stats").click().run()
    assert not at.exception
    assert any("数据统计" in item.value for item in at.markdown)
    assert any("检索质量来自评测报告" in item.value for item in at.caption)

    _by_key(at.sidebar.button, "nav-chat").click().run()
    assert not at.exception
    assert any("无法读取会话历史" in item.value for item in at.warning)


def test_wrong_password_shows_uniform_error(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    at = _login(_app(), password="wrong-password")
    assert not at.exception
    assert any("用户名或密码错误" in item.value for item in at.error)


def test_repeated_failures_lock_the_account(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setenv("UI_LOGIN_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("UI_LOGIN_LOCKOUT_SECONDS", "300")
    at = _app()
    for _ in range(2):
        at = _login(at, password="wrong-password")
    at = _login(at, password="wrong-password")
    assert any("尝试次数过多" in item.value for item in at.error)


def test_stats_page_renders_report_metrics(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "medical-health-v2.json").write_text(json.dumps({
        "dataset_version": "v2",
        "summary": {"precision": 0.61, "recall": 0.55, "ndcg": 0.5},
        "config": {"answer_provider": "qwen", "judge_model": "qwen-max"},
        "cases": [{"id": "q1"}],
    }, ensure_ascii=False), encoding="utf-8")
    at = _login(_app())
    _by_key(at.sidebar.button, "nav-stats").click().run()
    assert not at.exception
    assert any(item.label == "检索 Precision" for item in at.metric)
    assert any(item.label == "最新报告" or item.label.startswith("检索") for item in at.metric)


def test_stats_page_without_reports_shows_guidance(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    at = _login(_app())
    _by_key(at.sidebar.button, "nav-stats").click().run()
    assert not at.exception
    assert any("还没有可识别的评测报告" in item.value for item in at.info)


def test_stats_latency_tab_is_empty_without_queries(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    at = _login(_app())
    _by_key(at.sidebar.button, "nav-stats").click().run()
    assert any("还没有问答指标" in item.value for item in at.info)


def test_logout_returns_to_login_page(tmp_path, monkeypatch):
    state_file, tenant = _prepare(tmp_path, monkeypatch)
    at = _login(_app())
    _by_key(at.sidebar.button, "logout").click().run()
    assert not at.exception
    assert any("尚未配置任何租户" in item.value for item in at.info) is False
    assert at.text_input, "退出后应回到登录表单"
    # The login attempt audit was persisted.
    events = TenantStore(state_file).recent_auth_events()
    assert events and events[0]["success"] is True


def test_new_conversation_is_persisted_for_the_tenant(tmp_path, monkeypatch):
    state_file, tenant = _prepare(tmp_path, monkeypatch)
    at = _login(_app())
    # Logging in opens a fresh chat page, which creates the first conversation.
    assert len(UiStateStore(state_file).list_sessions(tenant.tenant_id)) == 1
    at.sidebar.button(key="new-chat").click().run()
    assert not at.exception
    sessions = UiStateStore(state_file).list_sessions(tenant.tenant_id)
    assert len(sessions) == 2
    assert sessions[0]["conversation_id"].startswith(tenant.tenant_id[:12])
    assert sessions[0]["title"] == "新对话"


def test_full_chat_roundtrip_against_loopback_backend(tmp_path, monkeypatch):
    state_file, tenant = _prepare(tmp_path, monkeypatch)
    backend = _FakeBackend()
    monkeypatch.setenv("UI_API_BASE_URL", backend.base_url)
    try:
        at = _login(_app())
        assert not at.exception
        at.chat_input[0].set_value("糖尿病患者能吃水果吗？").run()
        assert not at.exception

        # The authoritative answer is rendered and the turn was persisted in metrics.
        assert any("建议控制总热量并合理分餐" in item.value for item in at.markdown)
        metrics_store = QueryMetricsStore(state_file)
        summary = metrics_store.summary(tenant.tenant_id)
        assert summary["count"] == 1
        assert summary["success_count"] == 1
        assert summary["p95_latency_ms"] is not None
        nodes = metrics_store.node_latency(tenant.tenant_id)
        assert {row["node_id"] for row in nodes} == {"retrieval.dense", "generation.llm"}

        sessions = UiStateStore(state_file).list_sessions(tenant.tenant_id)
        assert sessions[0]["title"] == "糖尿病患者能吃水果吗？"[:24]
        assert any(path == "/chat/runs/stream" for _, path in backend.requests)
    finally:
        backend.close()
