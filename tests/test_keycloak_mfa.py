"""Actual native password+OTP, signed assurance and isolated auditor API.

Provider boundary: pinned Keycloak real verified-HTTPS sockets. Application:
fixed HTTPS TestClient, real disposable least-privilege PostgreSQL authority.
"""

# ruff: noqa: F401,F811 -- imported fixture functions
import base64
import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from keycloak_forms import password_challenge, submit_otp
from keycloak_mfa import ACR, compose_mfa, otp_code, wrong_otp_code
from keycloak_runtime import APP_ORIGIN, identity_runtime
from test_keycloak_identity import application as raw_application
from test_keycloak_identity import csrf_headers, no_provider_url_logging
from test_keycloak_rotation import captured_logout

from medical_rag.oidc_login import OIDCLogin
from medical_rag.patient_api import create_patient_app
from medical_rag.patient_service import HealthNotice

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def provider(tmp_path_factory):
    with identity_runtime(tmp_path_factory.mktemp("actual-mfa"), realm_transform=compose_mfa) as runtime:
        yield runtime


@pytest.fixture
def application(raw_application, provider):
    _, original, tenant, accounts = raw_application
    with psycopg.connect(**provider.admin) as db:
        for name in ("staff", "staff_norole", "staff_unenrolled"):
            db.execute("DELETE FROM medical_rag.identity_roles WHERE tenant_id=%s AND account_id=%s",
                       (tenant, accounts[name]))
            if name != "staff_norole":
                db.execute("INSERT INTO medical_rag.identity_roles VALUES (%s,%s,'auditor')", (tenant, accounts[name]))
    login = OIDCLogin(replace(original.config, mfa_acr_values=frozenset({ACR})),
                      lambda: psycopg.connect(**provider.database), tenant_id=tenant,
                      transaction_encryption_key=b"m" * 32)
    notice = HealthNotice("fictional-v1", "Fictional reference MFA only.", 3600)
    with TestClient(create_patient_app(login, notice), base_url=APP_ORIGIN, headers={}) as client:
        yield client, login, tenant, accounts


def begin(client, browser, provider, name):
    response = client.post("/auth/login", headers={"Origin": APP_ORIGIN}, follow_redirects=False)
    started = response.status_code == 303
    assert started, "Application must start the native OIDC flow"
    return password_challenge(browser, response.headers["location"], name, provider.users[name]["password"])


def finish(client, redirect):
    callback_matches = bool(redirect) and urlsplit(redirect)[:3] == urlsplit(APP_ORIGIN + "/auth/callback")[:3]
    assert callback_matches, "Provider callback must match the exact trusted origin and path"
    return client.get(redirect, follow_redirects=False)


def actual_mfa(client, browser, provider, name):
    pending, redirect = begin(client, browser, provider, name)
    actual_challenge = pending is not None and redirect is None
    assert actual_challenge, "Accepted password must require actual native OTP before any callback"
    no_session = not client.cookies.get("__Host-medical-session")
    assert no_session, "Password acceptance alone must not issue an application session"
    code = otp_code(provider.users[name])
    pending, redirect = submit_otp(browser, pending, code)
    accepted = pending is None and redirect is not None
    assert accepted, "Actual provider must accept the real fictional OTP"
    completed = finish(client, redirect).status_code == 303
    assert completed, "Actual signed OTP identity must complete the application session"
    return code


def test_actual_password_otp_signed_assurance_allows_only_current_auditor(application, provider):
    client, login, _, _ = application
    with provider.browser() as browser:
        actual_mfa(client, browser, provider, "staff")
    principal = login.sessions.authenticate(client.cookies.get("__Host-medical-session"))
    assurance_verified = principal.mfa_verified is True
    assert assurance_verified, "Existing verifier/session authority must record signed accepted assurance"
    audit_permission = client.get("/me").json()["data"]["permissions"]["audit"] is True
    assert audit_permission, "Current local auditor with verified OTP must have audit permission"
    auditor_allowed = client.get("/audit").status_code == 200
    assert auditor_allowed, "Existing isolated auditor API must allow current verified auditor"
    patient_denied = client.get("/me/consents/health").status_code == 403 and client.get("/conversations").status_code == 403
    assert patient_denied, "Staff MFA must not grant self-patient consent or content access"


