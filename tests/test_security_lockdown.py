"""The legacy single-user API must not be exposed as a secured service."""

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from medical_rag.api import create_app
from medical_rag.config import Settings


class FakeService:
    def health(self):
        return {"milvus": True}

    def list_documents(self):
        return []


@pytest.fixture
def no_access_configuration(monkeypatch):
    """Do not let an ignored local .env change default-mode test outcomes."""
    monkeypatch.delenv("MEDICAL_RAG_ACCESS_MODE", raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/conversations"),
        ("GET", "/conversations/someone-elses-id"),
        ("GET", "/memory"),
        ("GET", "/runs/someone-elses-id"),
        ("GET", "/documents"),
        ("GET", "/settings/model"),
        ("POST", "/chat/runs/stream"),
        ("POST", "/answers"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    ],
)
def test_unconfigured_system_rejects_sensitive_requests(no_access_configuration, method, path):
    response = TestClient(create_app(service=FakeService())).request(method, path)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SECURITY_NOT_READY"


def test_health_remains_available_without_initializing_dependencies(no_access_configuration):
    response = TestClient(create_app(service=FakeService())).get("/health")

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "security_not_ready"}


@pytest.mark.parametrize("mode", ["locked", "unsafe-local-demo"])
def test_unconfigured_system_also_rejects_websocket_routes(monkeypatch, mode):
    monkeypatch.setenv("MEDICAL_RAG_ACCESS_MODE", mode)
    app = create_app(service=FakeService())

    @app.websocket("/unexpected-websocket")
    async def unexpected_websocket(socket: WebSocket):
        await socket.accept()

    with pytest.raises(WebSocketDisconnect), TestClient(app).websocket_connect("/unexpected-websocket"):
        pytest.fail("WebSocket unexpectedly accepted")


def test_demo_mode_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("MEDICAL_RAG_ACCESS_MODE", "unsafe-local-demo")
    response = TestClient(create_app(service=FakeService()), client=("127.0.0.1", 50000)).get("/conversations")

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("peer", "headers"),
    [
        ("203.0.113.42", {}),
        ("127.0.0.1", {"Forwarded": "for=203.0.113.42"}),
        ("127.0.0.1", {"X-Forwarded-For": "203.0.113.42"}),
        ("127.0.0.1", {"X-Real-IP": "203.0.113.42"}),
        ("127.0.0.1", {"Host": "attacker.example"}),
        ("127.0.0.1", {"Origin": "https://attacker.example"}),
        ("127.0.0.1", {"Origin": "null"}),
    ],
)
def test_demo_mode_rejects_remote_and_forwarded_requests(monkeypatch, peer, headers):
    monkeypatch.setenv("MEDICAL_RAG_ACCESS_MODE", "unsafe-local-demo")
    client = TestClient(create_app(service=FakeService()), client=(peer, 50000))

    response = client.get("/conversations", headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "LOCAL_DEMO_ONLY"


def test_demo_mode_allows_loopback_development_origin(monkeypatch):
    monkeypatch.setenv("MEDICAL_RAG_ACCESS_MODE", "unsafe-local-demo")
    client = TestClient(create_app(service=FakeService()), client=("127.0.0.1", 50000))

    response = client.get("/conversations", headers={"Origin": "http://localhost:5173"})

    assert response.status_code == 200


@pytest.mark.parametrize("origin", ["https://attacker.example", None])
def test_demo_rejects_cross_site_or_originless_mutation_before_processing(monkeypatch, origin):
    monkeypatch.setenv("MEDICAL_RAG_ACCESS_MODE", "unsafe-local-demo")
    app = create_app(service=FakeService())
    monkeypatch.setattr(app.state.document_jobs, "submit", lambda *args: pytest.fail("processed upload"))
    headers = {"Origin": origin} if origin is not None else {}
    client = TestClient(app, client=("127.0.0.1", 50000), headers=headers)

    response = client.post("/documents", files={"file": ("document.txt", b"data")})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "LOCAL_DEMO_ONLY"


def test_invalid_access_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("MEDICAL_RAG_ACCESS_MODE", "production")

    with pytest.raises(ValueError):
        create_app(service=FakeService())


@pytest.mark.parametrize("role", ["relay", "ingest", "memory"])
@pytest.mark.asyncio
async def test_locked_worker_refuses_before_accessing_storage_or_broker(monkeypatch, no_access_configuration, role):
    from medical_rag import worker

    monkeypatch.setattr(worker, "DocumentJobStore", lambda *args: pytest.fail("opened job storage"))

    with pytest.raises(RuntimeError, match="security"):
        await worker.run_role(role, Settings(_env_file=None))


def test_locked_worker_cli_rejects_before_taking_ingest_lock(monkeypatch, no_access_configuration):
    import sys

    from medical_rag import worker

    monkeypatch.setattr(sys, "argv", ["worker", "ingest"])
    monkeypatch.setattr(worker, "DocumentJobStore", lambda *args: pytest.fail("opened job storage"))

    with pytest.raises(RuntimeError, match="security"):
        worker.main()
