"""Launcher option handling; the actual Streamlit process is not started here."""

import argparse

import pytest

from medical_rag.config import Settings
from medical_rag.ui import launcher


def test_defaults_match_settings_defaults():
    assert Settings.model_fields["ui_state_file"].default == launcher.DEFAULT_STATE_FILE
    assert Settings.model_fields["ui_report_dirs"].default == launcher.DEFAULT_REPORT_DIRS
    assert Settings.model_fields["chat_history_file"].default == launcher.DEFAULT_HISTORY_FILE


def test_parser_defaults_are_loopback_only():
    args = launcher.parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8501
    assert args.state_file is None
    assert args.history_file is None
    assert args.reports_dir is None
    assert args.no_browser is False
    launcher.validate_options(args)


def test_validate_rejects_non_loopback_host_and_bad_port():
    with pytest.raises(ValueError):
        launcher.validate_options(argparse.Namespace(
            host="0.0.0.0", port=8501, state_file=None, history_file=None,
            reports_dir=None, no_browser=False))
    with pytest.raises(ValueError):
        launcher.validate_options(argparse.Namespace(
            host="192.168.1.10", port=8501, state_file=None, history_file=None,
            reports_dir=None, no_browser=False))
    for port in (0, 65536, -1):
        with pytest.raises(ValueError):
            launcher.validate_options(argparse.Namespace(
                host="127.0.0.1", port=port, state_file=None, history_file=None,
                reports_dir=None, no_browser=False))
    with pytest.raises(ValueError):
        launcher.validate_options(argparse.Namespace(
            host="127.0.0.1", port=8501, state_file=" ", history_file=None,
            reports_dir=None, no_browser=False))
    with pytest.raises(ValueError):
        launcher.validate_options(argparse.Namespace(
            host="127.0.0.1", port=8501, state_file=None, history_file=None,
            reports_dir=["  "], no_browser=False))


def test_environment_overrides_are_optional_and_comma_joined(tmp_path):
    args = launcher.parser().parse_args([
        "--state-file", str(tmp_path / "state.sqlite3"),
        "--history-file", str(tmp_path / "history.sqlite3"),
        "--reports-dir", "outputs/reports",
        "--reports-dir", "data/reports",
        "--no-browser",
    ])
    assert launcher.environment_overrides(args) == {
        "UI_STATE_FILE": str(tmp_path / "state.sqlite3"),
        "CHAT_HISTORY_FILE": str(tmp_path / "history.sqlite3"),
        "UI_REPORT_DIRS": "outputs/reports,data/reports",
    }
    plain = launcher.parser().parse_args([])
    assert launcher.environment_overrides(plain) == {}


def test_streamlit_args_run_the_app_headless_without_watcher():
    args = launcher.build_streamlit_args("127.0.0.1", 8600)
    assert args[:3] == ["streamlit", "run", str(launcher.APP_PATH)]
    assert args[args.index("--server.address") + 1] == "127.0.0.1"
    assert args[args.index("--server.port") + 1] == "8600"
    assert args[args.index("--server.headless") + 1] == "true"
    assert args[args.index("--server.fileWatcherType") + 1] == "none"
    assert args[args.index("--browser.gatherUsageStats") + 1] == "false"


def test_main_reports_invalid_host_without_starting_streamlit(capsys):
    code = launcher.main(["--host", "10.0.0.5"])
    captured = capsys.readouterr()
    assert code == 2
    assert "回环" in captured.err


def test_app_path_exists_and_is_the_streamlit_script():
    assert launcher.APP_PATH.is_file()
    assert launcher.APP_PATH.name == "app.py"
