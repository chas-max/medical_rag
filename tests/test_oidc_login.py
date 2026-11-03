"""Protocol tests use a fake provider, but a real disposable PostgreSQL store."""

import gzip
import os
import secrets
import shutil
import subprocess
import time
from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from medical_rag.oidc_login import (
    InvalidLogin,
    LoginConfiguration,
    LoginTransactionStore,
    OIDCLogin,
    create_auth_app,
)

ROOT = Path(__file__).resolve().parents[1]
TENANT = "00000000-0000-0000-0000-000000000101"
ACCOUNT = "00000000-0000-0000-0000-000000000201"
ISSUER = "https://identity.example.org/realms/hospital"
APP_ORIGIN = "https://medical.example.org"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def configuration(key, **overrides):
    return LoginConfiguration(
        **({"issuer": ISSUER, "authorization_endpoint": ISSUER + "/protocol/openid-connect/auth",
            "token_endpoint": ISSUER + "/protocol/openid-connect/token",
            "callback_uri": APP_ORIGIN + "/auth/callback", "client_id": "medical-rag",
            "client_secret": "fictional-client-secret-with-32-characters", "public_key": key.public_key(),
            "mfa_acr_values": frozenset({"urn:hospital:mfa"})} | overrides)
    )


@pytest.fixture(scope="module")
def database():
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("set RUN_POSTGRES_INTEGRATION=1 for disposable PostgreSQL tests")
    if not shutil.which("docker"):
        pytest.skip("Docker is required")
    import psycopg
    password = secrets.token_urlsafe(24)
    started = subprocess.run([
        "docker", "run", "-d", "--rm", "-p", "127.0.0.1::5432",
        "--tmpfs", "/var/lib/postgresql:rw,size=256m", "-e", f"POSTGRES_PASSWORD={password}",
        "-e", "POSTGRES_INITDB_ARGS=--no-sync", "postgres:18.6-alpine",
    ], check=False, capture_output=True, text=True)
    if started.returncode != 0:
        # A CalledProcessError repr includes the command's ephemeral password.
        # Keep process output/arguments private even when Docker is unavailable.
        pytest.fail("disposable PostgreSQL could not start", pytrace=False)
    container = started.stdout.strip()
    try:
        port = subprocess.run(["docker", "port", container, "5432/tcp"], check=True,
                              capture_output=True, text=True).stdout.strip().rsplit(":", 1)[1]
        admin = {"host": "127.0.0.1", "port": int(port), "dbname": "postgres", "user": "postgres",
                 "password": password, "autocommit": True}
        for _ in range(100):
            try:
                connection = psycopg.connect(**admin)
                break
            except psycopg.OperationalError:
                time.sleep(0.1)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")
        with connection:
            for name in ("0001_patient_scope.sql", "0002_identity_sessions.sql", "0003_oidc_login.sql",
                         "0004_patient_access.sql", "0005_security_audit.sql", "0006_provider_logout.sql",
                         "0007_support_cases.sql"):
                connection.execute((ROOT / "database/migrations" / name).read_text(encoding="utf-8"))
            connection.execute(psycopg.sql.SQL("CREATE ROLE login_test LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS")
                               .format(psycopg.sql.Literal(password)))
            connection.execute("GRANT USAGE ON SCHEMA medical_rag TO login_test")
            connection.execute("GRANT SELECT ON medical_rag.tenants, medical_rag.identity_accounts, "
                               "medical_rag.identity_roles, medical_rag.identity_bindings TO login_test")
            connection.execute("GRANT SELECT,INSERT,UPDATE ON medical_rag.identity_sessions TO login_test")
            connection.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON medical_rag.oidc_login_transactions TO login_test")
            connection.execute("GRANT SELECT ON medical_rag.provider_logout_events TO login_test")
            connection.execute("GRANT INSERT (tenant_id,issuer,jti_hash,subject_hash,sid_hash,issued_at,expires_at) "
                               "ON medical_rag.provider_logout_events TO login_test")
        yield dict(admin, user="login_test", autocommit=False), admin
    finally:
        subprocess.run(["docker", "stop", "--time", "1", container], capture_output=True, check=False)


