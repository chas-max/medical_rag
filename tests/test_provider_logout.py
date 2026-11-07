"""Signed fictional provider logout, with actual least-privilege PostgreSQL."""
# ruff: noqa: F401,F811 -- imported pytest fixtures

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Event as ThreadEvent
from uuid import UUID, uuid4

import httpx2 as httpx
import jwt
import psycopg
import pytest
from test_patient_vertical import (
    ACCOUNT,
    APP_ORIGIN,
    ISSUER,
    OTHER_ACCOUNT,
    OTHER_TENANT,
    ROOT,
    TENANT,
    auth_system,
    database,
    grant,
    logged_in,
    mutate,
    oidc_database,
    signing_key,
    start,
    system,
)
from test_security_audit import audit_insert, events

from medical_rag.identity_sessions import AuthenticationDenied
from medical_rag.oidc_identity import VerifiedIdentity

PATH = "/auth/backchannel-logout"
EVENT = "http://schemas.openid.net/event/backchannel-logout"


def claims(**overrides):
    now = int(time.time())
    return {"iss": ISSUER, "aud": "medical-rag", "iat": now, "exp": now + 120,
            "jti": str(uuid4()), "events": {EVENT: {}}, "sub": "subject-1"} | overrides


def signed(key, payload=None, **headers):
    return jwt.encode(payload or claims(), key, algorithm="RS256", headers={"typ": "logout+jwt"} | headers)


def post(client, token):
    return client.post(PATH, data={"logout_token": token})


@pytest.mark.integration
def test_http_signed_logout_revokes_old_cookie_and_fresh_login_survives_replay(system, signing_key):
    client, login, _, admin = system
    headers = logged_in(system)
    grant(client, headers)
    assert client.get("/conversations").status_code == 200
    payload = claims()
    token = signed(signing_key, payload)
    response = post(client, token)
    assert response.status_code == 200, response.text
    assert response.json() == {"success": True, "data": None, "error": None}
    assert "set-cookie" not in response.headers
    assert client.get("/me").status_code == 401
    fresh = login.sessions.issue(VerifiedIdentity(ISSUER, "subject-1", True, authenticated_at=payload["iat"] + 1))
    assert post(client, token).status_code == 200
    assert login.sessions.authenticate(fresh.token).user_id == ACCOUNT
    row = next(r for r in events(admin) if str(r["request_id"]) == response.headers["X-Request-ID"])
    assert row["action"] == "provider.logout" and row["resource_type"] == "session"
    assert row["operator_id"] is None


@pytest.mark.integration
def test_sid_only_fence_before_any_session_blocks_old_pending_identity(system, signing_key):
    client, login, _, _ = system
    payload = claims(sid="provider-session-CANARY")
    del payload["sub"]
    client.cookies.clear()
    assert post(client, signed(signing_key, payload)).status_code == 200
    for when in (None, payload["iat"] - 1, payload["iat"]):
        with pytest.raises(AuthenticationDenied):
            login.sessions.issue(VerifiedIdentity(ISSUER, "subject-1", True,
                                                provider_session_id=payload["sid"], authenticated_at=when))
    login.sessions.issue(VerifiedIdentity(ISSUER, "subject-1", True,
                                        provider_session_id=payload["sid"], authenticated_at=payload["iat"] + 1))


@pytest.mark.integration
def test_provider_revoke_commits_before_audit_failure_and_retry_is_safe(system, signing_key):
    client, login, _, admin = system
    logged_in(system)
    old = client.cookies.get("__Host-medical-session")
    payload = claims()
    token = signed(signing_key, payload)
    audit_insert(admin, allowed=False)
    try:
        assert post(client, token).status_code == 503
        with pytest.raises(AuthenticationDenied):
            login.sessions.authenticate(old)
        with pytest.raises(AuthenticationDenied):
            login.sessions.issue(VerifiedIdentity(ISSUER, "subject-1", True, authenticated_at=payload["iat"]))
    finally:
        audit_insert(admin, allowed=True)
    assert post(client, token).status_code == 200
    assert len([r for r in events(admin) if r["action"] == "provider.logout"]) == 1


