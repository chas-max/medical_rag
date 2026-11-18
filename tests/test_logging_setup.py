"""Loguru sinks must rotate, expire, compress and never leak credentials."""

import json
import logging
import os
import time
from pathlib import Path

import pytest
from loguru import logger

from medical_rag import logging_setup
from medical_rag.config import Settings


def build_settings(tmp_path: Path, **overrides) -> Settings:
    values = {"log_dir": str(tmp_path)}
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def reset_logging_state():
    logging_setup.reset_logging()
    yield
    logging_setup.reset_logging()


def messages_in_capture():
    captured: list[str] = []
    sink = logger.add(captured.append, level=0, format="{message}")
    return captured, sink


def flush_and_detach(sink: int) -> None:
    logging_setup.flush_logging()
    logger.remove(sink)


def test_settings_expose_logging_defaults(monkeypatch):
    monkeypatch.delenv("LOG_DIR", raising=False)
    monkeypatch.delenv("LOG_ENABLED", raising=False)
    settings = Settings(_env_file=None)
    assert settings.log_enabled is True
    assert settings.log_dir == "outputs/logs"
    assert settings.log_level == "INFO"
    assert settings.log_rotation == "20 MB"
    assert settings.log_retention == "14 days"
    assert settings.log_compression == "zip"
    assert settings.log_json is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1 KB", "1 KB"), ("20 MB", "20 MB"), ("00:00", "00:00"), ("1 day", "1 day")],
)
def test_rotation_accepts_size_and_time_expressions(value, expected):
    assert logging_setup.normalize_rotation(value) == expected


@pytest.mark.parametrize("value", ["", "  ", "banana", "20 XB", "../../etc", None])
def test_invalid_rotation_falls_back_to_default(value):
    assert logging_setup.normalize_rotation(value) == logging_setup.DEFAULT_ROTATION


@pytest.mark.parametrize(
    ("value", "expected"),
    [("14 days", "14 days"), ("1 week", "1 week"), ("100 MB", "100 MB")],
)
def test_retention_accepts_size_and_time_expressions(value, expected):
    assert logging_setup.normalize_retention(value) == expected


@pytest.mark.parametrize("value", ["forever", "0", "-1 day", "../secrets"])
def test_invalid_retention_falls_back_to_default(value):
    assert logging_setup.normalize_retention(value) == logging_setup.DEFAULT_RETENTION


@pytest.mark.parametrize(
    ("value", "expected"),
    [("zip", "zip"), ("gz", "gz"), ("", ""), (None, ""), ("rar", logging_setup.DEFAULT_COMPRESSION)],
)
def test_compression_is_limited_to_supported_archives(value, expected):
    assert logging_setup.normalize_compression(value) == expected


@pytest.mark.parametrize(("value", "expected"), [("debug", "DEBUG"), ("INFO", "INFO"), ("", "INFO"),
                                                 ("verbose", "INFO"), (None, "INFO")])
def test_level_is_normalized_to_a_loguru_level(value, expected):
    assert logging_setup.normalize_level(value) == expected


def test_sink_options_never_diagnose_or_backtrace():
    options = logging_setup.sink_options(level="INFO", json_output=False)
    assert options["diagnose"] is False
    assert options["backtrace"] is False
    assert options["enqueue"] is True
    assert options["catch"] is True
    assert options["encoding"] == "utf-8"
    assert options["rotation"] == logging_setup.DEFAULT_ROTATION
    assert options["retention"] == logging_setup.DEFAULT_RETENTION
    assert options["compression"] == logging_setup.DEFAULT_COMPRESSION
    assert options["serialize"] is False


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("token sk-abcdefgh12345678", "sk-abcdefgh12345678"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("postgresql://operator:hunter2@db.local/medical", "hunter2"),
        ("api_key=topsecretvalue", "topsecretvalue"),
        ("password: anothersecret", "anothersecret"),
    ],
)
def test_redaction_masks_credentials(raw, secret):
    cleaned = logging_setup.redact(raw)
    assert secret not in cleaned
    assert "***" in cleaned


def test_redaction_keeps_ordinary_medical_messages():
    message = "重排模型不可用，已启用降级评分"
    assert logging_setup.redact(message) == message


