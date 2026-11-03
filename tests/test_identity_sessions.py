"""Disposable PostgreSQL tests for experimental, disconnected sessions."""
import os
import secrets
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from medical_rag.access_policy import Role
from medical_rag.oidc_identity import VerifiedIdentity

ROOT = Path(__file__).resolve().parents[1]
TENANT = "00000000-0000-0000-0000-000000000101"
ACCOUNT = "00000000-0000-0000-0000-000000000201"
PATIENT = "00000000-0000-0000-0000-000000000301"
ISSUER = "https://identity.example.org/realms/hospital"


@pytest.fixture(scope="module")
def database():
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("set RUN_POSTGRES_INTEGRATION=1 for disposable PostgreSQL tests")
    if not shutil.which("docker"):
        pytest.skip("Docker is required")
    psycopg = pytest.importorskip("psycopg")
    password = secrets.token_urlsafe(24)
    result = subprocess.run([
        "docker", "run", "-d", "--rm", "-p", "127.0.0.1::5432",
        "--tmpfs", "/var/lib/postgresql:rw,size=256m", "-e", f"POSTGRES_PASSWORD={password}",
        "-e", "POSTGRES_INITDB_ARGS=--no-sync", "postgres:18.6-alpine",
    ], capture_output=True, text=True, check=True)
    container = result.stdout.strip()
    try:
        port = int(subprocess.run(["docker", "port", container, "5432/tcp"], check=True,
                                  capture_output=True, text=True).stdout.strip().rsplit(":", 1)[1])
        admin = {"host": "127.0.0.1", "port": port, "dbname": "postgres",
                 "user": "postgres", "password": password, "autocommit": True}
        for _ in range(100):
            try:
                connection = psycopg.connect(**admin)
                break
            except psycopg.OperationalError:
                time.sleep(0.1)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")
        with connection:
            for path in ("0001_patient_scope.sql", "0002_identity_sessions.sql", "0003_oidc_login.sql",
                         "0004_patient_access.sql", "0005_security_audit.sql", "0006_provider_logout.sql",
                         "0007_support_cases.sql"):
                connection.execute((ROOT / "database/migrations" / path).read_text(encoding="utf-8"))
            connection.execute(psycopg.sql.SQL("CREATE ROLE session_test LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS")
                               .format(psycopg.sql.Literal(password)))
            connection.execute("GRANT USAGE ON SCHEMA medical_rag TO session_test")
            connection.execute("GRANT SELECT ON medical_rag.tenants, medical_rag.identity_accounts, "
                               "medical_rag.identity_roles, medical_rag.identity_bindings TO session_test")
            connection.execute("GRANT SELECT, INSERT, UPDATE ON medical_rag.identity_sessions TO session_test")
            connection.execute("GRANT SELECT ON medical_rag.provider_logout_events TO session_test")
            connection.execute("GRANT INSERT (tenant_id,issuer,jti_hash,subject_hash,sid_hash,issued_at,expires_at) "
                               "ON medical_rag.provider_logout_events TO session_test")
        yield dict(admin, user="session_test", autocommit=False), admin
    finally:
        subprocess.run(["docker", "stop", "--time", "1", container], capture_output=True, check=False)


