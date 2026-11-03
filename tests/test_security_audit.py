"""Connected audit with fictional identity and actual least-privilege PG."""
# ruff: noqa: F401,F811 -- imported pytest fixtures

import importlib.util
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from test_patient_vertical import (
    ACCOUNT,
    APP_ORIGIN,
    ISSUER,
    OTHER,
    OTHER_ACCOUNT,
    OTHER_TENANT,
    PATIENT,
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

from medical_rag.identity_sessions import AuthenticationDenied
from medical_rag.oidc_identity import VerifiedIdentity


def events(admin):
    with psycopg.connect(**admin) as db:
        cursor = db.execute("SELECT * FROM medical_rag.security_audit_events ORDER BY event_order")
        names = [column.name for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def audit_insert(admin, *, allowed):
    verb, direction = ("GRANT", "TO") if allowed else ("REVOKE", "FROM")
    mutate(admin, f"{verb} INSERT (tenant_id,operator_id,action,resource_type,outcome,reason_code,request_id) "
           f"ON medical_rag.security_audit_events {direction} login_test")


def auditor(system, *, mfa=True, role="auditor", account=ACCOUNT):
    client, login, _, admin = system
    mutate(admin, "DELETE FROM medical_rag.identity_roles WHERE account_id=%s", (account,))
    mutate(admin, "INSERT INTO medical_rag.identity_roles VALUES (%s,%s,%s)", (TENANT, account, role))
    issued = login.sessions.issue(VerifiedIdentity(ISSUER, "subject-1" if account == ACCOUNT else "subject-2", mfa))
    client.cookies.clear()
    client.cookies.set("__Host-medical-session", issued.token)
    client.cookies.set("__Host-medical-csrf", issued.csrf_token)
    return issued


def test_audit_is_a_connected_module():
    assert importlib.util.find_spec("medical_rag.security_audit") is not None


@pytest.mark.integration
def test_full_http_flow_has_one_minimal_event_per_request_and_private_canaries(system, caplog):
    client, _, _, admin = system
    headers = logged_in(system)
    responses = [client.get("/me"), client.get("/me/consents/health"), grant(client, headers)]
    created = client.post("/conversations", headers=headers | {"X-Request-ID": "untrusted-header-CANARY"},
                          json={"title": "title-private-CANARY"})
    responses.append(created)
    identifier = created.json()["data"]["id"]
    responses.extend([
        client.post(f"/conversations/{identifier}/turns", headers=headers,
                    json={"content": "health-content-private-CANARY"}),
        client.get(f"/conversations/{identifier}"),
        client.get(f"/conversations/{identifier}/turns"),
        client.get("/conversations"),
        client.delete("/me/consents/health", headers=headers),
        client.post("/auth/logout", headers=headers),
    ])
    assert all("X-Request-ID" in response.headers for response in responses)
    rows = events(admin)
    assert len(rows) == len(responses) + 3  # begin/callback/session during logged_in
    for response in responses:
        assert response.status_code == 200
        request_id = UUID(response.headers["X-Request-ID"])
        matching = [row for row in rows if row["request_id"] == request_id]
        assert len(matching) == 1
        assert matching[0]["operator_id"] == UUID(ACCOUNT)
        assert matching[0]["outcome"] == "allowed"
    assert {r["action"] for r in rows} >= {
        "login.start", "login.finish", "session.read", "identity.read", "consent.read",
        "consent.grant", "consent.withdraw", "conversation.create", "conversation.read",
        "conversation.list", "turn.append", "turn.list", "logout",
    }
    assert set(rows[0]) == {"event_id", "tenant_id", "operator_id", "action", "resource_type",
                            "outcome", "reason_code", "recorded_at", "event_order", "request_id"}
    serialized = json.dumps(rows, default=str) + caplog.text
    for secret in [identifier, PATIENT, "title-private-CANARY", "health-content-private-CANARY",
                   "untrusted-header-CANARY", "fake-access-NEVER-RETURN", "fake-refresh-NEVER-RETURN",
                   "fictional-client-secret-with-32-characters", headers["X-CSRF-Token"]]:
        assert secret not in serialized
    assert rows[0]["operator_id"] is None


@pytest.mark.integration
def test_auditor_live_policy_mfa_scope_and_metadata(system):
    client, _, _, admin = system
    logged_in(system)
    client.get("/me")
    auditor(system)
    assert client.get("/me").json()["data"]["permissions"] == {
        "identity": True, "health_consent": False, "conversations": False, "audit": True,
        "support_self": True, "support_queue": False,
    }
    response = client.get("/audit?limit=20&offset=0")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["limit"] == 20 and data["offset"] == 0
    assert len(data["items"]) > 0
    assert set(data["items"][0]) == {"id", "time", "operator_id", "action", "resource_type",
                                      "outcome", "reason_code", "request_id"}
    row = events(admin)[-1]
    assert row["action"] == "audit.read" and str(row["request_id"]) == response.headers["X-Request-ID"]
    assert client.get("/me/consents/health").status_code == 403
    assert client.get("/conversations").status_code == 403
    mutate(admin, "DELETE FROM medical_rag.identity_roles WHERE account_id=%s", (ACCOUNT,))
    assert client.get("/audit").status_code == 401  # role mutation invalidates the old session epoch
    auditor(system, mfa=False)
    assert client.get("/audit").status_code == 403
    assert client.get("/me").json()["data"]["permissions"]["audit"] is False
    auditor(system)
    mutate(admin, "UPDATE medical_rag.identity_accounts SET active=false WHERE account_id=%s", (ACCOUNT,))
    assert client.get("/audit").status_code == 401


@pytest.mark.integration
@pytest.mark.parametrize("role", ["patient", "support", "org_admin", "platform_operator"])
def test_roles_do_not_gain_audit(system, role):
    client, _, _, _ = system
    auditor(system, role=role)
    assert client.get("/me").json()["data"]["permissions"]["audit"] is False
    assert client.get("/audit").status_code == 403


@pytest.mark.integration
@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=10001", "limit=x",
                                    "tenant_id=CANARY", "account_id=CANARY", "limit=1&limit=2"])
