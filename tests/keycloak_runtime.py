"""Owned, disposable official Keycloak HTTPS + collector + PostgreSQL fixture."""

import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from medical_rag.provider_tls import provider_ssl_context

IMAGE = "quay.io/keycloak/keycloak:26.8.1@sha256:3f3c5f8e03c614774b4bd9173b88391fefa4153bfc010d8012d59ad825377332"
ROOT = Path(__file__).resolve().parents[1]
APP_ORIGIN = "https://medical.example.org"
CLIENT_ID = "medical-rag"


def docker(*args: str) -> str:
    """Keep credential-bearing command errors and daemon output private."""
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(
            "Disposable identity infrastructure command failed"
        ) from None
    if result.returncode:
        raise RuntimeError("Disposable identity infrastructure command failed")
    return result.stdout.strip()


def import_realm(container: str, payload: bytes) -> None:
    """Bound credentials in stdin into owned container tmpfs, never host files."""
    if type(payload) is not bytes or not 0 < len(payload) <= 65536 or b"\n" in payload:
        raise RuntimeError("Disposable identity import failed")
    try:
        result = subprocess.run(
            ["docker", "exec", "-i", container, "/bin/bash", "-c",
             ('umask 077; IFS= read -r payload && printf \'%s\' "$payload" > '
             '/opt/keycloak/data/import/realm.pending && mv '
             '/opt/keycloak/data/import/realm.pending /opt/keycloak/data/import/realm.json')],
            input=payload + b"\n", capture_output=True, timeout=30, check=False,
        )
        if result.returncode:
            raise RuntimeError()
    except (OSError, subprocess.SubprocessError, RuntimeError):
        raise RuntimeError("Disposable identity import failed") from None


def certificates(directory: Path) -> None:
    """Generate fixture CA/server certs, never upstream identity signatures."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Fictional disposable CA")]
    )
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            False,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.DNSName("collector"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    (directory / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (directory / "server.pem").write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
    )
    (directory / "server.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


@dataclass(repr=False)
class Runtime:
    """Private fixture handles; no credentials or upstream values in repr."""

    issuer: str
    collector_url: str
    ca_file: Path
    client_secret: str = field(repr=False)
    users: dict = field(repr=False)
    public_key: object = field(repr=False)
    database: dict = field(repr=False)
    admin: dict = field(repr=False)
    verification_keys: dict = field(default_factory=dict, repr=False)
    operator: dict = field(default_factory=dict, repr=False)

    def browser(self) -> httpx.Client:
        """Use the fixture CA with real HTTPX sockets and bounded deadlines."""
        return httpx.Client(
            verify=provider_ssl_context(self.ca_file),
            trust_env=False,
            timeout=httpx.Timeout(5, connect=2),
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        )


def _ready(browser: httpx.Client, url: str, seconds: int = 90) -> bytes:
    """Bound readiness duration and response bytes before parsing."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with browser.stream("GET", url) as response:
                data = bytearray()
                for part in response.iter_bytes():
                    if len(data) + len(part) > 65536:
                        raise RuntimeError(
                            "Disposable provider readiness response limit"
                        )
                    data.extend(part)
                if response.status_code == 200:
                    return bytes(data)
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError("Disposable identity HTTPS readiness deadline exceeded")


def cleanup_owned(containers: list[str], network: str, owner: str,
                  original: BaseException | None) -> None:
    """Attempt every separate target, never delete on failed ownership proof."""
    failed = False
    for kind, target in [("container", item) for item in reversed(containers)] + [("network", network)]:
        try:
            inspect = (("inspect", target, "--format", '{{index .Config.Labels "medical-rag.fixture"}}')
                       if kind == "container" else
                       ("network", "inspect", target, "--format", '{{index .Labels "medical-rag.fixture"}}'))
            if docker(*inspect) != owner:
                raise RuntimeError()
            docker(*(("rm", "-f", target) if kind == "container" else ("network", "rm", target)))
        except Exception:  # noqa: BLE001 -- daemon/credential context must stay private
            failed = True
    if failed:
        if original is not None:
            original.add_note("Disposable identity cleanup failed")
        else:
            raise RuntimeError("Disposable identity cleanup failed") from None


