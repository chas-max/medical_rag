"""Drive the real asynchronous upload contract without requiring a live broker."""

from medical_rag.jobs import DocumentProcessor


def upload_and_process(client, *, files, data=None):
    """Submit over HTTP, execute the consumer, then read authoritative job state."""
    accepted = client.post("/documents", files=files, data=data)
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["data"]["job_id"]
    service = client.app.state.chat_runtime.service_factory()
    processor = DocumentProcessor(client.app.state.document_jobs, service=service)
    assert processor.process(job_id)
    return client.get(f"/document-jobs/{job_id}").json()["data"]