def test_audit_rejects_filters_and_invalid_pagination(system, query):
    client, _, _, admin = system
    auditor(system)
    response = client.get("/audit?" + query)
    assert response.status_code == 400
    row = events(admin)[-1]
    assert row["outcome"] == "denied" and row["reason_code"] == "invalid_request"
    assert "CANARY" not in json.dumps(events(admin), default=str)


@pytest.mark.integration
def test_denials_uniform_missing_ids_actor_and_untrusted_input(system, caplog):
    client, _, _, admin = system
    response = client.get("/me", headers={"X-Request-ID": "caller-CANARY"})
    assert response.status_code == 401
    assert events(admin)[-1]["operator_id"] is None
    headers = logged_in(system)
    grant(client, headers)
    guessed = str(uuid4())
    first = client.get("/conversations/" + guessed)
    second = client.get("/conversations/not-a-uuid-CANARY")
    assert first.status_code == second.status_code == 404 and first.json() == second.json()
    invalid = client.post("/conversations", headers=headers, json={"title": "body-CANARY", "patient_id": PATIENT})
    assert invalid.status_code == 400
    origin = client.post("/conversations", headers=headers | {"Origin": "https://bad-CANARY.example"},
                         json={"title": "other-body-CANARY"})
    assert origin.status_code == 403
    wrong_csrf = client.post("/conversations", headers=headers | {"X-CSRF-Token": "csrf-CANARY"},
                            json={"title": "csrf-body-CANARY"})
    assert wrong_csrf.status_code == 401
    assert events(admin)[-1]["operator_id"] is None
    serialized = json.dumps(events(admin), default=str) + caplog.text
    for raw in [guessed, PATIENT, "caller-CANARY", "not-a-uuid-CANARY", "body-CANARY", "bad-CANARY", "csrf-CANARY"]:
        assert raw not in serialized


