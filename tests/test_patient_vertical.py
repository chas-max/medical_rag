"""Fictional patient slice against real unprivileged PostgreSQL and test IdP."""
# ruff: noqa: F401,F811 -- imported pytest fixtures are used by injection

import importlib.util
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from test_oidc_login import (
    ACCOUNT,
    APP_ORIGIN,
    ISSUER,
    ROOT,
    TENANT,
    signing_key,
    start,
)
from test_oidc_login import (
    database as oidc_database,
)
from test_oidc_login import (
    system as auth_system,
)

from medical_rag.identity_sessions import AuthenticationDenied
from medical_rag.oidc_identity import VerifiedIdentity
from medical_rag.patient_chat import ChatClient

PATIENT = "00000000-0000-0000-0000-000000000301"
OTHER = "00000000-0000-0000-0000-000000000302"
OTHER_ACCOUNT = "00000000-0000-0000-0000-000000000202"
OTHER_TENANT = "00000000-0000-0000-0000-000000000102"


@pytest.fixture(scope="module")
def database(oidc_database):
    runtime, admin = oidc_database
    with psycopg.connect(**admin) as db:
        # The shared actual SessionStore fixture requires schema6 even headless.
        db.execute("GRANT SELECT ON medical_rag.security_audit_events TO login_test")
        db.execute("GRANT INSERT (tenant_id,operator_id,action,resource_type,outcome,reason_code,request_id) "
                   "ON medical_rag.security_audit_events TO login_test")
        db.execute("REVOKE SELECT ON medical_rag.tenants,medical_rag.identity_accounts, "
                   "medical_rag.identity_roles,medical_rag.identity_bindings FROM login_test")
        for table, columns in {
            "tenants": "tenant_id,active,session_version",
            "identity_accounts": "tenant_id,account_id,issuer,subject,active,session_version",
            "identity_roles": "tenant_id,account_id,role",
            "identity_bindings": "tenant_id,account_id,patient_id,verified",
        }.items():
            db.execute(f"GRANT SELECT ({columns}) ON medical_rag.{table} TO login_test")
        db.execute("GRANT SELECT (tenant_id,patient_id,age_classification,identity_verified) "
                   "ON medical_rag.patient_subjects TO login_test")
        db.execute("GRANT UPDATE (patient_id) ON medical_rag.patient_subjects TO login_test")
        db.execute("GRANT SELECT,INSERT ON medical_rag.patient_consent_events, "
                   "medical_rag.conversations,medical_rag.conversation_turns TO login_test")
        # 0009: the runtime role may only write the dialogue columns of a
        # conversation row, mirroring the owner-run recipe.
        db.execute("GRANT UPDATE (dialogue_state,dialogue_revision,context_version) "
                   "ON medical_rag.conversations TO login_test")
        db.execute("GRANT SELECT (tenant_id,case_id,requester_account_id,category,status,assigned_account_id,"
                   "resolution_code,created_at,updated_at) ON medical_rag.support_cases TO login_test")
        db.execute("GRANT INSERT (tenant_id,case_id,requester_account_id,category), "
                   "UPDATE (status,assigned_account_id,resolution_code) ON medical_rag.support_cases TO login_test")
    return runtime, admin


def provision(auth_system):
    """Create the fictional patient rows and return the trusted app inputs."""
    from medical_rag.patient_service import HealthNotice

    _, login, provider, admin = auth_system
    with psycopg.connect(**admin) as db:
        db.execute("INSERT INTO medical_rag.patient_subjects(tenant_id,patient_id,age_classification,identity_verified) "
                   "VALUES (%s,%s,'adult',true),(%s,%s,'adult',true)", (TENANT, PATIENT, TENANT, OTHER))
        db.execute("INSERT INTO medical_rag.identity_bindings VALUES (%s,%s,%s,true)", (TENANT, ACCOUNT, PATIENT))
        db.execute("INSERT INTO medical_rag.identity_accounts(tenant_id,account_id,issuer,subject) "
                   "VALUES (%s,%s,%s,'subject-2')", (TENANT, OTHER_ACCOUNT, ISSUER))
        db.execute("INSERT INTO medical_rag.identity_roles VALUES (%s,%s,'patient')", (TENANT, OTHER_ACCOUNT))
        db.execute("INSERT INTO medical_rag.identity_bindings VALUES (%s,%s,%s,true)", (TENANT, OTHER_ACCOUNT, OTHER))
        db.execute("INSERT INTO medical_rag.tenants(tenant_id) VALUES (%s)", (OTHER_TENANT,))
        db.execute("INSERT INTO medical_rag.patient_subjects(tenant_id,patient_id,age_classification,identity_verified) "
                   "VALUES (%s,%s,'adult',true)", (OTHER_TENANT, PATIENT))
    return (login, provider, admin,
            HealthNotice("fictional-v1", "Fictional development notice: health information only.", 3600))


@pytest.fixture
def system(auth_system):
    from medical_rag.patient_api import create_patient_app

    login, provider, admin, notice = provision(auth_system)
    with TestClient(create_patient_app(login, notice),
                    base_url=APP_ORIGIN, headers={}) as client:
        yield client, login, provider, admin


