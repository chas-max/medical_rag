"""Streamlit entry point: tenant login, sidebar and page routing.

This is the local demonstration console for the medical RAG project. The login gate
is an application-level feature: the legacy RAG backend itself is unauthenticated
and loopback-only, so tenant accounts here isolate the *interface* (sessions,
metrics, notes) and are not a production security boundary.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow ``streamlit run src/medical_rag/ui/app.py`` without an editable install.
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402

from medical_rag.config import Settings  # noqa: E402
from medical_rag.ui.api_client import BackendError, RagApiClient  # noqa: E402
from medical_rag.ui.auth import (  # noqa: E402
    AccountLockedError,
    InvalidCredentialsError,
    TenantStore,
    generate_password,
)
from medical_rag.ui.chat_page import render_chat_page  # noqa: E402
from medical_rag.ui.stats import QueryMetricsStore  # noqa: E402
from medical_rag.ui.stats_page import render_stats_page  # noqa: E402
from medical_rag.ui.state import UiStateStore, new_conversation_id  # noqa: E402

PAGES = ("智能问答", "数据统计")
DEFAULT_PAGE = "智能问答"
AUTH_KEY = "auth"
PAGE_KEY = "page"
CONVERSATION_KEY = "conversation"

LOGIN_CSS = """
<style>
  .block-container { padding-top: 2.4rem; max-width: 1040px; }
  [data-testid="stSidebar"] { background: #f5f7ff; }
  [data-testid="stSidebar"] .block-container { padding-top: 1.4rem; }
  .login-card { padding: 1.2rem 0 0.4rem 0; }
  .login-title { font-size: 1.9rem; font-weight: 700; letter-spacing: .02em; }
  .login-sub { color: #5f6368; margin-bottom: 1.2rem; }
  .model-chip { display: inline-block; padding: .15rem .55rem; border-radius: 999px;
                background: #e8edff; color: #3552c9; font-size: .75rem; margin-right: .3rem; }
  .chat-meta { color: #6b7280; font-size: .8rem; }
  div[data-testid="stChatMessage"] { background: transparent; }
  div[data-testid="stChatInput"] { position: sticky; bottom: 1rem; }
</style>
"""


# -- resources --------------------------------------------------------------
def _settings() -> Settings:
    return Settings()


@st.cache_resource(show_spinner=False)
def _client(base_url: str, timeout: float, top_k: int) -> RagApiClient:
    """One shared HTTP client; ``backend_origin`` rejects non-loopback URLs."""
    return RagApiClient(base_url, timeout=timeout, top_k=top_k)


@st.cache_data(ttl=10, show_spinner=False)
def _backend_health(base_url: str, timeout: float, top_k: int) -> dict:
    try:
        client = _client(base_url, timeout, top_k)
        payload = client.health()
    except (BackendError, ValueError) as error:
        return {"ok": False, "error": str(error)}
    status = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return {"ok": True, "status": status if isinstance(status, dict) else {}}


def _stores(settings: Settings) -> tuple[TenantStore, UiStateStore, QueryMetricsStore]:
    return (TenantStore(settings.ui_state_file),
            UiStateStore(settings.ui_state_file),
            QueryMetricsStore(settings.ui_state_file))


# -- authentication ---------------------------------------------------------
def _session_timeout(settings: Settings) -> float:
    return float(settings.ui_session_timeout_minutes) * 60.0


def _current_session(settings: Settings, tenants: TenantStore) -> dict | None:
    """Validate the login session on every rerun; returns the tenant snapshot."""
    auth = st.session_state.get(AUTH_KEY)
    if not isinstance(auth, dict):
        return None
    try:
        tenant = tenants.get_tenant(str(auth.get("tenant_id") or ""))
    except Exception:  # noqa: BLE001 - a broken state DB must not grant access
        st.session_state.pop(AUTH_KEY, None)
        return None
    if tenant is None or tenant.disabled:
        st.session_state.pop(AUTH_KEY, None)
        st.session_state["_auth_notice"] = "账号已停用或不存在，请重新登录。"
        return None
    expires_at = float(auth.get("expires_at") or 0)
    if expires_at < time.time():
        st.session_state.pop(AUTH_KEY, None)
        st.session_state["_auth_notice"] = "登录已超时，请重新登录。"
        return None
    # Sliding expiry: an active operator is not logged out mid-session.
    auth = {
        "tenant_id": tenant.tenant_id,
        "username": tenant.username,
        "display_name": tenant.display_name,
        "expires_at": time.time() + _session_timeout(settings),
    }
    st.session_state[AUTH_KEY] = auth
    return auth


def _login_page(settings: Settings, tenants: TenantStore) -> None:
    st.markdown(LOGIN_CSS, unsafe_allow_html=True)
    left, middle, right = st.columns([1, 1.4, 1])
    with middle:
        st.markdown('<div class="login-card">', unsafe_allow_html=True)
        st.markdown('<div class="login-title">医疗资料 RAG 智能问答</div>',
                    unsafe_allow_html=True)
        st.markdown(
            '<div class="login-sub">面向公开医疗资料的多租户演示控制台 · 检索问答 / 指标监控</div>',
            unsafe_allow_html=True,
        )
        st.markdown('</div>', unsafe_allow_html=True)

        notice = st.session_state.pop("_auth_notice", None)
        if notice:
            st.warning(str(notice))
        created = st.session_state.pop("_created_tenant", None)
        if created:
            st.success(f"已创建演示租户 **{created['username']}**，请保存下面的初始密码（只显示一次）。")
            st.code(created["password"], language=None)

        try:
            tenant_count = len(tenants.list_tenants())
        except Exception:  # noqa: BLE001 - surface storage failures on the login page
            st.error("本地界面数据库不可用：请检查 UI_STATE_FILE 的路径与权限。")
            return

        if tenant_count == 0:
            st.info("尚未配置任何租户。可先创建本机演示租户，或用 CLI 添加正式演示账号。")
            if st.button("创建演示租户（随机密码）", type="primary",
                         use_container_width=True, key="create-demo-tenant"):
                password = generate_password()
                try:
                    tenant = tenants.create_tenant("demo", password, display_name="演示租户")
                except Exception as error:  # noqa: BLE001 - keep the login page readable
                    st.error(f"创建租户失败：{type(error).__name__}")
                else:
                    st.session_state["_created_tenant"] = {
                        "username": tenant.username, "password": password}
                    st.rerun()
            st.caption(
                '也可以使用命令行：`python -m medical_rag.ui.tenants add <用户名> --generate`'
            )
        else:
            with st.form("login", border=True):
                username = st.text_input("用户名", placeholder="tenant-username")
                password = st.text_input("密码", type="password", placeholder="••••••••")
                submitted = st.form_submit_button("登录", type="primary",
                                                  use_container_width=True)
            if submitted:
                try:
                    tenant = tenants.verify_login(
                        username, password,
                        max_attempts=settings.ui_login_max_attempts,
                        lockout_seconds=settings.ui_login_lockout_seconds,
                    )
                except AccountLockedError as error:
                    wait = max(1, int(error.retry_after_seconds))
                    st.error(f"尝试次数过多，请在约 {wait} 秒后重试。")
                except InvalidCredentialsError:
                    st.error("用户名或密码错误。")
                except Exception as error:  # noqa: BLE001 - storage failure
                    st.error(f"登录失败：{type(error).__name__}，请检查界面状态库。")
                else:
                    st.session_state[AUTH_KEY] = {
                        "tenant_id": tenant.tenant_id,
                        "username": tenant.username,
                        "display_name": tenant.display_name,
                        "expires_at": time.time() + _session_timeout(settings),
                    }
                    st.session_state.pop(PAGE_KEY, None)
                    st.session_state.pop(CONVERSATION_KEY, None)
                    st.rerun()
            st.caption("首次使用请用 CLI 或演示按钮创建租户。连续失败会被临时锁定。")

        st.divider()
        st.caption(
            "仅限本机演示环境：界面登录用于隔离展示与指标，**不能替代 RAG 后端的"
            "身份认证**；后端须以 unsafe-local-demo 模式仅监听 127.0.0.1。"
        )


# -- sidebar ----------------------------------------------------------------
def _task_panel(client: RagApiClient) -> None:
    try:
        jobs = client.document_jobs(limit=8)
    except BackendError:
        st.caption("后端不可用，无法获取任务进程。")
        return
    active = [job for job in jobs if str(job.get("status")) not in {"succeeded", "failed"}]
    st.caption(f"运行中 {len(active)} 个 · 最近 {len(jobs)} 个任务")
    icons = {"queued": "⏳", "running": "🔄", "retrying": "🔁",
             "succeeded": "✅", "failed": "❌"}
    for job in jobs[:6]:
        status = str(job.get("status") or "unknown")
        icon = icons.get(status, "•")
        stage = job.get("stage") or status
        st.caption(f"{icon} {job.get('filename') or '未知文件'} · {stage}")
    if active:
        st.caption("任务在后台 worker 中异步执行；完成后可在数据统计页查看吞吐。")


def _sidebar(settings: Settings, tenant: dict, client: RagApiClient,
             state_store: UiStateStore, tenants: TenantStore) -> str:
    tenant_id = tenant["tenant_id"]
    with st.sidebar:
        st.markdown("### 🩺 医疗 RAG")
        st.caption(f"租户：{tenant['display_name']} · @{tenant['username']}")
        health = _backend_health(settings.ui_api_base_url,
                                 settings.ui_request_timeout_seconds, settings.ui_top_k)
        if health.get("ok"):
            st.caption("🟢 后端在线 · Milvus " + ("可用" if health.get("status", {}).get("milvus")
                                                else "不可用"))
        else:
            st.caption("🔴 后端不可用（问答需先启动 start.ps1）")
        st.divider()

        if st.button("＋ 新对话", type="primary", use_container_width=True, key="new-chat"):
            try:
                conversation_id = new_conversation_id(tenant_id)
                state_store.remember_session(tenant_id, conversation_id, "新对话")
                state_store.set_last_conversation(tenant_id, conversation_id)
            except Exception as error:  # noqa: BLE001 - storage failure is actionable
                st.error(f"创建会话失败：{type(error).__name__}")
            else:
                st.session_state[CONVERSATION_KEY] = conversation_id
                st.session_state[PAGE_KEY] = "智能问答"
                st.rerun()

        current_page = st.session_state.get(PAGE_KEY, DEFAULT_PAGE)
        if st.button("💬 智能问答", use_container_width=True, key="nav-chat",
                     type="primary" if current_page == "智能问答" else "secondary"):
            st.session_state[PAGE_KEY] = "智能问答"
            try:
                state_store.set_page("智能问答")
            except Exception:  # noqa: BLE001 - view persistence is best effort
                pass
            st.rerun()
        if st.button("📊 数据统计", use_container_width=True, key="nav-stats",
                     type="primary" if current_page == "数据统计" else "secondary"):
            st.session_state[PAGE_KEY] = "数据统计"
            try:
                state_store.set_page("数据统计")
            except Exception:  # noqa: BLE001 - view persistence is best effort
                pass
            st.rerun()

        st.divider()
        with st.expander("任务进程", expanded=True):
            _task_panel(client)

        st.divider()
        st.caption("最近会话")
        try:
            sessions = state_store.list_sessions(tenant_id, limit=30)
        except Exception as error:  # noqa: BLE001 - storage failure is actionable
            sessions = []
            st.error(f"会话列表不可用：{type(error).__name__}")
        backend_titles: dict[str, str] = {}
        try:
            backend_titles = {str(item.get("id")): str(item.get("title") or "")
                              for item in client.conversations()}
        except BackendError:
            pass
        current = st.session_state.get(CONVERSATION_KEY)
        if not sessions:
            st.caption("还没有会话，点击「＋ 新对话」开始。")
        for session in sessions:
            conversation_id = str(session["conversation_id"])
            title = backend_titles.get(conversation_id) or str(session["title"]) or "新对话"
            label = title if len(title) <= 22 else title[:21] + "…"
            if st.button(label, use_container_width=True, key=f"conv-{conversation_id}",
                         type="primary" if conversation_id == current else "secondary"):
                st.session_state[CONVERSATION_KEY] = conversation_id
                st.session_state[PAGE_KEY] = "智能问答"
                try:
                    state_store.set_last_conversation(tenant_id, conversation_id)
                    state_store.set_page("智能问答")
                except Exception:  # noqa: BLE001 - view persistence is best effort
                    pass
                st.rerun()

        st.divider()
        if st.button("退出登录", use_container_width=True, key="logout"):
            st.session_state.pop(AUTH_KEY, None)
            st.session_state.pop(CONVERSATION_KEY, None)
            st.session_state.pop(PAGE_KEY, None)
            st.rerun()
        st.caption("仅限本机虚构数据；界面登录不等同于后端鉴权。")
    return st.session_state.get(PAGE_KEY, DEFAULT_PAGE)


# -- entry point ------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="医疗 RAG 智能问答", page_icon="🩺", layout="wide")
    st.markdown(LOGIN_CSS, unsafe_allow_html=True)
    try:
        settings = _settings()
    except Exception as error:  # noqa: BLE001 - invalid .env must stay readable
        st.error(f"读取配置失败：{type(error).__name__}。请检查 .env 中的取值。")
        st.stop()
        return
    try:
        tenants, state_store, metrics_store = _stores(settings)
    except Exception as error:  # noqa: BLE001 - configuration errors must stay readable
        st.error(f"初始化本地界面状态失败：{type(error).__name__}")
        st.stop()
        return

    session = _current_session(settings, tenants)
    if session is None:
        _login_page(settings, tenants)
        return

    try:
        client = _client(settings.ui_api_base_url, settings.ui_request_timeout_seconds,
                         settings.ui_top_k)
    except ValueError as error:
        st.error(f"后端地址配置无效：{error}")
        st.stop()
        return

    if PAGE_KEY not in st.session_state:
        stored_page = state_store.get_page(DEFAULT_PAGE)
        st.session_state[PAGE_KEY] = stored_page if stored_page in PAGES else DEFAULT_PAGE
    page = _sidebar(settings, session, client, state_store, tenants)
    if page not in PAGES:
        page = DEFAULT_PAGE
    if page == "智能问答":
        render_chat_page(settings=settings, client=client, state_store=state_store,
                         metrics_store=metrics_store, tenant=session)
    else:
        render_stats_page(settings=settings, client=client, metrics_store=metrics_store,
                          tenant=session)


main()
