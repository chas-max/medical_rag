"""Integration tests for the experimental PostgreSQL patient repository.

The disposable database listens only on loopback and contains fictional data.
"""

import importlib
import importlib.util
import os
import secrets
import shutil
import subprocess
import time
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from medical_rag.access_policy import Action, GuardianGrant, Principal, Resource, Role
from medical_rag.consent_policy import (
    ConsentPurpose,
    ConsentRecord,
    PatientIdentity,
    SubjectAge,
)

ROOT = Path(__file__).resolve().parents[1]
TENANT_A = "00000000-0000-0000-0000-000000000101"
TENANT_B = "00000000-0000-0000-0000-000000000102"
PATIENT_A = "00000000-0000-0000-0000-000000000201"
PATIENT_B = "00000000-0000-0000-0000-000000000202"
PATIENT_C = "00000000-0000-0000-0000-000000000203"
CONV_A = "00000000-0000-0000-0000-000000000301"
CONV_B = "00000000-0000-0000-0000-000000000302"
CONV_C = "00000000-0000-0000-0000-000000000303"
MISSING = "00000000-0000-0000-0000-000000000399"
TURN_ID = "00000000-0000-0000-0000-000000000401"
VERSION = "health-v1"


def evidence(tenant=TENANT_A, patient=PATIENT_A, account="account-1"):
    now = datetime.now(UTC)
    subject = PatientIdentity(tenant, patient, account, SubjectAge.ADULT, True)
    consent = ConsentRecord(tenant, patient, ConsentPurpose.HEALTH_INFORMATION, VERSION, account,
                            True, False, now - timedelta(days=1), now + timedelta(days=1),
                            now - timedelta(days=1))
    return {"subject": subject, "consent": consent}


def minor_evidence():
    proof = evidence()
    now = datetime.now(UTC)
    proof["subject"] = replace(proof["subject"], age=SubjectAge.UNDER_14)
    proof["consent"] = replace(proof["consent"], grantor_user_id="signer")
    proof["consent_guardian_grant"] = GuardianGrant(
        TENANT_A, "signer", PATIENT_A, frozenset({Action.PATIENT_READ, Action.PATIENT_WRITE}),
        True, True, now + timedelta(days=1), valid_from=now - timedelta(days=2), revoked=False,
    )
    return proof


def repository_for(module, factory):
    return module.PatientConversationRepository(factory, required_health_consent_version=VERSION)


def perform(repository, operation, *, principal=None, **proof):
    principal = principal or actor()
    resource = Resource(TENANT_A, PATIENT_A)
    if operation == "get":
        return repository.get_conversation(principal, resource, CONV_A, **proof)
    if operation == "list":
        return repository.list_conversations(principal, resource, **proof)
    if operation == "create":
        return repository.create_conversation(principal, resource, MISSING, "new", **proof)
    if operation == "append":
        return repository.append_turn(principal, resource, CONV_A, TURN_ID, "fictional", **proof)
    return repository.list_turns(principal, resource, CONV_A, **proof)


def test_repository_module_exists():
    assert importlib.util.find_spec("medical_rag.patient_repository") is not None


def test_repository_has_patient_conversation_listing():
    module = importlib.import_module("medical_rag.patient_repository")
    assert callable(getattr(module.PatientConversationRepository, "list_conversations", None))


def test_missing_institution_configuration_and_consent_denies_patient_read():
    module = importlib.import_module("medical_rag.patient_repository")
    connection = _FakeConnection()
    repository = module.PatientConversationRepository(lambda: nullcontext(connection))
    with pytest.raises(module.PatientResourceNotFound):
        repository.get_conversation(actor(), Resource(TENANT_A, PATIENT_A), CONV_A)
    assert connection.queries == []


@pytest.mark.parametrize("version", [None, "", " "])
def test_missing_trusted_version_denied_even_with_consent(version):
    module = importlib.import_module("medical_rag.patient_repository")
    connection = _FakeConnection()
    repository = module.PatientConversationRepository(lambda: nullcontext(connection),
                                                     required_health_consent_version=version)
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "get", **evidence())
    assert connection.queries == []