@contextmanager
def identity_runtime(directory: Path, *, realm_transform: Callable[[dict, dict], None] | None = None) -> Iterator[Runtime]:
    """Explicit selection fails when infra absent; clean only verified ownership."""
    if os.environ.get("RUN_KEYCLOAK_INTEGRATION") != "1":
        pytest.skip("set RUN_KEYCLOAK_INTEGRATION=1 for actual disposable provider")
    if not shutil.which("docker"):
        pytest.fail("Actual Keycloak suite selected but Docker unavailable")
    for image in (IMAGE, "python:3.13-alpine", "postgres:18.6-alpine"):
        docker("image", "inspect", image, "--format", "{{.Id}}")
    owner = "task6-" + secrets.token_hex(8)
    label = "medical-rag.fixture=" + owner
    network = owner + "-network"
    containers = []
    certificates(directory)
    realm = "fictional-" + secrets.token_hex(6)
    client_secret = secrets.token_urlsafe(32)
    operator = {"id": str(uuid4()), "username": "fixture-operator",
                "password": secrets.token_urlsafe(24), "client_secret": secrets.token_urlsafe(32)}
    users = {
        name: {"id": str(uuid4()), "password": secrets.token_urlsafe(24)}
        for name in (
            "patient1",
            "patient2",
            "disabled",
            "unprovisioned",
            "unverified",
            "admin",
        )
    }
    imported = {
        "id": realm,
        "realm": realm,
        "enabled": True,
        "sslRequired": "all",
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "loginWithEmailAllowed": False,
        "accessTokenLifespan": 120,
        "roles": {"realm": [{"name": "admin"}]},
        "clients": [
            {"clientId": "fixture-operator", "secret": operator["client_secret"],
             "protocol": "openid-connect", "publicClient": False,
             "standardFlowEnabled": True, "implicitFlowEnabled": False,
             "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False,
             "redirectUris": ["https://fixture-operator.invalid/callback"],
             "attributes": {"pkce.code.challenge.method": "S256"}},
            {
                "clientId": CLIENT_ID,
                "secret": client_secret,
                "protocol": "openid-connect",
                "publicClient": False,
                "standardFlowEnabled": True,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "redirectUris": [APP_ORIGIN + "/auth/callback"],
                "webOrigins": [APP_ORIGIN],
                "protocolMappers": [
                    {
                        "name": "fictional-id-token-roles",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-usermodel-realm-role-mapper",
                        "config": {
                            "claim.name": "roles",
                            "jsonType.label": "String",
                            "multivalued": "true",
                            "id.token.claim": "true",
                            "access.token.claim": "true",
                        },
                    }
                ],
                "attributes": {
                    "pkce.code.challenge.method": "S256",
                    "post.logout.redirect.uris": APP_ORIGIN + "/",
                    "backchannel.logout.url": "https://collector:8443/capture",
                    "backchannel.logout.session.required": "true",
                },
            }
        ],
        "users": [
            {"id": operator["id"], "username": operator["username"], "enabled": True,
             "firstName": "Fictional", "lastName": "Operator", "emailVerified": True,
             "email": "operator@invalid.example", "requiredActions": [],
             "clientRoles": {"realm-management": ["manage-realm"]},
             "credentials": [{"type": "password", "value": operator["password"], "temporary": False}]},
            *[
            {
                "id": user["id"],
                "username": name,
                "enabled": True,
                "firstName": "Fictional",
                "lastName": "Fixture",
                "email": name + "@invalid.example",
                "emailVerified": True,
                "realmRoles": ["admin"],
                "requiredActions": [],
                "credentials": [
                    {"type": "password", "value": user["password"], "temporary": False}
                ],
            }
            for name, user in users.items()
        ]],
    }
    if realm_transform is not None:
        realm_transform(imported, users)
    docker("network", "create", "--label", label, network)
    try:

        def launch(
            name: str,
            image: str,
            *,
            options: tuple[str, ...] = (),
            arguments: tuple[str, ...] = (),
            ports: tuple[int, ...] = (),
        ) -> str:
            """Remember every successfully created, explicitly labeled target."""
            command = [
                "run",
                "-d",
                "--pull=never",
                "--name",
                owner + "-" + name,
                "--label",
                label,
                "--network",
                network,
            ]
            for port in ports:
                command += ["-p", "127.0.0.1::" + str(port)]
            container = docker(*command, *options, image, *arguments)
            containers.append(container)
            return container

        collector = launch(
            "collector",
            "python:3.13-alpine",
            ports=(8443,),
            options=(
                "--network-alias",
                "collector",
                "-v",
                f"{directory}:/fixture:ro",
                "-v",
                f"{ROOT / 'tests/fixtures/keycloak_collector.py'}:/collector.py:ro",
            ),
            arguments=("python", "-u", "/collector.py"),
        )
        collector_port = docker("port", collector, "8443/tcp").rsplit(":", 1)[1]
        # Test-only dynamic issuer is derived from the loopback listener;
        # hostname-strict=false is never a production configuration recipe.
        idp = launch(
            "idp",
            IMAGE,
            ports=(8443,),
            options=(
                "-v",
                f"{directory}:/fixture:ro",
                "--tmpfs",
                "/opt/keycloak/data/import:rw,size=1m,mode=0700,uid=1000,gid=0",
                "--entrypoint",
                "/bin/bash",
            ),
            arguments=(
                "-c",
                ('for ((i=0;i<300;i++)); do [[ -s /opt/keycloak/data/import/realm.json ]] && '
                 'exec /opt/keycloak/bin/kc.sh "$@"; sleep 0.1; done; exit 1'),
                "fixture-start",
                "start-dev",
                "--http-enabled=false",
                "--https-certificate-file=/fixture/server.pem",
                "--https-certificate-key-file=/fixture/server.key",
                "--hostname-strict=false",
                "--truststore-paths=/fixture/ca.pem",
                "--import-realm",
                "--log-level=error",
            ),
        )
        import_realm(idp, json.dumps(imported).encode("utf-8"))
        idp_port = docker("port", idp, "8443/tcp").rsplit(":", 1)[1]
        issuer = f"https://localhost:{idp_port}/realms/{realm}"
        password = secrets.token_urlsafe(24)
        pg = launch(
            "pg",
            "postgres:18.6-alpine",
            ports=(5432,),
            options=(
                "--tmpfs",
                "/var/lib/postgresql:rw,size=256m",
                "-e",
                "POSTGRES_PASSWORD=" + password,
                "-e",
                "POSTGRES_INITDB_ARGS=--no-sync",
            ),
        )
        port = docker("port", pg, "5432/tcp").rsplit(":", 1)[1]
        admin = {
            "host": "127.0.0.1",
            "port": int(port),
            "dbname": "postgres",
            "user": "postgres",
            "password": password,
            "autocommit": True,
            "connect_timeout": 2,
        }
        deadline = time.monotonic() + 30
        while True:
            try:
                db = psycopg.connect(**admin)
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Disposable provider database readiness deadline exceeded"
                    ) from None
                time.sleep(0.2)
        with db:
            for migration in sorted((ROOT / "database/migrations").glob("*.sql")):
                db.execute(migration.read_text(encoding="utf-8"))
            db.execute(
                psycopg.sql.SQL(
                    "CREATE ROLE patient_runtime LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
                ).format(psycopg.sql.Literal(password))
            )
            recipe = (ROOT / "database/patient_runtime_grants.sql").read_text(
                encoding="utf-8"
            )
            db.execute(
                "\n".join(
                    line for line in recipe.splitlines() if not line.startswith("\\")
                ).replace(':"runtime_role"', '"patient_runtime"')
            )
        with httpx.Client(
            verify=provider_ssl_context(directory / "ca.pem"),
            trust_env=False,
            timeout=3,
            follow_redirects=False,
        ) as browser:
            _ready(browser, f"https://localhost:{collector_port}/")
            response = _ready(browser, issuer)
            public = json.loads(response)["public_key"]
            key = serialization.load_pem_public_key(
                (
                    "-----BEGIN PUBLIC KEY-----\n"
                    + public
                    + "\n-----END PUBLIC KEY-----\n"
                ).encode()
            )
            # Fixture enrollment only, over verified TLS; never app discovery.
            jwks = json.loads(_ready(browser, issuer + "/protocol/openid-connect/certs"))
            from jwt.algorithms import RSAAlgorithm

            verification_keys = {item["kid"]: RSAAlgorithm.from_jwk(item)
                                 for item in jwks["keys"] if item.get("kty") == "RSA"
                                 and item.get("alg") == "RS256" and item.get("use") == "sig"}
        yield Runtime(
            issuer,
            f"https://localhost:{collector_port}/",
            directory / "ca.pem",
            client_secret,
            users,
            key,
            dict(admin, user="patient_runtime", autocommit=False),
            admin,
            verification_keys,
            operator,
        )
    finally:
        cleanup_owned(containers, network, owner, sys.exception())
