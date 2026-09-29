"""Value objects shared across ingestion, retrieval, and HTTP layers."""

from dataclasses import dataclass

ORIGIN_KNOWLEDGE_BASE = "knowledge_base"
ORIGIN_WEB = "web"


@dataclass(frozen=True)
class Document:
    id: str
    source_id: str
    filename: str
    sha256: str
    chunk_count: int
    strategy: str
    reason: str


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    source_id: str
    text: str
    filename: str
    section: str
    page: int | None
    ordinal: int


@dataclass(frozen=True)
class Evidence:
    chunk: Chunk
    score: float
    fused_score: float = 0.0
    origin: str = ORIGIN_KNOWLEDGE_BASE


class ApiError(Exception):
    """Error safe for exposure through the public API."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
