"""Durable viewer state and tenant-scoped conversation map."""

import json
import sqlite3

import pytest

from medical_rag.settings_store import PROJECT_ROOT
from medical_rag.ui.state import (
    PAGE_KEY,
    UiStateStore,
    UiStoreError,
    new_conversation_id,
)


def test_values_survive_a_new_store_instance(tmp_path):
    path = tmp_path / "state" / "ui-state.sqlite3"
    first = UiStateStore(path)
    first.set(PAGE_KEY, "数据统计")
    first.set("ui.custom", {"tenant": "a", "pinned": True})

    second = UiStateStore(path)
    assert second.get(PAGE_KEY) == "数据统计"
    assert second.get("ui.custom") == {"tenant": "a", "pinned": True}
    assert path.is_file()


def test_missing_key_returns_default_and_snapshot_is_json_shaped(tmp_path):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    assert store.get("ui.missing") is None
    assert store.get("ui.missing", "fallback") == "fallback"
    store.set(PAGE_KEY, "智能问答")
    assert store.snapshot() == {PAGE_KEY: "智能问答"}
    assert json.loads(json.dumps(store.snapshot())) == {PAGE_KEY: "智能问答"}


def test_delete_and_invalid_values_are_rejected(tmp_path):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    store.set(PAGE_KEY, "智能问答")
    assert store.delete(PAGE_KEY) is True
    assert store.delete(PAGE_KEY) is False
    with pytest.raises(ValueError):
        store.set(PAGE_KEY, object())
    with pytest.raises(ValueError):
        store.set(PAGE_KEY, "x" * 100_000)
    with pytest.raises(ValueError):
        store.set("", "value")
    with pytest.raises(ValueError):
        store.get("")


def test_corrupt_state_row_degrades_to_default(tmp_path):
    path = tmp_path / "ui-state.sqlite3"
    store = UiStateStore(path)
    store.set(PAGE_KEY, "智能问答")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE ui_state SET value = '{broken' WHERE key = ?", (PAGE_KEY,))
        connection.commit()
    assert UiStateStore(path).get(PAGE_KEY, "fallback") == "fallback"


def test_page_helpers_default_when_unset_or_invalid(tmp_path):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    assert store.get_page("智能问答") == "智能问答"
    store.set_page("数据统计")
    assert store.get_page("智能问答") == "数据统计"
    store.set(PAGE_KEY, 123)
    assert store.get_page("智能问答") == "智能问答"
    with pytest.raises(ValueError):
        store.set_page("   ")


def test_conversation_ids_are_tenant_namespaced_and_unique():
    first = new_conversation_id("tenant-abcdef123456")
    second = new_conversation_id("tenant-abcdef123456")
    assert first.startswith("tenant-abc")
    assert first != second
    with pytest.raises(ValueError):
        new_conversation_id("")


def test_chat_sessions_are_tenant_scoped(tmp_path, monkeypatch):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    now = {"value": 100.0}
    monkeypatch.setattr("medical_rag.ui.state.time.time", lambda: now["value"])
    store.remember_session("tenant-a", "tenant-a-conv-1", "糖尿病饮食")
    now["value"] = 200.0
    store.remember_session("tenant-a", "tenant-a-conv-2", "高血压")
    now["value"] = 300.0
    store.remember_session("tenant-b", "tenant-b-conv-1", "另一租户")

    sessions = store.list_sessions("tenant-a")
    assert [item["conversation_id"] for item in sessions] == [
        "tenant-a-conv-2", "tenant-a-conv-1"]
    assert sessions[0]["title"] == "高血压"
    assert store.list_sessions("tenant-b")[0]["conversation_id"] == "tenant-b-conv-1"
    assert store.owned_conversation_ids("tenant-a") == {
        "tenant-a-conv-1", "tenant-a-conv-2"}
    assert store.conversation_title("tenant-a", "tenant-a-conv-1") == "糖尿病饮食"
    assert store.conversation_title("tenant-b", "tenant-a-conv-1") is None

    assert store.forget_session("tenant-a", "tenant-a-conv-1") is True
    assert store.forget_session("tenant-a", "tenant-a-conv-1") is False
    assert store.owned_conversation_ids("tenant-a") == {"tenant-a-conv-2"}


def test_remember_session_updates_title_without_duplicating(tmp_path):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    store.remember_session("tenant-a", "conv-1", "新对话")
    store.remember_session("tenant-a", "conv-1", "糖尿病能吃水果吗")
    sessions = store.list_sessions("tenant-a")
    assert len(sessions) == 1
    assert sessions[0]["title"] == "糖尿病能吃水果吗"


def test_last_conversation_is_per_tenant(tmp_path):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    assert store.get_last_conversation("tenant-a") is None
    store.set_last_conversation("tenant-a", "conv-a")
    store.set_last_conversation("tenant-b", "conv-b")
    assert store.get_last_conversation("tenant-a") == "conv-a"
    assert store.get_last_conversation("tenant-b") == "conv-b"
    with pytest.raises(ValueError):
        store.remember_session("", "conv-1", "标题")


def test_relative_state_path_hangs_off_project_root():
    store = UiStateStore("data/streamlit/ui-state.sqlite3")
    assert store.path == PROJECT_ROOT / "data" / "streamlit" / "ui-state.sqlite3"


def test_unwritable_path_raises_store_error(tmp_path):
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    store = UiStateStore(directory)
    with pytest.raises(UiStoreError):
        store.get(PAGE_KEY)
