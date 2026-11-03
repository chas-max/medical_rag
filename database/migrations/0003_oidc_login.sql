-- Experimental login transactions. Do not mount the auth application in a
-- deployment until its identity provider, TLS, secrets, limits and audit have
-- been configured and independently reviewed. No patient content is stored.
-- The login encryption key is configured outside this database. The runtime
-- role is trusted to INSERT/SELECT/UPDATE/DELETE login transactions; it must
-- never accept arbitrary SQL from requests. Grants are deployment-specific.
BEGIN;
CREATE TABLE medical_rag.oidc_login_transactions (
    tenant_id uuid NOT NULL REFERENCES medical_rag.tenants(tenant_id),
    state_hash bytea NOT NULL CHECK (octet_length(state_hash) = 32),
    browser_hash bytea NOT NULL CHECK (octet_length(browser_hash) = 32),
    tenant_version bigint NOT NULL,
    encrypted_context bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, state_hash)
);
CREATE INDEX oidc_login_transactions_expiry ON medical_rag.oidc_login_transactions(expires_at);
REVOKE ALL ON medical_rag.oidc_login_transactions FROM PUBLIC;
COMMIT;