@pytest.mark.integration
@pytest.mark.parametrize("target,revoked", [
    ({"sub": "subject-1"}, {0, 1}),
    ({"sid": "sid-A"}, {0, 2}),
    ({"sub": "subject-1", "sid": "sid-A"}, {0}),
    ({"sub": "no-local-match"}, set()),
], ids=["sub", "sid", "both", "missing"])
def test_targets_are_conjunction_and_do_not_revoke_new_authentication(system, signing_key, target, revoked):
    client, login, _, admin = system
    when = int(time.time())
    identities = [("subject-1", "sid-A", when - 1), ("subject-1", "sid-B", None),
                  ("subject-2", "sid-A", when), ("subject-1", "sid-A", when + 1)]
    sessions = [login.sessions.issue(VerifiedIdentity(ISSUER, subject, True, sid, at))
                for subject, sid, at in identities]
    payload = claims(iat=when)
    del payload["sub"]
    payload.update(target)
    response = post(client, signed(signing_key, payload))
    assert response.status_code == 200
    assert response.json()["data"] is None
    for index, session in enumerate(sessions):
        if index in revoked:
            with pytest.raises(AuthenticationDenied):
                login.sessions.authenticate(session.token)
        else:
            login.sessions.authenticate(session.token)
    with psycopg.connect(**admin) as db:
        stored = db.execute("SELECT provider_sid_hash FROM medical_rag.identity_sessions").fetchall()
    assert {bytes(row[0]) for row in stored} == {sha256(x.encode()).digest() for x in ("sid-A", "sid-B")}


@pytest.mark.integration
def test_disabled_authority_is_still_revoked_and_cross_issuer_tenant_untouched(system, database, signing_key):
    from medical_rag.identity_sessions import SessionStore
    client, login, _, admin = system
    runtime, _ = database
    other_issuer = ISSUER + "/other"
    other_account = str(uuid4())
    mutate(admin, "INSERT INTO medical_rag.identity_accounts(tenant_id,account_id,issuer,subject) "
           "VALUES (%s,%s,%s,'subject-1'),(%s,%s,%s,'subject-1')",
           (TENANT, other_account, other_issuer, OTHER_TENANT, str(uuid4()), ISSUER))
    other = login.sessions.issue(VerifiedIdentity(other_issuer, "subject-1", True, "sid-A", 1))
    tenant_store = SessionStore(lambda: psycopg.connect(**runtime), tenant_id=OTHER_TENANT)
    elsewhere = tenant_store.issue(VerifiedIdentity(ISSUER, "subject-1", True, "sid-A", 1))
    old = login.sessions.issue(VerifiedIdentity(ISSUER, "subject-1", False, "sid-A", 1))
    mutate(admin, "UPDATE medical_rag.identity_accounts SET active=false WHERE account_id=%s", (ACCOUNT,))
    mutate(admin, "UPDATE medical_rag.tenants SET active=false WHERE tenant_id=%s", (TENANT,))
    response = post(client, signed(signing_key))
    assert response.status_code == 200
    with psycopg.connect(**admin) as db:
        assert db.execute("SELECT revoked FROM medical_rag.identity_sessions WHERE token_hash=%s",
                          (sha256(old.token.encode()).digest(),)).fetchone() == (True,)
        assert db.execute("SELECT revoked FROM medical_rag.identity_sessions WHERE token_hash=%s",
                          (sha256(other.token.encode()).digest(),)).fetchone() == (False,)
    tenant_store.authenticate(elsewhere.token)


@pytest.mark.integration
def test_conflicting_jti_retarget_and_time_are_static400(system, signing_key):
    client, _, _, admin = system
    payload = claims()
    assert post(client, signed(signing_key, payload)).status_code == 200
    for changed in ({"sub": "subject-2"}, {"sid": "new-sid"}, {"iat": payload["iat"] - 1},
                    {"exp": payload["exp"] + 1}):
        response = post(client, signed(signing_key, payload | changed))
        assert response.status_code == 400 and response.json()["data"] is None
        row = events(admin)[-1]
        assert row["action"] == "provider.logout" and row["operator_id"] is None
        assert row["reason_code"] == "invalid_request"