class FakeChat:
    """Deterministic answer source; records calls and can run a side effect first."""

    def __init__(self, text="基于公开资料的虚构回答。", citations=(), error=None, before=None):
        self.text, self.citations, self.error, self.before = text, list(citations), error, before
        self.questions: list[str] = []

    def answer(self, question):
        self.questions.append(question)
        if self.before is not None:
            self.before()
        if self.error is not None:
            raise self.error
        return {"answer": self.text, "citations": self.citations, "abstained": False}


def logged_in(system):
    client, _, _, _ = system
    _, query = start(system)
    response = client.get("/auth/callback", params={"state": query["state"][0], "code": "fictional-code"},
                          follow_redirects=False)
    assert response.status_code == 303, response.text
    csrf = client.get("/auth/session").json()["data"]["csrf_token"]
    return {"Origin": APP_ORIGIN, "X-CSRF-Token": csrf}


def grant(client, headers):
    response = client.post("/me/consents/health", headers=headers, json={"version": "fictional-v1", "accepted": True})
    assert response.status_code == 200, response.text
    return response


def mutate(admin, sql, params=()):
    with psycopg.connect(**admin) as db:
        db.execute(sql, params)


def count_events(admin):
    with psycopg.connect(**admin) as db:
        return db.execute("SELECT count(*),array_agg(DISTINCT purpose) FROM medical_rag.patient_consent_events").fetchone()


def test_patient_slice_has_live_service_and_protected_app():
    assert importlib.util.find_spec("medical_rag.patient_service") is not None
    assert importlib.util.find_spec("medical_rag.patient_api") is not None


@pytest.mark.integration
def test_full_oidc_patient_consent_conversation_and_turn_flow(system):
    client, _, _, admin = system
    headers = logged_in(system)
    me = client.get("/me").json()["data"]
    assert me["user_id"] == ACCOUNT and me["tenant_id"] == TENANT
    assert me["binding"] == {"verified": True}
    assert me["permissions"] == {"identity": True, "health_consent": True, "conversations": False, "chat": False,
                                 "audit": False, "support_self": True, "support_queue": False}
    assert client.get("/conversations").status_code == 403
    notice = client.get("/me/consents/health").json()["data"]
    assert notice["notice"]["text"].startswith("Fictional development") and notice["granted"] is False
    assert grant(client, headers).json()["data"]["granted"] is True
    assert client.get("/me").json()["data"]["permissions"]["conversations"] is True
    response = client.post("/conversations", headers=headers, json={"title": "fictional title"})
    assert response.status_code == 200
    conversation = response.json()["data"]
    assert conversation["title"] == "fictional title"
    path = "/conversations/" + conversation["id"]
    assert client.get(path).json()["data"] == conversation
    assert client.get("/conversations?limit=1&offset=0").json()["data"]["items"] == [conversation]
    turn = client.post(path + "/turns", headers=headers, json={"content": "fictitious patient text"})
    assert turn.status_code == 200
    assert client.get(path + "/turns").json()["data"]["items"] == [turn.json()["data"]]
    assert client.get(path).headers["cache-control"] == "no-store"
    assert count_events(admin) == (1, ["health_information"])


@pytest.mark.integration
def test_content_mutations_authorize_write_action_independently_of_shared_lock(system, monkeypatch):
    from medical_rag import patient_service
    from medical_rag.access_policy import Action
    client, _, _, _ = system
    headers = logged_in(system)
    grant(client, headers)
    original = patient_service.is_allowed
    monkeypatch.setattr(patient_service, "is_allowed",
                        lambda principal, action, resource: action is not Action.PATIENT_WRITE
                        and original(principal, action, resource))
    assert client.get("/conversations").status_code == 200
    assert client.post("/conversations", headers=headers, json={"title": "x"}).status_code == 403


@pytest.mark.integration
def test_withdrawal_waits_for_shared_content_transaction_and_denies_next_operation(system, database, monkeypatch):
    from medical_rag.identity_sessions import SessionStore
    from medical_rag.patient_service import HealthNotice, PatientDenied, PatientService
    client, login, _, admin = system
    runtime, _ = database
    headers = logged_in(system)
    grant(client, headers)
    token = client.cookies.get("__Host-medical-session")
    notice = HealthNotice("fictional-v1", "fictional notice", 3600)
    service = PatientService(login.sessions, notice)
    waiting_name = "patient-withdraw-" + secrets.token_hex(8)
    waiting_store = SessionStore(lambda: psycopg.connect(**dict(runtime, application_name=waiting_name)), tenant_id=TENANT)
    other_session = waiting_store.issue(VerifiedIdentity(ISSUER, "subject-1", True))
    withdrawal = PatientService(waiting_store, notice)
    checked, finish = Event(), Event()
    original = service._consent
    def hold(*args, **kwargs):
        current = original(*args, **kwargs)
        checked.set()
        assert finish.wait(5)
        return current
    monkeypatch.setattr(service, "_consent", hold)
    with ThreadPoolExecutor(max_workers=2) as executor:
        content = executor.submit(service.list_conversations, token)
        pending = None
        try:
            assert checked.wait(3)
            pending = executor.submit(withdrawal.change_consent, other_session.token,
                                      csrf_token=other_session.csrf_token, granted=False)
            wait_for_lock(admin, waiting_name)
            assert not pending.done()
        finally:
            finish.set()
        assert content.result(timeout=5)["items"] == []
        assert pending.result(timeout=5)["granted"] is False
    with pytest.raises(PatientDenied):
        withdrawal.list_conversations(other_session.token)


