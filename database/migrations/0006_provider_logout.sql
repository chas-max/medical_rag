-- Owner-run only. No expiry cleanup: fences require a reviewed retention policy.
BEGIN;
ALTER TABLE medical_rag.identity_sessions
    ADD COLUMN authenticated_at bigint CHECK (authenticated_at >= 0),
    ADD COLUMN provider_sid_hash bytea CHECK (octet_length(provider_sid_hash)=32);

CREATE TABLE medical_rag.provider_logout_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES medical_rag.tenants(tenant_id),
    issuer text NOT NULL,
    jti_hash bytea NOT NULL CHECK (octet_length(jti_hash)=32),
    subject_hash bytea CHECK (octet_length(subject_hash)=32),
    sid_hash bytea CHECK (octet_length(sid_hash)=32),
    issued_at bigint NOT NULL CHECK (issued_at >= 0),
    expires_at bigint NOT NULL CHECK (expires_at > issued_at AND expires_at <= issued_at+300),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (subject_hash IS NOT NULL OR sid_hash IS NOT NULL),
    UNIQUE (tenant_id,issuer,jti_hash)
);
CREATE INDEX provider_logout_fences ON medical_rag.provider_logout_events(tenant_id,issuer,issued_at);
CREATE TRIGGER provider_logout_append_only BEFORE UPDATE OR DELETE
    ON medical_rag.provider_logout_events FOR EACH ROW
    EXECUTE FUNCTION medical_rag.reject_audit_mutation();
CREATE TRIGGER provider_logout_no_truncate BEFORE TRUNCATE
    ON medical_rag.provider_logout_events FOR EACH STATEMENT
    EXECUTE FUNCTION medical_rag.reject_audit_mutation();
ALTER TABLE medical_rag.provider_logout_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_rag.provider_logout_events FORCE ROW LEVEL SECURITY;
CREATE POLICY provider_logout_institution_scope ON medical_rag.provider_logout_events
    USING (current_setting('app.provider_logout_scope',true) =
        pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text)
    WITH CHECK (current_setting('app.provider_logout_scope',true) =
        pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text);
REVOKE ALL ON medical_rag.provider_logout_events FROM PUBLIC;

ALTER TABLE medical_rag.security_audit_events DROP CONSTRAINT security_audit_events_action_check;
ALTER TABLE medical_rag.security_audit_events ADD CONSTRAINT security_audit_events_action_check CHECK (action IN (
    'login.start','login.finish','session.read','logout','identity.read','consent.read','consent.grant',
    'consent.withdraw','conversation.create','conversation.list','conversation.read','turn.append','turn.list',
    'audit.read','request','provider.logout'));
ALTER TABLE medical_rag.security_audit_events DROP CONSTRAINT security_audit_events_resource_type_check;
ALTER TABLE medical_rag.security_audit_events ADD CONSTRAINT security_audit_events_resource_type_check CHECK (resource_type IN (
    'authentication','identity','health_consent','conversation','turn','audit','application','session'));
COMMIT;