@pytest.fixture
def system(database, signing_key):
    import psycopg
    runtime, admin = database
    with psycopg.connect(**admin) as connection:
        # Audited patient fixtures add append-only history. Only this disposable
        # database's owner may disable the truncate guard for per-test reset.
        audited = connection.execute("SELECT to_regclass('medical_rag.security_audit_events')").fetchone()[0]
        if audited:
            connection.execute("ALTER TABLE medical_rag.security_audit_events DISABLE TRIGGER security_audit_no_truncate")
        connection.execute("ALTER TABLE medical_rag.provider_logout_events DISABLE TRIGGER provider_logout_no_truncate")
        connection.execute("TRUNCATE medical_rag.oidc_login_transactions, medical_rag.identity_sessions, "
                           "medical_rag.identity_roles, medical_rag.identity_bindings, "
                           "medical_rag.identity_accounts, medical_rag.tenants CASCADE")
        if audited:
            connection.execute("ALTER TABLE medical_rag.security_audit_events ENABLE TRIGGER security_audit_no_truncate")
        connection.execute("ALTER TABLE medical_rag.provider_logout_events ENABLE TRIGGER provider_logout_no_truncate")
        connection.execute("INSERT INTO medical_rag.tenants(tenant_id) VALUES (%s)", (TENANT,))
        connection.execute("INSERT INTO medical_rag.identity_accounts(tenant_id,account_id,issuer,subject) "
                           "VALUES (%s,%s,%s,'subject-1')", (TENANT, ACCOUNT, ISSUER))
        connection.execute("INSERT INTO medical_rag.identity_roles VALUES (%s,%s,'patient')", (TENANT, ACCOUNT))
    provider = {"nonce": None, "calls": [], "mode": "valid"}

    def response(request):
        provider["calls"].append(request)
        if provider["mode"] == "raise":
            raise RuntimeError("provider-private-secret-NEVER-LOG")
        if provider["mode"] == "reactivate":
            with psycopg.connect(**admin) as connection:
                connection.execute("UPDATE medical_rag.tenants SET active=false")
                connection.execute("UPDATE medical_rag.tenants SET active=true")
        if provider["mode"] == "redirect":
            return httpx.Response(302, headers={"location": "https://attacker.example/steal"})
        if provider["mode"] == "error":
            return httpx.Response(400, json={"error": "fake-token-NEVER-LEAK", "error_description": "private-record"})
        if provider["mode"] == "large":
            return httpx.Response(200, content=b"x" * 100000)
        now = datetime.now(UTC)
        token = jwt.encode({"iss": ISSUER, "aud": "medical-rag", "sub": "subject-1",
                            "iat": now, "exp": now + timedelta(minutes=5),
                            "auth_time": int(now.timestamp()), "acr": "urn:hospital:mfa",
                            "nonce": provider["nonce"] if provider["mode"] != "nonce" else "wrong-nonce"},
                           signing_key, algorithm="RS256")
        payload = {"access_token": "fake-access-NEVER-RETURN", "token_type": "Bearer",
                   "refresh_token": "fake-refresh-NEVER-RETURN", "id_token": token}
        mutations = {
            "missing_access": ("access_token", None), "empty_access": ("access_token", ""),
            "nonstring_access": ("access_token", {"secret": "fake-access-NEVER-RETURN"}),
            "missing_type": ("token_type", None), "empty_type": ("token_type", ""),
            "wrong_type": ("token_type", "MAC"), "missing_id": ("id_token", None),
            "empty_id": ("id_token", ""),
        }
        if provider["mode"] in mutations:
            field, value = mutations[provider["mode"]]
            if value is None:
                del payload[field]
            else:
                payload[field] = value
        if provider["mode"] == "non_json":
            import json
            return httpx.Response(200, content=json.dumps(payload).encode(), headers={"Content-Type": "text/plain"})
        if provider["mode"].startswith("valid_token_status_"):
            status = int(provider["mode"].rsplit("_", 1)[1])
            headers = {"Location": "https://attacker.example/steal"} if status == 302 else {}
            result = httpx.Response(status, json=payload, headers=headers)
            provider["non_success_response"] = result
            return result
        if provider["mode"] in {"large_valid", "large_stream", "compressed"}:
            payload["padding"] = "x" * 100000
        if provider["mode"] == "large_stream":
            import json
            body = json.dumps(payload).encode()
            class ResponseStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    for index in range(0, len(body), 1000):
                        yield body[index:index + 1000]
            return httpx.Response(200, stream=ResponseStream(), headers={"Content-Type": "application/json"})
        if provider["mode"] == "compressed":
            import json
            return httpx.Response(200, content=gzip.compress(json.dumps(payload).encode()),
                                  headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})
        return httpx.Response(200, json=payload)

    login = OIDCLogin(configuration(signing_key), lambda: psycopg.connect(**runtime), tenant_id=TENANT,
                      transaction_encryption_key=secrets.token_bytes(32), transport=httpx.MockTransport(response))
    app = create_auth_app(login)
    with TestClient(app, base_url=APP_ORIGIN, headers={}) as client:
        yield client, login, provider, admin