@pytest.mark.integration
@pytest.mark.parametrize("changes", [
    {"iss": ISSUER + "/wrong"}, {"aud": "wrong"}, {"aud": ["medical-rag"]},
    {"iat": True}, {"exp": True}, {"iat": 1}, {"exp": 1}, {"iat": 10**20},
    {"events": {}}, {"events": {EVENT: None}}, {"events": {EVENT: []}}, {"events": []},
    {"nonce": None}, {"nonce": "private-CANARY"},
    {"sub": None}, {"sub": ""}, {"sub": " subject-1"}, {"sub": "s" * 256},
    {"sub": "bad\x00sub"}, {"sub": "bad\x01sub"},
    {"sid": None}, {"sid": ""}, {"sid": "bad\x00sid"}, {"sid": "sid\x7f"}, {"sid": "s" * 256},
    {"jti": None}, {"jti": ""}, {"jti": " jti"}, {"jti": "j" * 256}, {"jti": "bad\n"},
])
def test_invalid_claims_are_static_and_safely_audited(system, signing_key, changes, caplog):
    client, _, _, admin = system
    token = signed(signing_key, claims(**changes))
    response = post(client, token)
    assert response.status_code == 400
    row = events(admin)[-1]
    assert row["operator_id"] is None and row["action"] == "provider.logout"
    assert str(row["request_id"]) == response.headers["X-Request-ID"]
    assert token not in caplog.text + response.text + json.dumps(events(admin), default=str)
    assert "private-CANARY" not in caplog.text + response.text + json.dumps(events(admin), default=str)


@pytest.mark.integration
@pytest.mark.parametrize("headers", [
    {"typ": "JWT"}, {"crit": []}, {"jku": "https://bad-CANARY"}, {"jwk": {}},
    {"x5u": "https://bad-CANARY"}, {"x5c": []}, {"kid": "https://bad-CANARY"},
    {"kid": "../key"}, {"kid": ""}, {"kid": "a" * 129},
])
def test_token_headers_cannot_choose_trust(system, signing_key, headers):
    client, _, _, _ = system
    assert post(client, signed(signing_key, **headers)).status_code == 400