@pytest.mark.integration
def test_audit_insert_loss_rolls_back_write_blocks_reads_login_and_clears_revoked_logout(system):
    client, login, _, admin = system
    headers = logged_in(system)
    grant(client, headers)
    created = client.post("/conversations", headers=headers, json={"title": "private-rollback-CANARY"})
    identifier = created.json()["data"]["id"]
    session = client.cookies.get("__Host-medical-session")
    audit_insert(admin, allowed=False)
    try:
        for response in [client.post("/conversations", headers=headers, json={"title": "must-roll-back"}),
                         client.get("/conversations/" + identifier),
                         client.post("/me/consents/health", headers=headers,
                                     json={"version": "fictional-v1", "accepted": True})]:
            assert response.status_code == 503 and response.json()["data"] is None
            assert "private-rollback-CANARY" not in response.text
        with psycopg.connect(**admin) as db:
            assert db.execute("SELECT count(*) FROM medical_rag.conversations").fetchone() == (1,)
            assert db.execute("SELECT count(*) FROM medical_rag.patient_consent_events").fetchone() == (1,)
        response = client.post("/auth/logout", headers=headers)
        assert response.status_code == 503
        assert "__Host-medical-session" not in client.cookies
        assert any("Max-Age=0" in value for value in response.headers.get_list("set-cookie"))
        with pytest.raises(AuthenticationDenied):
            login.sessions.authenticate(session)
        response = client.post("/auth/login", headers={"Origin": APP_ORIGIN}, follow_redirects=False)
        assert response.status_code == 503 and "set-cookie" not in response.headers
    finally:
        audit_insert(admin, allowed=True)


@pytest.mark.integration
def test_callback_audit_failure_emits_no_session_cookie_and_revokes_unexposed_session(system):
    client, _, _, admin = system
    _, query = start(system)
    audit_insert(admin, allowed=False)
    try:
        response = client.get("/auth/callback", params={"state": query["state"][0], "code": "fictional-code"},
                              follow_redirects=False)
        assert response.status_code == 503
        assert "__Host-medical-session" not in client.cookies
        with psycopg.connect(**admin) as db:
            assert db.execute("SELECT count(*) FROM medical_rag.identity_sessions WHERE NOT revoked").fetchone() == (0,)
    finally:
        audit_insert(admin, allowed=True)


@pytest.mark.integration
def test_audit_rls_other_institution_pool_scope_and_no_mutation(system, database):
    client, _, _, admin = system
    runtime, _ = database
    logged_in(system)
    auditor(system)
    # Owner provisioning of fictional second institution metadata, not a user filter.
    with psycopg.connect(**admin) as db:
        db.execute("INSERT INTO medical_rag.security_audit_events "
                   "(tenant_id,operator_id,action,resource_type,outcome,reason_code,request_id) "
                   "VALUES (%s,NULL,'identity.read','identity','allowed','ok',%s)", (OTHER_TENANT, uuid4()))
        other_id = db.execute("SELECT event_id FROM medical_rag.security_audit_events WHERE tenant_id=%s",
                              (OTHER_TENANT,)).fetchone()[0]
    assert str(other_id) not in client.get("/audit").text
    with psycopg.connect(**runtime) as db:
        with db.transaction():
            assert db.execute("SELECT count(*) FROM medical_rag.security_audit_events").fetchone() == (0,)
            db.execute("SELECT set_config('app.audit_scope',pg_current_xact_id()::text||'/'||%s,false)", (TENANT,))
            count = db.execute("SELECT count(*) FROM medical_rag.security_audit_events").fetchone()[0]
            assert count > 0
            assert db.execute("SELECT count(*) FROM medical_rag.security_audit_events WHERE tenant_id=%s",
                              (OTHER_TENANT,)).fetchone() == (0,)
        # A session-level stale pooled value cannot authorize a later xact.
        with db.transaction():
            db.execute("SELECT pg_current_xact_id()")
            assert db.execute("SELECT count(*) FROM medical_rag.security_audit_events").fetchone() == (0,)
        for statement in ["UPDATE medical_rag.security_audit_events SET outcome='error'",
                          "DELETE FROM medical_rag.security_audit_events", "TRUNCATE medical_rag.security_audit_events"]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
                db.execute(statement)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute("INSERT INTO medical_rag.security_audit_events "
                       "(tenant_id,action,resource_type,outcome,reason_code,request_id) "
                       "VALUES (%s,'identity.read','identity','allowed','ok',%s)", (TENANT, uuid4()))
    # Even owner DML is rejected by the append-only trigger (DDL owner remains trusted).
    for statement in ["UPDATE medical_rag.security_audit_events SET outcome='error'",
                      "DELETE FROM medical_rag.security_audit_events", "TRUNCATE medical_rag.security_audit_events"]:
        with pytest.raises(psycopg.errors.RaiseException), psycopg.connect(**admin) as db:
            db.execute(statement)


