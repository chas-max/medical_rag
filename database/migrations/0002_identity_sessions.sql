-- Experimental identity data, not wired to the running application.
-- Provision accounts/roles/bindings only through a separately controlled,
-- audited administrative process after independent identity verification.
-- Session service needs SELECT on the authority tables and SELECT/INSERT/UPDATE
-- on identity_sessions; it must not be able to alter authority tables. This
-- script does not create roles or prove a complete PostgreSQL privilege audit.
BEGIN;

ALTER TABLE medical_rag.tenants ADD COLUMN session_version bigint NOT NULL DEFAULT 0;
CREATE FUNCTION medical_rag.bump_tenant_session_version() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
BEGIN
    IF NEW.active IS DISTINCT FROM OLD.active THEN
        NEW.session_version := OLD.session_version + 1;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER tenant_active_invalidates_sessions BEFORE UPDATE ON medical_rag.tenants
FOR EACH ROW EXECUTE FUNCTION medical_rag.bump_tenant_session_version();

CREATE TABLE medical_rag.identity_accounts (
    tenant_id uuid NOT NULL REFERENCES medical_rag.tenants(tenant_id),
    account_id uuid NOT NULL,
    issuer text NOT NULL,
    subject text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    session_version bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, account_id),
    UNIQUE (tenant_id, issuer, subject)
);

CREATE FUNCTION medical_rag.bump_account_session_version() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
BEGIN
    IF NEW.active IS DISTINCT FROM OLD.active OR NEW.issuer IS DISTINCT FROM OLD.issuer
       OR NEW.subject IS DISTINCT FROM OLD.subject THEN
        NEW.session_version := OLD.session_version + 1;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER account_identity_invalidates_sessions BEFORE UPDATE ON medical_rag.identity_accounts
FOR EACH ROW EXECUTE FUNCTION medical_rag.bump_account_session_version();

CREATE TABLE medical_rag.identity_roles (
    tenant_id uuid NOT NULL,
    account_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('patient', 'guardian', 'org_admin', 'knowledge_admin',
                                     'integration_admin', 'auditor', 'support', 'platform_operator')),
    PRIMARY KEY (tenant_id, account_id, role),
    FOREIGN KEY (tenant_id, account_id) REFERENCES medical_rag.identity_accounts(tenant_id, account_id)
);

CREATE TABLE medical_rag.identity_bindings (
    tenant_id uuid NOT NULL,
    account_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    verified boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, account_id),
    FOREIGN KEY (tenant_id, account_id) REFERENCES medical_rag.identity_accounts(tenant_id, account_id),
    FOREIGN KEY (tenant_id, patient_id) REFERENCES medical_rag.patient_subjects(tenant_id, patient_id)
);

-- Changes to authorization or a verified binding force a fresh login. The
-- trusted provisioning role needs UPDATE on identity_accounts for this invoker
-- trigger; failing that the change fails closed. No SECURITY DEFINER is used.
CREATE FUNCTION medical_rag.invalidate_account_sessions_for_authority_change() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        UPDATE medical_rag.identity_accounts SET session_version = session_version + 1
        WHERE tenant_id = OLD.tenant_id AND account_id = OLD.account_id;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        UPDATE medical_rag.identity_accounts SET session_version = session_version + 1
        WHERE tenant_id = NEW.tenant_id AND account_id = NEW.account_id;
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER role_change_invalidates_sessions
AFTER INSERT OR UPDATE OR DELETE ON medical_rag.identity_roles
FOR EACH ROW EXECUTE FUNCTION medical_rag.invalidate_account_sessions_for_authority_change();
CREATE TRIGGER binding_change_invalidates_sessions
AFTER INSERT OR UPDATE OR DELETE ON medical_rag.identity_bindings
FOR EACH ROW EXECUTE FUNCTION medical_rag.invalidate_account_sessions_for_authority_change();

CREATE TABLE medical_rag.identity_sessions (
    token_hash bytea PRIMARY KEY CHECK (octet_length(token_hash) = 32),
    csrf_hash bytea NOT NULL CHECK (octet_length(csrf_hash) = 32),
    tenant_id uuid NOT NULL,
    account_id uuid NOT NULL,
    mfa_verified boolean NOT NULL,
    account_version bigint NOT NULL,
    tenant_version bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked boolean NOT NULL DEFAULT false,
    FOREIGN KEY (tenant_id, account_id) REFERENCES medical_rag.identity_accounts(tenant_id, account_id)
);

REVOKE ALL ON medical_rag.identity_accounts, medical_rag.identity_roles,
              medical_rag.identity_bindings, medical_rag.identity_sessions FROM PUBLIC;
COMMIT;
