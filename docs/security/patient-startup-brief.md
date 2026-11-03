# Trusted startup for the patient slice

Requirements supplement patient-vertical-brief.md global constraints.
Do not edit .env, old Settings, old api.py or worker.py; keep legacy locked.

Create security_server.py and tests/test_security_server.py. Pure bounded
configuration loading may live in focused security_config.py with typed
trusted dataclasses; leave DB preflight/static mount/CLI in security_server.py.
Create
deploy/patient/config.example.toml with only example secret FILE paths and
non-secret trusted settings, and database/patient_runtime_grants.sql for psql
owner-run grants to a quoted runtime-role variable. No auto-apply migrations,
no auto-created accounts/roles/patient verification, no dev/mock IdP endpoint.
Controller updates README separately after final evidence.

Expose load_patient_application(config_path: Path) returning new patient app
with dedicated built frontend. Expose main(argv=None), invoked with python -m
medical_rag.security_server --config PATH. Missing/invalid config, key files,
build assets or authoritative DB preflight fail closed with a fixed Chinese
error and no paths/DSN/keys/SQL/traceback. CLI arguments contain file paths,
never raw credentials. Configuration is trusted operator input, not an API.
Do not read cwd .env or allow request-driven identity-provider configuration.

Strict TOML root keys:
tenant_id, database_dsn_file, transaction_key_file, frontend_dist,
listen_host, listen_port, tls_cert_file, tls_key_file;
[oidc] issuer,authorization_endpoint,token_endpoint,callback_uri,client_id,
client_secret_file,public_key_file,mfa_acr_values;
[health_notice] version,text_file,validity_seconds.
Unknown/missing/wrong-type values denied. All paths resolve relative to the
config file, not process cwd. Bound configuration and secret/notice file reads
(64KiB config/publickey/notice,16KiB DSN/clientsecret, exact32 raw encryption
key). Nonempty text required; notice <=approved HealthNotice bounds.
Secrets excluded from repr/exception/logs. Public key parsed with mature
cryptography PEM loader and validated by existing LoginConfiguration.

Use primary PostgreSQL connection, bounded connect/lock/statement timeouts.
Preflight before serving: required migrations1-4 tables/columns reachable,
current tenant active; partial runtime_role_basic_check passes and primary
not recovery. Reject runtime privileges which mutate institution/account/
roles/bindings/verification/age or update/delete consent events. Check column
privileges as well as table privileges; runtime subject UPDATE(patient_id)
is allowed solely for row locking. Full database security audit is still an
external gate, not guaranteed by these checks. Runtime grants exclude schema
creation, table ownership, DDL,TRUNCATE,superuser/BYPASSRLS and authority writes;
include only needed explicit columns,session/login DML,patient subject lock
key,conversation/turn SELECT+INSERT,consent SELECT+INSERT and identity sequence
requirements from actual migration. Do not grant PUBLIC broader privileges.

Direct TLS only; no trust in Forwarded/X-Forwarded-* from browsers. Keep bind
loopback by default; trusted config may supply explicit IP, not wildcard
0.0.0.0/::, with scope warning. Exact trusted callback origin still applies.
Uvicorn access_log=False,proxy_headers=False,server_header=False,debug=False,
bounded concurrency and keepalive; no request/response body logging. No unsafe
HTTP shortcut. Do not claim experimental old nginx config is integrated:
it currently uses HTTP upstream and strips Host port.

Mount only generated patient.html at GET / and build assets at /assets.
Validate dedicated build has patient.html + assets directory; no directory
browsing, arbitrary config-directory serving or legacy /ui fallback. Serve
no-store responses, self-only script/style/connect CSP without unsafe-inline
or eval; base-uri none,frame-ancestors none. Form-action permits self plus only
the fixed trusted authorization-endpoint origin, derived from server config
with valid CSP source syntax, never from a request. Browser A/B testing showed
form-action self alone blocks the required POST303 redirect to the IdP.
Retain all auth
headers and OIDC routes. Auth headless default CSP may receive one trusted
optional factory parameter/hook for the patient HTML app; do not accept CSP
from requests. Public docs/openapi/legacy API paths remain absent.

Test TDD: missing/unknown/wrong config, relative path resolution, bounds and
invalid keys all sanitized; no implicit env dependency; app / root + assets
works with actual dist test fixture and TLS Origin/Host controls; old routes
absent; default auth-only CSP remains restrictive; server invocation carries
access/proxy logging protections. Focused disposable-PG tests prove startup
with least privileges succeeds and superuser/authority-write/consent-update/
wrong-tenant/inactive/recovery/missing-schema denied. Infrastructure state
can be mocked only for pure loader tests; distinguish actual DB tests.
Report exact RED/GREEN, regression commands and limitations to
docs/security/patient-startup-report.md. No commits or deployment of real data.