def test_text_file_sink_writes_rotated_capable_file(tmp_path):
    state = logging_setup.configure_logging("api", settings=build_settings(tmp_path))
    assert state.file_path is not None and state.file_path.parent == tmp_path / "api"
    logger.info("文档入库已开始")
    logging_setup.flush_logging()
    content = state.file_path.read_text(encoding="utf-8")
    assert "文档入库已开始" in content
    assert "INFO" in content


def test_json_sink_emits_one_object_per_line(tmp_path):
    state = logging_setup.configure_logging(
        "api", settings=build_settings(tmp_path, log_json=True)
    )
    logger.info("json-mode-message")
    logging_setup.flush_logging()
    lines = [line for line in state.file_path.read_text(encoding="utf-8").splitlines() if line]
    assert lines
    parsed = [json.loads(line) for line in lines]
    assert any(item["record"]["message"] == "json-mode-message" for item in parsed)
    assert all(item["record"]["level"]["name"] for item in parsed)


def test_rotation_compresses_archives(tmp_path):
    state = logging_setup.configure_logging(
        "api",
        settings=build_settings(tmp_path, log_rotation="1 KB", log_retention="10 days"),
    )
    for index in range(400):
        logger.info("payload {} {}", index, "x" * 200)
    logging_setup.flush_logging()
    archives = sorted(state.file_path.parent.glob("*.zip"))
    assert archives, "rotation must produce a compressed archive"
    assert state.file_path.exists()


def test_retention_deletes_expired_rotated_files(tmp_path):
    state = logging_setup.configure_logging(
        "api",
        settings=build_settings(
            tmp_path, log_rotation="1 KB", log_retention="1 second", log_compression=""
        ),
    )
    directory = state.file_path.parent
    for index in range(400):
        logger.info("payload {} {}", index, "y" * 200)
    logging_setup.flush_logging()
    rotated = [path for path in directory.glob("*.log") if path != state.file_path]
    assert rotated, "rotation must produce a second log file"
    expired = rotated[0]
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(expired, (old, old))
    for index in range(400):
        logger.info("payload {} {}", index, "z" * 200)
    logging_setup.flush_logging()
    assert not expired.exists()


def test_disabled_logging_keeps_console_only_and_writes_no_file(tmp_path):
    state = logging_setup.configure_logging(
        "api", settings=build_settings(tmp_path, log_enabled=False)
    )
    logger.info("console-only-message")
    logging_setup.flush_logging()
    assert state.file_path is None
    assert not list(tmp_path.rglob("*.log"))


def test_configure_logging_is_idempotent(tmp_path):
    state = logging_setup.configure_logging("api", settings=build_settings(tmp_path))
    sinks = logging_setup.active_sink_ids()
    again = logging_setup.configure_logging("api", settings=build_settings(tmp_path))
    assert again is state
    assert logging_setup.active_sink_ids() == sinks


def test_stdlib_third_party_records_are_intercepted(tmp_path):
    state = logging_setup.configure_logging("api", settings=build_settings(tmp_path))
    logging.getLogger("httpx").warning("upstream unavailable")
    logging_setup.flush_logging()
    content = state.file_path.read_text(encoding="utf-8")
    assert "upstream unavailable" in content
    assert "httpx" in content


def test_patient_logging_is_off_by_default(tmp_path):
    from medical_rag.security_config import PatientLoggingConfig

    sinks = logging_setup.active_sink_ids()
    state = logging_setup.configure_logging(
        "patient", patient=PatientLoggingConfig(enabled=False, directory=None)
    )
    assert state.file_path is None
    assert logging_setup.active_sink_ids() == sinks
    assert not list(tmp_path.rglob("*.log"))


def test_patient_logging_enabled_writes_fixed_messages_only(tmp_path):
    from medical_rag.security_config import PatientLoggingConfig

    logging_setup.silence_logging()
    state = logging_setup.configure_logging(
        "patient",
        patient=PatientLoggingConfig(
            enabled=True, directory=tmp_path, level="INFO", rotation="1 MB",
            retention="7 days", compression="zip", json_output=False,
        ),
    )
    assert state.file_path is not None
    logger.info("患者服务已启动")
    logging_setup.flush_logging()
    content = state.file_path.read_text(encoding="utf-8")
    assert "患者服务已启动" in content


def test_silence_logging_removes_every_sink():
    logger.info("before-silence")
    logging_setup.silence_logging()
    assert logging_setup.active_sink_ids() == ()