@pytest.mark.integration
def test_wrong_signature_algorithm_missing_claims_and_time_bounds(request, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from provider_logout_clock import freeze_logout_clock

    # Resolve credentials privately, after disposable database readiness. Never
    # freeze global time: database, asyncio and unrelated threads keep real time.
    private_system = request.getfixturevalue("system")
    client, _, _, private_admin = private_system
    private_key = request.getfixturevalue("signing_key")
    epoch = int(time.time())
    freeze_logout_clock(monkeypatch, epoch)

    def fixed_claims(**overrides):
        # All payload times derive from the same epoch, including invalid cases.
        return {"iss": ISSUER, "aud": "medical-rag", "iat": epoch, "exp": epoch + 120,
                "jti": str(uuid4()), "events": {EVENT: {}}, "sub": "subject-1"} | overrides

    def response_matches(private_token, expected):
        response = post(client, private_token)
        private_rows = [row for row in events(private_admin)
                        if str(row["request_id"]) == response.headers.get("X-Request-ID")]
        return (response.status_code == expected and response.json()["data"] is None
                and len(private_rows) == 1 and private_rows[0]["action"] == "provider.logout"
                and private_rows[0]["resource_type"] == "session" and private_rows[0]["operator_id"] is None
                and private_rows[0]["outcome"] == ("allowed" if expected == 200 else "denied")
                and private_rows[0]["reason_code"] == ("ok" if expected == 200 else "invalid_request"))

    private_token = signed(rsa.generate_private_key(public_exponent=65537, key_size=2048), fixed_claims())
    wrong_signature_rejected = response_matches(private_token, 400)
    assert wrong_signature_rejected, "Wrong RSA signature must be rejected and mandatorily audited"
    private_token = jwt.encode(fixed_claims(), "x" * 32, algorithm="HS256", headers={"typ": "logout+jwt"})
    substituted_algorithm_rejected = response_matches(private_token, 400)
    assert substituted_algorithm_rejected, "HS256 substitution must be rejected and mandatorily audited"
    missing_claim_results = []
    for key in ("iss", "aud", "iat", "exp", "events", "jti", "sub"):
        private_payload = fixed_claims()
        del private_payload[key]
        missing_claim_results.append(response_matches(signed(private_key, private_payload), 400))
    (missing_iss, missing_aud, missing_iat, missing_exp, missing_events, missing_jti, missing_sub) = missing_claim_results
    assert missing_iss, "Missing iss must be rejected and audited"
    assert missing_aud, "Missing aud must be rejected and audited"
    assert missing_iat, "Missing iat must be rejected and audited"
    assert missing_exp, "Missing exp must be rejected and audited"
    assert missing_events, "Missing events must be rejected and audited"
    assert missing_jti, "Missing jti must be rejected and audited"
    assert missing_sub, "Missing sub without sid must be rejected and audited"

    current_iat_accepted = response_matches(signed(private_key, fixed_claims()), 200)
    assert current_iat_accepted, "Current iat with positive expiration must be accepted and audited"
    lifetime_300_accepted = response_matches(signed(private_key, fixed_claims(exp=epoch + 300)), 200)
    assert lifetime_300_accepted, "Expiration at iat plus 300 seconds must be accepted and audited"
    lifetime_301_rejected = response_matches(signed(private_key, fixed_claims(exp=epoch + 301)), 400)
    assert lifetime_301_rejected, "Expiration at iat plus 301 seconds must be rejected and audited"
    future_iat_rejected = response_matches(signed(private_key, fixed_claims(iat=epoch + 1)), 400)
    assert future_iat_rejected, "Iat one second in the future must be rejected and audited"
    age_120_accepted = response_matches(signed(private_key, fixed_claims(iat=epoch - 120)), 200)
    assert age_120_accepted, "Iat exactly 120 seconds old with valid expiration must be accepted and audited"
    age_121_rejected = response_matches(signed(private_key, fixed_claims(iat=epoch - 121)), 400)
    assert age_121_rejected, "Iat exactly 121 seconds old must be rejected and audited"
    expiration_now_rejected = response_matches(signed(private_key, fixed_claims(exp=epoch)), 400)
    assert expiration_now_rejected, "Expiration equal to now must be rejected and audited"
    expiration_past_rejected = response_matches(signed(private_key, fixed_claims(exp=epoch - 1)), 400)
    assert expiration_past_rejected, "Expiration before now must be rejected and audited"
    nonpositive_lifetime_rejected = response_matches(signed(private_key, fixed_claims(iat=epoch - 1, exp=epoch - 1)), 400)
    assert nonpositive_lifetime_rejected, "Nonpositive lifetime must be rejected and audited"


@pytest.mark.integration
def test_unknown_claims_and_bounded_unknown_form_fields_are_ignored(system, signing_key):
    client, _, _, _ = system
    token = signed(signing_key, claims(custom_extension="ignored-CANARY"), kid="pinned-key-1")
    assert client.post(PATH, data={"logout_token": token, "unknown": "ignored-CANARY"}).status_code == 200


@pytest.mark.integration
@pytest.mark.parametrize("body,content_type,encoding", [
    (b"logout_token=%", "application/x-www-form-urlencoded", None),
    (b"logout_token=%GG", "application/x-www-form-urlencoded", None),
    (b"logout_token=%FF", "application/x-www-form-urlencoded", None),
    (b"logout_token=\xff", "application/x-www-form-urlencoded", None),
    (b"logout_token=x&logout_token=y", "application/x-www-form-urlencoded", None),
    (b"logout_token=x&foo=1&foo=2", "application/x-www-form-urlencoded", None),
    (b"logout_token=x&logout%5Ftoken=y", "application/x-www-form-urlencoded", None),
    (b"logout_token=", "application/x-www-form-urlencoded", None),
    (b"foo=x", "application/x-www-form-urlencoded", None),
    (b"logout_token=" + b"x" * 16385, "application/x-www-form-urlencoded", None),
    (b"x" * 32769, "application/x-www-form-urlencoded", None),
    (b"logout_token=x", "application/json", None),
    (b"logout_token=x", "application/x-www-form-urlencoded;charset=latin1", None),
    (b"logout_token=x", "application/x-www-form-urlencoded", "gzip"),
], ids=["bad-percent", "nonhex", "escaped-invalid-utf8", "invalid-utf8", "duplicate-token", "duplicate-extra",
        "encoded-duplicate", "empty", "missing", "token-bound", "body-bound", "media", "charset", "encoding"])
def test_strict_form_bounds_and_encoding(system, body, content_type, encoding):
    client, _, _, _ = system
    headers = {"Content-Type": content_type}
    if encoding:
        headers["Content-Encoding"] = encoding
    assert client.post(PATH, content=body, headers=headers).status_code == 400


@pytest.mark.integration
def test_transport_exception_is_exact_and_never_reads_or_changes_cookies(system, signing_key):
    client, _, _, _ = system
    token = signed(signing_key)
    response = client.post(PATH, data={"logout_token": token}, headers={
        "Cookie": "__Host-medical-session=bad; __Host-medical-session=duplicate",
        "X-Forwarded-User": ACCOUNT, "X-Forwarded-Tenant": OTHER_TENANT})
    assert response.status_code == 200 and "set-cookie" not in response.headers
    assert client.post(PATH, data={"logout_token": token}, headers={"Origin": APP_ORIGIN}).status_code == 200
    for header in ({"Origin": "https://wrong.example"}, [("Origin", APP_ORIGIN), ("Origin", APP_ORIGIN)],
                   {"Host": "wrong.example"}, [("Host", "medical.example.org"), ("Host", "medical.example.org")]):
        assert client.post(PATH, data={"logout_token": token}, headers=header).status_code == 403
    assert client.post(APP_ORIGIN.replace("https:", "http:") + PATH, data={"logout_token": token}).status_code == 403
    assert client.post(PATH + "?ignored=x", data={"logout_token": token}).status_code == 400
    for route in ("/auth/login", "/auth/logout", "/auth/backchannel-logout/", "/me/consents/health"):
        assert client.post(route, follow_redirects=False).status_code == 403


@pytest.mark.integration
def test_storage_failure_is_static503_and_never_reports_success(system, signing_key):
    client, _, _, admin = system
    mutate(admin, "REVOKE INSERT(tenant_id,issuer,jti_hash,subject_hash,sid_hash,issued_at,expires_at) "
           "ON medical_rag.provider_logout_events FROM login_test")
    try:
        response = post(client, signed(signing_key))
        assert response.status_code == 503 and response.json()["data"] is None
        assert events(admin)[-1]["reason_code"] == "operation_unavailable"
        with psycopg.connect(**admin) as db:
            assert db.execute("SELECT count(*) FROM medical_rag.provider_logout_events").fetchone() == (0,)
    finally:
        mutate(admin, "GRANT INSERT(tenant_id,issuer,jti_hash,subject_hash,sid_hash,issued_at,expires_at) "
               "ON medical_rag.provider_logout_events TO login_test")


def wait_for_advisory(admin, name):
    deadline = time.monotonic() + 2
    with psycopg.connect(**admin) as db:
        while time.monotonic() < deadline:
            if db.execute("SELECT 1 FROM pg_stat_activity WHERE application_name=%s "
                          "AND wait_event_type='Lock' AND wait_event='advisory'", (name,)).fetchone():
                return
            time.sleep(0.01)
    pytest.fail("operation never waited for the shared institution advisory lock")


@pytest.mark.integration
@pytest.mark.parametrize("provider_first", [True, False])
def test_real_advisory_lock_order_prevents_pending_login_resurrection(system, database, signing_key, provider_first):
    from medical_rag.identity_sessions import SessionStore
    _, login, _, admin = system
    runtime, _ = database
    payload = claims(sid="pending-session")
    del payload["sub"]  # sid-only before any matching local session
    event = login.provider_logout_verifier.verify(signed(signing_key, payload))
    pending = VerifiedIdentity(ISSUER, "subject-1", True, "pending-session", payload["iat"])
    issue_store = SessionStore(lambda: psycopg.connect(**dict(runtime, application_name="pending-issue")), tenant_id=TENANT)
    event_store = SessionStore(lambda: psycopg.connect(**dict(runtime, application_name="provider-fence")), tenant_id=TENANT)
    with ThreadPoolExecutor(max_workers=2) as executor:
        with psycopg.connect(**dict(admin, autocommit=False)) as lock:
            lock.execute("SET LOCAL lock_timeout='2s'")
            login.sessions._provider_serialization(lock)
            if provider_first:
                fence = executor.submit(event_store.provider_logout, event)
                wait_for_advisory(admin, "provider-fence")
                issued = executor.submit(issue_store.issue, pending)
                wait_for_advisory(admin, "pending-issue")
            else:
                issued = executor.submit(issue_store.issue, pending)
                wait_for_advisory(admin, "pending-issue")
                fence = executor.submit(event_store.provider_logout, event)
                wait_for_advisory(admin, "provider-fence")
        fence.result(timeout=3)
        if provider_first:
            with pytest.raises(AuthenticationDenied):
                issued.result(timeout=3)
        else:
            session = issued.result(timeout=3)
            with pytest.raises(AuthenticationDenied):
                login.sessions.authenticate(session.token)
    with pytest.raises(AuthenticationDenied):
        login.sessions.issue(pending)


@pytest.mark.integration
def test_event_rls_append_only_and_pooled_transaction_scope(system, database, signing_key):
    client, login, _, admin = system
    runtime, _ = database
    payload = claims(sub="private-sub-CANARY", sid="private-sid-CANARY", jti="private-jti-CANARY")
    token = signed(signing_key, payload)
    assert post(client, token).status_code == 200
    with psycopg.connect(**admin) as db:
        stored = db.execute("SELECT * FROM medical_rag.provider_logout_events").fetchall()
        assert len(stored) == 1
        assert all(raw not in repr(stored) for raw in (token, payload["sub"], payload["sid"], payload["jti"]))
        for query in ("UPDATE medical_rag.provider_logout_events SET issued_at=issued_at",
                      "DELETE FROM medical_rag.provider_logout_events", "TRUNCATE medical_rag.provider_logout_events"):
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(query)
    with psycopg.connect(**runtime) as db:
        with db.transaction():
            assert db.execute("SELECT count(*) FROM medical_rag.provider_logout_events").fetchone() == (0,)
            login.sessions._provider_serialization(db)
            assert db.execute("SELECT count(*) FROM medical_rag.provider_logout_events WHERE tenant_id=%s", (TENANT,)).fetchone() == (1,)
            db.execute("SELECT set_config('app.provider_logout_scope',pg_current_xact_id()::text||'/'||%s,true)", (OTHER_TENANT,))
            assert db.execute("SELECT count(*) FROM medical_rag.provider_logout_events").fetchone() == (0,)
        with db.transaction():
            assert db.execute("SELECT count(*) FROM medical_rag.provider_logout_events").fetchone() == (0,)
        for query in ("UPDATE medical_rag.provider_logout_events SET issued_at=issued_at",
                      "DELETE FROM medical_rag.provider_logout_events", "TRUNCATE medical_rag.provider_logout_events"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
                db.execute(query)


@pytest.mark.integration
def test_replayed_event_cannot_repeat_revocation_even_for_trusted_unknown_time(system, signing_key):
    # An identical committed event must not run a second UPDATE at all. A later
    # fresh auth is independently permitted and remains active after retries.
    client, login, _, admin = system
    payload = claims()
    token = signed(signing_key, payload)
    assert post(client, token).status_code == 200
    fresh = login.sessions.issue(VerifiedIdentity(ISSUER, "subject-1", True, authenticated_at=payload["iat"] + 1))
    # Owner-only deliberate legacy state proves replay never executes UPDATE.
    mutate(admin, "UPDATE medical_rag.identity_sessions SET authenticated_at=NULL WHERE token_hash=%s",
           (sha256(fresh.token.encode()).digest(),))
    assert post(client, token).status_code == 200
    login.sessions.authenticate(fresh.token)


@pytest.mark.integration
def test_http_pending_code_exchange_cannot_restore_old_identity_and_new_auth_time_can_login(system, signing_key):
    client, login, provider, _ = system
    entered, release = ThreadEvent(), ThreadEvent()
    authentication_time = int(time.time())

    async def response(request):
        entered.set()
        assert await asyncio.to_thread(release.wait, 2)
        now = int(time.time())
        token = jwt.encode({"iss": ISSUER, "aud": "medical-rag", "sub": "subject-1", "sid": "pending-sid",
                            "auth_time": authentication_time, "iat": now, "exp": now + 120,
                            "nonce": provider["nonce"], "acr": "urn:hospital:mfa"}, signing_key, algorithm="RS256")
        return httpx.Response(200, json={"access_token": "fictional", "token_type": "Bearer", "id_token": token})

    login._test_transport = httpx.MockTransport(response)
    _, query = start(system)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.get, "/auth/callback", params={"state": query["state"][0], "code": "fictional"},
                                  follow_redirects=False)
        assert entered.wait(1)
        try:
            logout = claims(sid="pending-sid")
            assert post(client, signed(signing_key, logout)).status_code == 200
        finally:
            release.set()
        result = pending.result(timeout=3)
        assert result.status_code == 401
        assert "__Host-medical-session" not in client.cookies
    deadline = time.monotonic() + 1.2
    while int(time.time()) <= logout["iat"] and time.monotonic() < deadline:
        time.sleep(0.01)
    authentication_time = int(time.time())
    assert authentication_time > logout["iat"]
    headers = logged_in(system)
    assert client.get("/me").status_code == 200
    assert client.get("/conversations").status_code == 403  # no implicit consent/binding
    assert post(client, signed(signing_key, logout)).status_code == 200
    assert client.get("/me").status_code == 200
    assert grant(client, headers).status_code == 200


@pytest.mark.integration
@pytest.mark.parametrize("field", ["sid", "sub", "jti"])
def test_unencodable_claim_identifier_is_invalid400_not_storage503(system, signing_key, field):
    client, _, _, _ = system
    assert post(client, signed(signing_key, claims(**{field: "bad\ud800"}))).status_code == 400


@pytest.mark.integration
def test_standard_urlencoded_unknown_flag_is_ignored_and_all_bounded_fields_allowed(system, signing_key):
    from urllib.parse import urlencode
    client, _, _, _ = system
    body = urlencode({"logout_token": signed(signing_key)}) + "&unknown_flag&" + "&".join(f"f{x}=" for x in range(1500))
    assert len(body) < 32768
    assert client.post(PATH, content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 200


@pytest.mark.integration
def test_headless_unconfigured_audit_cannot_enable_provider_authority(system, signing_key):
    from fastapi.testclient import TestClient

    from medical_rag.oidc_login import create_auth_app
    _, login, _, _ = system
    old = login.sessions.issue(VerifiedIdentity(ISSUER, "subject-1", True))
    with TestClient(create_auth_app(login), base_url=APP_ORIGIN, headers={}) as headless:
        assert post(headless, signed(signing_key)).status_code == 503
    login.sessions.authenticate(old.token)


@pytest.mark.integration
@pytest.mark.parametrize("nbf", [True, None, "1", 1.5, 10**20])
def test_registered_nbf_requires_integer_and_cannot_be_in_future(system, signing_key, nbf):
    client, _, _, _ = system
    assert post(client, signed(signing_key, claims(nbf=nbf))).status_code == 400


@pytest.mark.integration
def test_registered_past_nbf_is_valid_and_malformed_media_token_is_rejected(system, signing_key):
    client, _, _, _ = system
    token = signed(signing_key, claims(nbf=int(time.time()) - 1))
    assert post(client, token).status_code == 200
    assert client.post(PATH, content="logout_token=" + token,
                       headers={"Content-Type": "appli cation/x-www-form-urlencoded"}).status_code == 400


def test_direct_verifier_bounded_inputs_and_id_logout_cross_type(signing_key):
    from medical_rag.oidc_identity import InvalidIdentity, OIDCIdentityVerifier
    from medical_rag.provider_logout import (
        InvalidProviderLogout,
        ProviderLogoutVerifier,
    )
    verifier = ProviderLogoutVerifier(issuer=ISSUER, audience="medical-rag", public_key=signing_key.public_key())
    for token in (None, "", "a" * 16385, "not-a-jwt"):
        with pytest.raises(InvalidProviderLogout):
            verifier.verify(token)
    identity_verifier = OIDCIdentityVerifier(issuer=ISSUER, audience="medical-rag", public_key=signing_key.public_key(),
                                            mfa_acr_values=frozenset())
    payload = claims(nonce="one-time-nonce", auth_time=int(time.time()), sid="provider-sid-CANARY")
    id_token = jwt.encode(payload, signing_key, algorithm="RS256")
    with pytest.raises(InvalidProviderLogout):
        verifier.verify(id_token)
    with pytest.raises(InvalidIdentity):
        identity_verifier.verify(signed(signing_key, payload), expected_nonce="one-time-nonce")


def test_id_token_correlation_is_verified_and_hidden_from_repr(signing_key):
    from medical_rag.oidc_identity import InvalidIdentity, OIDCIdentityVerifier
    verifier = OIDCIdentityVerifier(issuer=ISSUER, audience="medical-rag", public_key=signing_key.public_key(),
                                    mfa_acr_values=frozenset())
    payload = claims(nonce="one-time-nonce", auth_time=int(time.time()), sid="provider-sid-CANARY")
    del payload["events"]
    identity = verifier.verify(jwt.encode(payload, signing_key, algorithm="RS256"), expected_nonce="one-time-nonce")
    assert identity.authenticated_at == payload["auth_time"]
    assert identity.provider_session_id == payload["sid"]
    assert payload["sid"] not in repr(identity) and "authenticated_at" not in repr(identity)
    for bad in (None, "", " sid", "bad\x00", "s" * 256):
        with pytest.raises(InvalidIdentity):
            verifier.verify(jwt.encode(payload | {"sid": bad}, signing_key, algorithm="RS256"),
                            expected_nonce="one-time-nonce")
