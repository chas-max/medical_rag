"""Exercise the gateway with disposable TLS and an isolated fake upstream."""

import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "deploy" / "gateway" / "nginx.conf"
COMPOSE = ROOT / "docker-compose.gateway.yml"
IMAGE = "nginx:1.30.6-alpine"
UPSTREAM_IMAGE = "python:3.13-alpine"


def test_gateway_configuration_exists():
    assert CONFIG.is_file()


def test_gateway_deployment_configuration_exists():
    assert COMPOSE.is_file()


@pytest.mark.integration
def test_gateway_compose_is_read_only_and_requires_explicit_certificates(tmp_path):
    if os.environ.get("RUN_GATEWAY_INTEGRATION") != "1":
        pytest.skip("set RUN_GATEWAY_INTEGRATION=1 to validate Docker Compose")
    if not shutil.which("docker"):
        pytest.skip("Docker is required")
    assert COMPOSE.is_file()
    env = os.environ | {
        "GATEWAY_TLS_CERT": str(tmp_path / "tls.crt"),
        "GATEWAY_TLS_KEY": str(tmp_path / "tls.key"),
        "GATEWAY_BACKEND_NETWORK": "test-internal-network",
    }
    result = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
                            cwd=ROOT, env=env, check=True, capture_output=True, text=True)
    service = json.loads(result.stdout)["services"]["gateway"]
    assert service["read_only"] is True
    assert service["user"] == "101:101"
    assert "ALL" in service["cap_drop"]
    assert service["ports"][0]["host_ip"] == "127.0.0.1"