@pytest.mark.parametrize("operation", ["get", "list", "create", "append", "turns"])
@pytest.mark.parametrize("invalid", ["missing_subject", "missing_consent", "cross_subject_tenant",
                                    "cross_subject_patient", "cross_consent_tenant", "cross_consent_patient",
                                    "wrong_purpose", "wrong_version", "revoked", "expired", "unverified",
                                    "minor_without_guardian"])
def test_invalid_health_consent_denied_for_every_operation(operation, invalid):
    module = importlib.import_module("medical_rag.patient_repository")
    connection = _FakeConnection()
    repository = repository_for(module, lambda: nullcontext(connection))
    proof = evidence()
    if invalid == "missing_subject":
        proof["subject"] = None
    elif invalid == "missing_consent":
        proof["consent"] = None
    elif invalid == "cross_subject_tenant":
        proof = evidence(TENANT_B, PATIENT_A)
    elif invalid == "cross_subject_patient":
        proof = evidence(TENANT_A, PATIENT_B)
    elif invalid == "cross_consent_tenant":
        proof["consent"] = replace(proof["consent"], tenant_id=TENANT_B)
    elif invalid == "cross_consent_patient":
        proof["consent"] = replace(proof["consent"], patient_id=PATIENT_B)
    elif invalid == "wrong_purpose":
        proof["consent"] = replace(proof["consent"], purpose=ConsentPurpose.EXTERNAL_MODEL)
    elif invalid == "wrong_version":
        proof["consent"] = replace(proof["consent"], version="old")
    elif invalid == "revoked":
        proof["consent"] = replace(proof["consent"], revoked=True)
    elif invalid == "expired":
        proof["consent"] = replace(proof["consent"], expires_at=datetime.now(UTC) - timedelta(seconds=1))
    elif invalid == "unverified":
        proof["subject"] = replace(proof["subject"], identity_verified=False)
    else:
        proof["subject"] = replace(proof["subject"], age=SubjectAge.UNDER_14)
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, operation, **proof)
    assert connection.queries == []


def test_denied_principal_never_borrows_a_database_connection():
    module = importlib.import_module("medical_rag.patient_repository")

    def forbidden_connection():
        raise AssertionError("denied principal must not borrow a connection")

    repository = repository_for(module, forbidden_connection)
    principal = Principal(user_id="support", tenant_id=TENANT_A, roles=frozenset({Role.SUPPORT}),
                          active=True, tenant_active=True)
    with pytest.raises(module.PatientResourceNotFound):
        repository.get_conversation(principal, Resource(TENANT_A, PATIENT_A), CONV_A, **evidence())


def test_revoked_guardian_grant_never_borrows_a_connection():
    module = importlib.import_module("medical_rag.patient_repository")

    def forbidden_connection():
        raise AssertionError("revoked guardian must not borrow a connection")

    repository = repository_for(module, forbidden_connection)
    principal = Principal(user_id="guardian", tenant_id=TENANT_A, roles=frozenset({Role.GUARDIAN}),
                          active=True, tenant_active=True)
    grant = GuardianGrant(TENANT_A, "guardian", PATIENT_A, frozenset({Action.PATIENT_READ}),
                          True, True, datetime.now(UTC) + timedelta(minutes=1),
                          valid_from=datetime.now(UTC) - timedelta(minutes=1), revoked=True)
    with pytest.raises(module.PatientResourceNotFound):
        repository.get_conversation(principal, Resource(TENANT_A, PATIENT_A), CONV_A,
                                    guardian_grant=grant, **minor_evidence())


def test_guardian_expiring_while_waiting_for_connection_is_denied(monkeypatch):
    module = importlib.import_module("medical_rag.patient_repository")
    real_is_allowed = module.is_allowed
    start = datetime.now(UTC)
    expiry = start + timedelta(minutes=1)
    current_time = [start]
    connection = _FakeConnection()

    def evaluate_at_controlled_time(*args, **kwargs):
        return real_is_allowed(*args, **kwargs, now=current_time[0])

    @contextmanager
    def delayed_connection():
        current_time[0] = expiry
        yield connection

    monkeypatch.setattr(module, "is_allowed", evaluate_at_controlled_time)
    repository = repository_for(module, delayed_connection)
    guardian = Principal(user_id="guardian", tenant_id=TENANT_A, roles=frozenset({Role.GUARDIAN}),
                         active=True, tenant_active=True)
    grant = GuardianGrant(TENANT_A, "guardian", PATIENT_A, frozenset({Action.PATIENT_READ}),
                          True, True, expiry, valid_from=start - timedelta(minutes=1), revoked=False)
    with pytest.raises(module.PatientResourceNotFound):
        repository.get_conversation(guardian, Resource(TENANT_A, PATIENT_A), CONV_A,
                                    guardian_grant=grant, **minor_evidence())
    assert connection.queries == []


