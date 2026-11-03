# Task4: Mandatory audit for the new patient application

Execute only after Task3 fixes pass independent re-review. This is connected
functionality, not a disconnected policy example. Global constraints from
patient-vertical-brief.md remain binding. No legacy APIs/workers, migration of
unknown data, real hospital integration or production acceptance is authorized
by this task. No commits/staging; preserve pre-existing edits and .env.

## Outcome

Every new-app patient-content success is atomically accompanied by a minimal
audit event. Failed authentication/authorization/validation attempts and new-app
login/logout/identity events are recorded safely before emitting responses.
Auditors with current AUDIT_READ and MFA can view only institution-scoped
metadata through a real API and dedicated portal view; no patient bodies or
patient/conversation IDs are exposed. Audit failure denies new sensitive use.
The standalone old headless-auth component may remain explicitly experimental
without audit; the new patient factory and trusted launcher cannot opt out.

## Data and trust

Add migration0005_security_audit.sql, focused security_audit.py, tests/test_security_audit.py.
Extend patient_service.py/patient_api.py/oidc_login.py and trusted startup/grant
recipe only as needed; keep modules focused. Extend portal model/view/tests
for audit navigation/table, without breaking the corrected terminal barrier.

Audit table: server UUID event ID, fixed institution UUID, optional verified
operator account UUID (null for unauthenticated attempt), enum/check-constrained
action and resource type, allowed/denied/error outcome, static reason code,
server timestamp/order, generated request UUID. No arbitrary JSON/text metadata,
path/query/URL, patient ID, resource UUID, raw IP, headers, prompts/answers,
documents, hospital fields, provider responses, tokens or keys. Do not infer an
actor from submitted claims/IDs; only a verified current local principal may
provide operator ID. No account FK that erases audit when an account is removed.
Tenant FK and explicit predicates/RLS enforce institution scope. FORCE RLS uses
transaction-bound trusted institution scope, not request tenant fields; keep
connection-pool scope lifetime tests. Runtime SELECT/INSERT only, not UPDATE,
DELETE,TRUNCATE,ownership or BYPASSRLS. Append-only trigger rejects changes.
Owner-run grants do not create/elevate roles. Startup preflight requires new
schema, minimal positive grants and denied mutation rights.

This is append-only with application-role separation, NOT protection against a
database owner who alters schema. Full independently protected external audit
transport/checkpoints, retention, alerting and privilege review remain production
work. No false tamper-proof/compliance claim or silent fallback when recording
fails. Do not log sensitive exceptions while reporting audit failure.

## Integration and transactions

Use mature current authority already available, not a second cached identity
for patient authorization. Successful patient reads/writes, health status,
grant/withdraw and /me insert their audit in the SAME transaction as the
operation before it returns; failed audit insertion rolls back writes and
prevents content return. This must be tested with real PG, not just mocks.

Expected failed operations roll back that transaction, then record a minimal
denied event in an independent primary transaction before returning the static
original denial. Unexpected failures use only static audit-unavailable/result
metadata. A failed attempt audit must not reveal guessed resource existence;
both missing and invisible IDs retain uniform404 with no raw ID in audit.
If audit itself fails, return static503 and no private content. Bounded DB
deadlines remain active; do not retry indefinitely or swallow audit exceptions.

New patient app auth routes must record login/session/logout results before
response emission. For login audit failure, do not emit new session cookies;
best-effort revoke an unexposed issued session, without logging raw values.
Logout must still clear cookies if session revocation succeeded even when the
audit response is503; never restore its old authority. A terminal UI failure
must retain the existing no-automatic-restore behavior. Do not weaken PKCE,
single-use login consumption, Origin, CSRF, token verification or expiry.

Generate fresh request UUID in trusted app middleware/context, propagate to
threadpool service work and safe X-Request-ID response, and use it in events.
Do not trust caller X-Request-ID as authority or log raw routes containing IDs.
Static assets can be excluded. Canonical action/resource enums are fixed in
code. Avoid duplicate success/denial events for a single logical request.

## Auditor API and UI

GET /audit?limit=20&offset=0: authenticate live, check is_allowed(AUDIT_READ,
Resource(institution)) including MFA, institution explicit WHERE and RLS,
bounded limit1..100/offset0..10000, stable server ordering. Return standard
envelope containing items {id,time,operator_id|null,action,resource_type,
outcome,reason_code,request_id},limit,offset; no patient/resource owner fields.
Audit reads also audit themselves transactionally. Wrong roles, disabled
accounts, revoked roles/sessions or missing MFA deny before event retrieval.
Reject arbitrary tenant/account/resource filters and malformed pagination.

GET /me includes effective permissions.audit, computed with existing policy
and current server state. It must never be true because of a client role.
Portal adds only permission-gated '安全审计' view, metadata table and bounded
pagination; direct forbidden hash shows403 without fetching /audit. Patients,
support, org-admin and platform-operator alone get no audit navigation/content.
Auditors gain no health consent/conversation navigation unless separately
authorized as verified adult self with its own current consent. Preserve all
terminal/stale-read guards, no storage and plain-text rendering.

## Verification

TDD RED/GREEN first. Actual disposable PostgreSQL with least runtime grants:

- Full HTTP login->grant->create/append/read->withdraw/logout events exist with
  correct operator,action,outcome and matching server request ID.
- Canary titles/content, all tokens/secrets, guessed IDs, query/body/header
  strings never appear in audit rows or logs; sensitive errors remain static.
- Two patients and second institution isolation; outsider401/403/404 uniform,
  role/status/consent changes and audit-role/MFA checks observed live.
- Revoke audit INSERT privilege or induce audit storage failure: patient write
  rolls back and read returns no private body; no new login cookie on audit
  failure; revoked logout remains revoked with cookies cleared.
- Append-only/RLS/pool-scope checks plus denied broad runtime grants/startup
  missing migration. Audit read failure itself remains fail closed.
- DOM tests for auditor navigation/table, denied roles/direct hash, safe text,
  late-result/logout/withdraw barriers unaffected. Existing frontend tests,
  lint,both builds and targeted backend auth/patient/startup regressions pass.

Update existing fixtures to apply migration5 only where audited patient app is
used; old headless auth regression can retain migration1-3. Do not quietly
disable mandatory audit for test compatibility. Target >80% new-module coverage.
Report full commands/results, intentional limitations and source files to
docs/security/patient-audit-report.md. Return concise observed status; wait for
independent spec+quality review before claiming task complete.
