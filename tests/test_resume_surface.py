import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from medical_rag.api import create_app
from medical_rag.chat_api import ChatRunRequest


def test_workflow_management_and_generic_execution_are_not_public():
    class Service:
        def nodes(self):
            return []

    client = TestClient(create_app(service=Service()))
    for path in ("/nodes", "/workflows", "/workflows/medical-answer"):
        assert client.get(path).status_code == 404
    assert client.post("/workflows", json={}).status_code == 404
    assert client.put("/workflows/medical-answer", json={}).status_code == 404
    assert client.post("/workflows/medical-answer/runs", json={}).status_code == 404


def test_chat_cannot_select_another_workflow():
    payload = {"request_id": "r", "conversation_id": "c", "inputs": {"question": "test"}}
    assert ChatRunRequest(**payload).workflow_id == "medical-answer"
    assert ChatRunRequest(**payload, workflow_id=None).workflow_id == "medical-answer"
    with pytest.raises(ValidationError):
        ChatRunRequest(**payload, workflow_id="medical-answer-agentic")


def test_settings_use_project_env_even_when_launched_from_another_directory(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from medical_rag import config
    from medical_rag.config import Settings

    # 项目 .env 必须由模块位置推导出绝对路径（仓库根目录），不能跟随进程启动目录。
    anchored = Path(Settings.model_config["env_file"])
    assert anchored.is_absolute()
    assert anchored == Path(config.__file__).resolve().parents[2] / ".env"

    # 用临时 .env 证明：即使从没有 .env 的其他目录启动，绝对路径 env_file 仍然生效。
    env_file = tmp_path / "project.env"
    env_file.write_text(
        "MILVUS_URI=http://127.0.0.1:19531\n"
        "COLLECTION_NAME=medical_rag_resume_chunks\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(Settings.model_config, "env_file", env_file)
    monkeypatch.delenv("MILVUS_URI", raising=False)
    monkeypatch.delenv("COLLECTION_NAME", raising=False)
    monkeypatch.chdir(tmp_path.parent)
    settings = Settings()
    assert settings.milvus_uri == "http://127.0.0.1:19531"
    assert settings.collection_name == "medical_rag_resume_chunks"
