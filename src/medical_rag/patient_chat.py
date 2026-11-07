"""Patient-side client for the institution's local RAG answer service.

The endpoint is selected only by trusted operator configuration; the browser
never sees it. Every upstream failure collapses to :class:`ChatUnavailable` so
URLs, model output, provider bodies and parser errors cannot reach the patient
response.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .dialogue_models import DialogueSnapshot, Resolution
from .question_resolver import validate_resolution_sources

MAX_QUESTION = 16000
MAX_ANSWER = 20000
MAX_CITATIONS = 50
MAX_TEXT = 512
MAX_CONTEXT_BYTES = 64 * 1024
ANSWER_TYPES = frozenset({"answer", "clarification"})


class ChatUnavailable(Exception):
    """Static public failure; never carries upstream text or identifiers."""


def _loopback(value: str) -> tuple[str, str] | None:
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (
            url.scheme not in {"http", "https"}
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
        ):
            return None
        _ = url.port  # Invalid ports raise ValueError below.
        if host == "localhost":
            return url.scheme, url.netloc
        if ipaddress.ip_address(host).is_loopback:
            return url.scheme, url.netloc
    except ValueError:
        pass
    return None


def _citation(value: object) -> dict[str, object] | None:
    if type(value) is not dict:
        return None
    chunk_id, filename = value.get("chunk_id"), value.get("filename")
    section, page, origin = value.get("section"), value.get("page"), value.get("origin")
    if not isinstance(chunk_id, str) or not 0 < len(chunk_id) <= 256:
        return None
    if not isinstance(filename, str) or not 0 < len(filename) <= MAX_TEXT:
        return None
    citation: dict[str, object] = {
        "chunk_id": chunk_id,
        "filename": filename,
        "section": section if isinstance(section, str) and len(section) <= MAX_TEXT else "",
    }
    if type(page) is int and 0 <= page <= 100000:
        citation["page"] = page
    if isinstance(origin, str) and 0 < len(origin) <= 32:
        citation["origin"] = origin
    excerpt = value.get("excerpt")
    if isinstance(excerpt, str) and 0 < len(excerpt) <= 2000:
        citation["excerpt"] = excerpt
    return citation


def _resolution(value: object, question: str,
                dialogue: DialogueSnapshot | None) -> dict[str, object] | None:
    """Validate one returned resolution against the snapshot that was sent.

    The patient backend saves what comes back here, so an unverifiable candidate is
    treated as an unusable upstream response instead of being stored.
    """
    if value is None:
        # A missing candidate is not an error: the caller simply learns nothing new
        # about the conversation's semantics for this turn.
        return None
    if dialogue is None:
        raise ChatUnavailable()
    try:
        parsed = Resolution.model_validate_json(json.dumps(value, ensure_ascii=False))
        if parsed.original_question != question:
            raise ChatUnavailable()
        validate_resolution_sources(parsed, question, dialogue)
    except ChatUnavailable:
        raise
    except Exception:  # noqa: BLE001 - untrusted upstream payload fails closed
        raise ChatUnavailable() from None
    return parsed.model_dump(mode="json")


def _answer(value: object, question: str,
            dialogue: DialogueSnapshot | None = None) -> dict[str, object]:
    if type(value) is not dict:
        raise ChatUnavailable()
    answer, abstained = value.get("answer"), value.get("abstained")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_ANSWER:
        raise ChatUnavailable()
    if type(abstained) is not bool:
        raise ChatUnavailable()
    raw = value.get("citations", [])
    if type(raw) is not list or len(raw) > MAX_CITATIONS:
        raise ChatUnavailable()
    citations = []
    for item in raw:
        citation = _citation(item)
        if citation is not None:
            citations.append(citation)
    answer_type = value.get("answer_type", "answer")
    if answer_type not in ANSWER_TYPES:
        raise ChatUnavailable()
    result: dict[str, object] = {
        "answer": answer,
        "citations": citations,
        "abstained": abstained,
        "answer_type": answer_type,
    }
    reason = value.get("abstain_reason")
    if isinstance(reason, str) and 0 < len(reason) <= 64:
        result["abstain_reason"] = reason
    resolution = _resolution(value.get("dialogue_resolution"), question, dialogue)
    if resolution is not None:
        result["dialogue_resolution"] = resolution
    return result


@dataclass(frozen=True, slots=True)
class ChatClient:
    """Trusted loopback caller; no cookies, proxies or redirects."""

    base_url: str = field(repr=False)
    timeout_seconds: int = 120
    top_k: int = 6
    transport: httpx.BaseTransport | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if _loopback(self.base_url) is None:
            raise ValueError("Loopback chat endpoint required")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 300:
            raise ValueError("Invalid chat timeout")
        if type(self.top_k) is not int or not 1 <= self.top_k <= 20:
            raise ValueError("Invalid chat top_k")

    @property
    def origin(self) -> str:
        scheme, netloc = _loopback(self.base_url) or ("http", "")
        return f"{scheme}://{netloc}"

    def answer(self, question: str,
               *, dialogue: DialogueSnapshot | None = None) -> dict[str, object]:
        """Ask the local service; an optional snapshot is the only extra context.

        Args:
            question: The patient's question, bounded by ``MAX_QUESTION``.
            dialogue: This conversation's already-authorised bounded snapshot. It
                is built by the patient backend, never by the browser.

        Returns:
            The validated answer mapping, including ``answer_type`` and, when a
            context was sent, the verified ``dialogue_resolution``.

        Raises:
            ValueError: For an invalid question.
            ChatUnavailable: For any upstream, transport or validation failure.
        """
        if type(question) is not str or not question.strip() or len(question) > MAX_QUESTION:
            raise ValueError("Invalid question")
        body: dict[str, object] = {
            "question": question, "top_k": self.top_k, "memory": False,
        }
        if dialogue is not None:
            encoded = dialogue.model_dump_json()
            if len(encoded.encode("utf-8")) > MAX_CONTEXT_BYTES:
                raise ValueError("Invalid dialogue context")
            body["dialogue_context"] = json.loads(encoded)
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers={"Origin": self.origin, "Accept": "application/json"},
                timeout=httpx.Timeout(self.timeout_seconds, connect=5.0),
                trust_env=False,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.post("/answers", json=body)
        except httpx.HTTPError:
            raise ChatUnavailable() from None
        if response.status_code != 200:
            raise ChatUnavailable()
        try:
            payload = response.json()
            if (
                type(payload) is not dict
                or payload.get("success") is not True
                or payload.get("error") is not None
            ):
                raise ChatUnavailable()
            return _answer(payload.get("data"), question, dialogue)
        except (ValueError, TypeError, KeyError):
            raise ChatUnavailable() from None