@pytest.mark.integration
def test_other_purposes_never_authorize_health_content(system):
    client, _, _, admin = system
    logged_in(system)
    for purpose in ["hospital_record", "external_model"]:
        mutate(admin, "INSERT INTO medical_rag.patient_consent_events "
            "(event_id,tenant_id,patient_id,grantor_account_id,purpose,notice_version,granted,expires_at) "
            "VALUES (%s,%s,%s,%s,%s,'fictional-v1',true,clock_timestamp()+interval '1 hour')",
            (uuid4(), TENANT, PATIENT, ACCOUNT, purpose))
    assert client.get("/conversations").status_code == 403
    assert client.get("/me/consents/health").json()["data"]["granted"] is False


@pytest.mark.integration
def test_owned_predicates_other_patient_and_tenant_and_uniform_404(system):
    client, _, _, admin = system
    headers = logged_in(system)
    grant(client, headers)
    ids = [str(uuid4()), str(uuid4())]
    with psycopg.connect(**admin) as db:
        for institution, patient, identifier in [(TENANT, OTHER, ids[0]), (OTHER_TENANT, PATIENT, ids[1])]:
            db.execute("INSERT INTO medical_rag.conversations(tenant_id,patient_id,conversation_id,title) "
                       "VALUES (%s,%s,%s,'other sensitive text')", (institution, patient, identifier))
    missing = client.get("/conversations/" + str(uuid4()))
    assert missing.status_code == 404
    for identifier in ids + ["not-a-uuid"]:
        for suffix in ["", "/turns"]:
            response = client.get("/conversations/" + identifier + suffix)
            assert response.status_code == 404 and response.json() == missing.json()
        response = client.post("/conversations/" + identifier + "/turns", headers=headers, json={"content": "x"})
        assert response.status_code == 404 and response.json() == missing.json()
    assert client.get("/conversations").json()["data"]["items"] == []


@pytest.mark.integration
def test_second_adult_session_isolated_from_first_patient_content(system):
    client, login, _, _ = system
    headers = logged_in(system)
    grant(client, headers)
    first = client.post("/conversations", headers=headers, json={"title": "first patient"}).json()["data"]["id"]
    second = login.sessions.issue(VerifiedIdentity(ISSUER, "subject-2", True))
    client.cookies.clear()
    client.cookies.set("__Host-medical-session", second.token)
    client.cookies.set("__Host-medical-csrf", second.csrf_token)
    headers = {"Origin": APP_ORIGIN, "X-CSRF-Token": second.csrf_token}
    assert client.get("/me").json()["data"]["user_id"] == OTHER_ACCOUNT
    assert client.get("/me/consents/health").json()["data"]["granted"] is False
    grant(client, headers)
    own = client.post("/conversations", headers=headers, json={"title": "second patient"}).json()["data"]
    assert client.get("/conversations").json()["data"]["items"] == [own]
    assert client.get("/conversations/" + first).status_code == 404


@pytest.mark.integration
def test_withdrawal_regrant_latest_event_expiry_and_history(system):
    client, _, _, admin = system
    headers = logged_in(system)
    # Withdrawal does not depend on an existing/current grant.
    assert client.delete("/me/consents/health", headers=headers).status_code == 200
    grant(client, headers)
    assert client.delete("/me/consents/health", headers=headers).json()["data"]["granted"] is False
    assert client.get("/conversations").status_code == 403
    assert client.post("/conversations", headers=headers, json={"title": "x"}).status_code == 403
    grant(client, headers)
    assert count_events(admin) == (4, ["health_information"])
    # Trusted writer records a newer incompatible notice, not an older grant fallback.
    mutate(admin, "INSERT INTO medical_rag.patient_consent_events "
           "(event_id,tenant_id,patient_id,grantor_account_id,purpose,notice_version,granted,expires_at) "
           "VALUES (%s,%s,%s,%s,'health_information','different-version',true,clock_timestamp()+interval '1 hour')",
           (uuid4(), TENANT, PATIENT, ACCOUNT))
    assert client.get("/conversations").status_code == 403
    grant(client, headers)
    mutate(admin, "INSERT INTO medical_rag.patient_consent_events "
           "(event_id,tenant_id,patient_id,grantor_account_id,purpose,notice_version,granted,expires_at) "
           "VALUES (%s,%s,%s,%s,'health_information','fictional-v1',true,clock_timestamp()-interval '1 second')",
           (uuid4(), TENANT, PATIENT, ACCOUNT))
    assert client.get("/conversations").status_code == 403
    assert client.get("/me/consents/health").json()["data"]["granted"] is False


@pytest.mark.integration
@pytest.mark.parametrize("field,value", [("age_classification", "under_14"), ("age_classification", "older_minor"),
    ("age_classification", "unknown"), ("identity_verified", False)])
def test_patient_verification_and_all_nonadult_classes_deny(system, field, value):
    client, _, _, admin = system
    headers = logged_in(system)
    grant(client, headers)
    mutate(admin, f"UPDATE medical_rag.patient_subjects SET {field}=%s WHERE tenant_id=%s AND patient_id=%s",
           (value, TENANT, PATIENT))
    assert client.get("/me/consents/health").status_code == 403
    assert client.get("/conversations").status_code == 403
    assert client.get("/me").json()["data"]["permissions"]["conversations"] is False


