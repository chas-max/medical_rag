"""Value objects shared across ingestion, retrieval, and HTTP layers."""

from dataclasses import dataclass

ORIGIN_KNOWLEDGE_BASE = "knowledge_base"
ORIGIN_WEB = "web"

# How an Evidence.score was produced. Scores from different producers are not
# comparable: a cross-encoder probability, a cosine distance, a BM25 score and a
# lexical coverage ratio all live on different scales. Downstream safety checks must
# branch on the kind instead of comparing every score with one global threshold.
SCORE_RERANK = "rerank"
SCORE_LEXICAL = "lexical"
SCORE_DENSE = "dense"
SCORE_BM25 = "bm25"
SCORE_WEB = "web"
SCORE_UNKNOWN = "unknown"


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
    # Small-to-big fields. The child text above is the retrieval unit; the parent
    # text is the coherent window handed to the generator under the same chunk id.
    parent_id: str = ""
    parent_text: str = ""
    parent_ordinal: int = 0
    token_count: int = 0

    @property
    def context_text(self) -> str:
        """Generation context: the parent window when available, else the child."""
        return self.parent_text or self.text


@dataclass(frozen=True)
class Evidence:
    chunk: Chunk
    score: float
    fused_score: float = 0.0
    origin: str = ORIGIN_KNOWLEDGE_BASE
    # Default is "unknown", and the evidence gate fails closed on it. Producers must
    # opt in by labelling their score, so a new retrieval path cannot silently inherit
    # the reranker's calibrated threshold.
    score_kind: str = SCORE_UNKNOWN


class ApiError(Exception):
    """Error safe for exposure through the public API."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
