"""Exercise preliminary row ownership using an isolated disposable PostgreSQL."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database" / "migrations" / "0001_patient_scope.sql"
IMAGE = "postgres:18.6-alpine"
TENANT_A = "00000000-0000-0000-0000-000000000101"
TENANT_B = "00000000-0000-0000-0000-000000000102"
PATIENT_A = "00000000-0000-0000-0000-000000000201"
PATIENT_B = "00000000-0000-0000-0000-000000000202"
PATIENT_C = "00000000-0000-0000-0000-000000000203"
CONV_A = "00000000-0000-0000-0000-000000000301"
CONV_B = "00000000-0000-0000-0000-000000000302"
CONV_C = "00000000-0000-0000-0000-000000000303"


def test_patient_scope_migration_exists():
    assert MIGRATION.is_file()


@pytest.fixture(scope="module")
def database():
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("set RUN_POSTGRES_INTEGRATION=1 for disposable PostgreSQL tests")
    if not shutil.which("docker"):
        pytest.skip("Docker is required")
    assert MIGRATION.is_file()
    start = subprocess.run([
        "docker", "run", "-d", "--rm", "--network", "none",
        "--tmpfs", "/var/lib/postgresql:rw,size=256m",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_INITDB_ARGS=--no-sync", IMAGE,
    ], capture_output=True, text=True, check=True)
    container = start.stdout.strip()

    def sql(query, *, check=True, user="postgres"):
        return subprocess.run([
            "docker", "exec", "-i", container, "psql", "-X", "-q", "-t", "-A",
            "-v", "ON_ERROR_STOP=1", "-U", user, "-d", "postgres",
        ], input=query, capture_output=True, text=True, check=check)

    try:
        for _ in range(100):
            ready = subprocess.run(["docker", "exec", container, "pg_isready", "-U", "postgres"],
                                   capture_output=True, check=False)
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")
        sql(MIGRATION.read_text(encoding="utf-8"))
        sql(f"""
            CREATE ROLE isolation_test LOGIN NOSUPERUSER NOBYPASSRLS;
            CREATE ROLE owner_test LOGIN NOSUPERUSER NOBYPASSRLS;
            GRANT USAGE ON SCHEMA medical_rag TO isolation_test;
            GRANT USAGE ON SCHEMA medical_rag TO owner_test;
            CREATE TABLE medical_rag.owner_test_table (id integer);
            ALTER TABLE medical_rag.owner_test_table OWNER TO owner_test;
            GRANT SELECT ON medical_rag.patient_subjects TO isolation_test;
            GRANT SELECT, INSERT, UPDATE, DELETE ON medical_rag.conversations, medical_rag.conversation_turns
              TO isolation_test;
            INSERT INTO medical_rag.tenants (tenant_id) VALUES ('{TENANT_A}'), ('{TENANT_B}');
            INSERT INTO medical_rag.patient_subjects (tenant_id, patient_id)
              VALUES ('{TENANT_A}', '{PATIENT_A}'), ('{TENANT_B}', '{PATIENT_B}'),
                     ('{TENANT_A}', '{PATIENT_C}');
            INSERT INTO medical_rag.conversations (tenant_id, patient_id, conversation_id, title)
              VALUES ('{TENANT_A}', '{PATIENT_A}', '{CONV_A}', 'a'),
                     ('{TENANT_B}', '{PATIENT_B}', '{CONV_B}', 'b'),
                     ('{TENANT_A}', '{PATIENT_C}', '{CONV_C}', 'c');
        """)
        yield sql
    finally:
        subprocess.run(["docker", "stop", "--time", "1", container], capture_output=True, check=False)


def scoped_sql(tenant, patient, statement):
    return ("BEGIN; SET LOCAL ROLE isolation_test; "
            f"DO $$ BEGIN PERFORM set_config('app.patient_scope', pg_current_xact_id()::text || '/{tenant}/{patient}', true); END $$; "
            f"{statement}; COMMIT;")


@pytest.mark.integration
def test_missing_context_and_cross_tenant_context_return_no_patient_data(database):
    no_context = database("SET ROLE isolation_test; SELECT count(*) FROM medical_rag.conversations;").stdout.strip()
    wrong_scope = database(scoped_sql(TENANT_A, PATIENT_B,
                                     "SELECT count(*) FROM medical_rag.conversations")).stdout.strip()
    assert no_context == "0"
    assert wrong_scope == "0"


@pytest.mark.integration
def test_patient_reads_only_own_conversation_and_subject(database):
    rows = database(scoped_sql(TENANT_A, PATIENT_A,
                               "SELECT title FROM medical_rag.conversations ORDER BY title")).stdout.strip()
    patients = database(scoped_sql(TENANT_A, PATIENT_A,
                                   "SELECT count(*) FROM medical_rag.patient_subjects")).stdout.strip()
    assert rows == "a"
    assert patients == "1"


@pytest.mark.integration
def test_wrong_patient_insert_and_update_are_denied(database):
    bad_insert = database(scoped_sql(TENANT_A, PATIENT_A,
        f"INSERT INTO medical_rag.conversations (tenant_id,patient_id,conversation_id,title) "
        f"VALUES ('{TENANT_B}','{PATIENT_B}','00000000-0000-0000-0000-000000000304','bad')"), check=False)
    assert bad_insert.returncode != 0
    assert "row-level security" in bad_insert.stderr
    database(scoped_sql(TENANT_A, PATIENT_A,
                        f"UPDATE medical_rag.conversations SET title='changed' WHERE conversation_id='{CONV_B}'"))
    assert database(f"SELECT title FROM medical_rag.conversations WHERE conversation_id='{CONV_B}';").stdout.strip() == "b"


@pytest.mark.integration
def test_same_institution_patients_are_isolated(database):
    rows = database(scoped_sql(TENANT_A, PATIENT_C,
                               "SELECT title FROM medical_rag.conversations ORDER BY title")).stdout.strip()
    assert rows == "c"
    database(scoped_sql(TENANT_A, PATIENT_A,
                        f"UPDATE medical_rag.conversations SET title='changed' WHERE conversation_id='{CONV_C}'"))
    assert database(f"SELECT title FROM medical_rag.conversations WHERE conversation_id='{CONV_C}';").stdout.strip() == "c"


@pytest.mark.integration
def test_patient_can_create_own_conversation_and_turn(database):
    result = database(f"""
        BEGIN;
        SET LOCAL ROLE isolation_test;
        DO $$ BEGIN PERFORM set_config('app.patient_scope', pg_current_xact_id()::text || '/{TENANT_A}/{PATIENT_A}', true); END $$;
        INSERT INTO medical_rag.conversations (tenant_id,patient_id,conversation_id,title)
          VALUES ('{TENANT_A}','{PATIENT_A}','00000000-0000-0000-0000-000000000305','temporary');
        INSERT INTO medical_rag.conversation_turns (tenant_id,patient_id,conversation_id,turn_id,content)
          VALUES ('{TENANT_A}','{PATIENT_A}','00000000-0000-0000-0000-000000000305',
                  '00000000-0000-0000-0000-000000000402','temporary');
        SELECT count(*) FROM medical_rag.conversation_turns;
        ROLLBACK;
    """)
    assert result.stdout.strip() == "1"


@pytest.mark.integration
def test_turn_cannot_reference_another_patients_conversation(database):
    result = database(scoped_sql(TENANT_A, PATIENT_A,
        "INSERT INTO medical_rag.conversation_turns (tenant_id,patient_id,conversation_id,turn_id,content) "
        f"VALUES ('{TENANT_A}','{PATIENT_A}','{CONV_B}','00000000-0000-0000-0000-000000000401','sample')"),
        check=False)
    assert result.returncode != 0
    assert "foreign key" in result.stderr


@pytest.mark.integration
def test_transaction_local_context_does_not_leak_after_commit(database):
    result = database(f"""
        SET ROLE isolation_test;
        BEGIN;
        DO $$ BEGIN PERFORM set_config('app.patient_scope', pg_current_xact_id()::text || '/{TENANT_A}/{PATIENT_A}', true); END $$;
        SELECT count(*) FROM medical_rag.conversations;
        COMMIT;
        SELECT count(*) FROM medical_rag.conversations;
    """)
    assert result.stdout.split() == ["1", "0"]


@pytest.mark.integration
@pytest.mark.parametrize("transaction_end", ["COMMIT", "ROLLBACK"])
def test_polluted_session_cannot_reuse_prior_patient_scope(database, transaction_end):
    result = database(f"""
        SET app.tenant_id = '{TENANT_A}';
        SET app.patient_id = '{PATIENT_A}';
        DO $$ BEGIN PERFORM set_config('app.patient_scope', pg_current_xact_id()::text || '/{TENANT_A}/{PATIENT_A}', false); END $$;
        BEGIN;
        SET LOCAL app.tenant_id = '{TENANT_B}';
        SET LOCAL app.patient_id = '{PATIENT_B}';
        DO $$ BEGIN PERFORM set_config('app.patient_scope', pg_current_xact_id()::text || '/{TENANT_B}/{PATIENT_B}', true); END $$;
        SELECT title FROM medical_rag.conversations;
        {transaction_end};
        SELECT count(*) FROM medical_rag.conversations;
    """, user="isolation_test")
    assert result.stdout.split() == ["b", "0"]


@pytest.mark.integration
@pytest.mark.parametrize("refresh", ["none", "tenant", "patient"])
def test_refreshing_only_part_of_old_scope_does_not_restore_access(database, refresh):
    partial = {
        "none": "",
        "tenant": f"SET LOCAL app.tenant_id = '{TENANT_A}';",
        "patient": f"SET LOCAL app.patient_id = '{PATIENT_A}';",
    }[refresh]
    result = database(f"""
        SET app.tenant_id = '{TENANT_A}';
        SET app.patient_id = '{PATIENT_A}';
        DO $$ BEGIN PERFORM set_config('app.patient_scope', pg_current_xact_id()::text || '/{TENANT_A}/{PATIENT_A}', false); END $$;
        BEGIN;
        {partial}
        DO $$ BEGIN PERFORM set_config('app.scope_txid', pg_current_xact_id()::text, true); END $$;
        SELECT count(*) FROM medical_rag.conversations;
        ROLLBACK;
    """, user="isolation_test")
    assert result.stdout.strip() == "0"


@pytest.mark.integration
def test_runtime_role_check_rejects_superuser_and_owner(database):
    safe = database("SELECT medical_rag.runtime_role_basic_check();", user="isolation_test").stdout.strip()
    elevated = database("SELECT medical_rag.runtime_role_basic_check();").stdout.strip()
    owner = database("SELECT medical_rag.runtime_role_basic_check();", user="owner_test").stdout.strip()
    assert safe == "t"
    assert elevated == "f"
    assert owner == "f"


@pytest.mark.integration
def test_runtime_role_check_rejects_server_program_privilege(database):
    database("CREATE ROLE dangerous_test LOGIN; GRANT USAGE ON SCHEMA medical_rag TO dangerous_test; "
             "GRANT pg_execute_server_program TO dangerous_test;")
    result = database("SELECT medical_rag.runtime_role_basic_check();", user="dangerous_test")
    assert result.stdout.strip() == "f"