@pytest.mark.integration
@pytest.mark.parametrize("role", ["org_admin", "support", "platform_operator", "guardian"])
def test_staff_and_guardians_have_no_implicit_patient_permissions(system, role):
    client, _, _, admin = system
    mutate(admin, "DELETE FROM medical_rag.identity_roles WHERE account_id=%s", (ACCOUNT,))
    mutate(admin, "INSERT INTO medical_rag.identity_roles VALUES (%s,%s,%s)", (TENANT, ACCOUNT, role))
    headers = logged_in(system)
    assert client.get("/me").json()["data"]["permissions"] == {
        "identity": True, "health_consent": False, "conversations": False, "chat": False, "audit": False,
        "support_self": True, "support_queue": role == "support"}
    assert client.get("/me/consents/health").status_code == 403
    assert client.post("/me/consents/health", headers=headers,
                       json={"version": "fictional-v1", "accepted": True}).status_code == 403


@pytest.mark.integration
@pytest.mark.parametrize("change", ["account", "tenant", "role", "binding", "expiry", "logout"])
def test_live_authority_changes_deny_next_operation(system, change):
    client, _, _, admin = system
    headers = logged_in(system)
    grant(client, headers)
    changes = {
        "account": "UPDATE medical_rag.identity_accounts SET active=false",
        "tenant": "UPDATE medical_rag.tenants SET active=false",
        "role": "DELETE FROM medical_rag.identity_roles",
        "binding": "UPDATE medical_rag.identity_bindings SET verified=false",
        "expiry": "UPDATE medical_rag.identity_sessions SET expires_at=clock_timestamp()-interval '1 second'",
    }
    if change == "logout":
        assert client.post("/auth/logout", headers=headers).status_code == 200
    else:
        mutate(admin, changes[change])
    assert client.get("/conversations").status_code == 401


@pytest.mark.integration
def test_unverified_binding_cannot_become_patient_from_body(system):
    client, _, _, admin = system
    mutate(admin, "UPDATE medical_rag.identity_bindings SET verified=false WHERE account_id=%s", (ACCOUNT,))
    headers = logged_in(system)
    assert client.get("/me").json()["data"]["binding"] == {"verified": False}
    assert client.get("/me/consents/health").status_code == 403
    assert client.post("/conversations", headers=headers,
                       json={"title": "x", "patient_id": PATIENT, "tenant_id": TENANT}).status_code in {400, 422}


@pytest.mark.integration
@pytest.mark.parametrize("body", [{"title": 1}, {"title": ""}, {"title": "x" * 201},
    {"title": "fictional secret", "patient_id": OTHER}, {"title": "x", "tenant_id": OTHER_TENANT},
    {"title": "x", "user_id": OTHER_ACCOUNT}, {"title": "x", "role": "org_admin"}])
def test_strict_conversation_bodies_never_echo_inputs(system, body):
    client, _, _, _ = system
    headers = logged_in(system)
    grant(client, headers)
    response = client.post("/conversations", headers=headers, json=body)
    assert response.status_code == 400
    assert response.json() == {"success": False, "data": None,
                               "error": {"code": "INVALID_REQUEST", "message": "Invalid request"}}
    assert client.get("/conversations").json()["data"]["items"] == []


@pytest.mark.integration
def test_csrf_origin_payload_pagination_and_unexpected_errors_are_static(system, monkeypatch, caplog):
    client, login, _, admin = system
    headers = logged_in(system)
    for invalid in [{"Origin": APP_ORIGIN}, headers | {"X-CSRF-Token": "wrong"},
                    headers | {"Origin": "https://evil.example"},
                    [("Origin", APP_ORIGIN), ("X-CSRF-Token", headers["X-CSRF-Token"]),
                     ("X-CSRF-Token", headers["X-CSRF-Token"])]]:
        assert client.post("/me/consents/health", headers=invalid,
                           json={"version": "fictional-v1", "accepted": True}).status_code in {401, 403}
    assert count_events(admin)[0] == 0
    for body in [{"version": "old", "accepted": True}, {"version": "fictional-v1", "accepted": False},
                 {"version": "fictional-v1", "accepted": 1},
                 {"version": "fictional-v1", "accepted": True, "purpose": "external_model"}]:
        assert client.post("/me/consents/health", headers=headers, json=body).status_code == 400
    grant(client, headers)
    conversation = client.post("/conversations", headers=headers, json={"title": "x"}).json()["data"]["id"]
    for body in [{"content": 1}, {"content": ""}, {"content": "x" * 16001}, {"content": "x", "patient_id": OTHER}]:
        assert client.post(f"/conversations/{conversation}/turns", headers=headers, json=body).status_code == 400
    for query in ["limit=101", "limit=0", "offset=-1", "offset=10001", "limit=bad", "limit=1&limit=2"]:
        assert client.get("/conversations?" + query).status_code == 400
    assert client.post("/conversations", headers=headers, content='{"title": "private',
                       ).status_code == 400
    for body in ['{"title":"x","title":"private"}', '{"title":"' + 'x' * 100000 + '"}',
                 '{"title":"   "}']:
        assert client.post("/conversations", headers=headers, content=body).status_code == 400
    assert client.post(f"/conversations/{conversation}/turns", headers=headers, json={"content": "  "}).status_code == 400
    assert client.get("/conversations?unexpected=private").status_code == 400
    # No mutation endpoint accepts a missing/duplicate CSRF proof.
    for method, path, body in [("DELETE", "/me/consents/health", None),
        ("POST", "/conversations", {"title": "x"}),
        ("POST", f"/conversations/{conversation}/turns", {"content": "x"})]:
        for invalid in [{"Origin": APP_ORIGIN}, [("Origin", APP_ORIGIN),
            ("X-CSRF-Token", headers["X-CSRF-Token"]), ("X-CSRF-Token", headers["X-CSRF-Token"])]]:
            assert client.request(method, path, headers=invalid, json=body).status_code == 403
    assert count_events(admin)[0] == 1
    assert client.get(f"/conversations/{conversation}/turns").json()["data"]["items"] == []
    def fail(*args, **kwargs):
        raise RuntimeError("fictional-DB-private-token-do-not-echo")
    monkeypatch.setattr(login.sessions, "authorized_transaction", fail)
    response = client.get("/conversations")
    assert response.status_code == 503 and response.json()["success"] is False
    assert "fictional-DB-private-token-do-not-echo" not in response.text + caplog.text


