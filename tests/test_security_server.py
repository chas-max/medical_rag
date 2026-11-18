"""Trusted startup tests; loader doubles are separate from real PostgreSQL."""

# ruff: noqa: F401,F811 -- imported pytest fixtures
import os
import secrets
import socket
import ssl
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from test_oidc_login import APP_ORIGIN, TENANT, configuration
from test_patient_vertical import (
    auth_system,
    database,
    oidc_database,
    signing_key,
    system,
)

from medical_rag import security_server
from medical_rag.security_config import StartupError, load_configuration


@pytest.fixture
def config_file(tmp_path, signing_key):
    (tmp_path / "dsn").write_text(
        "postgresql://fictional-secret@localhost/test", encoding="utf-8"
    )
    (tmp_path / "client-secret").write_text("fictional-secret", encoding="utf-8")
    (tmp_path / "key").write_bytes(b"x" * 32)
    (tmp_path / "notice").write_text(
        "Institution-approved fictional test notice", encoding="utf-8"
    )
    (tmp_path / "public.pem").write_bytes(
        signing_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "medical.example.org")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(signing_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(signing_key, hashes.SHA256())
    )
    (tmp_path / "tls.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "tls.key").write_bytes(
        signing_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    build = tmp_path / "dist-patient"
    (build / "assets").mkdir(parents=True)
    (build / "patient.html").write_text(
        '<html><script type="module" src="/assets/patient.js"></script></html>',
        encoding="utf-8",
    )
    (build / "assets/patient.js").write_text(
        'document.title="patient"', encoding="utf-8"
    )
    config = tmp_path / "patient.toml"
    config.write_text(
        """tenant_id = "00000000-0000-0000-0000-000000000101"
database_dsn_file = "dsn"
transaction_key_file = "key"
frontend_dist = "dist-patient"
listen_host = "127.0.0.1"
listen_port = 8443
tls_cert_file = "tls.pem"
tls_key_file = "tls.key"
[oidc]
issuer = "https://identity.example.org/realms/hospital"
authorization_endpoint = "https://identity.example.org/realms/hospital/protocol/openid-connect/auth"
token_endpoint = "https://identity.example.org/realms/hospital/protocol/openid-connect/token"
callback_uri = "https://medical.example.org/auth/callback"
client_id = "medical-rag"
client_secret_file = "client-secret"
public_key_file = "public.pem"
mfa_acr_values = ["urn:hospital:mfa"]
[health_notice]
version = "fictional-v1"
text_file = "notice"
validity_seconds = 3600
""",
        encoding="utf-8",
    )
    return config


def test_relative_config_is_bounded_and_environment_independent(
    config_file, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv("DATABASE_URL", "secret-ignored")
    loaded = load_configuration(config_file)
    assert loaded.frontend_dist == config_file.parent / "dist-patient"
    assert loaded.notice.version == "fictional-v1"
    assert "fictional-secret" not in repr(loaded)
    assert loaded.login.client_id == "medical-rag"
    assert loaded.chat is None


def test_optional_chat_endpoint_is_loopback_only(config_file):
    config_file.write_text(
        config_file.read_text(encoding="utf-8")
        + '\n[chat]\nbase_url = "http://127.0.0.1:8001"\ntimeout_seconds = 120\ntop_k = 6\n',
        encoding="utf-8",
    )
    loaded = load_configuration(config_file)
    assert loaded.chat is not None
    assert loaded.chat.base_url == "http://127.0.0.1:8001"


@pytest.mark.parametrize(
    "section",
    [
        'base_url = "https://example.org"\ntimeout_seconds = 120\ntop_k = 6',
        'base_url = "http://127.0.0.1:8001/path"\ntimeout_seconds = 120\ntop_k = 6',
        'base_url = "http://127.0.0.1:8001"\ntimeout_seconds = 0\ntop_k = 6',
        'base_url = "http://127.0.0.1:8001"\ntimeout_seconds = 120\ntop_k = 21',
        'base_url = "http://127.0.0.1:8001"\ntimeout_seconds = 120\ntop_k = 6\nsecret = "x"',
    ],
)
def test_invalid_chat_configuration_is_sanitized(config_file, section):
    config_file.write_text(
        config_file.read_text(encoding="utf-8") + "\n[chat]\n" + section + "\n",
        encoding="utf-8",
    )
    with pytest.raises(StartupError, match="^患者服务启动失败，请联系机构运维。$"):
        load_configuration(config_file)


@pytest.mark.parametrize(
    "replace",
    [
        ("listen_port = 8443", "listen_port = true"),
        ("listen_port = 8443", "listen_port = 0"),
        ('listen_host = "127.0.0.1"', 'listen_host = "0.0.0.0"'),
        ('listen_host = "127.0.0.1"', 'listen_host = "::"'),
        ('listen_host = "127.0.0.1"', 'listen_host = "localhost"'),
        ('tenant_id = "00000000-0000-0000-0000-000000000101"', 'tenant_id = "secret"'),
        ('client_id = "medical-rag"', 'unknown = "secret"'),
        ("listen_port = 8443", 'listen_port = 8443\nunknown = "secret"'),
        ('mfa_acr_values = ["urn:hospital:mfa"]', 'mfa_acr_values = "secret"'),
        ("validity_seconds = 3600", "validity_seconds = 31536001"),
        ('database_dsn_file = "dsn"', "database_dsn_file = 5"),
    ],
)
def test_invalid_configuration_is_sanitized(config_file, replace):
    config_file.write_text(
        config_file.read_text(encoding="utf-8").replace(*replace), encoding="utf-8"
    )
    with pytest.raises(StartupError, match="^患者服务启动失败，请联系机构运维。$"):
        load_configuration(config_file)


@pytest.mark.parametrize(
    "file,data",
    [
        ("key", b"x" * 31),
        ("public.pem", b"invalid-secret"),
        ("dsn", b"x" * 16385),
        ("client-secret", b""),
        ("notice", b"x" * 65537),
        ("tls.key", b"secret"),
        ("patient.toml", b"x" * 65537),
    ],
    ids=[
        "key",
        "publickey",
        "dsn-bound",
        "empty-secret",
        "notice-bound",
        "tls",
        "config-bound",
    ],
)
def test_bad_files_fail_without_sensitive_details(config_file, file, data):
    (config_file.parent / file).write_bytes(data)
    with pytest.raises(StartupError) as error:
        load_configuration(config_file)
    assert str(error.value) == "患者服务启动失败，请联系机构运维。"


def test_missing_files_and_dedicated_build_denied(config_file):
    with pytest.raises(StartupError):
        load_configuration(config_file.parent / "absent")
    (config_file.parent / "dist-patient/patient.html").unlink()
    with pytest.raises(StartupError):
        security_server.load_patient_application(config_file)


def test_cli_fixed_failure_and_no_sensitive_logs(capsys):
    assert security_server.main(["--config", "never-exists-secret"]) == 1
    output = capsys.readouterr()
    assert output.err.strip() == "患者服务启动失败，请联系机构运维。"
    assert "secret" not in output.out + output.err


def test_direct_tls_invocation_guards(config_file, monkeypatch):
    observed = {}
    monkeypatch.setattr(security_server, "preflight", lambda *_: None)
    monkeypatch.setattr(
        security_server.uvicorn, "run", lambda app, **kwargs: observed.update(kwargs)
    )
    assert security_server.main(["--config", str(config_file)]) == 0
    assert observed["access_log"] is False and observed["proxy_headers"] is False
    assert observed["server_header"] is False and observed["log_config"] is None
    assert observed["ssl_keyfile"] == str(config_file.parent / "tls.key")
    assert observed["limit_concurrency"] == 100 and observed["timeout_keep_alive"] == 5


def test_app_mounts_only_dedicated_build_with_self_only_csp(config_file, monkeypatch):
    monkeypatch.setattr(security_server, "preflight", lambda *_: None)
    app = security_server.load_patient_application(config_file)
    with TestClient(app, base_url=APP_ORIGIN, headers={}) as client:
        response = client.get("/")
        assert response.status_code == 200 and "/assets/patient.js" in response.text
        assert client.get("/assets/patient.js").status_code == 200
        assert client.get("/assets/").status_code == 404
        for path in [
            "/ui",
            "/api/chat",
            "/settings",
            "/docs",
            "/openapi.json",
            "/patient.toml",
            "/assets/../patient.toml",
        ]:
            # This isolated asset-loader test deliberately has no live DB.
            # Mandatory failed-attempt auditing therefore fails closed, rather
            # than silently disabling audit to return an ordinary route404.
            assert client.get(path).status_code == 503
        registered = {getattr(route, "path", None) for route in app.routes}
        # /chat is the dedicated POST-only patient answer route, not legacy chat.
        assert "/chat" in registered
        assert client.get("/chat").status_code in {405, 503}
        assert not registered & {"/ui", "/api/chat", "/settings", "/docs", "/openapi.json"}
        csp = response.headers["content-security-policy"]
        assert (
            "script-src 'self'" in csp
            and "unsafe-" not in csp
            and "form-action 'self'" in csp
        )
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
        assert (
            client.get("/", headers={"Origin": "https://evil.example"}).status_code
            == 403
        )
        assert (
            client.get("/", headers={"X-Forwarded-Host": "evil.example"}).status_code
            == 200
        )
    with TestClient(app, base_url="http://medical.example.org", headers={}) as client:
        assert (
            client.get("/", headers={"X-Forwarded-Proto": "https"}).status_code == 403
        )


def test_headless_csp_default_is_unchanged(signing_key):
    from medical_rag.oidc_login import OIDCLogin, create_auth_app

    login = OIDCLogin(
        configuration(signing_key),
        lambda: None,
        tenant_id=TENANT,
        transaction_encryption_key=b"x" * 32,
    )
    with TestClient(create_auth_app(login), base_url=APP_ORIGIN, headers={}) as client:
        assert (
            client.get("/absent").headers["content-security-policy"]
            == "default-src 'none'; frame-ancestors 'none'"
        )


def test_patient_form_csp_permits_only_trusted_fixed_idp_origin(signing_key):
    from medical_rag.oidc_login import OIDCLogin, create_auth_app

    login = OIDCLogin(
        configuration(signing_key),
        lambda: None,
        tenant_id=TENANT,
        transaction_encryption_key=b"x" * 32,
    )
    with TestClient(
        create_auth_app(login, patient_html=True), base_url=APP_ORIGIN, headers={}
    ) as client:
        csp = client.get(
            "/absent",
            headers={
                "Forwarded": "host=evil.example",
                "X-Forwarded-Host": "evil.example",
            },
        ).headers["content-security-policy"]
        directives = dict(part.strip().split(" ", 1) for part in csp.split(";"))
        assert directives["form-action"] == "'self' https://identity.example.org"
        assert (
            directives["script-src"]
            == directives["style-src"]
            == directives["connect-src"]
            == "'self'"
        )
        assert "evil" not in csp and "*" not in csp and "unsafe-" not in csp


@pytest.mark.parametrize(
    "authority",
    [
        "*.example.org",
        "identity.example.org;script-src",
        "identity.example.org'",
        "identity\\evil.example.org",
        "[::1%25;upgrade-insecure-requests;]",
        "[fe80::1%25eth0]",
    ],
)
def test_patient_csp_rejects_unsafe_authority_syntax(authority):
    from types import SimpleNamespace

    from medical_rag.oidc_login import create_auth_app

    login = SimpleNamespace(
        config=SimpleNamespace(
            app_origin=APP_ORIGIN,
            authorization_endpoint="https://" + authority + "/authorize",
        )
    )
    with pytest.raises(ValueError):
        create_auth_app(login, patient_html=True)


@pytest.mark.integration
def test_owner_grant_recipe_supports_real_runtime_operations(
    config_file, system, database
):
    from medical_rag.identity_sessions import SessionStore
    from medical_rag.oidc_identity import VerifiedIdentity
    from medical_rag.patient_service import PatientService

    _, admin = database
    role = "patient_recipe_" + secrets.token_hex(6)
    with psycopg.connect(**admin) as db:
        db.execute(
            psycopg.sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
            ).format(
                psycopg.sql.Identifier(role), psycopg.sql.Literal(admin["password"])
            )
        )
        recipe = (
            Path(__file__).resolve().parents[1] / "database/patient_runtime_grants.sql"
        ).read_text(encoding="utf-8")
        recipe = "\n".join(
            line for line in recipe.splitlines() if not line.startswith("\\")
        )
        db.execute(
            recipe.replace(
                ':"runtime_role"', psycopg.sql.Identifier(role).as_string(db)
            )
        )
    params = dict(admin, user=role, autocommit=False)
    (config_file.parent / "dsn").write_text(
        psycopg.conninfo.make_conninfo(
            **{k: v for k, v in params.items() if k != "autocommit"}
        ),
        encoding="utf-8",
    )
    loaded = load_configuration(config_file)
    security_server.preflight(loaded)
    sessions = SessionStore(lambda: psycopg.connect(**params), tenant_id=TENANT)
    issued = sessions.issue(VerifiedIdentity(loaded.login.issuer, "subject-1", True))
    service = PatientService(sessions, loaded.notice)
    assert service.change_consent(
        issued.token,
        csrf_token=issued.csrf_token,
        granted=True,
        version=loaded.notice.version,
    )["granted"]
    created = service.create_conversation(
        issued.token, csrf_token=issued.csrf_token, title="fictional"
    )
    service.append_turn(
        issued.token, created["id"], csrf_token=issued.csrf_token, content="fictional"
    )
    assert len(service.list_turns(issued.token, created["id"])["items"]) == 1
    login = __import__("medical_rag.oidc_login", fromlist=["OIDCLogin"]).OIDCLogin(
        loaded.login,
        lambda: psycopg.connect(**params),
        tenant_id=TENANT,
        transaction_encryption_key=loaded.transaction_key,
    )
    flow = login.transactions.create()
    assert (
        login.transactions.consume(flow.state, flow.browser_token).tenant_version >= 0
    )
    # Real append/revoke/fence/replay using ONLY the exact owner-run recipe
    # role above. Not a second broader fixture role or synthetic DB grants.
    from medical_rag.identity_sessions import AuthenticationDenied
    from medical_rag.provider_logout import VerifiedProviderLogout

    when = int(time.time())
    old = sessions.issue(VerifiedIdentity(loaded.login.issuer, "subject-1", False,
                                        "recipe-provider-session", when))
    event = VerifiedProviderLogout(loaded.login.issuer, secrets.token_urlsafe(24),
                                   "subject-1", "recipe-provider-session", when, when + 120)
    sessions.provider_logout(event)
    with pytest.raises(AuthenticationDenied):
        sessions.authenticate(old.token)
    with pytest.raises(AuthenticationDenied):
        sessions.issue(VerifiedIdentity(loaded.login.issuer, "subject-1", False,
                                       "recipe-provider-session", when))
    fresh = sessions.issue(VerifiedIdentity(loaded.login.issuer, "subject-1", False,
                                          "recipe-provider-session", when + 1))
    sessions.provider_logout(event)
    sessions.authenticate(fresh.token)
    with psycopg.connect(**admin) as db:
        assert db.execute("SELECT count(*) FROM medical_rag.provider_logout_events WHERE issuer=%s",
                          (loaded.login.issuer,)).fetchone() == (1,)


@pytest.mark.integration
def test_actual_recovery_postgres_denied(config_file, system, database):
    """Create one OWN disposable clone; never change the existing primary."""
    runtime, admin = database
    # Resolve only the fixture's random primary port. Attach an OWN temporary
    # network and create an OWN bounded-memory streaming standby container.
    inventory = subprocess.run(
        ["docker", "ps", "--format", "{{.ID}} {{.Ports}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    containers = [
        line.split()[0]
        for line in inventory
        if f"127.0.0.1:{admin['port']}->5432" in line
    ]
    assert len(containers) == 1
    container = containers[0]
    network = "patient-recovery-" + secrets.token_hex(6)
    standby = None
    subprocess.run(
        ["docker", "network", "create", network],
        capture_output=True,
        check=True,
        timeout=10,
    )
    try:
        subprocess.run(
            [
                "docker",
                "network",
                "connect",
                "--alias",
                "patient-primary",
                network,
                container,
            ],
            capture_output=True,
            check=True,
            timeout=10,
        )
        # Only this test's disposable primary: permit password-authenticated
        # replication on the isolated temporary Docker network.
        with psycopg.connect(**admin) as db:
            hba = db.execute("SHOW hba_file").fetchone()[0]
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-c",
                f"printf '\\nhost replication postgres all scram-sha-256\\n' >> '{hba}'",
            ],
            capture_output=True,
            check=True,
            timeout=10,
        )
        with psycopg.connect(**admin) as db:
            db.execute("SELECT pg_reload_conf()")
        standby = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--network",
                network,
                "-p",
                "127.0.0.1::5432",
                "--tmpfs",
                "/standby:rw,size=256m,uid=70,gid=70,mode=0700",
                "--user",
                "postgres",
                "-e",
                f"PGPASSWORD={admin['password']}",
                "--entrypoint",
                "sh",
                "postgres:18.6-alpine",
                "-c",
                "pg_basebackup -c fast -h patient-primary -U postgres -D /standby -R -X stream && exec postgres -D /standby -c listen_addresses='*'",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        port = (
            subprocess.run(
                ["docker", "port", standby, "5432/tcp"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            .stdout.strip()
            .rsplit(":", 1)[1]
        )
        params = dict(runtime, port=int(port))
        for _ in range(100):
            try:
                with psycopg.connect(**params) as db:
                    assert db.execute("SELECT pg_is_in_recovery()").fetchone() == (
                        True,
                    )
                break
            except psycopg.OperationalError:
                time.sleep(0.1)
        else:
            pytest.fail("disposable recovery server did not become ready")
        (config_file.parent / "dsn").write_text(
            psycopg.conninfo.make_conninfo(
                **{k: v for k, v in params.items() if k != "autocommit"}
            ),
            encoding="utf-8",
        )
        with pytest.raises(StartupError):
            security_server.load_patient_application(config_file)
    finally:
        if standby:
            subprocess.run(
                ["docker", "rm", "-f", standby],
                capture_output=True,
                timeout=15,
                check=False,
            )
        subprocess.run(
            ["docker", "network", "disconnect", network, container],
            capture_output=True,
            timeout=10,
            check=False,
        )
        subprocess.run(
            ["docker", "network", "rm", network],
            capture_output=True,
            timeout=10,
            check=False,
        )


@pytest.mark.integration
def test_actual_primary_least_privilege_startup_and_authority_denials(
    config_file, system, database
):
    runtime, admin = database
    dsn = config_file.parent / "dsn"
    dsn.write_text(
        psycopg.conninfo.make_conninfo(
            **{k: v for k, v in runtime.items() if k != "autocommit"}
        ),
        encoding="utf-8",
    )
    assert security_server.load_patient_application(config_file) is not None
    with psycopg.connect(**admin) as db:
        for grant, revoke in [
            (
                "GRANT UPDATE(active) ON medical_rag.tenants TO login_test",
                "REVOKE UPDATE(active) ON medical_rag.tenants FROM login_test",
            ),
            (
                "GRANT INSERT(role) ON medical_rag.identity_roles TO login_test",
                "REVOKE INSERT(role) ON medical_rag.identity_roles FROM login_test",
            ),
            (
                "GRANT UPDATE(identity_verified) ON medical_rag.patient_subjects TO login_test",
                "REVOKE UPDATE(identity_verified) ON medical_rag.patient_subjects FROM login_test",
            ),
            (
                "GRANT UPDATE(granted) ON medical_rag.patient_consent_events TO login_test",
                "REVOKE UPDATE(granted) ON medical_rag.patient_consent_events FROM login_test",
            ),
            (
                "GRANT DELETE ON medical_rag.patient_consent_events TO login_test",
                "REVOKE DELETE ON medical_rag.patient_consent_events FROM login_test",
            ),
        ]:
            db.execute(grant)
            try:
                with pytest.raises(StartupError):
                    security_server.load_patient_application(config_file)
            finally:
                db.execute(revoke)
        db.execute(
            "UPDATE medical_rag.tenants SET active=false WHERE tenant_id=%s", (TENANT,)
        )
        with pytest.raises(StartupError):
            security_server.load_patient_application(config_file)
        db.execute(
            "UPDATE medical_rag.tenants SET active=true WHERE tenant_id=%s", (TENANT,)
        )
    dsn.write_text(
        psycopg.conninfo.make_conninfo(
            **{k: v for k, v in admin.items() if k != "autocommit"}
        ),
        encoding="utf-8",
    )
    with pytest.raises(StartupError):
        security_server.load_patient_application(config_file)


@pytest.mark.integration
def test_actual_wrong_tenant_missing_schema_denied(config_file, system, database):
    runtime, admin = database
    (config_file.parent / "dsn").write_text(
        psycopg.conninfo.make_conninfo(
            **{k: v for k, v in runtime.items() if k != "autocommit"}
        ),
        encoding="utf-8",
    )
    original = config_file.read_text(encoding="utf-8")
    config_file.write_text(
        original.replace(TENANT, "00000000-0000-0000-0000-000000000999"),
        encoding="utf-8",
    )
    with pytest.raises(StartupError):
        security_server.load_patient_application(config_file)
    config_file.write_text(original, encoding="utf-8")
    with psycopg.connect(**admin) as db:
        db.execute(
            "ALTER TABLE medical_rag.patient_subjects RENAME COLUMN age_classification TO missing_age"
        )
        try:
            with pytest.raises(StartupError):
                security_server.load_patient_application(config_file)
        finally:
            db.execute(
                "ALTER TABLE medical_rag.patient_subjects RENAME COLUMN missing_age TO age_classification"
            )


@pytest.mark.integration
def test_required_dml_and_forced_rls_are_checked(config_file, system, database):
    runtime, admin = database
    (config_file.parent / "dsn").write_text(
        psycopg.conninfo.make_conninfo(
            **{k: v for k, v in runtime.items() if k != "autocommit"}
        ),
        encoding="utf-8",
    )
    with psycopg.connect(**admin) as db:
        for table in ["patient_consent_events", "conversations", "identity_sessions"]:
            db.execute(f"REVOKE INSERT ON medical_rag.{table} FROM login_test")
            try:
                with pytest.raises(StartupError):
                    security_server.load_patient_application(config_file)
            finally:
                db.execute(f"GRANT INSERT ON medical_rag.{table} TO login_test")
        db.execute("ALTER TABLE medical_rag.conversations NO FORCE ROW LEVEL SECURITY")
        try:
            with pytest.raises(StartupError):
                security_server.load_patient_application(config_file)
        finally:
            db.execute("ALTER TABLE medical_rag.conversations FORCE ROW LEVEL SECURITY")


@pytest.mark.integration
@pytest.mark.parametrize("change,restore", [
    ("ALTER TABLE medical_rag.provider_logout_events RENAME TO missing_provider_logout",
     "ALTER TABLE medical_rag.missing_provider_logout RENAME TO provider_logout_events"),
    ("ALTER TABLE medical_rag.identity_sessions RENAME COLUMN authenticated_at TO missing_authenticated_at",
     "ALTER TABLE medical_rag.identity_sessions RENAME COLUMN missing_authenticated_at TO authenticated_at"),
    ("ALTER TABLE medical_rag.provider_logout_events NO FORCE ROW LEVEL SECURITY",
     "ALTER TABLE medical_rag.provider_logout_events FORCE ROW LEVEL SECURITY"),
    ("GRANT UPDATE(issued_at) ON medical_rag.provider_logout_events TO login_test",
     "REVOKE UPDATE(issued_at) ON medical_rag.provider_logout_events FROM login_test"),
    ("GRANT DELETE ON medical_rag.provider_logout_events TO login_test",
     "REVOKE DELETE ON medical_rag.provider_logout_events FROM login_test"),
    ("GRANT TRUNCATE ON medical_rag.provider_logout_events TO login_test",
     "REVOKE TRUNCATE ON medical_rag.provider_logout_events FROM login_test"),
    ("GRANT INSERT(recorded_at) ON medical_rag.provider_logout_events TO login_test",
     "REVOKE INSERT(recorded_at) ON medical_rag.provider_logout_events FROM login_test"),
    ("REVOKE INSERT(issued_at) ON medical_rag.provider_logout_events FROM login_test",
     "GRANT INSERT(issued_at) ON medical_rag.provider_logout_events TO login_test"),
])
def test_schema6_preflight_rejects_missing_or_broad_provider_authority(config_file, system, database, change, restore):
    runtime, admin = database
    (config_file.parent / "dsn").write_text(psycopg.conninfo.make_conninfo(
        **{k: v for k, v in runtime.items() if k != "autocommit"}), encoding="utf-8")
    config = load_configuration(config_file)
    with psycopg.connect(**admin) as db:
        db.execute(change)
        try:
            with pytest.raises(StartupError):
                security_server.preflight(config)
        finally:
            db.execute(restore)


@pytest.mark.parametrize(
    "dsn",
    [
        "host=db.example.org dbname=postgres user=patient password=secret sslmode=disable",
        "host=127.0.0.1,db.example.org dbname=postgres user=patient password=secret",
        "host=127.0.0.1 hostaddr=203.0.113.1 dbname=postgres user=patient password=secret",
    ],
)
def test_unsafe_remote_database_transport_denied(config_file, dsn, monkeypatch):
    (config_file.parent / "dsn").write_text(dsn, encoding="utf-8")
    connected = []
    monkeypatch.setattr(
        security_server.psycopg,
        "connect",
        lambda *args, **kwargs: connected.append(True),
    )
    with pytest.raises(StartupError):
        security_server._connect(load_configuration(config_file))
    assert connected == []


def test_help_succeeds_without_startup_or_secret_access(capsys):
    assert security_server.main(["--help"]) == 0
    assert "启动失败" not in capsys.readouterr().err


@pytest.mark.integration
def test_real_direct_tls_process_serves_only_dedicated_build(
    config_file, system, database
):
    runtime, _ = database
    (config_file.parent / "dsn").write_text(
        psycopg.conninfo.make_conninfo(
            **{k: v for k, v in runtime.items() if k != "autocommit"}
        ),
        encoding="utf-8",
    )
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    config_file.write_text(
        config_file.read_text(encoding="utf-8").replace(
            "listen_port = 8443", f"listen_port = {port}"
        ),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "medical_rag.security_server",
            "--config",
            str(config_file),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        ),
    )
    context = ssl.create_default_context(cafile=str(config_file.parent / "tls.pem"))
    try:
        for _ in range(100):
            try:
                with (
                    socket.create_connection(("127.0.0.1", port), timeout=0.2) as raw,
                    context.wrap_socket(
                        raw, server_hostname="medical.example.org"
                    ) as tls,
                ):
                    tls.sendall(
                        b"GET / HTTP/1.1\r\nHost: medical.example.org\r\nConnection: close\r\n\r\n"
                    )
                    response = b""
                    while chunk := tls.recv(65536):
                        response += chunk
                break
            except (ConnectionError, TimeoutError):
                time.sleep(0.1)
        else:
            pytest.fail("direct TLS test listener did not become ready")
        assert (
            response.startswith(b"HTTP/1.1 200") and b"/assets/patient.js" in response
        )
        assert b"server: uvicorn" not in response.lower()
        assert b"cache-control: no-store" in response.lower()
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
    assert stdout == b"" and stderr == b""

def _with_logging(config_file, body):
    config_file.write_text(
        config_file.read_text(encoding="utf-8") + "\n[logging]\n" + body + "\n",
        encoding="utf-8",
    )
    return config_file


def test_portal_logging_is_off_unless_configured(config_file):
    loaded = load_configuration(config_file)
    assert loaded.logging_config.enabled is False
    assert loaded.logging_config.directory is None


def test_portal_logging_disabled_section_is_honoured(config_file):
    loaded = load_configuration(_with_logging(config_file, "enabled = false"))
    assert loaded.logging_config.enabled is False
    assert loaded.logging_config.directory is None


def test_portal_logging_enabled_resolves_bounded_options(config_file):
    loaded = load_configuration(
        _with_logging(
            config_file,
            "\n".join(
                (
                    "enabled = true",
                    'directory = "portal-logs"',
                    'level = "debug"',
                    'rotation = "5 MB"',
                    'retention = "3 days"',
                    'compression = "gz"',
                    "json = true",
                )
            ),
        )
    )
    logging_config = loaded.logging_config
    assert logging_config.enabled is True
    assert logging_config.directory == config_file.parent / "portal-logs"
    assert logging_config.level == "DEBUG"
    assert logging_config.rotation == "5 MB"
    assert logging_config.retention == "3 days"
    assert logging_config.compression == "gz"
    assert logging_config.json_output is True


def test_portal_logging_can_disable_compression(config_file):
    loaded = load_configuration(
        _with_logging(config_file, 'enabled = true\ndirectory = "logs"\ncompression = ""')
    )
    assert loaded.logging_config.compression == ""


@pytest.mark.parametrize(
    "body",
    [
        'enabled = "true"\ndirectory = "logs"',
        "enabled = true",
        'enabled = true\ndirectory = ""',
        'enabled = true\ndirectory = "logs"\nlevel = "verbose"',
        'enabled = true\ndirectory = "logs"\nrotation = "20 XB"',
        'enabled = true\ndirectory = "logs"\nretention = "forever"',
        'enabled = true\ndirectory = "logs"\ncompression = "rar"',
        'enabled = true\ndirectory = "logs"\njson = "yes"',
        'enabled = true\ndirectory = "logs"\nunexpected = 1',
    ],
)
def test_invalid_portal_logging_is_rejected_statically(config_file, body):
    with pytest.raises(StartupError):
        load_configuration(_with_logging(config_file, body))
