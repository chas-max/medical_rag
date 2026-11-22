"""Durable UI state keeps the viewer's view across process restarts."""

import json
import sqlite3

import pytest

from medical_rag.ui.state import (
    CONVERSATION_KEY,
    PAGE_KEY,
    REPORT_KEY,
    UiStateError,
    UiStateStore,
)


def test_values_survive_a_new_store_instance(tmp_path):
    path = tmp_path / "state" / "ui-state.sqlite3"
    first = UiStateStore(path)
    first.set(PAGE_KEY, "评测报告")
    first.set(CONVERSATION_KEY, {"conversation_id": "c-1", "pinned": True})

    second = UiStateStore(path)
    assert second.get(PAGE_KEY) == "评测报告"
    assert second.get(CONVERSATION_KEY) == {"conversation_id": "c-1", "pinned": True}
    assert path.is_file()


def test_missing_key_returns_default_and_snapshot_is_json_shaped(tmp_path):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    assert store.get("ui.missing") is None
    assert store.get("ui.missing", "fallback") == "fallback"

    store.set(PAGE_KEY, "概览")
    store.set(REPORT_KEY, ["a", "b"])
    assert store.snapshot() == {PAGE_KEY: "概览", REPORT_KEY: ["a", "b"]}


def test_delete_and_reset_navigation_keep_notes(tmp_path):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    store.set(PAGE_KEY, "会话历史")
    store.set(CONVERSATION_KEY, "c-1")
    store.set(REPORT_KEY, "report.json")
    note = store.save_note("conversation", "c-1", "需要回看引用")

    assert store.delete(CONVERSATION_KEY) is True
    assert store.delete(CONVERSATION_KEY) is False

    store.reset_navigation()
    assert store.snapshot() == {}
    assert [item["note_id"] for item in store.list_notes()] == [note["note_id"]]


def test_invalid_json_state_row_degrades_to_default(tmp_path):
    path = tmp_path / "ui-state.sqlite3"
    store = UiStateStore(path)
    store.set(PAGE_KEY, "概览")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE ui_state SET value = '{broken' WHERE key = ?", (PAGE_KEY,))
        connection.commit()

    assert UiStateStore(path).get(PAGE_KEY, "fallback") == "fallback"


def test_non_serialisable_and_oversized_values_are_rejected(tmp_path):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    with pytest.raises(ValueError):
        store.set(PAGE_KEY, object())
    with pytest.raises(ValueError):
        store.set(PAGE_KEY, "x" * 100_000)
    with pytest.raises(ValueError):
        store.set("", "value")


def test_notes_roundtrip_update_filter_and_delete(tmp_path):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    first = store.save_note("conversation", "conv-1", "第一版")
    second = store.save_note("report", "/tmp/report.json", "报告观察")

    assert first["target_kind"] == "conversation"
    assert first["created"] <= first["updated"]

    updated = store.save_note("conversation", "conv-1", "第二版", note_id=first["note_id"])
    assert updated["body"] == "第二版"
    assert updated["note_id"] == first["note_id"]

    assert len(store.list_notes()) == 2
    assert [item["note_id"] for item in store.list_notes("conversation")] == [first["note_id"]]
    assert [item["note_id"] for item in store.list_notes(target_kind="report")] == [
        second["note_id"]]
    assert store.list_notes("conversation", "conv-1")[0]["body"] == "第二版"
    assert store.list_notes("conversation", "other") == []

    assert store.delete_note(first["note_id"]) is True
    assert store.delete_note(first["note_id"]) is False
    assert [item["note_id"] for item in store.list_notes()] == [second["note_id"]]


def test_note_validation_rejects_unknown_kind_empty_and_oversized_body(tmp_path):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    with pytest.raises(ValueError):
        store.save_note("patient-record", "x", "body")
    with pytest.raises(ValueError):
        store.save_note("conversation", "conv-1", "   ")
    with pytest.raises(ValueError):
        store.save_note("conversation", "conv-1", "x" * 5000)
    with pytest.raises(ValueError):
        store.save_note("conversation", "", "body")
    with pytest.raises(ValueError):
        store.list_notes("unknown")


def test_note_update_of_missing_id_raises(tmp_path):
    store = UiStateStore(tmp_path / "ui-state.sqlite3")
    with pytest.raises(UiStateError):
        store.save_note("conversation", "conv-1", "body", note_id="does-not-exist")


def test_relative_state_path_hangs_off_project_root():
    from medical_rag.settings_store import PROJECT_ROOT

    store = UiStateStore("data/streamlit/ui-state.sqlite3")
    assert store.path == PROJECT_ROOT / "data" / "streamlit" / "ui-state.sqlite3"


def test_snapshot_ignores_corrupt_payload_but_keeps_good_rows(tmp_path):
    path = tmp_path / "ui-state.sqlite3"
    store = UiStateStore(path)
    store.set(PAGE_KEY, "概览")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO ui_state(key, value, updated) VALUES ('ui.bad', '{oops', 0)")
        connection.commit()
    assert UiStateStore(path).snapshot() == {PAGE_KEY: "概览"}
    assert json.loads(json.dumps(UiStateStore(path).snapshot())) == {PAGE_KEY: "概览"}