@pytest.mark.integration
def test_audit_read_insert_loss_and_storage_error_are_static_and_closed(system, caplog):
    client, _, _, admin = system
    auditor(system)
    audit_insert(admin, allowed=False)
    try:
        response = client.get("/audit")
        assert response.status_code == 503 and response.json()["data"] is None
    finally:
        audit_insert(admin, allowed=True)
    with psycopg.connect(**admin) as db:
        db.execute("CREATE FUNCTION medical_rag.audit_storage_failure() RETURNS trigger LANGUAGE plpgsql AS $$ "
                   "BEGIN RAISE EXCEPTION 'private-storage-error-CANARY'; END; $$")
        db.execute("CREATE TRIGGER audit_storage_failure BEFORE INSERT ON medical_rag.security_audit_events "
                   "FOR EACH ROW EXECUTE FUNCTION medical_rag.audit_storage_failure()")
    try:
        response = client.get("/audit")
        assert response.status_code == 503 and response.json()["data"] is None
        assert "private-storage-error-CANARY" not in response.text + caplog.text
    finally:
        mutate(admin, "DROP TRIGGER audit_storage_failure ON medical_rag.security_audit_events")
        mutate(admin, "DROP FUNCTION medical_rag.audit_storage_failure()")


@pytest.mark.integration
def test_preflight_requires_audit_schema_and_minimal_rights(system, database):
    from medical_rag.security_config import StartupError
    from medical_rag.security_server import preflight
    runtime, admin = database
    config = SimpleNamespace(database_dsn=psycopg.conninfo.make_conninfo(
        **{key: value for key, value in runtime.items() if key != "autocommit"}), tenant_id=TENANT)
    preflight(config)
    mutate(admin, "ALTER TABLE medical_rag.security_audit_events RENAME TO hidden_audit")
    try:
        with pytest.raises(StartupError):
            preflight(config)
    finally:
        mutate(admin, "ALTER TABLE medical_rag.hidden_audit RENAME TO security_audit_events")
    for privilege in ["UPDATE(outcome)", "DELETE", "TRUNCATE"]:
        mutate(admin, f"GRANT {privilege} ON medical_rag.security_audit_events TO login_test")
        try:
            with pytest.raises(StartupError):
                preflight(config)
        finally:
            mutate(admin, f"REVOKE {privilege} ON medical_rag.security_audit_events FROM login_test")
    audit_insert(admin, allowed=False)
    try:
        with pytest.raises(StartupError):
            preflight(config)
    finally:
        audit_insert(admin, allowed=True)


@pytest.mark.integration
def test_auth_failures_and_host_origin_guard_are_recorded_before_response(system):
    client, _, provider, admin = system
    for headers in [{"Origin": "https://untrusted-CANARY.example"},
                    {"Origin": APP_ORIGIN, "Host": "host-CANARY.example"}]:
        response = client.post("/auth/login", headers=headers, follow_redirects=False)
        assert response.status_code == 403
        row = events(admin)[-1]
        assert row["action"] == "login.start" and row["outcome"] == "denied" and row["operator_id"] is None
        assert str(row["request_id"]) == response.headers["X-Request-ID"]
    _, query = start(system)
    provider["mode"] = "raise"
    response = client.get("/auth/callback", params={"state": query["state"][0], "code": "code-private-CANARY"},
                          follow_redirects=False)
    assert response.status_code == 401
    assert events(admin)[-1]["action"] == "login.finish"
    assert events(admin)[-1]["operator_id"] is None
    response = client.get("/auth/session")
    assert response.status_code == 401 and events(admin)[-1]["action"] == "session.read"
    response = client.post("/auth/logout", headers={"Origin": APP_ORIGIN})
    assert response.status_code == 403 and events(admin)[-1]["action"] == "logout"
    assert "CANARY" not in json.dumps(events(admin), default=str)