def actor(patient_id=PATIENT_A, tenant_id=TENANT_A):
    return Principal(user_id="account-1", tenant_id=tenant_id, patient_id=patient_id,
                     roles=frozenset({Role.PATIENT}), active=True, tenant_active=True)


class _FakeConnection:
    """Exercise repository transaction discipline without a PostgreSQL server."""

    def __init__(self, *, role_safe=True):
        from psycopg.pq import TransactionStatus

        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.role_safe = role_safe
        self.queries = []
        self.committed = False
        self.rolled_back = False

    @contextmanager
    def transaction(self):
        try:
            yield
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "runtime_role_basic_check" in query:
            row = (self.role_safe,)
        elif "FROM medical_rag.conversations" in query:
            row = (UUID(CONV_A), "fictional")
        else:
            row = None
        return SimpleNamespace(fetchone=lambda: row, fetchall=list)


def test_minor_requires_distinct_valid_operator_and_consent_signer_grants():
    module = importlib.import_module("medical_rag.patient_repository")
    now = datetime.now(UTC)
    proof = evidence()
    proof["subject"] = replace(proof["subject"], age=SubjectAge.UNDER_14)
    proof["consent"] = replace(proof["consent"], grantor_user_id="signer")
    actor_grant = GuardianGrant(TENANT_A, "operator", PATIENT_A,
                                frozenset({Action.PATIENT_READ, Action.PATIENT_WRITE}),
                                True, True, now + timedelta(days=1),
                                valid_from=now - timedelta(days=2), revoked=False)
    signer_grant = replace(actor_grant, guardian_id="signer", actions=frozenset({Action.PATIENT_READ}))
    principal = Principal(user_id="operator", tenant_id=TENANT_A, roles=frozenset({Role.GUARDIAN}),
                          active=True, tenant_active=True)
    connection = _FakeConnection()
    repository = repository_for(module, lambda: nullcontext(connection))
    assert perform(repository, "get", principal=principal, guardian_grant=actor_grant,
                   consent_guardian_grant=signer_grant, **proof).title == "fictional"
    connection.queries.clear()
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "get", principal=principal, guardian_grant=signer_grant,
                consent_guardian_grant=signer_grant, **proof)
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "get", principal=principal, guardian_grant=actor_grant,
                consent_guardian_grant=actor_grant, **proof)
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "create", principal=principal, guardian_grant=actor_grant,
                consent_guardian_grant=signer_grant, **proof)
    assert connection.queries == []


def test_guardian_can_act_for_verified_under14_without_account():
    module = importlib.import_module("medical_rag.patient_repository")
    now = datetime.now(UTC)
    proof = evidence()
    proof["subject"] = replace(proof["subject"], age=SubjectAge.UNDER_14, account_id=None)
    proof["consent"] = replace(proof["consent"], grantor_user_id="signer")
    actor_grant = GuardianGrant(TENANT_A, "operator", PATIENT_A, frozenset({Action.PATIENT_READ}),
                                True, True, now + timedelta(days=1),
                                valid_from=now - timedelta(days=2), revoked=False)
    signer_grant = replace(actor_grant, guardian_id="signer")
    principal = Principal(user_id="operator", tenant_id=TENANT_A, roles=frozenset({Role.GUARDIAN}),
                          active=True, tenant_active=True)
    connection = _FakeConnection()
    repository = repository_for(module, lambda: nullcontext(connection))
    assert perform(repository, "get", principal=principal, guardian_grant=actor_grant,
                   consent_guardian_grant=signer_grant, **proof).title == "fictional"