@pytest.fixture
def system(database):
    import psycopg

    from medical_rag.identity_sessions import SessionStore

    runtime, admin = database
    with psycopg.connect(**admin) as connection:
        connection.execute("ALTER TABLE medical_rag.provider_logout_events DISABLE TRIGGER provider_logout_no_truncate")
        connection.execute("ALTER TABLE medical_rag.security_audit_events DISABLE TRIGGER security_audit_no_truncate")
        connection.execute("TRUNCATE medical_rag.identity_sessions, medical_rag.identity_roles, "
                           "medical_rag.identity_bindings, medical_rag.identity_accounts, "
                           "medical_rag.patient_subjects, medical_rag.tenants CASCADE")
        connection.execute("ALTER TABLE medical_rag.provider_logout_events ENABLE TRIGGER provider_logout_no_truncate")
        connection.execute("ALTER TABLE medical_rag.security_audit_events ENABLE TRIGGER security_audit_no_truncate")
        connection.execute("INSERT INTO medical_rag.tenants(tenant_id) VALUES (%s)", (TENANT,))
        connection.execute("INSERT INTO medical_rag.patient_subjects(tenant_id, patient_id) VALUES (%s,%s)",
                           (TENANT, PATIENT))
        connection.execute("INSERT INTO medical_rag.identity_accounts(tenant_id, account_id, issuer, subject) "
                           "VALUES (%s,%s,%s,%s)", (TENANT, ACCOUNT, ISSUER, "subject-1"))
        connection.execute("INSERT INTO medical_rag.identity_roles(tenant_id, account_id, role) "
                           "VALUES (%s,%s,'patient')", (TENANT, ACCOUNT))
        connection.execute("INSERT INTO medical_rag.identity_bindings(tenant_id, account_id, patient_id, verified) "
                           "VALUES (%s,%s,%s,true)", (TENANT, ACCOUNT, PATIENT))
    store = SessionStore(lambda: psycopg.connect(**runtime), tenant_id=TENANT,
                         absolute_lifetime_seconds=3600, idle_lifetime_seconds=600)
    return store, admin


def issue(store):
    return store.issue(VerifiedIdentity(ISSUER, "subject-1", True))


@pytest.mark.integration
def test_authorized_transaction_refreshes_live_authority_on_same_connection(system):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, admin = system
    session = issue(store)
    assert hasattr(store, "authorized_transaction")
    with store.authorized_transaction(session.token) as authorized:
        assert authorized.principal.patient_id == PATIENT
        assert authorized.connection.execute("SHOW transaction_isolation").fetchone() == ("read committed",)
        assert authorized.connection.execute("SHOW lock_timeout").fetchone() == ("3s",)
        assert authorized.refresh() == authorized.principal
        mutate(admin, "UPDATE medical_rag.identity_bindings SET verified=false")
        with pytest.raises(AuthenticationDenied):
            authorized.refresh()


def mutate(admin, statement, params=()):
    import psycopg
    with psycopg.connect(**admin) as connection:
        connection.execute(statement, params)


@pytest.mark.integration
def test_random_tokens_are_not_stored_and_fixation_is_prevented(system):
    import psycopg
    store, admin = system
    one, two = issue(store), issue(store)
    assert one.token != two.token and one.csrf_token != two.csrf_token
    assert one.token not in repr(one) and one.csrf_token not in repr(one)
    assert store.authenticate(one.token).patient_id == PATIENT
    with psycopg.connect(**admin) as connection:
        stored = repr(connection.execute("SELECT * FROM medical_rag.identity_sessions").fetchall())
    assert one.token not in stored and one.csrf_token not in stored


@pytest.mark.integration
def test_logout_revokes_next_access_and_does_not_revoke_another_session(system):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, _ = system
    one, two = issue(store), issue(store)
    store.revoke(one.token, csrf_token=one.csrf_token)
    with pytest.raises(AuthenticationDenied):
        store.authenticate(one.token)
    assert store.authenticate(two.token).user_id == ACCOUNT


@pytest.mark.integration
def test_logout_requires_csrf_and_does_not_revoke_for_wrong_csrf(system):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, _ = system
    one, two = issue(store), issue(store)
    with pytest.raises(AuthenticationDenied):
        store.revoke(one.token, csrf_token=None)
    store.revoke(one.token, csrf_token=two.csrf_token)
    assert store.authenticate(one.token).user_id == ACCOUNT


@pytest.mark.integration
def test_transaction_already_in_progress_is_rejected(system, database):
    from contextlib import nullcontext

    import psycopg

    from medical_rag.identity_sessions import SessionStore, UnsafeConnection
    _, _ = system
    runtime, _ = database
    with psycopg.connect(**runtime) as connection:
        connection.execute("SELECT 1")
        store = SessionStore(lambda: nullcontext(connection), tenant_id=TENANT)
        with pytest.raises(UnsafeConnection):
            issue(store)