@pytest.mark.integration
def test_patient_storage_error_rolls_back_then_records_only_static_error(system, caplog):
    client, _, _, admin = system
    headers = logged_in(system)
    grant(client, headers)
    with psycopg.connect(**admin) as db:
        db.execute("CREATE FUNCTION medical_rag.patient_storage_failure() RETURNS trigger LANGUAGE plpgsql AS $$ "
                   "BEGIN RAISE EXCEPTION 'private-patient-storage-error-CANARY'; END; $$")
        db.execute("CREATE TRIGGER patient_storage_failure BEFORE INSERT ON medical_rag.conversations "
                   "FOR EACH ROW EXECUTE FUNCTION medical_rag.patient_storage_failure()")
    try:
        response = client.post("/conversations", headers=headers, json={"title": "title-storage-CANARY"})
        assert response.status_code == 503 and response.json()["data"] is None
        row = events(admin)[-1]
        assert row["action"] == "conversation.create" and row["outcome"] == "error"
        assert row["reason_code"] == "operation_unavailable" and row["operator_id"] == UUID(ACCOUNT)
        assert "CANARY" not in response.text + caplog.text + json.dumps(events(admin), default=str)
        with psycopg.connect(**admin) as db:
            assert db.execute("SELECT count(*) FROM medical_rag.conversations").fetchone() == (0,)
    finally:
        mutate(admin, "DROP TRIGGER patient_storage_failure ON medical_rag.conversations")
        mutate(admin, "DROP FUNCTION medical_rag.patient_storage_failure()")


@pytest.mark.integration
def test_second_patient_invisible_and_missing_ids_have_identical_minimal_denial(system):
    client, _, _, admin = system
    headers = logged_in(system)
    grant(client, headers)
    created = client.post("/conversations", headers=headers, json={"title": "other-patient-CANARY"})
    other_id = created.json()["data"]["id"]
    auditor(system, role="patient", account=OTHER_ACCOUNT)
    csrf = client.get("/auth/session").json()["data"]["csrf_token"]
    grant(client, {"Origin": APP_ORIGIN, "X-CSRF-Token": csrf})
    first = client.get("/conversations/" + other_id)
    second = client.get("/conversations/" + str(uuid4()))
    assert first.status_code == second.status_code == 404 and first.json() == second.json()
    last = events(admin)[-2:]
    assert all(row["operator_id"] == UUID(OTHER_ACCOUNT) and row["reason_code"] == "resource_not_found" for row in last)
    assert other_id not in json.dumps(events(admin), default=str)


@pytest.mark.integration
def test_unrecorded_success_cannot_bypass_mandatory_guard(system):
    """An accidentally unconnected endpoint may not release any sensitive body."""
    client, _, _, admin = system

    @client.app.get("/unconnected")
    def unconnected():
        return {"success": True, "data": "private-unconnected-CANARY", "error": None}

    response = client.get("/unconnected")
    assert response.status_code == 503 and response.json()["data"] is None
    assert "private-unconnected-CANARY" not in response.text
    assert events(admin)[-1]["outcome"] == "error"


@pytest.mark.integration
def test_reauthentication_failure_revokes_unexposed_new_session_and_audits_error(system, monkeypatch, caplog):
    client, login, _, admin = system
    logged_in(system)
    previous = client.cookies.get("__Host-medical-session")
    _, query = start(system)
    original = login.sessions.revoke

    def fail_old(token, *, csrf_token):
        if token == previous:
            raise RuntimeError("private-reauthentication-storage-CANARY")
        original(token, csrf_token=csrf_token)

    monkeypatch.setattr(login.sessions, "revoke", fail_old)
    response = client.get("/auth/callback", params={"state": query["state"][0], "code": "fictional-code"},
                          follow_redirects=False)
    assert response.status_code == 503
    assert client.cookies.get("__Host-medical-session") == previous
    with psycopg.connect(**admin) as db:
        assert db.execute("SELECT count(*) FROM medical_rag.identity_sessions WHERE NOT revoked").fetchone() == (1,)
    row = events(admin)[-1]
    assert row["action"] == "login.finish" and row["outcome"] == "error"
    assert "private-reauthentication-storage-CANARY" not in response.text + caplog.text


