"""Vector index over this conversation's own turns (step 3 of short-term memory).

Scope is deliberately narrow: recall is filtered by ``conversation_id``, so the
index can never surface another session's words. The collection is optional — a
missing Milvus, schema mismatch or embedding failure only disables recall, never
the answer path.

Rows are one per finished turn (question + answer), deduplicated by a stable id so
re-indexing the same turn overwrites instead of duplicating.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

TURN_COLLECTION_SUFFIX = "_turns"
MAX_TURN_TEXT_CHARS = 2000
MAX_QUESTION_CHARS = 400
# Conversation ids reach a Milvus filter expression; anything outside this charset
# could change the expression's meaning, so it is rejected before indexing/search.
CONVERSATION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def turn_id(conversation_id: str, turn: int) -> str:
    """Deterministic primary key: re-indexing a turn overwrites its own row."""
    return hashlib.sha256(f"{conversation_id}:{turn}".encode()).hexdigest()[:32]


def turn_text(question: object, answer: object) -> str:
    """One searchable blob per turn; both sides matter for a later reference."""
    q = question.strip() if isinstance(question, str) else ""
    a = answer.strip() if isinstance(answer, str) else ""
    if not q or not a:
        return ""
    return f"问：{q}\n答：{a}"[:MAX_TURN_TEXT_CHARS]


@dataclass(frozen=True)
class MilvusTurnStore:
    """Thin Milvus adapter; the only place that knows the collection schema."""

    client: Any
    collection: str

    def ensure_schema(self) -> None:
        client = self.client
        if client.has_collection(self.collection):
            client.load_collection(self.collection)
            return
        from pymilvus import DataType, MilvusClient

        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("conversation_id", DataType.VARCHAR, max_length=128)
        schema.add_field("turn_id", DataType.INT64)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        schema.add_field("created_at", DataType.INT64)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=512)
        indices = MilvusClient.prepare_index_params()
        indices.add_index(field_name="dense", index_type="HNSW", index_name="dense_index",
                          metric_type="COSINE", params={"M": 16, "efConstruction": 200})
        client.create_collection(collection_name=self.collection, schema=schema,
                                 index_params=indices, consistency_level="Strong")
        client.load_collection(self.collection)

    def upsert(self, row: dict) -> None:
        self.client.upsert(collection_name=self.collection, data=[row])

    def search(self, vector: list[float], conversation_id: str | None,
               limit: int) -> list[dict]:
        """``conversation_id=None`` means cross-session recall for the local user."""
        expression = (f"conversation_id == {json.dumps(conversation_id)}"
                      if conversation_id else "")
        hits = self.client.search(
            collection_name=self.collection, data=[vector], anns_field="dense",
            limit=limit, filter=expression or None,
            output_fields=["turn_id", "text", "created_at"],
            consistency_level="Strong",
            search_params={"metric_type": "COSINE", "params": {"ef": 64}})
        rows = []
        for hit in hits[0]:
            entity = hit.get("entity") or {}
            rows.append({"turn": int(entity.get("turn_id", 0)),
                         "text": entity.get("text", ""),
                         "created_at": int(entity.get("created_at", 0) or 0),
                         "score": float(hit.get("distance", 0.0))})
        return rows

    def delete_conversation(self, conversation_id: str) -> int:
        """Remove every vector of one session; 0 when nothing matched."""
        result = self.client.delete(
            collection_name=self.collection,
            filter=f"conversation_id == {json.dumps(conversation_id)}")
        return int((result or {}).get("delete_count", 0) if isinstance(result, dict) else 0)


class ConversationHistory:
    """Index one session's turns and recall the relevant ones; never cross-session."""

    def __init__(self, client: Any = None, collection: str = "medical_rag_chat_turns",
                 embed: Any = None, top_k: int = 3, store: Any = None,
                 min_score: float = 0.35, scope: str = "conversation",
                 half_life_days: float = 7.0, now: Any = None) -> None:
        self.store = store or (MilvusTurnStore(client, collection) if client is not None else None)
        self.embed = embed
        self.top_k = top_k
        # A weak neighbour is worse than no neighbour: it would drag the rewrite
        # toward an unrelated turn. Below this similarity the turn is not recalled.
        self.min_score = min_score
        # "conversation" keeps recall inside the current session (the default);
        # "global" opens it to the local user's other sessions with a recency decay.
        self.scope = scope
        self.half_life_days = half_life_days
        self._now = now or time.time

    @property
    def enabled(self) -> bool:
        return self.store is not None and callable(self.embed)

    def ensure_schema(self) -> None:
        if self.store is not None:
            self.store.ensure_schema()

    def index_turn(self, conversation_id: str, turn: int, question: object,
                   answer: object) -> bool:
        """Idempotent write. Returns False for anything not worth indexing."""
        if not self.enabled or not isinstance(conversation_id, str):
            return False
        if not CONVERSATION_ID.fullmatch(conversation_id):
            return False
        text = turn_text(question, answer)
        if not text:
            return False
        vector = self.embed([text])[0]
        if len(vector) != 512:
            raise ValueError("会话向量维度必须为 512")
        self.store.upsert({"id": turn_id(conversation_id, turn),
                           "conversation_id": conversation_id, "turn_id": int(turn),
                           "text": text, "created_at": int(self._now()), "dense": vector})
        return True

    def recall(self, conversation_id: str, seed: str, top_k: int | None = None) -> list[dict]:
        """Most similar earlier turns; the span follows ``scope``."""
        if not self.enabled:
            return []
        global_scope = self.scope == "global"
        if not global_scope and (not isinstance(conversation_id, str)
                                 or not CONVERSATION_ID.fullmatch(conversation_id)):
            return []
        query = seed.strip()[:MAX_QUESTION_CHARS] if isinstance(seed, str) else ""
        if not query:
            return []
        limit = int(top_k or self.top_k)
        vector = self.embed([query])[0]
        if global_scope:
            # Fetch a wider net: decay reorders what similarity alone offered.
            rows = self._decay(self.store.search(vector, None, min(limit * 3, 30)))
        else:
            rows = self.store.search(vector, conversation_id, limit)
        return [{"turn": row["turn"], "text": row["text"]}
                for row in rows
                if row.get("text") and float(row.get("score", 0.0)) >= self.min_score][:limit]

    def _decay(self, rows: list[dict]) -> list[dict]:
        """Half-life weighting so an old but similar turn cannot outrank a recent one."""
        now = self._now()
        half_life = max(self.half_life_days, 0.001)
        weighted = []
        for row in rows:
            created = float(row.get("created_at") or 0)
            # Legacy rows without a timestamp get a neutral half weight: neither
            # immortal nor invisible.
            age_days = max(0.0, (now - created) / 86400) if created > 0 else half_life
            weighted.append({**row, "score": float(row.get("score", 0.0))
                             * 0.5 ** (age_days / half_life)})
        weighted.sort(key=lambda item: item["score"], reverse=True)
        return weighted

    def delete_turns(self, conversation_id: str) -> bool:
        """Cascade hook: remove this session's vectors after its history is gone."""
        if not self.enabled or not isinstance(conversation_id, str) or \
                not CONVERSATION_ID.fullmatch(conversation_id):
            return False
        self.store.delete_conversation(conversation_id)
        return True