@pytest.mark.integration
def test_borrowed_connection_with_repeatable_read_default_observes_revocation(system, database):
    from contextlib import nullcontext

    import psycopg

    from medical_rag.identity_sessions import AuthenticationDenied, SessionStore
    store, admin = system
    session = issue(store)
    runtime, _ = database
    with psycopg.connect(**runtime) as connection:
        connection.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        connection.commit()
        reuse = SessionStore(lambda: nullcontext(connection), tenant_id=TENANT)
        assert reuse.authenticate(session.token).user_id == ACCOUNT
        mutate(admin, "UPDATE medical_rag.identity_accounts SET active=false")
        with pytest.raises(AuthenticationDenied):
            reuse.authenticate(session.token)


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["absolute", "idle", "account", "tenant"])
def test_expired_or_disabled_authority_denies_next_access(system, kind):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, admin = system
    session = issue(store)
    commands = {
        "absolute": "UPDATE medical_rag.identity_sessions SET expires_at = clock_timestamp() - interval '1 second'",
        "idle": "UPDATE medical_rag.identity_sessions SET last_seen_at = clock_timestamp() - interval '601 seconds'",
        "account": "UPDATE medical_rag.identity_accounts SET active = false",
        "tenant": "UPDATE medical_rag.tenants SET active = false",
    }
    mutate(admin, commands[kind])
    with pytest.raises(AuthenticationDenied):
        store.authenticate(session.token)


@pytest.mark.integration
@pytest.mark.parametrize("authority", ["account", "tenant"])
def test_disabling_then_reenabling_does_not_resurrect_old_sessions(system, authority):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, admin = system
    session = issue(store)
    table = "medical_rag.identity_accounts" if authority == "account" else "medical_rag.tenants"
    mutate(admin, f"UPDATE {table} SET active=false")
    mutate(admin, f"UPDATE {table} SET active=true")
    with pytest.raises(AuthenticationDenied):
        store.authenticate(session.token)


@pytest.mark.integration
def test_superuser_database_connection_is_rejected(system):
    import psycopg

    from medical_rag.identity_sessions import SessionStore, UnsafeConnection
    _, admin = system
    store = SessionStore(lambda: psycopg.connect(**admin), tenant_id=TENANT)
    with pytest.raises(UnsafeConnection):
        issue(store)


@pytest.mark.integration
def test_role_and_verified_binding_are_reloaded_on_next_access(system):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, admin = system
    session = issue(store)
    initial = store.authenticate(session.token)
    assert initial.roles == frozenset({Role.PATIENT}) and initial.patient_id == PATIENT
    mutate(admin, "DELETE FROM medical_rag.identity_roles")
    with pytest.raises(AuthenticationDenied):
        store.authenticate(session.token)


@pytest.mark.integration
def test_changed_patient_binding_invalidates_old_session(system):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, admin = system
    session = issue(store)
    mutate(admin, "UPDATE medical_rag.identity_bindings SET verified = false")
    with pytest.raises(AuthenticationDenied):
        store.authenticate(session.token)


@pytest.mark.integration
@pytest.mark.parametrize("field,value", [("issuer", "https://other.example"), ("subject", "other")])
def test_changed_upstream_identity_invalidates_old_session(system, field, value):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, admin = system
    session = issue(store)
    mutate(admin, f"UPDATE medical_rag.identity_accounts SET {field}=%s", (value,))
    with pytest.raises(AuthenticationDenied):
        store.authenticate(session.token)


@pytest.mark.integration
def test_csrf_is_required_for_mutations_and_bound_to_session(system):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, _ = system
    one, two = issue(store), issue(store)
    for token in (None, "invalid", two.csrf_token):
        with pytest.raises(AuthenticationDenied):
            store.authenticate(one.token, require_csrf=True, csrf_token=token)
    assert store.authenticate(one.token, require_csrf=True, csrf_token=one.csrf_token).user_id == ACCOUNT