@pytest.mark.parametrize("variant", ["missing", "revoked", "wrong_patient", "wrong_tenant", "expired", "no_action"])
def test_minor_consent_guardian_grant_must_be_current_and_match_subject(variant):
    module = importlib.import_module("medical_rag.patient_repository")
    now = datetime.now(UTC)
    proof = evidence()
    proof["subject"] = replace(proof["subject"], age=SubjectAge.UNDER_14)
    proof["consent"] = replace(proof["consent"], grantor_user_id="signer")
    actor_grant = GuardianGrant(TENANT_A, "operator", PATIENT_A, frozenset({Action.PATIENT_READ}),
                                True, True, now + timedelta(days=1),
                                valid_from=now - timedelta(days=2), revoked=False)
    signer_grant = replace(actor_grant, guardian_id="signer")
    if variant == "missing":
        signer_grant = None
    elif variant == "revoked":
        signer_grant = replace(signer_grant, revoked=True)
    elif variant == "wrong_patient":
        signer_grant = replace(signer_grant, patient_id=PATIENT_B)
    elif variant == "wrong_tenant":
        signer_grant = replace(signer_grant, tenant_id=TENANT_B)
    elif variant == "expired":
        signer_grant = replace(signer_grant, expires_at=now - timedelta(seconds=1))
    else:
        signer_grant = replace(signer_grant, actions=frozenset())
    principal = Principal(user_id="operator", tenant_id=TENANT_A, roles=frozenset({Role.GUARDIAN}),
                          active=True, tenant_active=True)
    connection = _FakeConnection()
    repository = repository_for(module, lambda: nullcontext(connection))
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "get", principal=principal, guardian_grant=actor_grant,
                consent_guardian_grant=signer_grant, **proof)
    assert connection.queries == []


def test_patient_binding_must_match_the_verified_subject_account():
    module = importlib.import_module("medical_rag.patient_repository")
    connection = _FakeConnection()
    repository = repository_for(module, lambda: nullcontext(connection))
    mismatched = Principal(user_id="attacker", tenant_id=TENANT_A, patient_id=PATIENT_A,
                           roles=frozenset({Role.PATIENT}), active=True, tenant_active=True)
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "get", principal=mismatched, **evidence())
    assert connection.queries == []


def test_guardian_cannot_access_an_adult_even_with_current_grant_and_consent():
    module = importlib.import_module("medical_rag.patient_repository")
    connection = _FakeConnection()
    repository = repository_for(module, lambda: nullcontext(connection))
    principal = Principal(user_id="guardian", tenant_id=TENANT_A, roles=frozenset({Role.GUARDIAN}),
                          active=True, tenant_active=True)
    now = datetime.now(UTC)
    grant = GuardianGrant(TENANT_A, "guardian", PATIENT_A, frozenset({Action.PATIENT_READ}),
                          True, True, now + timedelta(days=1), valid_from=now - timedelta(days=1), revoked=False)
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "get", principal=principal, guardian_grant=grant, **evidence())
    assert connection.queries == []


def test_adult_with_mixed_patient_guardian_roles_can_access_self():
    module = importlib.import_module("medical_rag.patient_repository")
    connection = _FakeConnection()
    repository = repository_for(module, lambda: nullcontext(connection))
    principal = Principal(user_id="account-1", tenant_id=TENANT_A, patient_id=PATIENT_A,
                          roles=frozenset({Role.PATIENT, Role.GUARDIAN}), active=True, tenant_active=True)
    assert perform(repository, "get", principal=principal, **evidence()).title == "fictional"


@pytest.mark.parametrize("roles", [None, [Role.PATIENT], "patient", frozenset({"patient"})])
def test_invalid_principal_roles_fail_closed_without_db_access(roles):
    module = importlib.import_module("medical_rag.patient_repository")
    connection = _FakeConnection()
    repository = repository_for(module, lambda: nullcontext(connection))
    principal = replace(actor(), roles=roles)
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "get", principal=principal, **evidence())
    assert connection.queries == []


