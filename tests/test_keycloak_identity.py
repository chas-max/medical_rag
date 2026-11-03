"""Actual pinned Keycloak native code/PKCE/TLS integration; fictional users only.

Provider HTTPS uses real sockets and signatures; app HTTP runs in TestClient on
the exact fixed HTTPS origin. This is not full-browser/full-socket E2E.
"""

import json
import logging
import secrets
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from keycloak_forms import password_redirect, provider_logout
from keycloak_runtime import APP_ORIGIN, CLIENT_ID, certificates, identity_runtime

from medical_rag.oidc_login import LoginConfiguration, OIDCLogin
from medical_rag.patient_api import create_patient_app
from medical_rag.patient_service import HealthNotice
from medical_rag.provider_logout import InvalidProviderLogout

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def no_provider_url_logging(monkeypatch):
    """Never emit authorization/callback queries through transport loggers."""
    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", True)


@pytest.fixture(scope="module")
def provider(tmp_path_factory):
    with identity_runtime(tmp_path_factory.mktemp("actual-keycloak")) as runtime:
        yield runtime


@pytest.fixture
def application(provider):
    tenant = str(uuid4())
    accounts = {}
    with psycopg.connect(**provider.admin) as db:
        db.execute("INSERT INTO medical_rag.tenants(tenant_id) VALUES (%s)", (tenant,))
        for name, user in provider.users.items():
            if name == "unprovisioned":
                continue
            account, patient = str(uuid4()), str(uuid4())
            accounts[name] = account
            db.execute(
                "INSERT INTO medical_rag.identity_accounts(tenant_id,account_id,issuer,subject,active) "
                "VALUES (%s,%s,%s,%s,%s)",
                (tenant, account, provider.issuer, user["id"], name != "disabled"),
            )
            db.execute(
                "INSERT INTO medical_rag.identity_roles VALUES (%s,%s,%s)",
                (tenant, account, "org_admin" if name == "admin" else "patient"),
            )
            if name == "admin":
                db.execute(
                    "INSERT INTO medical_rag.identity_roles VALUES (%s,%s,'auditor')",
                    (tenant, account),
                )
            db.execute(
                "INSERT INTO medical_rag.patient_subjects(tenant_id,patient_id,age_classification,identity_verified) "
                "VALUES (%s,%s,'adult',true)",
                (tenant, patient),
            )
            db.execute(
                "INSERT INTO medical_rag.identity_bindings VALUES (%s,%s,%s,%s)",
                (tenant, account, patient, name != "unverified"),
            )
    config = LoginConfiguration(
        issuer=provider.issuer,
        authorization_endpoint=provider.issuer + "/protocol/openid-connect/auth",
        token_endpoint=provider.issuer + "/protocol/openid-connect/token",
        callback_uri=APP_ORIGIN + "/auth/callback",
        client_id=CLIENT_ID,
        client_secret=provider.client_secret,
        public_key=None,
        verification_keys=provider.verification_keys,
        mfa_acr_values=frozenset({"urn:institution:real-mfa-required"}),
        provider_ca_file=provider.ca_file,
    )
    login = OIDCLogin(
        config,
        lambda: psycopg.connect(**provider.database),
        tenant_id=tenant,
        transaction_encryption_key=secrets.token_bytes(32),
    )
    notice = HealthNotice(
        "fictional-v1", "Fictional identity interoperability test notice only.", 3600
    )
    with TestClient(
        create_patient_app(login, notice), base_url=APP_ORIGIN, headers={}
    ) as client:
        yield client, login, tenant, accounts