@pytest.mark.integration
def test_session_cannot_be_used_in_another_tenant(system, database):
    import psycopg

    from medical_rag.identity_sessions import AuthenticationDenied, SessionStore
    store, admin = system
    session = issue(store)
    other_tenant = "00000000-0000-0000-0000-000000000102"
    mutate(admin, "INSERT INTO medical_rag.tenants (tenant_id) VALUES (%s)", (other_tenant,))
    runtime, _ = database
    other = SessionStore(lambda: psycopg.connect(**runtime), tenant_id=other_tenant)
    with pytest.raises(AuthenticationDenied):
        other.authenticate(session.token)


@pytest.mark.integration
def test_unmapped_identity_cannot_create_session(system):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, _ = system
    for identity in (VerifiedIdentity(ISSUER, "not-mapped", False),
                     VerifiedIdentity("https://evil.example", "subject-1", True)):
        with pytest.raises(AuthenticationDenied):
            store.issue(identity)


@pytest.mark.integration
@pytest.mark.parametrize("table", ["identity_accounts", "identity_roles", "identity_bindings"])
def test_runtime_cannot_change_account_or_bindings(system, table):
    import psycopg
    store, _ = system
    with pytest.raises(psycopg.errors.InsufficientPrivilege), store._connection_factory() as connection:
        connection.execute(f"DELETE FROM medical_rag.{table}")


@pytest.mark.integration
def test_role_revoked_then_regranted_does_not_resurrect_session(system):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, admin = system
    old = issue(store)
    mutate(admin, "DELETE FROM medical_rag.identity_roles")
    mutate(admin, "INSERT INTO medical_rag.identity_roles(tenant_id, account_id, role) "
                  "VALUES (%s,%s,'patient')", (TENANT, ACCOUNT))
    with pytest.raises(AuthenticationDenied):
        store.authenticate(old.token)
    assert store.authenticate(issue(store).token).roles == frozenset({Role.PATIENT})


@pytest.mark.integration
@pytest.mark.parametrize("method", ["switch_back", "delete_recreate"])
def test_binding_restoration_does_not_resurrect_session(system, method):
    from medical_rag.identity_sessions import AuthenticationDenied
    store, admin = system
    old = issue(store)
    if method == "switch_back":
        patient_b = "00000000-0000-0000-0000-000000000302"
        mutate(admin, "INSERT INTO medical_rag.patient_subjects(tenant_id,patient_id) VALUES (%s,%s)",
               (TENANT, patient_b))
        mutate(admin, "UPDATE medical_rag.identity_bindings SET patient_id=%s", (patient_b,))
        mutate(admin, "UPDATE medical_rag.identity_bindings SET patient_id=%s", (PATIENT,))
    else:
        mutate(admin, "DELETE FROM medical_rag.identity_bindings")
        mutate(admin, "INSERT INTO medical_rag.identity_bindings(tenant_id,account_id,patient_id,verified) "
                      "VALUES (%s,%s,%s,true)", (TENANT, ACCOUNT, PATIENT))
    with pytest.raises(AuthenticationDenied):
        store.authenticate(old.token)
    assert store.authenticate(issue(store).token).patient_id == PATIENT


@pytest.mark.integration
@pytest.mark.parametrize("sql,params", [
    ("INSERT INTO medical_rag.identity_accounts(tenant_id,account_id,issuer,subject) VALUES (%s,%s,%s,%s)",
     (TENANT, "00000000-0000-0000-0000-000000000202", ISSUER, "other")),
    ("UPDATE medical_rag.identity_accounts SET active=false", ()),
    ("INSERT INTO medical_rag.identity_roles(tenant_id,account_id,role) VALUES (%s,%s,'org_admin')", (TENANT, ACCOUNT)),
    ("UPDATE medical_rag.identity_roles SET role='org_admin'", ()),
    ("INSERT INTO medical_rag.identity_bindings(tenant_id,account_id,patient_id) VALUES (%s,%s,%s)",
     (TENANT, ACCOUNT, PATIENT)),
    ("UPDATE medical_rag.identity_bindings SET verified=false", ()),
    ("UPDATE medical_rag.tenants SET active=false", ()),
])
def test_runtime_cannot_insert_or_update_authority(system, sql, params):
    import psycopg
    store, _ = system
    with pytest.raises(psycopg.errors.InsufficientPrivilege), store._connection_factory() as connection:
        connection.execute(sql, params)