def start(system):
    client, _, provider, _ = system
    response = client.post("/auth/login", headers={"Origin": APP_ORIGIN}, follow_redirects=False)
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    provider["nonce"] = query["nonce"][0]
    return response, query


@pytest.mark.integration
def test_login_uses_fixed_endpoints_pkce_and_secure_cookie(system):
    response, query = start(system)
    assert urlsplit(response.headers["location"]).path == "/realms/hospital/protocol/openid-connect/auth"
    assert query["response_type"] == ["code"] and query["scope"] == ["openid"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [APP_ORIGIN + "/auth/callback"]
    assert query["max_age"] == ["600"]
    assert query["state"] != query["nonce"]
    assert "code_verifier" not in query and "client_secret" not in query
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-medical-oidc=")
    assert all(flag in cookie.lower() for flag in ("secure", "httponly", "samesite=lax", "path=/"))
    assert "domain=" not in cookie.lower()


@pytest.mark.integration
@pytest.mark.parametrize("accepted", [frozenset({"urn:z:mfa", "urn:a:mfa"}), frozenset()])
def test_login_requests_only_sorted_trusted_nonessential_acr(request, accepted):
    from dataclasses import replace

    system = request.getfixturevalue("system")
    system[1].config = replace(system[1].config, mfa_acr_values=accepted)
    _, query = start(system)
    request_matches = query.get("acr_values") == ([" ".join(sorted(accepted))] if accepted else None)
    assert request_matches, "Trusted ACR request must be sorted or omitted when empty"
    nonessential = "claims" not in query
    assert nonessential, "ACR request must not require an essential claim"


@pytest.mark.integration
def test_callback_verifies_identity_issues_session_and_never_returns_provider_tokens(system, caplog):
    client, login, provider, _ = system
    _, query = start(system)
    response = client.get("/auth/callback", params={"code": "one-use-code", "state": query["state"][0], "iss": ISSUER},
                          follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert len(provider["calls"]) == 1
    request = provider["calls"][0]
    form = parse_qs(request.content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["one-use-code"] and len(form["code_verifier"][0]) >= 43
    assert form["redirect_uri"] == [APP_ORIGIN + "/auth/callback"]
    challenge = urlsafe_b64encode(sha256(form["code_verifier"][0].encode("ascii")).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [challenge]
    assert request.headers["accept-encoding"] == "identity"
    session = client.cookies.get("__Host-medical-session")
    assert login.sessions.authenticate(session).user_id == ACCOUNT
    for cookie in response.headers.get_list("set-cookie"):
        assert "httponly" in cookie.lower() and "secure" in cookie.lower()
        assert "domain=" not in cookie.lower()
    assert "fake-access-NEVER-RETURN" not in response.text + caplog.text
    assert "fake-refresh-NEVER-RETURN" not in response.text + caplog.text
    assert form["code_verifier"][0] not in caplog.text


@pytest.mark.integration
def test_callback_is_browser_bound_single_use_and_rejects_replay(system):
    client, _, provider, _ = system
    _, query = start(system)
    state = query["state"][0]
    cookie = client.cookies.get("__Host-medical-oidc")
    client.cookies.clear()
    assert client.get("/auth/callback", params={"code": "code", "state": state}).status_code == 401
    assert provider["calls"] == []
    client.cookies.set("__Host-medical-oidc", cookie)
    assert client.get("/auth/callback", params={"code": "code", "state": state}, follow_redirects=False).status_code == 303
    client.cookies.set("__Host-medical-oidc", cookie)
    assert client.get("/auth/callback", params={"code": "code", "state": state}).status_code == 401
    assert len(provider["calls"]) == 1


@pytest.mark.integration
def test_reauthentication_rotates_session_and_revokes_previous_session(system):
    from medical_rag.identity_sessions import AuthenticationDenied

    client, login, _, _ = system
    _, first = start(system)
    assert client.get("/auth/callback", params={"code": "first", "state": first["state"][0]},
                      follow_redirects=False).status_code == 303
    old = client.cookies.get("__Host-medical-session")
    assert login.sessions.authenticate(old).user_id == ACCOUNT
    _, second = start(system)
    assert client.get("/auth/callback", params={"code": "second", "state": second["state"][0]},
                      follow_redirects=False).status_code == 303
    new = client.cookies.get("__Host-medical-session")
    assert new != old
    assert login.sessions.authenticate(new).user_id == ACCOUNT
    with pytest.raises(AuthenticationDenied):
        login.sessions.authenticate(old)


@pytest.mark.integration
def test_duplicate_session_cookies_are_rejected(system):
    client, login, _, _ = system
    _, query = start(system)
    assert client.get("/auth/callback", params={"code": "code", "state": query["state"][0]},
                      follow_redirects=False).status_code == 303
    token = client.cookies.get("__Host-medical-session")
    csrf = client.cookies.get("__Host-medical-csrf")
    cookies = f"__Host-medical-session={token}; __Host-medical-session={token}; __Host-medical-csrf={csrf}"
    response = client.get("/auth/session", headers={"Cookie": cookies})
    assert response.status_code == 401
    assert login.sessions.authenticate(token).user_id == ACCOUNT


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["nonce", "error", "redirect", "large", "large_valid", "large_stream", "compressed", "raise",
                                  "missing_access", "empty_access", "nonstring_access", "missing_type", "empty_type",
                                  "wrong_type", "missing_id", "empty_id", "non_json"])
def test_provider_failures_are_generic_terminal_and_never_create_session(system, mode, caplog):
    import psycopg
    client, _, provider, admin = system
    _, query = start(system)
    browser = client.cookies.get("__Host-medical-oidc")
    provider["mode"] = mode
    response = client.get("/auth/callback", params={"code": "code", "state": query["state"][0]}, follow_redirects=False)
    assert response.status_code == 401
    assert response.json()["success"] is False
    assert not client.cookies.get("__Host-medical-session")
    assert len(provider["calls"]) == 1
    assert "fake-token-NEVER-LEAK" not in response.text + caplog.text
    assert "private-record" not in response.text + caplog.text
    assert "provider-private-secret-NEVER-LOG" not in response.text + caplog.text
    with psycopg.connect(**admin) as connection:
        assert connection.execute("SELECT count(*) FROM medical_rag.identity_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM medical_rag.oidc_login_transactions").fetchone()[0] == 0
    client.cookies.set("__Host-medical-oidc", browser)
    assert client.get("/auth/callback", params={"code": "code", "state": query["state"][0]}, follow_redirects=False).status_code == 401
    assert len(provider["calls"]) == 1


@pytest.mark.integration
@pytest.mark.parametrize("status", [302, 400, 401])
def test_valid_id_token_in_non_200_response_is_rejected_and_flow_stays_consumed(system, status, caplog):
    import psycopg
    client, _, provider, admin = system
    _, query = start(system)
    browser = client.cookies.get("__Host-medical-oidc")
    provider["mode"] = f"valid_token_status_{status}"
    callback = {"code": "one-use-code", "state": query["state"][0]}
    response = client.get("/auth/callback", params=callback, follow_redirects=False)
    assert response.status_code == 401
    assert response.json()["success"] is False
    assert not client.cookies.get("__Host-medical-session")
    assert provider["non_success_response"].is_closed
    with psycopg.connect(**admin) as connection:
        assert connection.execute("SELECT count(*) FROM medical_rag.identity_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM medical_rag.oidc_login_transactions").fetchone()[0] == 0
    # Restore the browser proof so this specifically checks durable consumption,
    # rather than being rejected only because the failure cleared its cookie.
    client.cookies.set("__Host-medical-oidc", browser)
    assert client.get("/auth/callback", params=callback, follow_redirects=False).status_code == 401
    assert len(provider["calls"]) == 1
    assert "fake-access-NEVER-RETURN" not in response.text + caplog.text
    assert "fake-refresh-NEVER-RETURN" not in response.text + caplog.text


@pytest.mark.integration
def test_logout_requires_session_bound_csrf_and_origin_and_revokes(system):
    client, login, _, _ = system
    _, query = start(system)
    client.get("/auth/callback", params={"code": "code", "state": query["state"][0]}, follow_redirects=False)
    session_token = client.cookies.get("__Host-medical-session")
    info = client.get("/auth/session")
    assert info.status_code == 200 and info.json()["data"]["user_id"] == ACCOUNT
    csrf = info.json()["data"]["csrf_token"]
    assert client.post("/auth/logout", headers={"Origin": APP_ORIGIN}).status_code == 403
    assert client.post("/auth/logout", headers={"Origin": "https://attacker.example", "X-CSRF-Token": csrf}).status_code == 403
    assert client.post("/auth/logout", headers={"Origin": APP_ORIGIN, "X-CSRF-Token": csrf}).status_code == 200
    from medical_rag.identity_sessions import AuthenticationDenied
    with pytest.raises(AuthenticationDenied):
        login.sessions.authenticate(session_token)


@pytest.mark.integration
@pytest.mark.parametrize("query", ["state=x&state=y&code=x", "state=x&code=x&code=y", "state=x&code=x&id_token=evil", "state=x&code=x&iss=https://evil.example"])
def test_malformed_callback_never_contacts_token_endpoint(system, query):
    client, _, provider, _ = system
    start(system)
    assert client.get("/auth/callback?" + query).status_code == 401
    assert provider["calls"] == []


@pytest.mark.integration
def test_expired_login_transaction_and_stored_secrets_are_protected(system, database):
    import psycopg
    client, login, _, admin = system
    _, query = start(system)
    browser = client.cookies.get("__Host-medical-oidc")
    with psycopg.connect(**admin) as connection:
        stored = repr(connection.execute("SELECT * FROM medical_rag.oidc_login_transactions").fetchall())
        assert query["state"][0] not in stored and query["nonce"][0] not in stored and browser not in stored
        connection.execute("UPDATE medical_rag.oidc_login_transactions SET expires_at=clock_timestamp()-interval '1 second'")
    assert client.get("/auth/callback", params={"code": "code", "state": query["state"][0]}).status_code == 401
    assert login.config.client_secret not in repr(login.config)


@pytest.mark.integration
def test_tenant_disable_and_reenable_cannot_revive_pending_login(system):
    import psycopg
    client, _, provider, admin = system
    _, query = start(system)
    with psycopg.connect(**admin) as connection:
        connection.execute("UPDATE medical_rag.tenants SET active=false")
        connection.execute("UPDATE medical_rag.tenants SET active=true")
    assert client.get("/auth/callback", params={"code": "code", "state": query["state"][0]}).status_code == 401
    assert provider["calls"] == []


@pytest.mark.integration
def test_tenant_disable_and_reenable_during_token_exchange_cannot_issue_session(system):
    client, _, provider, _ = system
    _, query = start(system)
    provider["mode"] = "reactivate"
    response = client.get("/auth/callback", params={"code": "code", "state": query["state"][0]}, follow_redirects=False)
    assert response.status_code == 401
    assert not client.cookies.get("__Host-medical-session")


@pytest.mark.integration
@pytest.mark.parametrize("version", [True, "0", -1, 2**63])
def test_session_issuance_rejects_malformed_trusted_epoch(system, version):
    from medical_rag.identity_sessions import AuthenticationDenied
    from medical_rag.oidc_identity import VerifiedIdentity
    _, login, _, _ = system
    with pytest.raises(AuthenticationDenied):
        login.sessions.issue(VerifiedIdentity(ISSUER, "subject-1", True), expected_tenant_version=version)


@pytest.mark.integration
def test_not_found_errors_use_the_api_envelope(system):
    client, _, _, _ = system
    response = client.get("/conversations")
    assert response.status_code == 404
    assert response.json()["success"] is False
    assert response.json()["data"] is None and "error" in response.json()


@pytest.mark.integration
def test_unexpected_storage_exception_is_not_reraised_to_asgi_or_logged(system, monkeypatch, caplog):
    client, login, _, _ = system
    def fail():
        raise RuntimeError("fictional-private-record-NEVER-LOG")
    monkeypatch.setattr(login.transactions, "create", fail)
    # TestClient re-raises unhandled ASGI errors by default. A fixed response
    # alone is insufficient if ServerErrorMiddleware then logs the traceback.
    response = client.post("/auth/login", headers={"Origin": APP_ORIGIN})
    assert response.status_code == 503
    assert "fictional-private-record-NEVER-LOG" not in response.text + caplog.text


@pytest.mark.integration
def test_consumption_is_atomic_across_workers(system, database):
    import psycopg
    _, _, _, _ = system
    runtime, _ = database
    key = secrets.token_bytes(32)
    first = LoginTransactionStore(lambda: psycopg.connect(**runtime), tenant_id=TENANT, encryption_key=key)
    challenge = first.create()
    other = LoginTransactionStore(lambda: psycopg.connect(**runtime), tenant_id=TENANT,
                                  encryption_key=key)
    def consume(store):
        try:
            store.consume(challenge.state, challenge.browser_token)
            return True
        except InvalidLogin:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(consume, (first, other)))
    assert sorted(results) == [False, True]


@pytest.mark.integration
def test_expiry_is_rechecked_after_waiting_for_login_row_lock(system, database):
    import psycopg
    _, _, _, admin = system
    runtime, _ = database
    key = secrets.token_bytes(32)
    store = LoginTransactionStore(lambda: psycopg.connect(**runtime), tenant_id=TENANT, encryption_key=key)
    challenge = store.create()
    app_name = "oidc-lock-" + secrets.token_hex(8)
    waiting = LoginTransactionStore(lambda: psycopg.connect(**dict(runtime, application_name=app_name)),
                                    tenant_id=TENANT, encryption_key=key)
    with psycopg.connect(**admin) as connection:
        connection.execute("UPDATE medical_rag.oidc_login_transactions SET expires_at=clock_timestamp()+interval '2 seconds'")
    with psycopg.connect(**dict(admin, autocommit=False)) as locker:
        locker.execute("SELECT state_hash FROM medical_rag.oidc_login_transactions FOR UPDATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(waiting.consume, challenge.state, challenge.browser_token)
            try:
                deadline = time.monotonic() + 2
                with psycopg.connect(**admin) as observer:
                    while time.monotonic() < deadline:
                        row = observer.execute("SELECT wait_event_type FROM pg_catalog.pg_stat_activity WHERE application_name=%s",
                                               (app_name,)).fetchone()
                        if row and row[0] == "Lock":
                            break
                        time.sleep(0.03)
                    else:
                        pytest.fail("login consumption did not wait for row lock")
                time.sleep(2.1)
            finally:
                locker.commit()
            with pytest.raises(InvalidLogin):
                pending.result(timeout=5)


@pytest.mark.integration
def test_corrupted_encrypted_context_is_rejected_and_stays_consumed(system):
    import psycopg
    _, login, _, admin = system
    challenge = login.transactions.create()
    with psycopg.connect(**admin) as connection:
        stored = repr(connection.execute("SELECT * FROM medical_rag.oidc_login_transactions").fetchall())
        assert challenge.code_verifier not in stored and challenge.nonce not in stored
        connection.execute("UPDATE medical_rag.oidc_login_transactions SET encrypted_context=%s", (b"corrupt",))
    for _ in range(2):
        with pytest.raises(InvalidLogin):
            login.transactions.consume(challenge.state, challenge.browser_token)
    with psycopg.connect(**admin) as connection:
        assert connection.execute("SELECT count(*) FROM medical_rag.oidc_login_transactions").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [("issuer", "http://identity.example.org"),
    ("token_endpoint", "https://evil.example/token"), ("authorization_endpoint", ISSUER + "/auth?next=evil"),
    ("callback_uri", APP_ORIGIN + "/auth/callback?next=evil"), ("callback_uri", "http://medical.example.org/auth/callback"),
    ("token_endpoint", ISSUER + "/token?"), ("issuer", ISSUER + "#")])
def test_untrusted_or_ambiguous_configuration_rejected(signing_key, field, value):
    with pytest.raises(ValueError):
        configuration(signing_key, **{field: value})


@pytest.mark.integration
def test_origin_and_host_cannot_change_trusted_login_configuration(system):
    client, _, provider, _ = system
    assert client.post("/auth/login", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/auth/login", headers={"Origin": APP_ORIGIN, "Host": "evil.example"}).status_code == 403
    assert client.post("/auth/login").status_code == 403
    assert client.get("/conversations").status_code == 404
    assert provider["calls"] == []
