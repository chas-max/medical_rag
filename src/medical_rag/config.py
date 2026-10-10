"""Runtime configuration, loaded from environment variables."""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    milvus_uri: str = "http://localhost:19530"
    milvus_token: str = ""
    collection_name: str = "medical_chunks"
    # Where the UI-written settings live. `.env` stays read-only so a configured
    # model survives restarts without anyone editing secrets into the env file.
    settings_file: str = "settings.json"
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.8-flash"
    candidate_k: int = Field(default=20, ge=1, le=100)
    rerank_k: int = Field(default=15, ge=1, le=100)
    top_k: int = Field(default=5, ge=1, le=20)
    max_upload_bytes: int = Field(default=20_000_000, ge=1)
    evidence_threshold: float = Field(default=0.5, ge=0, le=1)
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"
    tavily_search_depth: Literal["basic", "advanced"] = "basic"
    tavily_max_results: int = Field(default=5, ge=1, le=20)
    tavily_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    web_content_max_chars: int = Field(default=1200, ge=200, le=4000)
    clear_evidence_threshold: float = Field(default=0.65, ge=0, le=1)
    min_clear_evidence: int = Field(default=2, ge=1, le=20)
    max_agent_tool_calls: int = Field(default=2, ge=0, le=5)
