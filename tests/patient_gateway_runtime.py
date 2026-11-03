"""Owned TLS sockets: real isolated app/PG and separate synthetic transport proof.

Never print Docker output, credentials, response bodies or fixture state on failure.
All keys are disposable and generated below an ignored pytest output directory.
"""

import json
import logging
import os
import secrets
import socket
import ssl
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from keycloak_runtime import certificates

from medical_rag.identity_sessions import SessionStore
from medical_rag.oidc_identity import VerifiedIdentity
from medical_rag.oidc_login import LoginConfiguration
from medical_rag.patient_gateway import load_configuration, render_nginx
from medical_rag.patient_service import HealthNotice
from medical_rag.security_config import PatientConfiguration
from medical_rag.security_server import _application

ROOT = Path(__file__).resolve().parents[1]
NGINX = "nginx:1.30.6-alpine@sha256:0e52bd054126cdc040b1276c0af347aeb02a41b9cfe305d6094767ac30e97949"
TENANT = "00000000-0000-0000-0000-000000000101"
ISSUER = "https://identity.example.org/realms/fictional"


def check(condition: bool, message: str) -> None:
    """Only a static diagnostic, not assertion rewriting of private operands."""
    if not condition:
        pytest.fail(message, pytrace=False)


def docker(*args: str, stdin: bytes | None = None) -> str:
    try:
        run = subprocess.run(["docker", *args], input=stdin, capture_output=True, timeout=45, check=False)
        if run.returncode:
            raise RuntimeError()
        return run.stdout.decode().strip()
    except (OSError, subprocess.SubprocessError, RuntimeError, UnicodeError):
        raise RuntimeError("Owned gateway infrastructure command failed") from None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Owned:
    """Resource IDs are private, cleanup requires the exact unique owner label."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.owner = "patient-gateway-" + secrets.token_hex(8)
        self.resources = []
        self.gateway_port = free_port()
        self.origin = f"https://127.0.0.1:{self.gateway_port}"

    def __repr__(self):
        return "<Owned patient gateway fixture>"

    def network(self, suffix: str, internal: bool = False) -> str:
        name = self.owner + suffix
        docker("network", "create", "--label", "medical-rag.fixture=" + self.owner,
               *(["--internal"] if internal else []), name)
        self.resources.append(("network", name))
        return name

    def container(self, *args: str) -> str:
        name = self.owner + "-" + secrets.token_hex(4)
        docker("create", "--name", name, "--label", "medical-rag.fixture=" + self.owner, *args)
        self.resources.append(("container", name))
        return name

    def cleanup(self):
        failed = False
        for kind, name in reversed(self.resources):
            try:
                path = ".Config.Labels" if kind == "container" else ".Labels"
                owner = docker(kind, "inspect", name, "--format", '{{index ' + path + ' "medical-rag.fixture"}}')
                if owner != self.owner:
                    raise RuntimeError()
                docker(kind, "rm", *( ["-f"] if kind == "container" else []), name)
            except Exception:  # noqa: BLE001 -- attempt all owned cleanup, static error only
                failed = True
        for name in ("server.key", "server.pem", "ca.pem"):
            (self.directory / name).unlink(missing_ok=True)
        check(not failed, "Owned gateway cleanup failed")

    def browser(self):
        context = ssl.create_default_context(cafile=str(self.directory / "ca.pem"))
        return httpx.Client(base_url=self.origin, verify=context, trust_env=False,
                            timeout=httpx.Timeout(8, connect=2), follow_redirects=False)

    def host_ipv4(self) -> str:
        """Resolve Docker Desktop's IPv4 host route without relying on DNS order."""
        return docker(
            "run", "--rm", "--label", "medical-rag.fixture=" + self.owner,
            "--add-host", "host.docker.internal:host-gateway", "python:3.13-alpine",
            "python", "-c",
            "import socket;print(socket.getaddrinfo('host.docker.internal',None,socket.AF_INET)[0][4][0])",
        )

    def gateway(self, upstream: str, *, tls_name: str = "localhost", ca: str = "ca.pem"):
        fields = {
            "public_origin": self.origin, "upstream_url": upstream, "upstream_tls_name": tls_name,
            "tls_certificate": "/fixture/server.pem", "tls_key": "/fixture/server.key",
            "upstream_ca": "/fixture/" + ca,
        }
        config = load_configuration("\n".join(f"{key} = {json.dumps(value)}" for key, value in fields.items()).encode())
        target = self.directory / ("nginx-" + secrets.token_hex(4) + ".conf")
        target.write_text(render_nginx(config), encoding="utf-8")
        edge = self.network("-edge-" + secrets.token_hex(3))
        container = self.container(
            "--network", edge, "-p", f"127.0.0.1:{self.gateway_port}:8443",
            "--user", "101:101", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "32", "--memory", "128m",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,uid=101,gid=101",
            "--mount", f"type=bind,source={target},target=/etc/nginx/nginx.conf,readonly",
            "--mount", f"type=bind,source={self.directory},target=/fixture,readonly",
            "--mount", f"type=bind,source={ROOT / 'deploy/patient-gateway/start.sh'},target=/start.sh,readonly",
            "--entrypoint", "/bin/sh", NGINX, "/start.sh",
        )
        docker("network", "connect", self.backend, container)
        docker("start", container)
        with self.browser() as client:
            ready = False
            for _ in range(60):
                try:
                    ready = client.get("/not-allowed").status_code == 404
                    if ready:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            check(ready, "Owned gateway HTTPS readiness failed")
        return container