@pytest.mark.integration
def test_wrong_audit_role_denies_before_select_and_read_storage_loss_is_closed(system):
    client, _, _, admin = system
    auditor(system, role="support")
    mutate(admin, "REVOKE SELECT ON medical_rag.security_audit_events FROM login_test")
    try:
        assert client.get("/audit").status_code == 403
        auditor(system)
        response = client.get("/audit")
        assert response.status_code == 503 and response.json()["data"] is None
        assert events(admin)[-1]["outcome"] == "error"
    finally:
        mutate(admin, "GRANT SELECT ON medical_rag.security_audit_events TO login_test")


@pytest.mark.integration
def test_removed_account_does_not_erase_audit_history(system):
    client, _, _, admin = system
    auditor(system)
    assert client.get("/me").status_code == 200
    before = events(admin)
    # This fictional account has no consent history. Remove its existing
    # authority/session FK dependants first; do not alter production schema.
    for table in ("identity_roles", "identity_bindings", "identity_sessions"):
        mutate(admin, f"DELETE FROM medical_rag.{table} WHERE tenant_id=%s AND account_id=%s", (TENANT, ACCOUNT))
    mutate(admin, "DELETE FROM medical_rag.identity_accounts WHERE tenant_id=%s AND account_id=%s", (TENANT, ACCOUNT))
    assert events(admin) == before and before[-1]["operator_id"] == UUID(ACCOUNT)


@pytest.mark.integration
def test_threadpool_request_ids_are_distinct_and_correlate_even_for_invisible_ids(system):
    from concurrent.futures import ThreadPoolExecutor

    client, _, _, admin = system
    headers = logged_in(system)
    grant(client, headers)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.get("/conversations/" + str(uuid4()),
                                 headers={"X-Request-ID": "forged-shared-CANARY"}), range(8)))
    assert all(response.status_code == 404 for response in responses)
    ids = {UUID(response.headers["X-Request-ID"]) for response in responses}
    assert len(ids) == 8
    rows = [row for row in events(admin) if row["request_id"] in ids]
    assert len(rows) == 8
    assert all(row["action"] == "conversation.read" and row["operator_id"] == UUID(ACCOUNT) for row in rows)


@pytest.mark.integration
def test_auditor_revoked_session_denies_and_separately_authorized_adult_can_use_own_patient_scope(system):
    client, login, _, admin = system
    issued = auditor(system)
    mutate(admin, "INSERT INTO medical_rag.identity_roles VALUES (%s,%s,'patient')", (TENANT, ACCOUNT))
    # Current role mutation invalidates prior session; a new independently
    # verified local login has both permissions, still no consent by role alone.
    assert client.get("/audit").status_code == 401
    client.cookies.clear()
    issued = login.sessions.issue(VerifiedIdentity(ISSUER, "subject-1", True))
    client.cookies.set("__Host-medical-session", issued.token)
    client.cookies.set("__Host-medical-csrf", issued.csrf_token)
    permissions = client.get("/me").json()["data"]["permissions"]
    assert permissions == {"identity": True, "health_consent": True, "conversations": False, "audit": True,
                           "support_self": True, "support_queue": False}
    grant(client, {"Origin": APP_ORIGIN, "X-CSRF-Token": issued.csrf_token})
    assert client.get("/me").json()["data"]["permissions"]["conversations"] is True
    assert client.get("/audit").status_code == 200
    login.sessions.revoke(issued.token, csrf_token=issued.csrf_token)
    assert client.get("/audit").status_code == 401
    assert events(admin)[-1]["operator_id"] is None


@pytest.mark.integration
@pytest.mark.parametrize("privilege", ["INSERT(event_id)", "INSERT(recorded_at)", "INSERT(event_order)",
                                        "REFERENCES", "TRIGGER"])
def test_preflight_rejects_audit_server_metadata_or_extra_table_grants(system, database, privilege):
    from medical_rag.security_config import StartupError
    from medical_rag.security_server import preflight
    runtime, admin = database
    config = SimpleNamespace(database_dsn=psycopg.conninfo.make_conninfo(
        **{key: value for key, value in runtime.items() if key != "autocommit"}), tenant_id=TENANT)
    mutate(admin, f"GRANT {privilege} ON medical_rag.security_audit_events TO login_test")
    try:
        with pytest.raises(StartupError):
            preflight(config)
    finally:
        mutate(admin, f"REVOKE {privilege} ON medical_rag.security_audit_events FROM login_test")