@pytest.mark.parametrize("name", ["patient1", "admin"])
def test_actual_requested_acr_does_not_upgrade_password_only_identity(application, provider, name):
    client, login, _, _ = application
    with provider.browser() as browser:
        pending, redirect = begin(client, browser, provider, name)
        lower_authentication = pending is None and redirect is not None
        assert lower_authentication, "Non-staff classification must complete only password authentication"
        completed = finish(client, redirect).status_code == 303
        assert completed, "Nonessential requested ACR must permit lower-assurance identity"
    principal = login.sessions.authenticate(client.cookies.get("__Host-medical-session"))
    lower_assurance = principal.mfa_verified is False
    assert lower_assurance, "Requested ACR alone must not verify MFA"
    audit_denied = client.get("/audit").status_code == 403 and client.get("/me").json()["data"]["permissions"]["audit"] is False
    assert audit_denied, "Patient or local low-assurance auditor must not access audit"
    if name == "patient1":
        patient_self = client.get("/me/consents/health").status_code == 200
        assert patient_self, "Patient password identity must retain adult verified self functionality"


def test_actual_mfa_without_local_role_cannot_grant_audit(application, provider):
    client, login, _, _ = application
    with provider.browser() as browser:
        actual_mfa(client, browser, provider, "staff_norole")
    verified_but_unprivileged = login.sessions.authenticate(client.cookies.get("__Host-medical-session")).mfa_verified is True and client.get("/audit").status_code == 403
    assert verified_but_unprivileged, "Provider classification and actual MFA must not grant a local auditor role"


def test_actual_wrong_missing_and_uncompleted_otp_cannot_issue_session(application, provider):
    client, _, _, _ = application
    with provider.browser() as browser:
        pending, redirect = begin(client, browser, provider, "staff")
        password_only_denied = pending is not None and redirect is None and client.get("/me").status_code == 401
        assert password_only_denied, "Accepted password with incomplete OTP must have no application session"
        for code in ("", wrong_otp_code(provider.users["staff"])):
            pending, redirect = submit_otp(browser, pending, code)
            actual_rejection = pending is not None and redirect is None
            assert actual_rejection, "Actual provider must reject absent or incorrect OTP without callback"
        guessed = client.get("/auth/callback", params={"state": "guessed", "code": "guessed"}, follow_redirects=False)
        guessed_denied = guessed.status_code == 401 and not client.cookies.get("__Host-medical-session")
        assert guessed_denied, "Guessed callback must not manufacture staff authority"


def test_actual_unenrolled_staff_cannot_silently_bypass_otp(application, provider):
    client, _, _, _ = application
    with provider.browser() as browser:
        pending, redirect = begin(client, browser, provider, "staff_unenrolled")
    unenrolled_denied = pending is None and bool(redirect) and urlsplit(redirect)[:2] == urlsplit(provider.issuer)[:2] and "required-action" in urlsplit(redirect).path and client.get("/me").status_code == 401
    assert unenrolled_denied, "Unenrolled staff must redirect to provider required action, never application callback/session"