@pytest.mark.integration
def test_provisioner_authority_trigger_needs_only_account_version_column_update(system, database):
    import psycopg

    from medical_rag.identity_sessions import AuthenticationDenied
    store, admin = system
    role = "test_provision_" + secrets.token_hex(5)
    with psycopg.connect(**admin) as connection:
        connection.execute(psycopg.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            psycopg.sql.Identifier(role), psycopg.sql.Literal(admin["password"])))
        connection.execute(psycopg.sql.SQL("GRANT USAGE ON SCHEMA medical_rag TO {}").format(
            psycopg.sql.Identifier(role)))
        connection.execute(psycopg.sql.SQL(
            "GRANT SELECT (tenant_id,account_id,session_version), UPDATE (session_version) "
            "ON medical_rag.identity_accounts TO {}"
        ).format(psycopg.sql.Identifier(role)))
        connection.execute(psycopg.sql.SQL(
            "GRANT SELECT,INSERT,UPDATE,DELETE ON medical_rag.identity_roles, "
            "medical_rag.identity_bindings TO {}"
        ).format(psycopg.sql.Identifier(role)))
    provisioning = dict(database[0], user=role)
    old = issue(store)
    with psycopg.connect(**provisioning) as connection:
        connection.execute("INSERT INTO medical_rag.identity_roles VALUES (%s,%s,'support')", (TENANT, ACCOUNT))
    with pytest.raises(AuthenticationDenied):
        store.authenticate(old.token)
    new = issue(store)
    with psycopg.connect(**provisioning) as connection:
        connection.execute("UPDATE medical_rag.identity_bindings SET verified=false")
    with pytest.raises(AuthenticationDenied):
        store.authenticate(new.token)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(**provisioning) as connection:
        connection.execute("UPDATE medical_rag.identity_accounts SET active=false")


