"""HTTP acceptance is durable, asynchronous, and independent of dependencies."""

from fastapi.testclient import TestClient
from test_api import FakeService
from test_service import Embedder, MemoryStore, Reranker, Transport

from medical_rag.api import create_app
from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.jobs import DocumentProcessor
from medical_rag.service import RagService


def test_upload_is_accepted_without_calling_milvus_models_or_broker():
    class NoHeavyDependencies(FakeService):
        def ingest(self, *args):
            raise AssertionError("HTTP must not run ingestion")

    app = create_app(service=NoHeavyDependencies())
    client = TestClient(app)
    response = client.post("/documents", files={"file": ("guide.md", b"# guide")})
    assert response.status_code == 202
    data = response.json()["data"]
    assert set(data) == {"job_id", "status", "status_url"}
    job = client.get(data["status_url"]).json()["data"]
    assert job["status"] == "queued" and job["result"] is None
    assert job["attempts"] == 0
    assert "upload_path" not in job and "snapshot" not in job
    assert client.get("/document-jobs?active=true").json()["data"][0]["job_id"] == data["job_id"]


def test_async_upload_becomes_searchable_only_after_worker_finishes():
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(), GroundedGenerator(Transport()))
    app = create_app(service=service)
    client = TestClient(app)
    accepted = client.post("/documents", files={"file": ("guide.md", "# 高血压\n\n应规律随访。".encode())})
    assert accepted.status_code == 202
    assert client.get("/documents").json()["data"] == []
    job_id = accepted.json()["data"]["job_id"]
    assert DocumentProcessor(app.state.document_jobs, service=service).process(job_id)
    finished = client.get(f"/document-jobs/{job_id}").json()["data"]
    assert finished["status"] == "succeeded"
    assert finished["result"]["chunk_count"] > 0
    assert finished["trace"]
    assert len(client.get("/documents").json()["data"]) == 1


def test_invalid_content_is_a_persisted_task_failure_not_http_rejection():
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(), GroundedGenerator(Transport()))
    app = create_app(service=service)
    client = TestClient(app)
    accepted = client.post("/documents", files={"file": ("scan.pdf", b"invalid")})
    assert accepted.status_code == 202
    job_id = accepted.json()["data"]["job_id"]
    DocumentProcessor(app.state.document_jobs, service=service).process(job_id)
    response = client.get(f"/document-jobs/{job_id}")
    assert response.status_code == 200 and response.json()["success"]
    job = response.json()["data"]
    assert job["status"] == "failed" and job["error"]["code"] == "INVALID_DOCUMENT"


def test_job_routes_validate_limits_and_report_missing_ids():
    client = TestClient(create_app(service=FakeService()))
    assert client.get("/document-jobs/missing").status_code == 404
    for limit in (0, 101):
        response = client.get(f"/document-jobs?limit={limit}")
        assert response.status_code == 422
        assert response.json()["success"] is False


def test_health_reports_async_degradation_without_breaking_primary_liveness():
    client = TestClient(create_app(service=FakeService()))
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["milvus"] is True
    assert data["messaging"]["roles"]["ingest"]["stale"] is True


def test_api_lifespan_does_not_start_a_memory_polling_thread():
    app = create_app(service=FakeService())
    with TestClient(app):
        assert app.state.memory_worker is None