def test_actual_sso_reauthentication_requires_otp_again_and_stale_challenge_denied(application, provider):
    client, _login, _, _ = application
    with provider.browser() as browser:
        pending, redirect = begin(client, browser, provider, "staff")
        first_challenge = pending is not None and redirect is None
        assert first_challenge, "Cold staff authentication must require OTP"
        original_pending_cookie = client.cookies.get("__Host-medical-oidc")
        browser_proof_present = bool(original_pending_cookie)
        assert browser_proof_present, "Pending login must issue browser-bound OIDC proof"
        stale = pending
        first_code = otp_code(provider.users["staff"])
        pending, redirect = submit_otp(browser, pending, first_code)
        first_callback = redirect
        completed = finish(client, redirect).status_code == 303
        assert completed, "Initial OTP authentication must complete"
        client.cookies.clear()
        pending, redirect = begin(client, browser, provider, "staff")
        new_challenge = pending is not None and redirect is None and client.get("/me").status_code == 401
        assert new_challenge, "Provider SSO must require a new OTP challenge at level2 max-age zero"
        pending, redirect = submit_otp(browser, pending, first_code)
        replay_rejected = pending is not None and redirect is None and client.get("/me").status_code == 401
        assert replay_rejected, "Actual provider must reject replayed OTP on a new SSO challenge"
        pending, redirect = submit_otp(browser, pending, otp_code(provider.users["staff"]))
        second_completed = finish(client, redirect).status_code == 303
        assert second_completed, "SSO reauthentication must complete only after a fresh real OTP"
        client.cookies.clear()
        no_authenticated_cookie = not client.cookies.get("__Host-medical-session")
        assert no_authenticated_cookie, "Consumed callback replay must start without an authenticated local cookie"
        replay = client.get(first_callback, headers={"Cookie": "__Host-medical-oidc=" + original_pending_cookie},
                            follow_redirects=False)
        callback_replay_denied = replay.status_code == 401 and not client.cookies.get("__Host-medical-session") and client.get("/me").status_code == 401
        assert callback_replay_denied, "Consumed first callback with original browser proof must not issue a new session"
        _old_pending, old_redirect = submit_otp(browser, stale, first_code)
        stale_callback = bool(old_redirect) and urlsplit(old_redirect)[:3] == urlsplit(APP_ORIGIN + "/auth/callback")[:3]
        provider_redirect = not old_redirect or urlsplit(old_redirect)[:2] == urlsplit(provider.issuer)[:2]
        stale_status = client.get(old_redirect, headers={"Cookie": "__Host-medical-oidc=" + original_pending_cookie},
                                  follow_redirects=False).status_code if stale_callback else None
        stale_denied = (provider_redirect or stale_status == 401) and not client.cookies.get("__Host-medical-session") and client.get("/me").status_code == 401
        assert stale_denied, "Consumed provider challenge must not issue a usable new application session"


@pytest.mark.parametrize("change", ["role", "disabled"])
def test_actual_mfa_current_role_and_account_revocation_block_next_access(application, provider, change):
    client, _, tenant, accounts = application
    with provider.browser() as browser:
        actual_mfa(client, browser, provider, "staff")
    with psycopg.connect(**provider.admin) as db:
        if change == "role":
            db.execute("DELETE FROM medical_rag.identity_roles WHERE tenant_id=%s AND account_id=%s", (tenant, accounts["staff"]))
        else:
            db.execute("UPDATE medical_rag.identity_accounts SET active=false WHERE tenant_id=%s AND account_id=%s", (tenant, accounts["staff"]))
    revoked = client.get("/audit").status_code in (401, 403)
    assert revoked, "Current local revocation must block the next auditor access"


def test_actual_mfa_provider_logout_and_audit_public_log_privacy(application, provider, caplog):
    client, _login, tenant, _ = application
    with provider.browser() as browser:
        used_code = actual_mfa(client, browser, provider, "staff")
        cookie = client.cookies.get("__Host-medical-session")
        response = client.get("/audit")
        request_ids = bool(response.headers.get("x-request-id"))
        assert request_ids, "Auditor responses must retain server request IDs"
        body = captured_logout(provider, browser)
    delivered = client.post("/auth/backchannel-logout", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 200
    assert delivered, "Actual signed provider logout must pass the existing signed route"
    revoked = client.get("/audit").status_code == 401 and client.get("/me").status_code == 401
    assert revoked, "Provider logout must invalidate the old actual-MFA local session"
    with psycopg.connect(**provider.admin) as db:
        audit = json.dumps(db.execute("SELECT * FROM medical_rag.security_audit_events WHERE tenant_id=%s", (tenant,)).fetchall(), default=str)
    exposed = audit + caplog.text + response.text + client.get("/me").text
    credentials_private = all(user["password"] not in exposed and ("otp_seed" not in user or user["otp_seed"].hex() not in exposed) for user in provider.users.values())
    assert credentials_private, "Audit/public errors/logs must exclude credentials and OTP seeds"
    seed_base32_private = all("otp_seed" not in user or base64.b32encode(user["otp_seed"]).decode().rstrip("=") not in exposed for user in provider.users.values())
    assert seed_base32_private, "Audit/public errors/logs must exclude actual Base32-imported OTP seeds"
    logout_token = parse_qs(body.decode())["logout_token"][0]
    tokens_private = cookie not in exposed and logout_token not in exposed
    assert tokens_private, "Audit/public errors/logs must exclude raw sessions and provider logout tokens"
    otp_private = used_code not in exposed
    assert otp_private, "Audit/public errors/logs must exclude the OTP actually submitted to provider"