def test_mixed_patient_guardian_roles_require_guardian_authorization_on_account_mismatch():
    module = importlib.import_module("medical_rag.patient_repository")
    connection = _FakeConnection()
    repository = repository_for(module, lambda: nullcontext(connection))
    principal = Principal(user_id="guardian", tenant_id=TENANT_A, patient_id=PATIENT_A,
                          roles=frozenset({Role.PATIENT, Role.GUARDIAN}), active=True, tenant_active=True)
    now = datetime.now(UTC)
    grant = GuardianGrant(TENANT_A, "guardian", PATIENT_A, frozenset({Action.PATIENT_READ}),
                          True, True, now + timedelta(days=1), valid_from=now - timedelta(days=1), revoked=False)
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "get", principal=principal, **minor_evidence())
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "get", principal=principal, guardian_grant=replace(grant, revoked=True), **minor_evidence())
    assert connection.queries == []
    assert perform(repository, "get", principal=principal, guardian_grant=grant, **minor_evidence()).title == "fictional"


@pytest.mark.parametrize("stage", ["pool", "role_check"])
def test_consent_expiring_during_wait_denied_before_patient_sql(monkeypatch, stage):
    module = importlib.import_module("medical_rag.patient_repository")
    real_check = module.has_current_consent
    start = datetime.now(UTC)
    expiry = start + timedelta(minutes=1)
    current_time = [start]

    def evaluate_at_controlled_time(*args, **kwargs):
        return real_check(*args, **kwargs, now=current_time[0])

    class DelayedCheckConnection(_FakeConnection):
        def execute(self, query, params=()):
            result = super().execute(query, params)
            if stage == "role_check" and "runtime_role_basic_check" in query:
                current_time[0] = expiry
            return result

    connection = DelayedCheckConnection()

    @contextmanager
    def delayed_connection():
        if stage == "pool":
            current_time[0] = expiry
        yield connection

    proof = evidence()
    proof["consent"] = replace(proof["consent"], expires_at=expiry)
    monkeypatch.setattr(module, "has_current_consent", evaluate_at_controlled_time)
    repository = repository_for(module, delayed_connection)
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "get", **proof)
    assert len(connection.queries) == (0 if stage == "pool" else 2)
    assert connection.rolled_back is (stage == "role_check")


@pytest.mark.parametrize("expiring", ["consent", "guardian"])
def test_authorization_expiring_during_scope_setup_denied_before_patient_sql(monkeypatch, expiring):
    module = importlib.import_module("medical_rag.patient_repository")
    real_consent_check = module.has_current_consent
    real_access_check = module.is_allowed
    start = datetime.now(UTC)
    expiry = start + timedelta(minutes=1)
    current_time = [start]

    def consent_at_controlled_time(*args, **kwargs):
        return real_consent_check(*args, **kwargs, now=current_time[0])

    def access_at_controlled_time(*args, **kwargs):
        return real_access_check(*args, **kwargs, now=current_time[0])

    class DelayedScopeConnection(_FakeConnection):
        def execute(self, query, params=()):
            result = super().execute(query, params)
            if "pg_catalog.set_config" in query:
                current_time[0] = expiry
            return result

    connection = DelayedScopeConnection()
    monkeypatch.setattr(module, "has_current_consent", consent_at_controlled_time)
    monkeypatch.setattr(module, "is_allowed", access_at_controlled_time)
    repository = repository_for(module, lambda: nullcontext(connection))
    proof = evidence()
    principal = actor()
    if expiring == "consent":
        proof["consent"] = replace(proof["consent"], expires_at=expiry)
    else:
        proof = minor_evidence()
        principal = Principal(user_id="guardian", tenant_id=TENANT_A, roles=frozenset({Role.GUARDIAN}),
                              active=True, tenant_active=True)
        proof["guardian_grant"] = GuardianGrant(
            TENANT_A, "guardian", PATIENT_A, frozenset({Action.PATIENT_READ}), True, True, expiry,
            valid_from=start - timedelta(minutes=1), revoked=False,
        )
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, "get", principal=principal, **proof)
    assert connection.rolled_back
    assert len(connection.queries) == 3