@pytest.fixture(scope="module")
def gateway(tmp_path_factory):
    if os.environ.get("RUN_GATEWAY_INTEGRATION") != "1":
        pytest.skip("set RUN_GATEWAY_INTEGRATION=1 to run disposable Docker gateway tests")
    if not shutil.which("docker") or not shutil.which("openssl"):
        pytest.skip("Docker and OpenSSL are required")
    assert CONFIG.is_file(), "gateway configuration missing"
    temp = tmp_path_factory.mktemp("gateway")
    cert, key = temp / "cert.pem", temp / "key.pem"
    openssl_config = temp / "openssl.cnf"
    openssl_config.write_text("[req]\ndistinguished_name = dn\n[dn]\n", encoding="ascii")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-config", str(openssl_config), "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert),
    ], check=True, capture_output=True)
    # This is an ephemeral test key only; make it readable by the non-root container.
    key.chmod(0o644)
    upstream = r'''
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        self.respond()
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.respond()
    def respond(self):
        self.send_response(200)
        if self.path in ("/stream", "/slow-stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"event: token\ndata: hello\n\n")
            self.wfile.flush()
            if self.path == "/slow-stream":
                time.sleep(5)
            return
        pairs = {"user":"X-User-ID", "tenant":"X-Tenant-ID", "forwarded":"X-Forwarded-For",
                 "authorization":"Authorization", "request_id":"X-Request-ID", "content_type":"Content-Type",
                 "content_length":"Content-Length", "cookie":"Cookie", "csrf":"X-CSRF-Token"}
        body = json.dumps({key: self.headers.get(value, "") for key, value in pairs.items()}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
'''

    network = f"medical-rag-test-{uuid4().hex[:12]}"
    edge_network = f"{network}-edge"
    containers = []

    def docker(*args):
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()

    docker("network", "create", "--internal", network)
    try:
        docker("network", "create", edge_network)
        containers.append(docker(
            "run", "-d", "--rm", "--network", network, "--network-alias", "medical-rag-api",
            UPSTREAM_IMAGE, "python", "-u", "-c", upstream,
        ))
        containers.append(docker(
            "create", "--network", edge_network, "-p", "127.0.0.1::8443",
            "--user", "101:101", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--mount", f"type=bind,source={CONFIG},target=/etc/nginx/nginx.conf,readonly",
            "--mount", f"type=bind,source={cert},target=/run/tls/tls.crt,readonly",
            "--mount", f"type=bind,source={key},target=/run/tls/tls.key,readonly",
            "--entrypoint", "nginx", IMAGE, "-g", "daemon off;",
        ))
        docker("network", "connect", network, containers[-1])
        docker("start", containers[-1])
        try:
            port = docker("port", containers[-1], "8443/tcp").rsplit(":", 1)[-1]
        except subprocess.CalledProcessError:
            logs = subprocess.run(["docker", "logs", containers[-1]], capture_output=True, text=True, check=False)
            state = subprocess.run(["docker", "inspect", "--format", "{{json .State}} {{json .NetworkSettings.Ports}}",
                                    containers[-1]], capture_output=True, text=True, check=False)
            pytest.fail(f"gateway failed to start: {logs.stderr}; {state.stdout}")
        base_url = f"https://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(base_url + "/inspect", verify=False, timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("gateway did not become ready")
        yield base_url
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "stop", container], capture_output=True, check=False)
            subprocess.run(["docker", "rm", container], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", edge_network], capture_output=True, check=False)


@pytest.mark.integration
def test_gateway_strips_spoofed_identity_and_sets_request_id(gateway):
    response = httpx.get(gateway + "/inspect", verify=False, headers={
        "X-User-ID": "other-user", "X-Tenant-ID": "other-tenant",
        "X-Forwarded-For": "192.0.2.123", "X-Request-ID": "forged",
        "Authorization": "Bearer test-credential",
    })
    assert response.status_code == 200, response.text[:300]
    data = response.json()
    assert data["user"] == data["tenant"] == data["forwarded"] == ""
    assert data["authorization"] == "Bearer test-credential"
    assert data["request_id"] and data["request_id"] != "forged"
    assert response.headers["x-request-id"] == data["request_id"]


@pytest.mark.integration
def test_gateway_preserves_event_stream(gateway):
    response = httpx.get(gateway + "/stream", verify=False)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: token\ndata: hello\n\n" in response.text


@pytest.mark.integration
def test_gateway_preserves_authentication_and_body_headers(gateway):
    response = httpx.post(gateway + "/inspect", verify=False, content=b'{"question":"example"}', headers={
        "Content-Type": "application/json", "Cookie": "session=example", "X-CSRF-Token": "example-csrf",
    })
    data = response.json()
    assert data["content_type"] == "application/json"
    assert data["content_length"] == str(len(b'{"question":"example"}'))
    assert data["cookie"] == "session=example"
    assert data["csrf"] == "example-csrf"


@pytest.mark.integration
def test_gateway_rejects_oversized_request(gateway):
    response = httpx.post(gateway + "/inspect", verify=False, content=b"x" * (21 * 1024 * 1024), timeout=10)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


@pytest.mark.integration
def test_gateway_rejects_oversized_chunked_request_before_upstream_response(gateway):
    chunks = (b"x" * (1024 * 1024) for _ in range(21))
    response = httpx.post(gateway + "/inspect", verify=False, content=chunks, timeout=15)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


@pytest.mark.integration
def test_gateway_accepts_chunked_request_just_under_limit(gateway):
    chunks = (b"x" * (1024 * 1024) for _ in range(19))
    response = httpx.post(gateway + "/inspect", verify=False, content=chunks, timeout=15)
    assert response.status_code == 200, response.text[:300]


@pytest.mark.integration
def test_gateway_bounds_buffered_uploads_and_recovers(gateway):
    started = threading.Barrier(3)
    release = threading.Event()

    def upload():
        def chunks():
            yield b"x" * (1024 * 1024)
            started.wait(timeout=10)
            if not release.wait(timeout=15):
                raise TimeoutError("test did not release uploads")
            yield from (b"x" * (1024 * 1024) for _ in range(18))

        return httpx.post(gateway + "/inspect", verify=False, content=chunks(), timeout=25)

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(upload) for _ in range(2)]
        try:
            started.wait(timeout=10)
            time.sleep(0.5)  # Let the gateway receive the two request headers.
            assert httpx.get(gateway + "/inspect", verify=False).status_code == 200
            assert httpx.post(gateway + "/inspect", verify=False, content=b"x" * 99999).status_code == 200
            denied = httpx.post(gateway + "/inspect", verify=False, content=b"x" * 100000)
            assert denied.status_code == 429
        finally:
            release.set()
        assert [job.result(timeout=25).status_code for job in jobs] == [200, 200]
    assert httpx.post(gateway + "/inspect", verify=False, content=b"x" * 100000).status_code == 200


@pytest.mark.integration
def test_small_post_streams_do_not_exhaust_upload_capacity(gateway):
    started = threading.Barrier(3)
    release = threading.Event()

    def stream():
        with httpx.stream("POST", gateway + "/slow-stream", verify=False,
                          content=b'{"question":"hello"}', timeout=15) as response:
            started.wait(timeout=10)
            if not release.wait(timeout=10):
                raise TimeoutError("test did not release streams")
            return response.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(stream) for _ in range(2)]
        try:
            started.wait(timeout=10)
            response = httpx.post(gateway + "/inspect", verify=False, content=b"x" * 100000)
            assert response.status_code == 200
        finally:
            release.set()
        assert [job.result(timeout=10) for job in jobs] == [200, 200]


@pytest.mark.integration
def test_gateway_rate_limits_a_single_client(gateway):
    with httpx.Client(verify=False, timeout=5) as client:
        responses = [client.get(gateway + "/inspect") for _ in range(80)]
    limited = [response for response in responses if response.status_code == 429]
    assert limited
    assert limited[0].json()["error"]["code"] == "RATE_LIMITED"
