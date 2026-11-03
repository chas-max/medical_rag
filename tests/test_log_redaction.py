"""Sensitive dependency exceptions must never be formatted into application logs."""

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from medical_rag import embeddings
from medical_rag.config import Settings
from medical_rag.embeddings import BgeReranker
from medical_rag.settings_store import ModelSettings, SettingsStore
from medical_rag.tokenization import TokenCounter, estimate_tokens
from medical_rag.types import SCORE_LEXICAL

FAKE_QUESTION = "fictional-question-secret"
FAKE_RECORD = "fictional-patient-record-secret"
FAKE_TOKEN = "fictional-model-token-secret"
FAKE_PATH = "fictional-private-path-secret"
FAKE_DEVICE = "fictional-device-secret"
SENSITIVE = (FAKE_QUESTION, FAKE_RECORD, FAKE_TOKEN, FAKE_PATH, FAKE_DEVICE)


def assert_no_sensitive_logs(caplog):
    assert caplog.records
    for secret in SENSITIVE:
        assert secret not in caplog.text
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)


def test_reranker_failure_logs_no_query_patient_record_or_token(caplog):
    class Broken:
        def predict(self, pairs):
            raise RuntimeError(f"{pairs[0][0]} {pairs[0][1]} {FAKE_TOKEN}")

    caplog.set_level(logging.INFO)
    scores, kind = BgeReranker(Broken()).score_with_kind(FAKE_QUESTION, [FAKE_RECORD])
    assert kind == SCORE_LEXICAL and len(scores) == 1
    assert_no_sensitive_logs(caplog)


@pytest.mark.parametrize("fails_first", [True, False])
def test_model_loading_logs_no_device_name_or_raw_failure(monkeypatch, caplog, fails_first):
    monkeypatch.setattr(embeddings, "resolve_device", lambda: FAKE_DEVICE)
    calls = []
    model = object()

    def load(device):
        calls.append(device)
        if fails_first and len(calls) == 1:
            raise OSError(f"{FAKE_PATH} {FAKE_TOKEN} {FAKE_RECORD}")
        return model

    caplog.set_level(logging.INFO)
    loaded, device = embeddings._load_on_best_device(load)
    assert loaded is model
    assert device == ("cpu" if fails_first else FAKE_DEVICE)
    assert calls == ([FAKE_DEVICE, "cpu"] if fails_first else [FAKE_DEVICE])
    assert_no_sensitive_logs(caplog)


def test_tokenizer_count_failure_logs_no_text_or_token(caplog):
    class Broken:
        def encode(self, text, **kwargs):
            raise RuntimeError(f"{text} {FAKE_RECORD} {FAKE_TOKEN}")

    caplog.set_level(logging.INFO)
    counter = TokenCounter(tokenizer=Broken())
    assert counter.count(FAKE_QUESTION) == estimate_tokens(FAKE_QUESTION)
    assert_no_sensitive_logs(caplog)


def test_tokenizer_load_failure_logs_no_path_or_model_name(monkeypatch, caplog):
    class Broken:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            raise OSError(f"{model_name} {FAKE_PATH} {FAKE_TOKEN}")

    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=Broken))
    caplog.set_level(logging.INFO)
    counter = TokenCounter(model_name=FAKE_RECORD)
    assert counter.count(FAKE_QUESTION) == estimate_tokens(FAKE_QUESTION)
    assert_no_sensitive_logs(caplog)


def test_settings_read_os_error_does_not_log_path_or_exception(monkeypatch, tmp_path, caplog):
    def unreadable(self, *args, **kwargs):
        raise OSError(f"{self} {FAKE_TOKEN} {FAKE_RECORD}")

    monkeypatch.setattr(Path, "read_text", unreadable)
    caplog.set_level(logging.INFO)
    assert SettingsStore(tmp_path / FAKE_PATH).read() == {}
    assert_no_sensitive_logs(caplog)


def test_settings_read_json_error_does_not_log_error_description(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "{}")

    def bad_json(*args, **kwargs):
        raise json.JSONDecodeError(f"{FAKE_TOKEN} {FAKE_RECORD}", "{}", 0)

    monkeypatch.setattr(json, "loads", bad_json)
    caplog.set_level(logging.INFO)
    assert SettingsStore(tmp_path / FAKE_PATH).read() == {}
    assert_no_sensitive_logs(caplog)


def test_settings_read_invalid_utf8_safely_falls_back_without_logging_bytes(tmp_path, caplog):
    path = tmp_path / FAKE_PATH
    path.write_bytes(b"\xff" + FAKE_TOKEN.encode("utf-8"))
    caplog.set_level(logging.INFO)
    assert SettingsStore(path).read() == {}
    assert_no_sensitive_logs(caplog)


def test_invalid_model_settings_never_logs_validation_input_or_profile_id(tmp_path, caplog):
    class FakeStore:
        def section(self, name):
            return {"profiles": [
                {"id": "invalid", "model_name": "m", "base_url": "https://example.invalid",
                 "api_key": {"k": FAKE_TOKEN}},
                {"id": FAKE_PATH, "model_name": "m", "base_url": "https://example.invalid", "api_key": ""},
            ]}

    caplog.set_level(logging.INFO)
    settings = Settings(_env_file=None, settings_file=str(tmp_path / "settings.json"))
    model_settings = ModelSettings(settings, store=FakeStore())
    assert model_settings.apply_saved() is False
    assert model_settings.profiles == []
    assert_no_sensitive_logs(caplog)
