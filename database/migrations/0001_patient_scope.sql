-- Experimental, one-time bootstrap. Do not put real patient information in
-- this schema until authentication, key management and migration are complete.
-- After identity and authority are verified, each read-write transaction must
-- set app.patient_scope locally as one atomic value:
-- pg_current_xact_id()::text || '/' || tenant_id || '/' || patient_id.
-- A stale pooled session value then cannot authorize a different transaction.
-- RLS is defense in depth, not authentication: superusers and BYPASSRLS roles
-- bypass it, and an SQL client with the application role can forge settings.
-- runtime_role_basic_check() is only a partial diagnostic. It does not inspect
-- SECURITY DEFINER functions or all possible privileges; a deployment needs
-- a full least-privilege audit. This schema is not connected to the application.

BEGIN;

CREATE SCHEMA medical_rag;

CREATE FUNCTION medical_rag.runtime_role_basic_check() RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER
AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS caller
        WHERE caller.rolname IN (current_user, session_user)
          AND (
              EXISTS (
                  SELECT 1 FROM pg_catalog.pg_roles AS privileged
                  WHERE (privileged.rolsuper OR privileged.rolbypassrls
                         OR privileged.rolcreaterole OR privileged.rolcreatedb
                         OR left(privileged.rolname, 3) = 'pg_')
                    AND pg_catalog.pg_has_role(caller.oid, privileged.oid, 'MEMBER')
              )
              OR EXISTS (
                  SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                  WHERE namespace.nspname = 'medical_rag'
                    AND pg_catalog.has_schema_privilege(caller.oid, namespace.oid, 'CREATE')
              )
              OR EXISTS (
                  SELECT 1 FROM pg_catalog.pg_class AS relation
                  JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                  WHERE namespace.nspname = 'medical_rag'
                    AND relation.relkind IN ('r', 'p')
                    AND (pg_catalog.pg_has_role(caller.oid, relation.relowner, 'MEMBER')
                         OR pg_catalog.has_table_privilege(caller.oid, relation.oid, 'TRUNCATE'))
              )
          )
    );
$$;

CREATE TABLE medical_rag.tenants (
    tenant_id uuid PRIMARY KEY,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE medical_rag.patient_subjects (
    tenant_id uuid NOT NULL REFERENCES medical_rag.tenants (tenant_id),
    patient_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, patient_id)
);

CREATE TABLE medical_rag.conversations (
    tenant_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    title text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, patient_id, conversation_id),
    FOREIGN KEY (tenant_id, patient_id)
        REFERENCES medical_rag.patient_subjects (tenant_id, patient_id)
);

CREATE TABLE medical_rag.conversation_turns (
    tenant_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    turn_id uuid NOT NULL,
    content text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, patient_id, conversation_id, turn_id),
    FOREIGN KEY (tenant_id, patient_id, conversation_id)
        REFERENCES medical_rag.conversations (tenant_id, patient_id, conversation_id)
);

ALTER TABLE medical_rag.patient_subjects ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_rag.patient_subjects FORCE ROW LEVEL SECURITY;
CREATE POLICY patient_subject_scope ON medical_rag.patient_subjects
    USING (current_setting('app.patient_scope', true) =
           pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text || '/' || patient_id::text)
    WITH CHECK (current_setting('app.patient_scope', true) =
                pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text || '/' || patient_id::text);

ALTER TABLE medical_rag.conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_rag.conversations FORCE ROW LEVEL SECURITY;
CREATE POLICY conversation_scope ON medical_rag.conversations
    USING (current_setting('app.patient_scope', true) =
           pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text || '/' || patient_id::text)
    WITH CHECK (current_setting('app.patient_scope', true) =
                pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text || '/' || patient_id::text);

ALTER TABLE medical_rag.conversation_turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_rag.conversation_turns FORCE ROW LEVEL SECURITY;
CREATE POLICY conversation_turn_scope ON medical_rag.conversation_turns
    USING (current_setting('app.patient_scope', true) =
           pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text || '/' || patient_id::text)
    WITH CHECK (current_setting('app.patient_scope', true) =
                pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text || '/' || patient_id::text);

COMMIT;
