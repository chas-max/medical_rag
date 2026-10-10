import json
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.api import create_app
from medical_rag.config import Settings
from medical_rag.generation import OpenAITransport
from medical_rag.settings_store import ModelSettings, SettingsStore
from medical_rag.types import ApiError

FIRST = {"model_name": "qwen3.8-flash", "base_url": "https://model.example/v1", "api_key": "sk-first-1234"}
SECOND = {"model_name": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-second-5678"}
FIRST_ID = "qwen3-8-flash"
SOURCE = "medical_rag.generation.verify_connection"
SETTINGS_FILE = "settings.json"


def probe(recorder):
    """Stand in for the real endpoint call, recording what the confirmation asked about."""

    def verify(model, base_url, api_key, client_factory=None):
        recorder.append((model, base_url, api_key))

    return verify


def failing(recorder, code, message):
    def verify(model, base_url, api_key, client_factory=None):
        recorder.append((model, base_url, api_key))
        raise ApiError(code, message, 422)

    return verify


def api_for(tmp_path, monkeypatch, api_key=""):
    """A public API whose settings file and environment are pinned to this test only."""
    monkeypatch.setenv("SETTINGS_FILE", str(tmp_path / SETTINGS_FILE))
    monkeypatch.setenv("QWEN_API_KEY", api_key)
    monkeypatch.setenv("QWEN_MODEL", "env-model")
    monkeypatch.setenv("QWEN_BASE_URL", "https://env.example/v1")
    return TestClient(create_app(service=object()))


def stored(tmp_path):
    return json.loads((tmp_path / SETTINGS_FILE).read_text(encoding="utf-8"))["model"]


def test_incomplete_form_is_rejected_without_calling_the_endpoint(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(SOURCE, probe(calls))
    response = api_for(tmp_path, monkeypatch).post("/settings/model", json={**FIRST, "api_key": "  "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_CONFIG_INCOMPLETE"
    assert calls == []


def test_base_url_without_scheme_is_rejected_without_calling_the_endpoint(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(SOURCE, probe(calls))
    response = api_for(tmp_path, monkeypatch).post(
        "/settings/model", json={**FIRST, "base_url": "model.example/v1"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_CONFIG_INVALID_URL"
    assert calls == []


def test_unreachable_endpoint_keeps_the_running_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(SOURCE, probe([]))
    api = api_for(tmp_path, monkeypatch)
    api.post("/settings/model", json=FIRST)
    calls = []
    monkeypatch.setattr(SOURCE, failing(calls, "MODEL_CONFIG_UNREACHABLE", "无法连接该 Base URL"))
    assert api.post("/settings/model", json=SECOND).status_code == 422
    data = api.get("/settings/model").json()["data"]
    assert data["model_name"] == FIRST["model_name"]
    assert [item["id"] for item in data["profiles"]] == [FIRST_ID]
    assert calls == [(SECOND["model_name"], SECOND["base_url"], SECOND["api_key"])]


def test_confirmation_stores_a_profile_and_switches_to_it(monkeypatch, tmp_path):
    monkeypatch.setattr(SOURCE, probe([]))
    data = api_for(tmp_path, monkeypatch).post("/settings/model", json=FIRST).json()["data"]
    assert data["active_id"] == FIRST_ID
    assert data["model_name"] == FIRST["model_name"]
    assert data["source"] == "settings"
    assert data["profiles"][0]["api_key_masked"] == "••••1234"
    assert "api_key" not in data["profiles"][0]
    assert data["fallback"] == {"model_name": "env-model", "base_url": "https://env.example/v1",
                                "configured": False}
    assert stored(tmp_path)["profiles"][0]["api_key"] == FIRST["api_key"]


def test_profile_id_is_url_safe_and_unique_for_repeated_names(monkeypatch, tmp_path):
    monkeypatch.setattr(SOURCE, probe([]))
    api = api_for(tmp_path, monkeypatch)
    one = api.post("/settings/model", json={**FIRST, "model_name": "GPT-4o mini"}).json()["data"]
    two = api.post("/settings/model", json={**FIRST, "model_name": "GPT-4o mini",
                                            "base_url": "https://other.example/v1"}).json()["data"]
    assert [item["id"] for item in two["profiles"]] == ["gpt-4o-mini", "gpt-4o-mini-2"]
    assert two["active_id"] == "gpt-4o-mini-2"
    assert one["profiles"][0]["model_name"] == "GPT-4o mini"


def test_same_model_and_address_updates_the_entry_instead_of_duplicating(monkeypatch, tmp_path):
    monkeypatch.setattr(SOURCE, probe([]))
    api = api_for(tmp_path, monkeypatch)
    api.post("/settings/model", json=FIRST)
    api.post("/settings/model", json=SECOND)
    rotated = api.post("/settings/model", json={**FIRST, "api_key": "sk-rotated-9999"}).json()["data"]
    assert [item["model_name"] for item in rotated["profiles"]] == [FIRST["model_name"],
                                                                    SECOND["model_name"]]
    assert rotated["active_id"] == FIRST_ID
    assert rotated["profiles"][0]["api_key_masked"] == "••••9999"
    assert stored(tmp_path)["profiles"][0]["api_key"] == "sk-rotated-9999"


def test_switching_is_instant_and_never_probes(monkeypatch, tmp_path):
    monkeypatch.setattr(SOURCE, probe([]))
    api = api_for(tmp_path, monkeypatch)
    api.post("/settings/model", json=FIRST)
    api.post("/settings/model", json=SECOND)
    calls = []
    monkeypatch.setattr(SOURCE, probe(calls))
    data = api.post("/settings/model/select", json={"profile_id": FIRST_ID}).json()["data"]
    assert data["model_name"] == FIRST["model_name"]
    assert [item["active"] for item in data["profiles"]] == [True, False]
    assert calls == []
    assert stored(tmp_path)["active"] == FIRST_ID


def test_unknown_profile_is_reported_for_switch_and_removal(monkeypatch, tmp_path):
    monkeypatch.setattr(SOURCE, probe([]))
    api = api_for(tmp_path, monkeypatch)
    missing = api.post("/settings/model/select", json={"profile_id": "missing"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "MODEL_PROFILE_NOT_FOUND"
    assert api.delete("/settings/model/missing").json()["error"]["code"] == "MODEL_PROFILE_NOT_FOUND"


def test_removing_the_running_profile_falls_back_to_the_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(SOURCE, probe([]))
    api = api_for(tmp_path, monkeypatch, api_key="env-key")
    api.post("/settings/model", json=FIRST)
    api.post("/settings/model", json=SECOND)
    removed = api.delete("/settings/model/deepseek-chat").json()["data"]
    # Deleting the running model never selects a different vendor by itself: control returns
    # to `.env` and the stored profiles stay one click away.
    assert removed["model_name"] == "env-model"
    assert removed["active_id"] is None
    assert removed["source"] == "env"
    assert [item["id"] for item in removed["profiles"]] == [FIRST_ID]
    last = api.delete(f"/settings/model/{FIRST_ID}").json()["data"]
    assert last["profiles"] == []
    assert last["api_key_masked"] == "••••"
    assert stored(tmp_path) == {"active": None, "profiles": []}


def test_selection_survives_a_restart_without_a_new_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(SOURCE, probe([]))
    api = api_for(tmp_path, monkeypatch)
    api.post("/settings/model", json=FIRST)
    api.post("/settings/model", json=SECOND)
    api.post("/settings/model/select", json={"profile_id": FIRST_ID})
    calls = []
    monkeypatch.setattr(SOURCE, failing(calls, "MODEL_CONFIG_UNREACHABLE", "不应在启动时探活"))
    restarted = api_for(tmp_path, monkeypatch).get("/settings/model").json()["data"]
    assert restarted["model_name"] == FIRST["model_name"]
    assert restarted["active_id"] == FIRST_ID
    assert len(restarted["profiles"]) == 2
    assert calls == []


def test_unreadable_settings_file_falls_back_to_the_environment(monkeypatch, tmp_path):
    (tmp_path / SETTINGS_FILE).write_text("{ not json", encoding="utf-8")
    data = api_for(tmp_path, monkeypatch).get("/settings/model").json()["data"]
    assert data["model_name"] == "env-model"
    assert data["configured"] is False
    assert data["profiles"] == []


def test_incomplete_settings_entry_is_skipped_not_fatal(monkeypatch, tmp_path):
    (tmp_path / SETTINGS_FILE).write_text(json.dumps({"version": 1, "model": {"active": "kept", "profiles": [
        {"id": "kept", "model_name": "kept-model", "base_url": "https://kept.example/v1",
         "api_key": "sk-kept-1234"}, {"id": "half", "model_name": "half", "base_url": ""},
        {"id": 7, "model_name": "wrong type", "base_url": "https://x/v1", "api_key": "k"}]}}),
        encoding="utf-8")
    data = api_for(tmp_path, monkeypatch).get("/settings/model").json()["data"]
    assert [item["id"] for item in data["profiles"]] == ["kept"]
    assert data["model_name"] == "kept-model"


def test_transport_reads_whichever_profile_is_active(monkeypatch, tmp_path):
    monkeypatch.setattr(SOURCE, probe([]))
    settings = Settings(_env_file=None, qwen_api_key="old-key", qwen_base_url="https://old.invalid/v1",
                        qwen_model="old-model", settings_file=str(tmp_path / SETTINGS_FILE))
    model = ModelSettings(settings, SettingsStore(settings.settings_file))
    model.confirm(**FIRST)
    model.confirm(**SECOND)

    seen = {}

    class Completions:
        def create(self, **kwargs):
            seen["request"] = kwargs
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))])

    def client_factory(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    # The transport keeps the same Settings instance, so the active profile is what it reads.
    assert OpenAITransport(settings, client_factory=client_factory).complete("qwen", "s", "u") == "OK"
    assert seen["api_key"] == SECOND["api_key"]
    assert seen["base_url"] == SECOND["base_url"]
    model.select(FIRST_ID)
    assert OpenAITransport(settings, client_factory=client_factory).complete("qwen", "s", "u") == "OK"
    assert seen["api_key"] == FIRST["api_key"]
    assert seen["request"]["model"] == FIRST["model_name"]