@pytest.mark.parametrize("operation", ["append", "turns"])
def test_consent_expiring_after_parent_lookup_denies_next_sql_and_rolls_back(monkeypatch, operation):
    module = importlib.import_module("medical_rag.patient_repository")
    real_check = module.has_current_consent
    start = datetime.now(UTC)
    expiry = start + timedelta(minutes=1)
    current_time = [start]

    def consent_at_controlled_time(*args, **kwargs):
        return real_check(*args, **kwargs, now=current_time[0])

    class DelayedParentConnection(_FakeConnection):
        def execute(self, query, params=()):
            result = super().execute(query, params)
            if "SELECT 1 FROM medical_rag.conversations" in query:
                current_time[0] = expiry
            return result

    connection = DelayedParentConnection()
    monkeypatch.setattr(module, "has_current_consent", consent_at_controlled_time)
    repository = repository_for(module, lambda: nullcontext(connection))
    proof = evidence()
    proof["consent"] = replace(proof["consent"], expires_at=expiry)
    with pytest.raises(module.PatientResourceNotFound):
        perform(repository, operation, **proof)
    assert connection.rolled_back
    assert len(connection.queries) == 4


def test_guardian_expiring_during_role_check_is_denied_before_patient_sql(monkeypatch):
    module = importlib.import_module("medical_rag.patient_repository")
    real_is_allowed = module.is_allowed
    start = datetime.now(UTC)
    expiry = start + timedelta(minutes=1)
    current_time = [start]

    def evaluate_at_controlled_time(*args, **kwargs):
        return real_is_allowed(*args, **kwargs, now=current_time[0])

    class DelayedCheckConnection(_FakeConnection):
        def execute(self, query, params=()):
            result = super().execute(query, params)
            if "runtime_role_basic_check" in query:
                current_time[0] = expiry
            return result

    connection = DelayedCheckConnection()
    monkeypatch.setattr(module, "is_allowed", evaluate_at_controlled_time)
    repository = repository_for(module, lambda: nullcontext(connection))
    guardian = Principal(user_id="guardian", tenant_id=TENANT_A, roles=frozenset({Role.GUARDIAN}),
                         active=True, tenant_active=True)
    grant = GuardianGrant(TENANT_A, "guardian", PATIENT_A, frozenset({Action.PATIENT_READ}),
                          True, True, expiry, valid_from=start - timedelta(minutes=1), revoked=False)
    with pytest.raises(module.PatientResourceNotFound):
        repository.get_conversation(guardian, Resource(TENANT_A, PATIENT_A), CONV_A,
                                    guardian_grant=grant, **minor_evidence())
    assert connection.rolled_back
    assert len(connection.queries) == 2


def test_patient_read_uses_transaction_and_parameterized_scope():
    module = importlib.import_module("medical_rag.patient_repository")
    connection = _FakeConnection()
    repository = repository_for(module, lambda: nullcontext(connection))
    assert repository.get_conversation(actor(), Resource(TENANT_A, PATIENT_A), CONV_A, **evidence()).title == "fictional"
    assert connection.committed and not connection.rolled_back
    assert connection.queries[0][0] == "SET LOCAL search_path TO pg_catalog"
    scope_query, scope_params = connection.queries[2]
    assert "pg_catalog.set_config('app.patient_scope'" in scope_query
    assert "pg_catalog.pg_current_xact_id()" in scope_query
    assert scope_params == (UUID(TENANT_A), UUID(PATIENT_A))
    select_query, select_params = connection.queries[3]
    assert "tenant_id = %s AND patient_id = %s AND conversation_id = %s" in select_query
    assert select_params == (UUID(TENANT_A), UUID(PATIENT_A), UUID(CONV_A))
    assert PATIENT_A not in scope_query and CONV_A not in select_query


def test_unsafe_database_role_is_rolled_back_before_querying_patient_data():
    module = importlib.import_module("medical_rag.patient_repository")
    connection = _FakeConnection(role_safe=False)
    repository = repository_for(module, lambda: nullcontext(connection))
    with pytest.raises(module.UnsafeConnection):
        repository.get_conversation(actor(), Resource(TENANT_A, PATIENT_A), CONV_A, **evidence())
    assert connection.rolled_back and not connection.committed
    assert len(connection.queries) == 2


