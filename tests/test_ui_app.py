"""Smoke test for the Streamlit viewer; skipped when the optional extra is absent."""

import json

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from medical_rag.config import Settings  # noqa: E402
from medical_rag.ui import data, launcher  # noqa: E402
from medical_rag.ui.state import PAGE_KEY, UiStateStore  # noqa: E402


def _app() -> AppTest:
    # Each AppTest starts a fresh script run; data caches are keyed by their
    # arguments, so environment changes in a test are picked up as new entries.
    return AppTest.from_file(str(launcher.APP_PATH), default_timeout=60)


def _by_key(elements, key):
    return next(item for item in elements if item.key == key)


def _report_file(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "medical-health-v2.json"
    path.write_text(json.dumps({
        "dataset_version": "v2", "summary": {"precision": 0.6, "recall": 0.5},
        "config": {"answer_provider": "qwen", "judge_model": "qwen-max"},
        "cases": [{"id": "q1", "question": "问题", "category": "diet",
                   "answerability": "answerable", "abstained": False,
                   "retrieval": {"recall": 0.5}}],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_viewer_renders_overview():
    at = _app()
    at.run()
    assert not at.exception
    assert any(item.value == "概览" for item in at.title)
    assert at.sidebar.radio[0].options == ["概览", "会话历史", "文档任务", "评测报告", "界面笔记"]
    assert len(at.metric) == 5


def test_empty_artifact_directories_show_empty_states(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_FILE", str(tmp_path / "none.sqlite3"))
    monkeypatch.setenv("UI_REPORT_DIRS", str(tmp_path / "reports"))
    at = _app()
    at.run()
    assert not at.exception
    assert any("会话库不存在" in item.value for item in at.caption)
    assert any("还没有持久化的会话" in item.value for item in at.info)


def test_page_choice_is_persisted_for_the_next_process(tmp_path, monkeypatch):
    state_file = tmp_path / "persisted-ui-state.sqlite3"
    monkeypatch.setenv("UI_STATE_FILE", str(state_file))
    at = _app()
    at.run()
    assert not at.exception

    at.sidebar.radio[0].set_value("界面笔记").run()
    assert not at.exception
    assert any(item.value == "界面笔记" for item in at.title)
    assert UiStateStore(state_file).get(PAGE_KEY) == "界面笔记"

    # A fresh app run (new process equivalent) restores the stored page.
    restarted = _app()
    restarted.run()
    assert not restarted.exception
    assert any(item.value == "界面笔记" for item in restarted.title)


def test_notes_can_be_saved_from_the_viewer(tmp_path, monkeypatch):
    state_file = tmp_path / "notes-ui-state.sqlite3"
    report_dir = tmp_path / "reports"
    _report_file(report_dir)
    monkeypatch.setenv("UI_STATE_FILE", str(state_file))
    monkeypatch.setenv("UI_REPORT_DIRS", str(report_dir))

    at = _app()
    at.run()
    at.sidebar.radio[0].set_value("界面笔记").run()
    assert not at.exception

    _by_key(at.radio, "note-target-kind").set_value("评测报告").run()
    assert not at.exception
    _by_key(at.text_area, "notes-page-new-body").set_value("需要复核的报告")
    _by_key(at.button, "notes-page-save").click().run()
    assert not at.exception

    notes = UiStateStore(state_file).list_notes()
    assert [note["body"] for note in notes] == ["需要复核的报告"]
    assert notes[0]["target_kind"] == "report"
    assert notes[0]["target_id"].endswith("medical-health-v2.json")


def test_report_page_handles_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("UI_REPORT_DIRS", str(tmp_path / "nowhere"))
    at = _app()
    at.run()
    at.sidebar.radio[0].set_value("评测报告").run()
    assert not at.exception
    assert any("没有找到评测报告" in item.value for item in at.info)


def test_report_page_shows_persisted_report(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    _report_file(report_dir)
    monkeypatch.setenv("UI_REPORT_DIRS", str(report_dir))
    at = _app()
    at.run()
    at.sidebar.radio[0].set_value("评测报告").run()
    assert not at.exception
    assert any(item.label == "检索 Precision" for item in at.metric)
    assert any(len(item.value) == 1 for item in at.dataframe)


def test_viewer_uses_isolated_state_file():
    configured = Settings().ui_state_file
    assert configured != str(data.project_path("data/streamlit/ui-state.sqlite3"))
