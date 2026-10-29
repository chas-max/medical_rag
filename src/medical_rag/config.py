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

    milvus_uri: str = "http://localhost:19531"
    milvus_token: str = ""
    collection_name: str = "medical_rag_resume_chunks"
    # Where the UI-written settings live. `.env` stays read-only so a configured
    # model survives restarts without anyone editing secrets into the env file.
    settings_file: str = "settings.json"
    chat_history_file: str = "data/chat-history.sqlite3"
    memory_episodes_dir: str = "data/memory/episodes"
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

    @model_validator(mode="after")
    def _validate_chunking(self) -> "Settings":
        if self.chunk_max_tokens > MAX_CHUNK_TOKENS:
            raise ValueError(f"CHUNK_MAX_TOKENS 不能超过 {MAX_CHUNK_TOKENS}")
        if self.chunk_overlap_tokens >= self.chunk_max_tokens:
            raise ValueError("CHUNK_OVERLAP_TOKENS 必须小于 CHUNK_MAX_TOKENS")
        if self.parent_max_tokens < self.chunk_max_tokens:
            raise ValueError("PARENT_MAX_TOKENS 不能小于 CHUNK_MAX_TOKENS")
        return self
