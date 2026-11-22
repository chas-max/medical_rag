"""Runtime configuration, loaded from environment variables."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .tokenization import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_PARENT_TOKENS,
    MAX_CHUNK_TOKENS,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[2] / ".env", extra="ignore")

    # Until identity and per-patient ownership are implemented, the legacy API
    # must be unavailable by default. This switch is not an authentication mode.
    access_mode: Literal["locked", "unsafe-local-demo"] = Field(
        default="locked", validation_alias="MEDICAL_RAG_ACCESS_MODE"
    )
    milvus_uri: str = "http://localhost:19531"
    milvus_token: str = ""
    collection_name: str = "medical_rag_resume_chunks"
    # Where the UI-written settings live. `.env` stays read-only so a configured
    # model survives restarts without anyone editing secrets into the env file.
    settings_file: str = "settings.json"
    chat_history_file: str = "data/chat-history.sqlite3"
    memory_episodes_dir: str = "data/memory/episodes"
    # Conversation-turn vectors are a separate collection from the document store:
    # recall must be scoped to one session and never mixed into medical evidence.
    chat_recall_enabled: bool = True
    chat_turn_collection: str = "medical_rag_chat_turns"
    chat_recall_top_k: int = Field(default=3, ge=1, le=10)
    # "conversation" keeps recall inside the current session; "global" opens it to
    # the local user's other sessions, ranked with a recency half-life.
    chat_recall_scope: Literal["conversation", "global"] = "conversation"
    chat_recall_half_life_days: float = Field(default=7.0, gt=0, le=365)
    document_uploads_dir: str = "data/document-uploads"
    rabbitmq_url: str = "amqp://medical_rag@127.0.0.1:5673/medical-rag"
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.8-flash"
    hyde_enabled: bool = True
    hyde_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    candidate_k: int = Field(default=20, ge=1, le=100)
    rerank_k: int = Field(default=15, ge=1, le=100)
    top_k: int = Field(default=5, ge=1, le=20)
    # 子块是检索单位，必须为 query 与特殊 token 留出 512 窗口余量；父块只用于
    # 生成上下文，不参与嵌入，可以更宽。改这些值会改变块 ID，需重新入库。
    chunk_max_tokens: int = Field(default=DEFAULT_CHUNK_TOKENS, ge=100, le=MAX_CHUNK_TOKENS)
    chunk_overlap_tokens: int = Field(default=DEFAULT_OVERLAP_TOKENS, ge=0,
                                      le=MAX_CHUNK_TOKENS - 1)
    parent_max_tokens: int = Field(default=DEFAULT_PARENT_TOKENS, ge=100, le=4000)
    max_upload_bytes: int = Field(default=20_000_000, ge=1)
    evidence_threshold: float = Field(default=0.5, ge=0, le=1)
    # 重排不可用时默认拒答（fail-closed），而不是用词面分数继续生成。设为 false
    # 时才允许“查询实词覆盖率 ≥ 0.6 且命中 ≥ 2 个实词”的保守放行。
    rerank_fail_closed: bool = True
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"
    tavily_search_depth: Literal["basic", "advanced"] = "basic"
    tavily_max_results: int = Field(default=5, ge=1, le=20)
    tavily_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    web_content_max_chars: int = Field(default=1200, ge=200, le=4000)
    clear_evidence_threshold: float = Field(default=0.65, ge=0, le=1)
    min_clear_evidence: int = Field(default=2, ge=1, le=20)
    max_agent_tool_calls: int = Field(default=2, ge=0, le=5)
    # Structured dialogue state ships dark. Both the local chat entry and the
    # patient deployment must inject the same named environment variable; enabling
    # only half the chain would persist state nobody reads.
    dialogue_state_enabled: bool = False
    # 日志由 Loguru 统一输出：按进程分目录、按大小轮转、超期删除、历史归档压缩。
    # 关闭文件 sink 后只保留控制台；测试与 CI 不应在仓库里落盘。
    log_enabled: bool = True
    log_dir: str = "outputs/logs"
    log_level: str = "INFO"
    log_rotation: str = "20 MB"
    log_retention: str = "14 days"
    log_compression: str = "zip"
    log_json: bool = False
    # 本机 Streamlit 问答/指标控制台：它不改问答数据，只把租户账号、界面视图、
    # 租户会话映射与每次问答的耗时指标持久化到这个 SQLite 文件（见 ui 文档）。
    ui_state_file: str = "data/streamlit/ui-state.sqlite3"
    # 评测报告是普通 JSON；界面只读扫描这些目录（逗号分隔），按修改时间倒序展示。
    ui_report_dirs: str = "outputs/reports"
    # 概览页只对本机回环的 RAG /health 做只读探测；非回环地址会被界面拒绝。
    ui_api_base_url: str = "http://127.0.0.1:8001"
    # Streamlit 问答界面通过 HTTP 调用本机 RAG 服务；超时覆盖完整的检索/生成。
    ui_request_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    # 默认走 /chat/runs/stream（SSE 流式）；后端不支持流式时可在界面关闭。
    ui_stream_enabled: bool = True
    ui_top_k: int = Field(default=5, ge=1, le=20)
    # 界面登录会话的空闲有效期（每次操作滑动续期）；超时后需重新登录。
    ui_session_timeout_minutes: int = Field(default=480, ge=5, le=10080)
    # 同一用户名连续失败达到上限后锁定一段时间，避免在线爆破。
    ui_login_max_attempts: int = Field(default=5, ge=1, le=20)
    ui_login_lockout_seconds: int = Field(default=300, ge=30, le=3600)
    # 演示用账号密码自动填入：首次进入登录页时自动创建/校验本机演示租户，并把
    # 可用凭据填进表单，操作员点一次「登录」即可进入。仅限本机演示环境使用，
    # 任何共享或非演示部署都应设 UI_DEMO_LOGIN_ENABLED=false。
    ui_demo_login_enabled: bool = True
    ui_demo_username: str = "demo"
    # 固定演示密码（8-128 字符）；留空则在首次使用时随机生成并写入本机状态库。
    ui_demo_password: str = ""

    @model_validator(mode="after")
    def _validate_chunking(self) -> "Settings":
        if self.chunk_max_tokens > MAX_CHUNK_TOKENS:
            raise ValueError(f"CHUNK_MAX_TOKENS 不能超过 {MAX_CHUNK_TOKENS}")
        if self.chunk_overlap_tokens >= self.chunk_max_tokens:
            raise ValueError("CHUNK_OVERLAP_TOKENS 必须小于 CHUNK_MAX_TOKENS")
        if self.parent_max_tokens < self.chunk_max_tokens:
            raise ValueError("PARENT_MAX_TOKENS 不能小于 CHUNK_MAX_TOKENS")
        return self