@contextmanager
def infrastructure(directory: Path):
    owned = Owned(directory)
    try:
        for image in (NGINX, "postgres:18.6-alpine", "python:3.13-alpine"):
            docker("image", "inspect", image, "--format", "{{.Id}}")
        certificates(directory)
        owned.backend = owned.network("-backend", internal=True)
        yield owned
    except Exception:  # noqa: BLE001 -- external/private fixtures must not reach pytest repr
        pytest.fail("Owned gateway socket proof failed", pytrace=False)
    finally:
        owned.cleanup()


def database(owned: Owned):
    password = secrets.token_urlsafe(24)
    pg = owned.container(
        "-p", "127.0.0.1::5432", "--tmpfs", "/var/lib/postgresql:rw,size=256m",
        "--tmpfs", "/run/fixture:rw,noexec,nosuid,size=1m,mode=0700",
        "-e", "POSTGRES_PASSWORD_FILE=/run/fixture/password", "-e", "POSTGRES_INITDB_ARGS=--no-sync",
        "--entrypoint", "/bin/sh", "postgres:18.6-alpine", "-c",
        'i=0; while [ ! -s /run/fixture/password ]; do i=$((i+1)); [ "$i" -lt 300 ] || exit 1; sleep .1; done; exec docker-entrypoint.sh postgres',
    )
    docker("start", pg)
    docker("exec", "-i", pg, "/bin/sh", "-c",
           "umask 077; cat > /run/fixture/pending && mv /run/fixture/pending /run/fixture/password",
           stdin=password.encode())
    port = int(docker("port", pg, "5432/tcp").rsplit(":", 1)[1])
    admin = {"host": "127.0.0.1", "port": port, "dbname": "postgres", "user": "postgres", "password": password,
             "connect_timeout": 2, "autocommit": True}
    for _ in range(100):
        try:
            connection = psycopg.connect(**admin)
            break
        except psycopg.OperationalError:
            time.sleep(0.1)
    else:
        raise RuntimeError("Owned PostgreSQL readiness failed")
    with connection as db:
        for migration in sorted((ROOT / "database/migrations").glob("*.sql")):
            db.execute(migration.read_text(encoding="utf-8"))
        db.execute(psycopg.sql.SQL("CREATE ROLE gateway_runtime LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
            psycopg.sql.Literal(password)))
        grants = (ROOT / "database/patient_runtime_grants.sql").read_text(encoding="utf-8")
        grants = "\n".join(line for line in grants.splitlines() if not line.startswith("\\"))
        db.execute(grants.replace(':"runtime_role"', '"gateway_runtime"'))
        db.execute("INSERT INTO medical_rag.tenants(tenant_id) VALUES (%s)", (TENANT,))
        for number in (1, 2):
            account, patient = str(uuid4()), str(uuid4())
            db.execute("INSERT INTO medical_rag.identity_accounts(tenant_id,account_id,issuer,subject) VALUES (%s,%s,%s,%s)",
                       (TENANT, account, ISSUER, f"fictional-{number}"))
            db.execute("INSERT INTO medical_rag.identity_roles VALUES (%s,%s,'patient')", (TENANT, account))
            db.execute("INSERT INTO medical_rag.patient_subjects"
                       "(tenant_id,patient_id,age_classification,identity_verified) VALUES (%s,%s,'adult',true)",
                       (TENANT, patient))
            db.execute("INSERT INTO medical_rag.identity_bindings VALUES (%s,%s,%s,true)", (TENANT, account, patient))
    return dict(admin, user="gateway_runtime", autocommit=False), admin


