-- Fictional-data vertical slice. Provisioning roles are separate from runtime.
-- Runtime: SELECT subject columns, UPDATE(patient_id) solely for row locking;
-- SELECT/INSERT consent events, conversations and turns. Never grant runtime
-- UPDATE(age_classification,identity_verified), authority-table writes, table
-- ownership, BYPASSRLS, sequence/DDL/admin privileges or broad role membership.
BEGIN;

ALTER TABLE medical_rag.patient_subjects
    ADD COLUMN age_classification text NOT NULL DEFAULT 'unknown'
        CHECK (age_classification IN ('adult','under_14','older_minor','unknown')),
    ADD COLUMN identity_verified boolean NOT NULL DEFAULT false;

CREATE TABLE medical_rag.patient_consent_events (
    event_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    grantor_account_id uuid NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('health_information','hospital_record','external_model')),
    notice_version text NOT NULL CHECK (length(notice_version) BETWEEN 1 AND 128),
    granted boolean NOT NULL,
    recorded_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    event_order bigint NOT NULL,
    UNIQUE (tenant_id,patient_id,event_order),
    FOREIGN KEY (tenant_id,patient_id) REFERENCES medical_rag.patient_subjects(tenant_id,patient_id),
    FOREIGN KEY (tenant_id,grantor_account_id) REFERENCES medical_rag.identity_accounts(tenant_id,account_id)
);
CREATE INDEX patient_consent_latest ON medical_rag.patient_consent_events
    (tenant_id,patient_id,purpose,event_order DESC);

-- Allocate the per-patient order AFTER obtaining the exclusive patient lock,
-- not in a sequence/default evaluated before a lock wait. READ COMMITTED is
-- required: each operation reads fresh authority and latest committed events.
CREATE FUNCTION medical_rag.serialize_patient_consent() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Consent events are append only';
    END IF;
    PERFORM patient_id FROM medical_rag.patient_subjects
        WHERE tenant_id=NEW.tenant_id AND patient_id=NEW.patient_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Patient scope required';
    END IF;
    SELECT COALESCE(max(event_order),0)+1 INTO NEW.event_order
        FROM medical_rag.patient_consent_events
        WHERE tenant_id=NEW.tenant_id AND patient_id=NEW.patient_id;
    NEW.recorded_at := clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER serialize_patient_consent BEFORE INSERT OR UPDATE OR DELETE
    ON medical_rag.patient_consent_events FOR EACH ROW
    EXECUTE FUNCTION medical_rag.serialize_patient_consent();

ALTER TABLE medical_rag.patient_consent_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_rag.patient_consent_events FORCE ROW LEVEL SECURITY;
CREATE POLICY patient_consent_scope ON medical_rag.patient_consent_events
    USING (current_setting('app.patient_scope',true) =
        pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text || '/' || patient_id::text)
    WITH CHECK (current_setting('app.patient_scope',true) =
        pg_current_xact_id_if_assigned()::text || '/' || tenant_id::text || '/' || patient_id::text);
REVOKE ALL ON medical_rag.patient_consent_events FROM PUBLIC;

COMMIT;
