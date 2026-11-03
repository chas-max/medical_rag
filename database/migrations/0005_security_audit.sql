-- Owner-run migration, not a role creator or a tamper-proof external ledger.
-- The trusted application supplies the fixed institution and verified operator.
-- Application roles cannot mutate events; a schema owner can change protections.
BEGIN;

CREATE TABLE medical_rag.security_audit_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES medical_rag.tenants(tenant_id),
    -- Deliberately no account FK: account deletion must not erase history.
    operator_id uuid,
    action text NOT NULL CHECK (action IN (
        'login.start','login.finish','session.read','logout','identity.read',
        'consent.read','consent.grant','consent.withdraw','conversation.create',
        'conversation.list','conversation.read','turn.append','turn.list','audit.read','request')),
    resource_type text NOT NULL CHECK (resource_type IN (
        'authentication','identity','health_consent','conversation','turn','audit','application')),
    outcome text NOT NULL CHECK (outcome IN ('allowed','denied','error')),
    reason_code text NOT NULL CHECK (reason_code IN (
        'ok','authentication_denied','access_denied','resource_not_found',
        'invalid_request','request_denied','operation_unavailable')),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    event_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    request_id uuid NOT NULL
);
CREATE INDEX security_audit_institution_order
    ON medical_rag.security_audit_events(tenant_id,event_order DESC);

CREATE FUNCTION medical_rag.reject_audit_mutation() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'Audit events are append only';
END;
$$;
CREATE TRIGGER security_audit_append_only BEFORE UPDATE OR DELETE
    ON medical_rag.security_audit_events FOR EACH ROW
    EXECUTE FUNCTION medical_rag.reject_audit_mutation();
CREATE TRIGGER security_audit_no_truncate BEFORE TRUNCATE
    ON medical_rag.security_audit_events FOR EACH STATEMENT
    EXECUTE FUNCTION medical_rag.reject_audit_mutation();

ALTER TABLE medical_rag.security_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_rag.security_audit_events FORCE ROW LEVEL SECURITY;
CREATE POLICY security_audit_institution_scope ON medical_rag.security_audit_events
    USING (current_setting('app.audit_scope',true) =
        pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text)
    WITH CHECK (current_setting('app.audit_scope',true) =
        pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text);
REVOKE ALL ON medical_rag.security_audit_events FROM PUBLIC;
REVOKE ALL ON SEQUENCE medical_rag.security_audit_events_event_order_seq FROM PUBLIC;
COMMIT;