class ConversationIndexer:
    """Background write of the just-finished turn; at most one per process."""

    def __init__(self, history_factory: Any, enabled: bool = True) -> None:
        self.history_factory = history_factory
        self.enabled = enabled
        self._thread: threading.Thread | None = None

    def maybe_schedule(self, conversation_id: str, turn: int, question: object,
                       answer: object) -> None:
        if not self.enabled or not isinstance(conversation_id, str) or not conversation_id:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        thread = threading.Thread(target=self._run,
                                  args=(conversation_id, turn, question, answer),
                                  name="rag-turn-index", daemon=True)
        self._thread = thread
        thread.start()

    def _run(self, conversation_id: str, turn: int, question: object, answer: object) -> None:
        try:
            history = self.history_factory()
            if history is None:
                return
            history.index_turn(conversation_id, turn, question, answer)
        except Exception:  # noqa: BLE001 - optional index, never a reason to fail a turn
            # No question, answer, conversation id or upstream body in logs.
            logger.warning("Conversation index unavailable")

    def delete(self, conversation_id: str) -> bool:
        """Synchronous cascade on conversation deletion; False when unavailable."""
        if not self.enabled:
            return False
        try:
            history = self.history_factory()
            return bool(history is not None and history.delete_turns(conversation_id))
        except Exception:  # noqa: BLE001 - the remaining deletes already happened
            logger.warning("Conversation index deletion unavailable")
            return False