def authenticate(client, browser, provider, name, *, wrong_password=False):
    """No fake upstream response/signature/transport and no bearer disclosure."""
    response = client.post(
        "/auth/login", headers={"Origin": APP_ORIGIN}, follow_redirects=False
    )
    assert response.status_code == 303
    authorization = response.headers["location"]
    query = parse_qs(urlsplit(authorization).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [APP_ORIGIN + "/auth/callback"]
    redirect = password_redirect(
        browser,
        authorization,
        name,
        "fictional-wrong-password"
        if wrong_password
        else provider.users[name]["password"],
    )
    if redirect is None:
        return None
    if urlsplit(redirect)[:3] != urlsplit(APP_ORIGIN + "/auth/callback")[:3]:
        raise RuntimeError("Unexpected callback origin/path")
    return client.get(redirect, follow_redirects=False)


def csrf_headers(client):
    response = client.get("/auth/session")
    assert response.status_code == 200
    return {"Origin": APP_ORIGIN, "X-CSRF-Token": response.json()["data"]["csrf_token"]}


def grant(client):
    response = client.post(
        "/me/consents/health",
        headers=csrf_headers(client),
        json={"version": "fictional-v1", "accepted": True},
    )
    assert response.status_code == 200


def test_actual_provider_own_records_and_local_authority(application, provider):
    client, _, _, accounts = application
    with provider.browser() as browser:
        assert authenticate(client, browser, provider, "patient1").status_code == 303
    me = client.get("/me").json()["data"]
    assert me["user_id"] == accounts["patient1"] and me["roles"] == ["patient"]
    assert (
        me["permissions"]["audit"] is False
    )  # upstream realm role admin is not local authority
    assert client.get("/conversations").status_code == 403
    assert client.get("/me/consents/health").json()["data"]["granted"] is False
    grant(client)
    response = client.post(
        "/conversations",
        headers=csrf_headers(client),
        json={"title": "Fictional private record"},
    )
    assert response.status_code == 200
    path = "/conversations/" + response.json()["data"]["id"]
    assert (
        client.post(
            path + "/turns",
            headers=csrf_headers(client),
            json={"content": "Fictional patient text"},
        ).status_code
        == 200
    )
    assert (
        client.get(path).status_code == client.get(path + "/turns").status_code == 200
    )
    client.cookies.clear()
    with provider.browser() as browser:
        assert authenticate(client, browser, provider, "patient2").status_code == 303
    grant(client)
    assert client.get(path).status_code == 404
    assert client.get(path + "/turns").status_code == 404
    assert client.get("/conversations").json()["data"]["items"] == []


@pytest.mark.parametrize("name", ["disabled", "unprovisioned"])
def test_actual_provider_cannot_provision_or_enable_local_accounts(
    application, provider, name
):
    client, _, _, _ = application
    with provider.browser() as browser:
        assert authenticate(client, browser, provider, name).status_code == 401
    has_session = bool(client.cookies.get("__Host-medical-session"))
    assert not has_session
    assert client.get("/me").status_code == 401


def test_actual_wrong_password_does_not_issue_local_session(application, provider):
    client, _, _, _ = application
    with provider.browser() as browser:
        assert (
            authenticate(client, browser, provider, "patient1", wrong_password=True)
            is None
        )
    has_session = bool(client.cookies.get("__Host-medical-session"))
    assert not has_session
    assert client.get("/auth/session").status_code == 401


@pytest.mark.parametrize("name", ["unverified", "admin"])
def test_actual_password_is_not_verified_binding_or_admin_mfa(
    application, provider, name
):
    client, _, _, _ = application
    with provider.browser() as browser:
        assert authenticate(client, browser, provider, name).status_code == 303
    assert client.get("/me/consents/health").status_code in (401, 403)
    assert client.get("/conversations").status_code in (401, 403)
    assert client.get("/audit").status_code == 403
    if name == "unverified":
        assert client.get("/me").status_code == 200
        assert client.get("/me").json()["data"]["binding"] == {"verified": False}
    else:
        assert client.get("/me").status_code == 200
        assert client.get("/me").json()["data"]["permissions"]["audit"] is False
        assert (
            application[1]
            .sessions.authenticate(client.cookies.get("__Host-medical-session"))
            .mfa_verified
            is False
        )


def test_actual_ca_mismatch_handshake_and_exchange_fail_closed(
    application, provider, tmp_path
):
    client, login, _, _ = application
    certificates(tmp_path)
    wrong_ca = tmp_path / "ca.pem"
    from medical_rag.provider_tls import provider_ssl_context

    with (
        httpx.Client(
            verify=provider_ssl_context(wrong_ca), trust_env=False, timeout=3
        ) as browser,
        pytest.raises(httpx.ConnectError),
    ):
        browser.get(provider.issuer)
    login.config = replace(login.config, provider_ca_file=wrong_ca)
    with provider.browser() as browser:
        assert authenticate(client, browser, provider, "patient1").status_code == 401
    has_session = bool(client.cookies.get("__Host-medical-session"))
    assert not has_session


@pytest.mark.parametrize("corruption", ["invalid", "missing"])
def test_changed_ca_after_login_start_fails_closed(
    application, provider, tmp_path, corruption
):
    """Valid startup trust cannot silently fall back after a later file change."""
    client, login, _, _ = application
    path = tmp_path / "operator-ca.pem"
    path.write_bytes(provider.ca_file.read_bytes())
    login.config = replace(login.config, provider_ca_file=path)
    started = client.post(
        "/auth/login", headers={"Origin": APP_ORIGIN}, follow_redirects=False
    )
    assert started.status_code == 303
    with provider.browser() as browser:
        callback = password_redirect(
            browser,
            started.headers["location"],
            "patient1",
            provider.users["patient1"]["password"],
        )
    if corruption == "invalid":
        path.write_bytes(b"INVALID-PRIVATE-CA-CANARY")
    else:
        path.unlink()
    response = client.get(callback, follow_redirects=False)
    assert response.status_code == 401
    has_session = bool(client.cookies.get("__Host-medical-session"))
    safe_response = (
        str(path) not in response.text
        and "INVALID-PRIVATE-CA-CANARY" not in response.text
    )
    assert not has_session and safe_response


def test_actual_provider_backchannel_and_fresh_login_survives_replay(
    application, provider, caplog
):
    client, login, tenant, _ = application
    with provider.browser() as browser:
        assert authenticate(client, browser, provider, "patient1").status_code == 303
        old_cookie = client.cookies.get("__Host-medical-session")
        provider_logout(browser, provider.issuer, CLIENT_ID, APP_ORIGIN + "/")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            capture = browser.get(provider.collector_url)
            if capture.content:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Actual provider backchannel capture missing")
    body = capture.content
    # Only structural metadata on conflict; never token or raw claim values.
    token = parse_qs(body.decode("utf-8"))["logout_token"][0]
    try:
        event = login.provider_logout_verifier.verify(token)
    except InvalidProviderLogout:
        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
        structural = {
            "header": {k: header.get(k) for k in ("alg", "typ")},
            "claim_types": {k: type(v).__name__ for k, v in claims.items()},
            "lifetime": claims.get("exp", 0) - claims.get("iat", 0),
        }
        pytest.fail("Actual provider profile conflict: " + json.dumps(structural))
    response = client.post(
        "/auth/backchannel-logout",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    assert client.get("/me").status_code == 401
    with psycopg.connect(**provider.admin) as db:
        row = db.execute(
            "SELECT operator_id,action,resource_type,outcome,reason_code FROM medical_rag.security_audit_events "
            "WHERE tenant_id=%s AND action='provider.logout'",
            (tenant,),
        ).fetchone()
        assert row[0] is None and row[1] == "provider.logout"
        audit = json.dumps(
            db.execute(
                "SELECT * FROM medical_rag.security_audit_events WHERE tenant_id=%s",
                (tenant,),
            ).fetchall(),
            default=str,
        )
    safe_tokens = (
        token not in audit + caplog.text and old_cookie not in audit + caplog.text
    )
    safe_subject = provider.users["patient1"]["id"] not in audit
    assert safe_tokens and safe_subject, "Audit/log privacy boundary failed"
    while time.time() <= event.issued_at + 1:
        time.sleep(0.1)
    client.cookies.clear()
    with provider.browser() as browser:
        assert authenticate(client, browser, provider, "patient1").status_code == 303
    fresh_cookie = client.cookies.get("__Host-medical-session")
    assert client.get("/me").status_code == 200
    assert (
        client.post(
            "/auth/backchannel-logout",
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 200
    )
    assert client.get("/me").status_code == 200
    from medical_rag.identity_sessions import AuthenticationDenied

    with pytest.raises(AuthenticationDenied):
        login.sessions.authenticate(old_cookie)
    login.sessions.authenticate(fresh_cookie)
