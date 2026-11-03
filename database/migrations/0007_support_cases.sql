-- Owner-run only after 0001..0006. Fictional-development structured support.
-- Trusted application sets scope from live authority, never user-supplied SQL.
-- Not protection against arbitrary runtime SQL or a DDL/schema owner.
BEGIN;
CREATE TABLE medical_rag.support_cases (
    tenant_id uuid NOT NULL,
    case_id uuid NOT NULL,
    requester_account_id uuid NOT NULL,
    category text NOT NULL CHECK (category IN ('login_help','account_help','usage_help')),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','resolved','cancelled')),
    assigned_account_id uuid,
    resolution_code text CHECK (resolution_code IN ('guidance_provided','referred_to_institution','not_reproducible')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id,case_id),
    FOREIGN KEY (tenant_id,requester_account_id) REFERENCES medical_rag.identity_accounts(tenant_id,account_id),
    FOREIGN KEY (tenant_id,assigned_account_id) REFERENCES medical_rag.identity_accounts(tenant_id,account_id),
    CHECK ((status='open' AND assigned_account_id IS NULL AND resolution_code IS NULL)
        OR (status='in_progress' AND assigned_account_id IS NOT NULL AND resolution_code IS NULL)
        OR (status='resolved' AND assigned_account_id IS NOT NULL AND resolution_code IS NOT NULL)
        OR (status='cancelled' AND resolution_code IS NULL))
);
CREATE INDEX support_owner_recent ON medical_rag.support_cases(tenant_id,requester_account_id,created_at DESC,case_id);
CREATE INDEX support_owner_active ON medical_rag.support_cases(tenant_id,requester_account_id)
    WHERE status IN ('open','in_progress');
CREATE INDEX support_queue_recent ON medical_rag.support_cases(tenant_id,created_at DESC,case_id);
CREATE FUNCTION medical_rag.support_case_update() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
    IF (NEW.tenant_id,NEW.case_id,NEW.requester_account_id,NEW.category,NEW.created_at)
        IS DISTINCT FROM (OLD.tenant_id,OLD.case_id,OLD.requester_account_id,OLD.category,OLD.created_at) THEN
        RAISE EXCEPTION 'Immutable support case';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER support_case_update BEFORE UPDATE ON medical_rag.support_cases
    FOR EACH ROW EXECUTE FUNCTION medical_rag.support_case_update();
ALTER TABLE medical_rag.support_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_rag.support_cases FORCE ROW LEVEL SECURITY;
CREATE POLICY support_case_select ON medical_rag.support_cases FOR SELECT USING (
    current_setting('app.support_scope',true) = pg_current_xact_id_if_assigned()::text||'/'||tenant_id::text||'/queue'
    OR current_setting('app.support_scope',true) = pg_current_xact_id_if_assigned()::text||'/'||tenant_id::text||'/owner/'||requester_account_id::text);
CREATE POLICY support_case_insert ON medical_rag.support_cases FOR INSERT WITH CHECK (
    current_setting('app.support_scope',true) = pg_current_xact_id_if_assigned()::text||'/'||tenant_id::text||'/owner/'||requester_account_id::text
    AND status='open' AND assigned_account_id IS NULL AND resolution_code IS NULL);
CREATE POLICY support_case_update ON medical_rag.support_cases FOR UPDATE USING (
    current_setting('app.support_scope',true) = pg_current_xact_id_if_assigned()::text||'/'||tenant_id::text||'/queue'
    OR current_setting('app.support_scope',true) = pg_current_xact_id_if_assigned()::text||'/'||tenant_id::text||'/owner/'||requester_account_id::text)
    WITH CHECK (current_setting('app.support_scope',true) = pg_current_xact_id_if_assigned()::text||'/'||tenant_id::text||'/queue'
    OR current_setting('app.support_scope',true) = pg_current_xact_id_if_assigned()::text||'/'||tenant_id::text||'/owner/'||requester_account_id::text);
REVOKE ALL ON medical_rag.support_cases FROM PUBLIC;
ALTER TABLE medical_rag.security_audit_events DROP CONSTRAINT security_audit_events_action_check;
ALTER TABLE medical_rag.security_audit_events ADD CONSTRAINT security_audit_events_action_check CHECK (action IN (
    'login.start','login.finish','session.read','logout','identity.read','consent.read','consent.grant',
    'consent.withdraw','conversation.create','conversation.list','conversation.read','turn.append','turn.list',
    'audit.read','request','provider.logout','support.create','support.list','support.read','support.cancel',
    'support.queue','support.take','support.resolve'));
ALTER TABLE medical_rag.security_audit_events DROP CONSTRAINT security_audit_events_resource_type_check;
ALTER TABLE medical_rag.security_audit_events ADD CONSTRAINT security_audit_events_resource_type_check CHECK (
    resource_type IN ('authentication','identity','health_consent','conversation','turn','audit','application','session','support_case'));
COMMIT;