@pytest.mark.integration
def test_authentication_precedes_invalid_request_processing(system):
    client, _, _, _ = system
    for method, path, body in [("POST", "/conversations", {"patient_id": "fictional-private"}),
        ("GET", "/conversations/not-uuid", None), ("GET", "/conversations?limit=bad", None)]:
        response = client.request(method, path, headers={"Origin": APP_ORIGIN}, json=body)
        assert response.status_code == 401
        assert "fictional-private" not in response.text


@pytest.mark.integration
def test_rls_column_privileges_append_only_and_scope_lifetime(system, database):
    _, login, _, _admin = system
    runtime, _ = database
    headers = logged_in(system)
    grant(system[0], headers)
    with psycopg.connect(**runtime) as db:
        assert db.execute("SELECT count(*) FROM medical_rag.patient_consent_events").fetchone() == (0,)
        for column in ["identity_verified", "age_classification"]:
            assert db.execute("SELECT has_column_privilege(current_user,'medical_rag.patient_subjects',%s,'UPDATE')",
                              (column,)).fetchone() == (False,)
        assert db.execute("SELECT has_table_privilege(current_user,'medical_rag.patient_consent_events','UPDATE')").fetchone() == (False,)
        assert db.execute("SELECT has_table_privilege(current_user,'medical_rag.patient_consent_events','DELETE')").fetchone() == (False,)
        db.execute("SELECT set_config('app.patient_scope',pg_current_xact_id()::text||'/'||%s||'/'||%s,false)",
                   (TENANT, PATIENT))
        assert db.execute("SELECT count(*) FROM medical_rag.patient_consent_events").fetchone() == (1,)
        db.commit()
        assert db.execute("SELECT count(*) FROM medical_rag.patient_consent_events").fetchone() == (0,)
    assert login.sessions.authenticate(system[0].cookies.get("__Host-medical-session")).patient_id == PATIENT


def wait_for_lock(admin, app_name):
    with psycopg.connect(**admin) as observer:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            row = observer.execute("SELECT wait_event_type FROM pg_stat_activity WHERE application_name=%s",
                                   (app_name,)).fetchone()
            if row == ("Lock",):
                return
            time.sleep(0.02)
    pytest.fail("operation did not wait for patient lock")


@pytest.mark.integration
@pytest.mark.parametrize("change", ["withdrawal", "verification", "authority", "binding", "age", "tenant", "expiry"])
def test_patient_lock_wait_rechecks_consent_verification_and_authority(system, database, change):
    from medical_rag.identity_sessions import SessionStore
    from medical_rag.patient_service import HealthNotice, PatientDenied, PatientService
    client, _, _, admin = system
    runtime, _ = database
    headers = logged_in(system)
    grant(client, headers)
    app_name = "patient-wait-" + secrets.token_hex(8)
    store = SessionStore(lambda: psycopg.connect(**dict(runtime, application_name=app_name)), tenant_id=TENANT)
    service = PatientService(store, HealthNotice("fictional-v1", "fictional notice", 3600))
    token = client.cookies.get("__Host-medical-session")
    if change == "expiry":
        mutate(admin, "UPDATE medical_rag.identity_sessions SET expires_at=clock_timestamp()+interval '2 seconds'")
    with psycopg.connect(**dict(admin, autocommit=False)) as locker:
        locker.execute("SELECT patient_id FROM medical_rag.patient_subjects WHERE tenant_id=%s AND patient_id=%s FOR UPDATE",
                       (TENANT, PATIENT))
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(service.list_conversations, token)
            try:
                wait_for_lock(admin, app_name)
                if change == "withdrawal":
                    locker.execute("INSERT INTO medical_rag.patient_consent_events "
                        "(event_id,tenant_id,patient_id,grantor_account_id,purpose,notice_version,granted,expires_at) "
                        "VALUES (%s,%s,%s,%s,'health_information','fictional-v1',false,clock_timestamp())",
                        (uuid4(), TENANT, PATIENT, ACCOUNT))
                elif change in {"verification", "age"}:
                    locker.execute("UPDATE medical_rag.patient_subjects SET " +
                                   ("identity_verified=false" if change == "verification" else "age_classification='unknown'") +
                                   " WHERE tenant_id=%s AND patient_id=%s", (TENANT, PATIENT))
                elif change == "tenant":
                    mutate(admin, "UPDATE medical_rag.tenants SET active=false WHERE tenant_id=%s", (TENANT,))
                elif change == "expiry":
                    time.sleep(2.1)
                else:
                    mutate(admin, "UPDATE medical_rag.identity_accounts SET active=false WHERE account_id=%s"
                           if change == "authority" else "UPDATE medical_rag.identity_bindings SET verified=false WHERE account_id=%s",
                           (ACCOUNT,))
            finally:
                locker.commit()
            with pytest.raises((PatientDenied, AuthenticationDenied)):
                pending.result(timeout=5)


