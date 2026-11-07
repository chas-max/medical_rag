"""Conversation-turn index: scoped recall, idempotent writes, silent failures."""

import math
import time

from medical_rag.conversation_index import (
    ConversationHistory,
    ConversationIndexer,
    turn_id,
)

CONVERSATION = "conversation-1"


class FakeStore:
    """Dict-backed stand-in for the Milvus adapter, with the same row shape."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.searches = []
        self.deleted = []

    def ensure_schema(self):
        return None

    def upsert(self, row):
        self.rows[row["id"]] = row

    def delete_conversation(self, conversation_id):
        removed = [key for key, row in self.rows.items()
                   if row["conversation_id"] == conversation_id]
        for key in removed:
            self.rows.pop(key)
        self.deleted.append(conversation_id)
        return len(removed)

    def search(self, vector, conversation_id, limit):
        self.searches.append((conversation_id, limit))
        scored = []
        for row in self.rows.values():
            if conversation_id is not None and row["conversation_id"] != conversation_id:
                continue
            scored.append({**row, "score": _cosine(vector, row["dense"])})
        scored.sort(key=lambda item: item["score"], reverse=True)
        return [{"turn": row["turn_id"], "text": row["text"],
                 "created_at": row.get("created_at", 0), "score": row["score"]}
                for row in scored[:limit]]


def _cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _vector(text):
    """Tiny deterministic embedding: one hot axis per keyword."""
    vector = [0.0] * 512
    for index, keyword in enumerate(("氨氯地平", "剂量", "随访", "副作用")):
        if keyword in text:
            vector[index] = 1.0
    if not any(vector):
        vector[511] = 1.0
    return vector


def _embed(texts):
    return [_vector(text) for text in texts]


def _history(store=None, top_k=3, scope="conversation", now=None, half_life_days=7.0):
    return ConversationHistory(store=store or FakeStore(), embed=_embed, top_k=top_k,
                               scope=scope, half_life_days=half_life_days, now=now)


# -- writes -----------------------------------------------------------------

def test_one_row_per_turn_with_a_stable_id():
    store = FakeStore()
    history = _history(store)

    assert history.index_turn(CONVERSATION, 7, "氨氯地平怎么吃", "每天一次")
    assert history.index_turn(CONVERSATION, 7, "氨氯地平怎么吃", "每天一次")

    assert len(store.rows) == 1
    row = next(iter(store.rows.values()))
    assert row["id"] == turn_id(CONVERSATION, 7)
    assert row["turn_id"] == 7
    assert "问：氨氯地平怎么吃" in row["text"] and "答：每天一次" in row["text"]


def test_unusable_turns_are_not_indexed():
    store = FakeStore()
    history = _history(store)

    assert not history.index_turn(CONVERSATION, 1, "", "答")
    assert not history.index_turn(CONVERSATION, 2, "问", "   ")
    assert not history.index_turn("bad conversation id", 3, "问", "答")
    assert store.rows == {}


def test_index_without_a_store_is_disabled():
    history = ConversationHistory(store=None, embed=_embed)

    assert history.enabled is False
    assert not history.index_turn(CONVERSATION, 1, "问", "答")
    assert history.recall(CONVERSATION, "氨氯地平") == []


# -- recall -----------------------------------------------------------------

def test_recall_ranks_within_the_same_conversation_only():
    store = FakeStore()
    history = _history(store, top_k=2)
    history.index_turn(CONVERSATION, 1, "氨氯地平怎么吃", "每天一次")
    history.index_turn(CONVERSATION, 2, "随访频率", "每三个月")
    history.index_turn("another-conversation", 3, "氨氯地平副作用", "头晕")

    rows = history.recall(CONVERSATION, "氨氯地平")

    assert [row["turn"] for row in rows] == [1]
    assert all("another-conversation" not in row["text"] for row in rows)
    assert store.searches[0][0] == CONVERSATION


def test_recall_honours_top_k():
    store = FakeStore()
    history = _history(store, top_k=1)
    history.index_turn(CONVERSATION, 1, "氨氯地平剂量", "每天一次")
    history.index_turn(CONVERSATION, 2, "氨氯地平副作用", "头晕")

    assert len(history.recall(CONVERSATION, "氨氯地平副作用")) == 1


def test_blank_seed_never_reaches_the_index():
    store = FakeStore()
    history = _history(store)
    history.index_turn(CONVERSATION, 1, "氨氯地平", "每天一次")

    assert history.recall(CONVERSATION, "   ") == []
    assert store.searches == []


# -- cross-session scope and recency ----------------------------------------

def test_global_scope_recalls_other_conversations():
    store = FakeStore()
    now = 1_000 * 86_400.0
    history = _history(store, scope="global", now=lambda: now)
    history.index_turn(CONVERSATION, 1, "氨氯地平剂量", "每天一次")
    history.index_turn("other-conversation", 2, "氨氯地平副作用", "头晕")

    rows = history.recall(CONVERSATION, "氨氯地平")

    assert {row["turn"] for row in rows} == {1, 2}
    # 全局模式不传会话过滤，Milvus 侧才可能跨会话命中。
    assert store.searches[-1][0] is None


def test_global_scope_decays_older_turns():
    store = FakeStore()
    now = 1_000 * 86_400.0
    history = _history(store, scope="global", now=lambda: now)
    store.rows["old"] = {"id": "old", "conversation_id": CONVERSATION, "turn_id": 1,
                         "text": "旧的氨氯地平讨论", "created_at": int(now - 30 * 86_400),
                         "dense": _vector("氨氯地平")}
    store.rows["new"] = {"id": "new", "conversation_id": CONVERSATION, "turn_id": 2,
                         "text": "新的氨氯地平讨论", "created_at": int(now - 86_400),
                         "dense": _vector("氨氯地平")}

    rows = history.recall(CONVERSATION, "氨氯地平")

    # 30 天前的同类讨论衰减到门限以下，只留下最近的一条。
    assert [row["turn"] for row in rows] == [2]


def test_conversation_scope_still_filters():
    store = FakeStore()
    history = _history(store)
    history.index_turn(CONVERSATION, 1, "氨氯地平剂量", "每天一次")
    history.index_turn("other-conversation", 2, "氨氯地平副作用", "头晕")

    rows = history.recall(CONVERSATION, "氨氯地平")

    assert [row["turn"] for row in rows] == [1]
    assert store.searches[-1][0] == CONVERSATION


def test_legacy_rows_without_a_timestamp_keep_half_weight():
    store = FakeStore()
    now = 1_000 * 86_400.0
    history = _history(store, scope="global", now=lambda: now)
    store.rows["legacy"] = {"id": "legacy", "conversation_id": CONVERSATION, "turn_id": 1,
                            "text": "没有时间戳的旧行", "created_at": 0,
                            "dense": _vector("氨氯地平")}

    assert [row["turn"] for row in history.recall(CONVERSATION, "氨氯地平")] == [1]


# -- cascade ----------------------------------------------------------------

def test_delete_turns_removes_only_that_conversation():
    store = FakeStore()
    history = _history(store)
    history.index_turn(CONVERSATION, 1, "氨氯地平", "每天一次")
    history.index_turn("other-conversation", 2, "随访", "每三个月")

    assert history.delete_turns(CONVERSATION) is True

    assert store.deleted == [CONVERSATION]
    assert [row["conversation_id"] for row in store.rows.values()] == ["other-conversation"]


def test_delete_turns_rejects_unsafe_ids():
    store = FakeStore()
    history = _history(store)

    assert history.delete_turns("bad id") is False
    assert store.deleted == []


def test_indexer_delete_delegates_to_the_index():
    store = FakeStore()
    history = _history(store)
    history.index_turn(CONVERSATION, 1, "氨氯地平", "每天一次")
    indexer = ConversationIndexer(lambda: history)

    assert indexer.delete(CONVERSATION) is True
    assert store.deleted == [CONVERSATION]
    assert ConversationIndexer(None, enabled=False).delete(CONVERSATION) is False


# -- background writer ------------------------------------------------------

def test_indexer_writes_in_the_background():
    store = FakeStore()
    history = _history(store)
    indexer = ConversationIndexer(lambda: history)

    indexer.maybe_schedule(CONVERSATION, 1, "氨氯地平怎么吃", "每天一次")

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not store.rows:
        time.sleep(0.01)
    assert len(store.rows) == 1


def test_indexer_swallows_factory_and_store_failures():
    def broken():
        raise RuntimeError("milvus down")

    indexer = ConversationIndexer(broken)
    indexer.maybe_schedule(CONVERSATION, 1, "问", "答")

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and indexer._thread.is_alive():
        time.sleep(0.01)
    assert not indexer._thread.is_alive()


def test_indexer_is_disabled_when_asked(tmp_path):
    calls = []
    indexer = ConversationIndexer(lambda: calls.append(1), enabled=False)

    indexer.maybe_schedule(CONVERSATION, 1, "问", "答")

    assert indexer._thread is None
    assert calls == []