@pytest.fixture(scope="module")
def database():
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("set RUN_POSTGRES_INTEGRATION=1 for disposable PostgreSQL tests")
    if not shutil.which("docker"):
        pytest.skip("Docker is required")
    psycopg = pytest.importorskip("psycopg")
    password = secrets.token_urlsafe(24)
    start = subprocess.run([
        "docker", "run", "-d", "--rm", "-p", "127.0.0.1::5432",
        "--tmpfs", "/var/lib/postgresql:rw,size=256m", "-e", f"POSTGRES_PASSWORD={password}",
        "-e", "POSTGRES_INITDB_ARGS=--no-sync", "postgres:18.6-alpine",
    ], capture_output=True, text=True, check=True)
    container = start.stdout.strip()
    try:
        output = subprocess.run(["docker", "port", container, "5432/tcp"], check=True,
                                capture_output=True, text=True).stdout.strip()
        port = int(output.rsplit(":", 1)[1])
        admin_kwargs = {"host": "127.0.0.1", "port": port, "dbname": "postgres",
                        "user": "postgres", "password": password, "autocommit": True}
        for _ in range(100):
            try:
                admin = psycopg.connect(**admin_kwargs)
                break
            except psycopg.OperationalError:
                time.sleep(0.1)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")
        with admin:
            admin.execute((ROOT / "database/migrations/0001_patient_scope.sql").read_text(encoding="utf-8"))
            admin.execute(psycopg.sql.SQL("CREATE ROLE repository_test LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS")
                          .format(psycopg.sql.Literal(password)))
            admin.execute("GRANT USAGE ON SCHEMA medical_rag TO repository_test")
            admin.execute("GRANT SELECT ON medical_rag.patient_subjects TO repository_test")
            admin.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON medical_rag.conversations, "
                          "medical_rag.conversation_turns TO repository_test")
            admin.execute("INSERT INTO medical_rag.tenants (tenant_id) VALUES (%s), (%s)", (TENANT_A, TENANT_B))
            admin.execute("INSERT INTO medical_rag.patient_subjects (tenant_id, patient_id) "
                          "VALUES (%s,%s), (%s,%s), (%s,%s)",
                          (TENANT_A, PATIENT_A, TENANT_B, PATIENT_B, TENANT_A, PATIENT_C))
            admin.execute("INSERT INTO medical_rag.conversations (tenant_id, patient_id, conversation_id, title) "
                          "VALUES (%s,%s,%s,'a'), (%s,%s,%s,'b'), (%s,%s,%s,'c')",
                          (TENANT_A, PATIENT_A, CONV_A, TENANT_B, PATIENT_B, CONV_B,
                           TENANT_A, PATIENT_C, CONV_C))
        kwargs = dict(admin_kwargs, user="repository_test")
        yield kwargs, admin_kwargs
    finally:
        subprocess.run(["docker", "stop", "--time", "1", container], capture_output=True, check=False)


@pytest.fixture
def session(database):
    import psycopg

    with psycopg.connect(**database[0]) as connection:
        module = importlib.import_module("medical_rag.patient_repository")
        yield connection, repository_for(module, lambda: nullcontext(connection)), module


@pytest.mark.integration
def test_only_authorized_own_conversation_is_visible(session):
    _, repository, module = session
    resource = Resource(TENANT_A, PATIENT_A)
    assert repository.get_conversation(actor(), resource, CONV_A, **evidence()).title == "a"
    for conversation_id in (CONV_B, CONV_C, MISSING, "not-a-uuid"):
        with pytest.raises(module.PatientResourceNotFound):
            repository.get_conversation(actor(), resource, conversation_id, **evidence())
    with pytest.raises(module.PatientResourceNotFound):
        repository.get_conversation(actor(), Resource(TENANT_B, PATIENT_B), CONV_B, **evidence(TENANT_B, PATIENT_B))
    with pytest.raises(module.PatientResourceNotFound):
        repository.get_conversation(actor(), Resource(TENANT_A, PATIENT_C), CONV_C, **evidence(TENANT_A, PATIENT_C))


@pytest.mark.integration
def test_list_conversations_only_returns_own_patient(session):
    _, repository, _ = session
    conversations = repository.list_conversations(actor(), Resource(TENANT_A, PATIENT_A), **evidence())
    assert [conversation.title for conversation in conversations] == ["a"]