@pytest.mark.integration
def test_legacy_business_routes_absent(system):
    client, _, _, _ = system
    for path in ["/memory", "/documents", "/settings", "/jobs", "/api/chat"]:
        assert client.get(path).status_code == 404
    # /chat is this app's own POST-only answer route, not the legacy chat API.
    assert client.get("/chat").status_code == 405


CHAT_CITATIONS = [{"chunk_id": "c1", "filename": "指南.pdf", "section": "第1章",
                   "page": 3, "origin": "knowledge_base"}]


@pytest.mark.integration
def test_ask_persists_cited_answer_and_orders_turns(auth_system):
    from medical_rag.patient_api import create_patient_app

    chat = FakeChat(text="结论[c1]", citations=CHAT_CITATIONS)
    login, provider, admin, notice = provision(auth_system)
    with TestClient(create_patient_app(login, notice, chat=chat),
                    base_url=APP_ORIGIN, headers={}) as client:
        system = (client, login, provider, admin)
        headers = logged_in(system)
        grant(client, headers)
        response = client.post("/chat", headers=headers, json={"question": "高血压如何自测？"})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        cid = data["conversation"]["id"]
        assert data["conversation"]["title"] == "高血压如何自测？"
        assert data["question"]["role"] == "user" and data["question"]["content"] == "高血压如何自测？"
        assert data["answer"]["role"] == "assistant" and data["answer"]["content"] == "结论[c1]"
        assert data["answer"]["citations"] == CHAT_CITATIONS
        items = client.get(f"/conversations/{cid}/turns").json()["data"]["items"]
        assert [(item["role"], item["content"]) for item in items] == [
            ("user", "高血压如何自测？"), ("assistant", "结论[c1]")]
        with psycopg.connect(**admin) as db:
            actions = dict(db.execute(
                "SELECT action,count(*) FROM medical_rag.security_audit_events "
                "WHERE action IN ('chat.ask','turn.append') GROUP BY action").fetchall())
        assert actions == {"chat.ask": 1, "turn.append": 1}
        second = client.post("/chat", headers=headers,
                             json={"question": "继续", "conversation_id": cid})
        assert second.status_code == 200
        assert second.json()["data"]["conversation"]["id"] == cid
        assert len(client.get(f"/conversations/{cid}/turns").json()["data"]["items"]) == 4
        assert chat.questions == ["高血压如何自测？", "继续"]


@pytest.mark.integration
def test_ask_drops_malformed_citations_before_persisting(auth_system):
    from medical_rag.patient_api import create_patient_app

    chat = FakeChat(text="答案", citations=[
        {"chunk_id": "c1", "filename": "指南.pdf", "section": 7, "page": -1},
        {"chunk_id": "", "filename": "x.pdf"},
        "junk",
    ])
    login, provider, admin, notice = provision(auth_system)
    with TestClient(create_patient_app(login, notice, chat=chat),
                    base_url=APP_ORIGIN, headers={}) as client:
        headers = logged_in((client, login, provider, admin))
        grant(client, headers)
        response = client.post("/chat", headers=headers, json={"question": "q"})
        assert response.status_code == 200
        assert response.json()["data"]["answer"]["citations"] == [
            {"chunk_id": "c1", "filename": "指南.pdf", "section": ""}]
        cid = response.json()["data"]["conversation"]["id"]
        items = client.get(f"/conversations/{cid}/turns").json()["data"]["items"]
        assert items[1]["citations"] == [
            {"chunk_id": "c1", "filename": "指南.pdf", "section": ""}]


@pytest.mark.integration
def test_ask_denies_before_calling_answerer_without_consent(auth_system):
    from medical_rag.patient_api import create_patient_app

    chat = FakeChat(error=AssertionError("answerer must not run"))
    login, provider, admin, notice = provision(auth_system)
    with TestClient(create_patient_app(login, notice, chat=chat),
                    base_url=APP_ORIGIN, headers={}) as client:
        headers = logged_in((client, login, provider, admin))
        assert client.post("/chat", headers=headers, json={"question": "x"}).status_code == 403
        assert chat.questions == []
        assert client.get("/conversations").status_code == 403