@contextmanager
def application(owned: Owned):
    runtime, admin = database(owned)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    login = LoginConfiguration(
        issuer=ISSUER, authorization_endpoint=ISSUER + "/auth", token_endpoint=ISSUER + "/token",
        callback_uri=owned.origin + "/auth/callback", client_id="fictional-gateway",
        client_secret=secrets.token_urlsafe(32), public_key=key.public_key(),
        mfa_acr_values=frozenset({"urn:fictional:mfa"}),
    )
    port = free_port()
    config = PatientConfiguration(
        tenant_id=TENANT, database_dsn=psycopg.conninfo.make_conninfo(**{k: v for k, v in runtime.items() if k != "autocommit"}),
        transaction_key=secrets.token_bytes(32), frontend_dist=ROOT / "frontend/dist-patient",
        listen_host="127.0.0.1", listen_port=port,
        tls_cert_file=owned.directory / "server.pem", tls_key_file=owned.directory / "server.key",
        login=login, notice=HealthNotice("fictional-v1", "Fictional test notice only.", 3600),
    )
    app = _application(config)  # Real schema/privilege preflight and real isolated factory.
    sessions = SessionStore(lambda: psycopg.connect(**runtime), tenant_id=TENANT)
    first = sessions.issue(VerifiedIdentity(ISSUER, "fictional-1", True))
    second = sessions.issue(VerifiedIdentity(ISSUER, "fictional-2", True))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).disabled = True
    server = uvicorn.Server(uvicorn.Config(
        app, host=config.listen_host, port=port, ssl_certfile=str(config.tls_cert_file),
        ssl_keyfile=str(config.tls_key_file), proxy_headers=False, access_log=False,
        server_header=False, log_config=None, log_level="critical", limit_concurrency=100,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        check(server.started, "Actual isolated TLS app readiness failed")
        yield port, first, second, admin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        check(not thread.is_alive(), "Actual isolated TLS app cleanup failed")


def _headers(owned, session):
    return {"Cookie": "__Host-medical-session=" + session.token,
            "X-CSRF-Token": session.csrf_token, "Origin": owned.origin}


def actual_application_proof(directory: Path):
    with infrastructure(directory) as owned, application(owned) as private:
        port, first, second, admin = private
        owned.gateway(f"https://{owned.host_ipv4()}:{port}")
        with owned.browser() as client:
            response = client.get("/")
            check(response.status_code == 200 and 'type="module"' in response.text, "Real frontend retrieval failed")
            asset = next((ROOT / "frontend/dist-patient/assets").glob("*.js"))
            check(client.get("/assets/" + asset.name).status_code == 200, "Real asset retrieval failed")
            check(client.get("/me").status_code == 401, "Missing session was not denied")
            check(client.get("/me", headers={"Authorization": "Bearer fictional", "X-User-ID": "fictional-1", "X-Role": "admin"}).status_code == 401,
                  "Forged identity was not denied")
            headers = _headers(owned, first)
            response = client.get("/me", headers=headers | {"X-Request-ID": "forged", "X-Gateway-Request-ID": "forged", "X-Tenant-ID": "other"})
            check(response.status_code == 200 and response.json()["data"]["tenant_id"] == TENANT, "Current fictional session identity failed")
            request_id = response.headers.get("x-request-id")
            gateway_id = response.headers.get("x-gateway-request-id")
            check(bool(request_id and gateway_id and request_id != gateway_id and "forged" not in (request_id, gateway_id)), "Independent trace IDs failed")
            check(len(response.headers.get_list("x-request-id")) == 1, "Duplicate app request ID")
            with psycopg.connect(**admin) as db:
                check(db.execute("SELECT count(*) FROM medical_rag.security_audit_events WHERE request_id=%s", (request_id,)).fetchone()[0] > 0,
                      "Application audit correlation lost")
            check(client.get("/me", headers=headers | {"Host": "wrong.example"}).status_code == 400, "Wrong Host accepted")
            check(client.get("/me", headers=headers | {"Origin": "https://wrong.example"}).status_code == 403, "Wrong Origin accepted")
            check(client.post("/me/consents/health", headers={"Cookie": headers["Cookie"], "Origin": owned.origin},
                              json={"version": "fictional-v1", "accepted": True}).status_code in (401, 403), "Missing CSRF accepted")
            check(client.post("/me/consents/health", headers=headers,
                              json={"version": "fictional-v1", "accepted": True}).status_code == 200, "Cookie/CSRF consent mutation failed")
            response = client.post("/conversations", headers=headers, json={"title": "Fictional owned conversation"})
            check(response.status_code == 200, "Owned resource creation failed")
            path = "/conversations/" + response.json()["data"]["id"]
            check(client.get(path, headers=headers).status_code == 200, "Owned resource access failed")
            other = _headers(owned, second)
            check(client.post("/me/consents/health", headers=other,
                              json={"version": "fictional-v1", "accepted": True}).status_code == 200, "Second fictional consent failed")
            denied = client.get(path, headers=other)
            check(denied.status_code == 404 and denied.json()["error"]["code"] == "RESOURCE_NOT_FOUND", "Foreign owner was not denied by real app")
            check(client.get("/audit", headers=headers).status_code == 403, "Real application role denial changed")
            for path in ("/ui", "/api", "/settings", "/documents", "/docs", "/unknown", "/me/unknown"):
                response = client.get(path, headers=headers)
                check(response.status_code == 404 and response.json()["error"]["code"] == "GATEWAY_NOT_FOUND", "Closed path reached application")
            check("nginx/" not in response.headers.get("server", ""), "Server version disclosed")


def synthetic_proof(directory: Path):
    with infrastructure(directory) as owned:
        synthetic = owned.container(
            "--network", owned.backend, "--network-alias", "synthetic", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "128m",
            "--mount", f"type=bind,source={directory},target=/fixture,readonly",
            "--mount", f"type=bind,source={ROOT / 'tests/fixtures/patient_gateway_synthetic.py'},target=/synthetic.py,readonly",
            "python:3.13-alpine", "python", "-B", "/synthetic.py",
        )
        docker("start", synthetic)
        gateway = owned.gateway("https://synthetic:8443")
        with owned.browser() as client:
            response = client.post("/me", content=b"fictional-body", headers={
                "Cookie": "opaque=fictional", "Origin": owned.origin, "X-CSRF-Token": "fictional-csrf",
                "Content-Type": "text/plain", "Accept": "application/json", "Accept-Language": "en",
                "Authorization": "Bearer private", "Forwarded": "for=forged;proto=http",
                "X-Forwarded-For": "192.0.2.4", "X-Forwarded-Host": "forged.example", "X-Forwarded-Proto": "http",
                "X-User-ID": "forged", "X-Tenant-ID": "forged", "X-Role": "admin", "X-Request-ID": "forged",
                "X-Arbitrary-Identity": "forged",
            })
            check(response.status_code == 200, "Synthetic verified upstream unreachable")
            payload = response.json()
            received = {key.lower(): value for key, value in payload["headers"].items()}
            check(set(received) == {"host", "content-length", "cookie", "origin", "content-type", "accept", "accept-language", "x-csrf-token"},
                  "Request header allowlist violated")
            check(received["host"] == owned.origin.removeprefix("https://") and received["content-length"] == "14" and payload["length"] == 14,
                  "Port-preserving Host or request framing failed")
            check(received["cookie"] == "opaque=fictional" and received["x-csrf-token"] == "fictional-csrf", "Allowed Cookie/CSRF lost")
            check(response.headers.get_list("x-request-id") == ["synthetic-application-id"] and response.headers["x-gateway-request-id"] != "forged-upstream-gateway-id",
                  "Upstream response ID semantics changed")
            for status in (401, 403, 404, 409, 429, 302):
                time.sleep(0.21)
                response = client.get(f"/assets/status-{status}")
                check(response.status_code == status and response.json()["synthetic"] is True, "Application status/body intercepted")
                check(response.headers["location"] == "https://identity.example.org/authorize?state=fictional", "Provider redirect rewritten")
            for chunks, expected in (([b"x" * 100000], 200), ([b"x" * 50000, b"x" * 50001], 413)):
                time.sleep(0.21)
                response = client.post("/me", content=iter(chunks))
                check(response.status_code == expected, "Chunked body boundary failed")
                if expected == 413:
                    check(response.json()["error"]["code"] == "GATEWAY_BODY_LIMIT", "Body failure was not static")
            response = client.post("/me", content=b"x" * 100001)
            check(response.status_code == 413, "Fixed length body limit failed")
            started = time.monotonic()
            with client.stream("GET", "/assets/stream") as response:
                lines = response.iter_lines()
                check(next(lines) == "data: first" and time.monotonic() - started < 2, "SSE first chunk buffered")
                check("data: complete" in list(lines) and time.monotonic() - started >= 3, "SSE completion missing")
            statuses = []
            limited = None
            for index in range(80):
                response = client.get("/me", headers={"X-Forwarded-For": f"192.0.2.{index}", "Cookie": f"fake={index}"})
                statuses.append(response.status_code)
                if response.status_code == 429:
                    limited = response
                    break
            check(429 in statuses and 200 in statuses, "Source-IP development admission not enforced")
            check(limited is not None and limited.json()["error"]["code"] == "GATEWAY_RATE_LIMIT",
                  "Rate-limit failure was not static")
        # Each negative case creates a new gateway with an otherwise identical
        # fixed rendered configuration. The upstream remains strictly TLS-only.
        docker("stop", "--time", "1", gateway)
        foreign = directory / "foreign"
        foreign.mkdir()
        certificates(foreign)
        (directory / "wrong-ca.pem").write_bytes((foreign / "ca.pem").read_bytes())
        for name in ("server.key", "server.pem", "ca.pem"):
            (foreign / name).unlink()
        for upstream, tls_name, ca in (("https://synthetic:8443", "localhost", "wrong-ca.pem"),
                                       ("https://synthetic:8443", "wrong.example.org", "ca.pem"),
                                       ("https://synthetic:9443", "localhost", "ca.pem")):
            gateway = owned.gateway(upstream, tls_name=tls_name, ca=ca)
            with owned.browser() as client:
                response = client.get("/me?private=fictional")
                check(response.status_code == 502 and response.json() == {
                    "success": False, "data": None, "error": {"code": "GATEWAY_UNAVAILABLE", "message": "Service unavailable"}},
                    "Negative upstream TLS case did not fail closed")
                check(docker("logs", gateway) == "", "Gateway request diagnostics leaked")
            docker("stop", "--time", "1", gateway)


def compose_proof(directory: Path):
    empty = directory / "empty-env"
    empty.write_bytes(b"")
    env = os.environ | {
        "COMPOSE_DISABLE_ENV_FILE": "1", "PATIENT_GATEWAY_CONFIG": str(directory / "rendered.conf"),
        "PATIENT_GATEWAY_CERT": str(directory / "server.pem"), "PATIENT_GATEWAY_KEY": str(directory / "server.key"),
        "PATIENT_GATEWAY_UPSTREAM_CA": str(directory / "ca.pem"),
    }
    result = subprocess.run(["docker", "compose", "--env-file", str(empty), "-f", str(ROOT / "docker-compose.patient-gateway.yml"),
                             "config", "--format", "json"], env=env, capture_output=True, check=False, timeout=30)
    check(result.returncode == 0, "Dedicated Compose config failed")
    parsed = json.loads(result.stdout)
    check(set(parsed["services"]) == {"patient-gateway"}, "Gateway Compose unexpectedly deploys another service")
    service = parsed["services"]["patient-gateway"]
    check(service["image"] == NGINX and service["read_only"] and service["user"] == "101:101", "Gateway container boundary changed")
    check(service["cap_drop"] == ["ALL"] and "no-new-privileges:true" in service["security_opt"], "Gateway privileges changed")
    check(service["ports"][0]["host_ip"] == "127.0.0.1" and len(service["ports"]) == 1, "Gateway publish boundary changed")
    check(parsed["networks"]["backend"]["internal"] is True and "internal" not in parsed["networks"]["edge"], "Gateway networks not separated")
    check(all(item["read_only"] and not item["bind"]["create_host_path"] for item in service["volumes"]), "Gateway mounts are not explicit read-only files")