@pytest.mark.integration
def test_staff_cannot_read_even_when_they_know_ids(session):
    _, repository, module = session
    for role in (Role.ORG_ADMIN, Role.AUDITOR, Role.SUPPORT, Role.PLATFORM_OPERATOR):
        staff = Principal(user_id="staff", tenant_id=TENANT_A, roles=frozenset({role}),
                          active=True, tenant_active=True)
        with pytest.raises(module.PatientResourceNotFound):
            repository.get_conversation(staff, Resource(TENANT_A, PATIENT_A), CONV_A, **evidence())


@pytest.mark.integration
def test_guardian_scope_and_revocation(session):
    _, repository, module = session
    guardian = Principal(user_id="guardian", tenant_id=TENANT_A, roles=frozenset({Role.GUARDIAN}),
                         active=True, tenant_active=True)
    resource = Resource(TENANT_A, PATIENT_A)
    grant = GuardianGrant(TENANT_A, "guardian", PATIENT_A, frozenset({Action.PATIENT_READ}),
                          True, True, datetime.now(UTC) + timedelta(minutes=1),
                          valid_from=datetime.now(UTC) - timedelta(minutes=1), revoked=False)
    assert repository.get_conversation(guardian, resource, CONV_A, guardian_grant=grant, **minor_evidence()).title == "a"
    with pytest.raises(module.PatientResourceNotFound):
        repository.get_conversation(guardian, resource, CONV_A, **minor_evidence())
    with pytest.raises(module.PatientResourceNotFound):
        repository.create_conversation(guardian, resource, MISSING, "new", guardian_grant=grant, **minor_evidence())


@pytest.mark.integration
def test_write_and_read_turn_within_scope(session):
    _, repository, module = session
    resource = Resource(TENANT_A, PATIENT_A)
    repository.create_conversation(actor(), resource, MISSING, "temporary", **evidence())
    repository.append_turn(actor(), resource, MISSING, TURN_ID, "fictional", **evidence())
    assert repository.get_conversation(actor(), resource, MISSING, **evidence()).title == "temporary"
    assert repository.list_turns(actor(), resource, MISSING, **evidence())[0].content == "fictional"
    with pytest.raises(module.PatientResourceNotFound):
        repository.list_turns(actor(), resource, CONV_B, **evidence())


@pytest.mark.integration
def test_pool_connection_is_not_authorized_after_transaction(session):
    connection, repository, _ = session
    assert repository.get_conversation(actor(), Resource(TENANT_A, PATIENT_A), CONV_A, **evidence()).title == "a"
    assert connection.execute("SELECT count(*) FROM medical_rag.conversations").fetchone()[0] == 0
    assert repository.get_conversation(actor(PATIENT_B, TENANT_B), Resource(TENANT_B, PATIENT_B), CONV_B,
                                       **evidence(TENANT_B, PATIENT_B)).title == "b"
    assert connection.execute("SELECT count(*) FROM medical_rag.conversations").fetchone()[0] == 0


@pytest.mark.integration
def test_failed_write_rolls_back_and_reused_connection_remains_safe(session):
    import psycopg

    connection, repository, _ = session
    resource = Resource(TENANT_A, PATIENT_A)
    with pytest.raises(psycopg.IntegrityError):
        repository.append_turn(actor(), resource, CONV_A, TURN_ID, None, **evidence())
    assert connection.execute("SELECT count(*) FROM medical_rag.conversation_turns").fetchone()[0] == 0
    assert repository.get_conversation(actor(), resource, CONV_A, **evidence()).title == "a"


@pytest.mark.integration
def test_existing_outer_transaction_is_rejected(session):
    connection, repository, module = session
    with connection.transaction(), pytest.raises(module.UnsafeConnection):
        repository.get_conversation(actor(), Resource(TENANT_A, PATIENT_A), CONV_A, **evidence())


@pytest.mark.integration
def test_superuser_connection_rejected(database):
    import psycopg

    module = importlib.import_module("medical_rag.patient_repository")
    with psycopg.connect(**database[1]) as connection:
        repository = repository_for(module, lambda: nullcontext(connection))
        with pytest.raises(module.UnsafeConnection):
            repository.get_conversation(actor(), Resource(TENANT_A, PATIENT_A), CONV_A, **evidence())