@pytest.mark.integration
def test_ask_is_static_503_when_upstream_fails(auth_system):
    from medical_rag.patient_api import create_patient_app
    from medical_rag.patient_chat import ChatUnavailable

    chat = FakeChat(error=ChatUnavailable())
    login, provider, admin, notice = provision(auth_system)
    with TestClient(create_patient_app(login, notice, chat=chat),
                    base_url=APP_ORIGIN, headers={}) as client:
        headers = logged_in((client, login, provider, admin))
        grant(client, headers)
        response = client.post("/chat", headers=headers, json={"question": "x"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "CHAT_UNAVAILABLE"
        assert client.get("/conversations").json()["data"]["items"] == []
        with psycopg.connect(**admin) as db:
            assert db.execute("SELECT count(*) FROM medical_rag.conversation_turns").fetchone() == (0,)


@pytest.mark.integration
def test_ask_is_static_503_when_not_configured(system):
    client, _, _, _ = system
    headers = logged_in(system)
    grant(client, headers)
    assert client.get("/me").json()["data"]["permissions"]["chat"] is False
    assert client.post("/chat", headers=headers, json={"question": "x"}).status_code == 503


@pytest.mark.integration
def test_ask_discards_answer_when_consent_lapses_during_generation(auth_system):
    from medical_rag.patient_api import create_patient_app

    login, provider, admin, notice = provision(auth_system)

    def withdraw():
        with psycopg.connect(**admin) as db:
            db.execute("INSERT INTO medical_rag.patient_consent_events "
                       "(event_id,tenant_id,patient_id,grantor_account_id,purpose,notice_version,granted,expires_at) "
                       "VALUES (%s,%s,%s,%s,'health_information','fictional-v1',false,clock_timestamp())",
                       (uuid4(), TENANT, PATIENT, ACCOUNT))

    chat = FakeChat(before=withdraw)
    with TestClient(create_patient_app(login, notice, chat=chat),
                    base_url=APP_ORIGIN, headers={}) as client:
        headers = logged_in((client, login, provider, admin))
        grant(client, headers)
        assert client.post("/chat", headers=headers, json={"question": "x"}).status_code == 403
        assert chat.questions == ["x"]
        assert client.get("/conversations").status_code == 403
        with psycopg.connect(**admin) as db:
            assert db.execute("SELECT count(*) FROM medical_rag.conversation_turns").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM medical_rag.conversations").fetchone() == (0,)


@pytest.mark.integration
def test_ask_requires_csrf_and_bounded_body(auth_system):
    from medical_rag.patient_api import create_patient_app

    chat = FakeChat()
    login, provider, admin, notice = provision(auth_system)
    with TestClient(create_patient_app(login, notice, chat=chat),
                    base_url=APP_ORIGIN, headers={}) as client:
        headers = logged_in((client, login, provider, admin))
        grant(client, headers)
        assert client.post("/chat", headers={"Origin": APP_ORIGIN},
                           json={"question": "x"}).status_code == 403
        for body in [{"question": ""}, {"question": "x" * 16001}, {"question": "x", "extra": 1},
                     {"question": 1}, {"question": "  "}]:
            assert client.post("/chat", headers=headers, json=body).status_code in {400, 422}
        assert chat.questions == []


@pytest.mark.integration
def test_ask_rejects_another_patients_conversation_before_calling_answerer(auth_system):
    from medical_rag.patient_api import create_patient_app

    chat = FakeChat(error=AssertionError("answerer must not run for a foreign conversation"))
    login, provider, admin, notice = provision(auth_system)
    with TestClient(create_patient_app(login, notice, chat=chat),
                    base_url=APP_ORIGIN, headers={}) as client:
        headers = logged_in((client, login, provider, admin))
        grant(client, headers)
        foreign = client.post("/conversations", headers=headers,
                              json={"title": "first patient"}).json()["data"]["id"]
        second = login.sessions.issue(VerifiedIdentity(ISSUER, "subject-2", True))
        client.cookies.clear()
        client.cookies.set("__Host-medical-session", second.token)
        client.cookies.set("__Host-medical-csrf", second.csrf_token)
        headers = {"Origin": APP_ORIGIN, "X-CSRF-Token": second.csrf_token}
        grant(client, headers)
        response = client.post("/chat", headers=headers,
                               json={"question": "x", "conversation_id": foreign})
        assert response.status_code == 404
        assert chat.questions == []


@pytest.mark.integration
def test_consent_server_order_timestamp_and_append_only(system):
    _, _, _, admin = system
    supplied = uuid4()
    mutate(admin, "INSERT INTO medical_rag.patient_consent_events "
        "(event_id,tenant_id,patient_id,grantor_account_id,purpose,notice_version,granted,expires_at,event_order,recorded_at) "
        "VALUES (%s,%s,%s,%s,'health_information','fictional-v1',true,clock_timestamp()+interval '1 hour',"
        "999999,'2000-01-01T00:00:00Z')", (supplied, TENANT, PATIENT, ACCOUNT))
    with psycopg.connect(**admin) as db:
        row = db.execute("SELECT event_order,recorded_at > '2020-01-01' FROM medical_rag.patient_consent_events").fetchone()
        assert row == (1, True)
        for operation in ["UPDATE medical_rag.patient_consent_events SET granted=false", "DELETE FROM medical_rag.patient_consent_events"]:
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(operation)
        assert db.execute("SELECT granted FROM medical_rag.patient_consent_events").fetchone() == (True,)
    client = system[0]
    headers = logged_in(system)
    grant(client, headers)
    with psycopg.connect(**admin) as db:
        assert db.execute("SELECT array_agg(event_order ORDER BY event_order) FROM medical_rag.patient_consent_events").fetchone() == ([1, 2],)


@pytest.mark.parametrize("args", [("", "text", 1), ("v", "", 1), ("v", "text", 0),
    ("v", "text", 31536001), ("v", "text", True), ("v" * 129, "text", 1), ("v", "x" * 64001, 1)])
def test_notice_configuration_requires_actual_bounded_trusted_text(args):
    from medical_rag.patient_service import HealthNotice
    with pytest.raises(ValueError):
        HealthNotice(*args)

# -- structured dialogue state ----------------------------------------------

@dataclass
class DialogueSystem:
    client: object
    admin: dict
    entered: Event
    release: Event
    auth: tuple
    requests: list = field(default_factory=list)
    block_on_first: bool = False


def _dialogue_handler(container):
    """Synthetic upstream that resolves from the context the backend sent."""

    def handler(request):
        current = container[0]
        payload = json.loads(request.read())
        current.requests.append(payload)
        if current.block_on_first and len(current.requests) == 1:
            current.entered.set()
            assert current.release.wait(5)
        question = payload["question"]
        previous = (payload.get("dialogue_context") or {}).get("state") or {}
        old_topic = previous.get("active_topic") or {}
        topic = ("糖尿病" if "糖尿病" in question
                 else "高血压" if "高血压" in question else old_topic.get("name", "糖尿病"))
        source = "current" if topic in question else old_topic["source_turn_id"]
        cause = "引起" in question
        resolution = {
            "original_question": question, "status": "resolved",
            "relation": ("standalone" if not previous else
                         "topic_switch" if source == "current" else "follow_up"),
            "resolved_question": topic + ("是由什么引起的？" if cause
                                          else "在日常生活中需要注意什么？"),
            "topic": {"name": topic, "type": "disease", "source_turn_id": source,
                      "source_role": "user"},
            "intent": "etiology" if cause else "daily_precautions",
            "intent_text": "由什么引起" if cause else "日常注意事项",
            "references": [], "clarification": None,
        }
        return httpx.Response(200, json={"success": True, "error": None, "data": {
            "answer": "合成回答", "abstained": False, "citations": [],
            "answer_type": "answer", "dialogue_resolution": resolution,
        }})

    return handler


@pytest.fixture
def dialogue_system(auth_system):
    from medical_rag.patient_api import create_patient_app

    login, provider, admin, notice = provision(auth_system)
    container = []
    chat = ChatClient("http://127.0.0.1:8001",
                      transport=httpx.MockTransport(_dialogue_handler(container)))
    with TestClient(create_patient_app(login, notice, chat=chat,
                                      dialogue_state_enabled=True),
                    base_url=APP_ORIGIN, headers={}) as client:
        result = DialogueSystem(client, admin, Event(), Event(),
                                (client, login, provider, admin))
        container.append(result)
        try:
            yield result
        finally:
            result.release.set()


@pytest.mark.integration
def test_patient_three_turns_keep_the_subject_and_intent(dialogue_system):
    client = dialogue_system.client
    headers = logged_in(dialogue_system.auth)
    grant(client, headers)

    first = client.post("/chat", headers=headers,
                        json={"question": "高血压在日常生活中需要注意什么？"})
    assert first.status_code == 200, first.text
    identifier = first.json()["data"]["conversation"]["id"]
    second = client.post("/chat", headers=headers,
                         json={"question": "糖尿病呢？", "conversation_id": identifier})
    assert second.status_code == 200, second.text
    third = client.post("/chat", headers=headers,
                        json={"question": "那它是由什么引起的？", "conversation_id": identifier})
    assert third.status_code == 200, third.text

    requests = dialogue_system.requests
    assert requests[0]["dialogue_context"]["state"] is None
    assert requests[1]["dialogue_context"]["state"]["active_topic"]["name"] == "高血压"
    assert requests[2]["dialogue_context"]["state"]["active_topic"]["name"] == "糖尿病"
    assert requests[2]["dialogue_context"]["state"]["active_intent"] == "daily_precautions"

    items = client.get(f"/conversations/{identifier}/turns").json()["data"]["items"]
    assert items[1]["answer_type"] == "answer"
    assert "dialogue_state" not in items[0]

    fourth = client.post("/chat", headers=headers,
                         json={"question": "它有什么症状？", "conversation_id": identifier})
    assert fourth.status_code == 200, fourth.text
    assert requests[3]["dialogue_context"]["state"]["active_intent"] == "etiology"
    assert requests[3]["dialogue_context"]["state"]["active_topic"]["name"] == "糖尿病"


@pytest.mark.integration
def test_append_during_generation_rejects_stale_answer(dialogue_system):
    client = dialogue_system.client
    dialogue_system.block_on_first = True
    headers = logged_in(dialogue_system.auth)
    grant(client, headers)
    identifier = client.post("/conversations", headers=headers,
                             json={"title": "并发合成会话"}).json()["data"]["id"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(client.post, "/chat", headers=headers,
                                 json={"conversation_id": identifier,
                                       "question": "高血压日常需要注意什么？"})
        try:
            assert dialogue_system.entered.wait(5)
            appended = client.post(f"/conversations/{identifier}/turns", headers=headers,
                                   json={"content": "另一条合成消息"})
            assert appended.status_code == 200, appended.text
        finally:
            dialogue_system.release.set()
        response = future.result(timeout=10)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONVERSATION_CONTEXT_CHANGED"
    turns = client.get(f"/conversations/{identifier}/turns").json()["data"]["items"]
    assert len(turns) == 1
    assert turns[0]["content"] == "另一条合成消息"