def wait_for_row_lock(admin, application_name):
    import psycopg
    deadline = time.monotonic() + 5
    with psycopg.connect(**admin) as observer:
        while time.monotonic() < deadline:
            row = observer.execute("SELECT wait_event_type FROM pg_catalog.pg_stat_activity "
                                   "WHERE application_name=%s", (application_name,)).fetchone()
            if row and row[0] == "Lock":
                return
            time.sleep(0.05)
    pytest.fail("authentication did not wait for the session row lock")


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["absolute", "idle", "revoked"])
def test_row_lock_wait_rechecks_expiry_or_revocation(system, database, kind):
    import psycopg

    from medical_rag.identity_sessions import AuthenticationDenied, SessionStore
    store, admin = system
    session = issue(store)
    runtime, _ = database
    app_name = "session-lock-" + secrets.token_hex(8)
    marked_runtime = dict(runtime, application_name=app_name)
    waiting_store = SessionStore(lambda: psycopg.connect(**marked_runtime), tenant_id=TENANT,
                                 absolute_lifetime_seconds=3600,
                                 idle_lifetime_seconds=2 if kind == "idle" else 600)
    if kind == "absolute":
        mutate(admin, "UPDATE medical_rag.identity_sessions SET expires_at = clock_timestamp() + interval '2 seconds'")
    locker_kwargs = dict(admin, autocommit=False)
    with psycopg.connect(**locker_kwargs) as locker:
        if kind == "revoked":
            locker.execute("UPDATE medical_rag.identity_sessions SET revoked=true")
        else:
            locker.execute("SELECT token_hash FROM medical_rag.identity_sessions FOR UPDATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(waiting_store.authenticate, session.token)
            try:
                wait_for_row_lock(admin, app_name)
                if kind in {"absolute", "idle"}:
                    time.sleep(2.2)
            finally:
                locker.commit()
            with pytest.raises(AuthenticationDenied):
                pending.result(timeout=5)
    if kind in {"absolute", "idle"}:
        with psycopg.connect(**admin) as observer:
            absolute_expired, idle_expired = observer.execute(
                "SELECT expires_at <= clock_timestamp(), "
                "last_seen_at <= clock_timestamp() - (%s * interval '1 second') "
                "FROM medical_rag.identity_sessions", (2 if kind == "idle" else 600,),
            ).fetchone()
        assert (absolute_expired, idle_expired) == (kind == "absolute", kind == "idle")


@pytest.mark.integration
def test_session_issued_concurrently_with_disable_is_not_usable_after_commit(system):
    import psycopg

    from medical_rag.identity_sessions import AuthenticationDenied
    store, admin = system
    with psycopg.connect(**dict(admin, autocommit=False)) as locker:
        locker.execute("UPDATE medical_rag.identity_accounts SET active=false")
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(issue, store)
            try:
                session = pending.result(timeout=5)
            finally:
                locker.commit()
    with pytest.raises(AuthenticationDenied):
        store.authenticate(session.token)


@pytest.mark.integration
def test_account_disabled_while_authentication_waits_for_session_lock_is_denied(system, database):
    import psycopg

    from medical_rag.identity_sessions import AuthenticationDenied, SessionStore
    store, admin = system
    session = issue(store)
    app_name = "session-lock-" + secrets.token_hex(8)
    runtime, _ = database
    waiting_store = SessionStore(lambda: psycopg.connect(**dict(runtime, application_name=app_name)),
                                 tenant_id=TENANT)
    with psycopg.connect(**dict(admin, autocommit=False)) as locker:
        locker.execute("SELECT token_hash FROM medical_rag.identity_sessions FOR UPDATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(waiting_store.authenticate, session.token)
            try:
                wait_for_row_lock(admin, app_name)
                mutate(admin, "UPDATE medical_rag.identity_accounts SET active=false")
            finally:
                locker.commit()
            with pytest.raises(AuthenticationDenied):
                pending.result(timeout=5)


@pytest.mark.integration
def test_live_session_principal_enforces_patient_ownership_and_staff_mfa(system):
    from medical_rag.access_policy import Action, Resource, is_allowed
    from medical_rag.identity_sessions import AuthenticationDenied

    store, admin = system
    patient_session = issue(store)
    patient = store.authenticate(patient_session.token)
    own = Resource(TENANT, PATIENT)
    other = Resource(TENANT, "00000000-0000-0000-0000-000000000302")
    assert is_allowed(patient, Action.PATIENT_READ, own)
    assert not is_allowed(patient, Action.PATIENT_READ, other)
    assert not is_allowed(patient, Action.MEMBER_MANAGE, Resource(TENANT))

    mutate(admin, "DELETE FROM medical_rag.identity_roles WHERE role='patient'")
    mutate(admin, "INSERT INTO medical_rag.identity_roles VALUES (%s,%s,'org_admin')", (TENANT, ACCOUNT))
    with pytest.raises(AuthenticationDenied):
        store.authenticate(patient_session.token)
    no_mfa = store.issue(VerifiedIdentity(ISSUER, "subject-1", False))
    assert not is_allowed(store.authenticate(no_mfa.token), Action.MEMBER_MANAGE, Resource(TENANT))
    admin_session = store.issue(VerifiedIdentity(ISSUER, "subject-1", True))
    staff = store.authenticate(admin_session.token)
    assert is_allowed(staff, Action.MEMBER_MANAGE, Resource(TENANT))
    assert not is_allowed(staff, Action.PATIENT_READ, own)
