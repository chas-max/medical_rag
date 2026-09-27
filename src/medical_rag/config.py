"""Runtime configuration, loaded from environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    milvus_uri: str = "http://localhost:19530"
    milvus_token: str = ""
    collection_name: str = "medical_chunks"
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.8-flash"
    candidate_k: int = Field(default=20, ge=1, le=100)
    top_k: int = Field(default=5, ge=1, le=20)
    max_upload_bytes: int = Field(default=20_000_000, ge=1)
    evidence_threshold: float = Field(default=0.5, ge=0, le=1)
