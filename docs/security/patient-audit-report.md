# Task4 mandatory patient audit — implementer report

Date: 2026-11-02. Scope: `patient-audit-brief.md` and the global constraints in
`patient-vertical-brief.md`. Implementation and scoped verification are ready
for independent specification and quality review; this is not production,
clinical, legal, real-IdP interoperability or whole-plan completion.

## Connected result and transaction semantics

- `create_patient_app` always constructs the service's mandatory `AuditStore`
  and supplies it to the protected OIDC factory. There is no patient-factory
  audit-disable option. Only standalone experimental headless auth retains its
  explicit no-audit default.
- Every patient read/write, health status/grant/withdrawal and `/me` wraps the
  existing live `SessionStore.authorized_transaction`; successful audit INSERT
  runs on that exact connection before commit. Existing patient locks, fresh
  authority refresh, current verification/age/consent and institution/patient
  predicates are retained. INSERT failure rolls back patient/consent changes
  and prevents private response data from being returned.
- Expected failures first unwind/roll back the operation. The outer guard then
  records a static denied event in an independent bounded primary transaction
  before allowing the original static response. An earlier authenticated actor
  is re-authenticated in that independent transaction, not cached as authority.
  No-longer-valid or unauthenticated actors are null. Unexpected failures use
  static error outcomes/reasons. Audit failure yields a static503, without
  exception text or private content. There is no indefinite retry or silent
  success fallback. A newly added endpoint accidentally missing its success
  recorder is also denied instead of releasing its successful body.
- Login start, callback, session reads and logout are connected before response
  emission, including Origin/Host middleware denials. Callback success is not
  recorded until previous-session replacement finishes. If this replacement
  or audit fails, the unexposed new session is best-effort revoked and no new
  session cookie is emitted. Logout revocation remains final if audit fails;
  its503 still deletes all three auth cookies. PKCE, nonce, one-use login state,
  token verification, exact Origin, CSRF and session expiry checks remain in use.
- Trusted middleware creates a fresh request UUID, shares its context with
  threadpool work and emits `X-Request-ID`; caller values are ignored. Static
  HTML/assets are excluded from event storage. Route classification produces
  fixed enums, never stored paths, IDs, bodies, queries or headers.

## Schema, least privilege and auditor access

Migration0005 has exactly server event UUID, fixed institution UUID, optional
verified operator-account UUID, constrained action/resource/outcome/reason,
server time/identity order and generated request UUID. There is no arbitrary
metadata field or patient/resource identifier. The institution FK is retained;
there is deliberately no account FK/cascade that erases audit history.

Audit rows use explicit institution predicates plus FORCE RLS, with
`app.audit_scope = transaction_id/institution_id`. Local and stale session-level
pooled settings cannot authorize another transaction. UPDATE/DELETE row
triggers and a TRUNCATE statement trigger reject mutation. The owner-run grant
recipe creates/elevates no roles, grants only the required SELECT and INSERT
columns, and grants no sequence authority. Event UUID/time/order are not
runtime-insertable. Preflight requires schema5, positive minimal grants, FORCE
RLS and a safe primary runtime role, and denies audit mutation, extra
REFERENCES/TRIGGER or server-metadata INSERT grants, including inherited/PUBLIC
column authority visible through PostgreSQL privilege checks.

`GET /audit` uses current local session authority and existing
`is_allowed(AUDIT_READ, Resource(institution))`, including MFA, before selecting
events. It accepts only bounded limit1..100/offset0..10000, uses stable descending
server order, returns only the eight specified metadata fields, and audits
itself in the same transaction. Wrong roles, revoked sessions/roles, disabled
accounts or absent MFA do not retrieve events. `/me.permissions.audit` is
computed server-side from that same policy.

The portal adds permission-gated 安全审计 navigation, a plain-text metadata table
and offset-capped pagination. Forbidden direct `#audit` navigation shows403
without fetching `/audit`. Staff/auditor roles alone do not gain patient views;
a separately verified adult self with its own current consent may have both
independently effective permissions. Audit results join the existing generation,
clear-on-hide/navigation and terminal logout/withdraw barriers. No browser
storage was added. The terminal barrier itself was not replaced or weakened.

## TDD and observed verification

Commands ran from the worktree root unless marked `frontend`. All PG commands
used the current `.venv/Scripts/python.exe`, Windows PowerShell,
`$env:PYTHONUTF8='1'` and `$env:RUN_POSTGRES_INTEGRATION='1'`. The tests created
actual disposable Docker PostgreSQL18.6-alpine instances, generated fictional
credentials/identities and non-owner runtime roles, and cleaned up those
instances. The existing owner-grant-recipe test additionally exercises real
operations with a fresh column-minimal role. No external hospital/IdP or real
patient service was called.

### RED evidence

```powershell
$env:PYTHONUTF8='1'; $env:RUN_POSTGRES_INTEGRATION='1'
.venv/Scripts/python.exe -m pytest tests/test_security_audit.py -k 'connected_module or full_http_flow' -q --basetemp=.pytest-tmp-audit-red2-20261102
```

Observed: **2 failed, 16 deselected, 2 warnings**. Assertions proved the audit
module was absent and real HTTP responses lacked server `X-Request-ID`.
An earlier exploratory RED also reached the missing audit table; the corrected
RED above checks the intended missing feature with assertions first.

```powershell
# frontend
npm test -- --run src/test/patient-app.test.tsx -t 'audit|审计'
```

Before UI implementation: **7 failed, 51 skipped** (audit navigation absent,
forbidden audit hash not denied). After implementation, the same command:
**7 passed, 51 skipped**.

Additional self-review REDs, each observed before its implementation change:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_security_audit.py -k 'unrecorded_success' -q --basetemp=.pytest-tmp-audit-guard-red-20261102
.venv/Scripts/python.exe -m pytest tests/test_security_audit.py -k reauthentication_failure -q --basetemp=.pytest-tmp-audit-reauth-red-20261102
.venv/Scripts/python.exe -m pytest tests/test_security_audit.py -k extra_table_grants -q --basetemp=.pytest-tmp-audit-grants-red-20261102
```

- Unconnected success: **1 failed, 24 deselected**; accidental successful
  content was200 rather than fail-closed503. Now covered by the final GREEN.
- Reauthentication replacement error: **1 failed, 25 deselected**; two active
  sessions remained instead of only the previously exposed one, and callback
  audit success had been premature. Cleanup and success-event placement were
  corrected. Focused rerun with `--basetemp=.pytest-tmp-audit-reauth-green-20261102`:
  **1 passed, 25 deselected, 2 warnings**.
- Additional audit privileges: **5 failed, 30 deselected, 2 warnings**; preflight
  accepted INSERT(event_id/recorded_at/event_order), REFERENCES and TRIGGER.
  The focused check was tightened and all five cases join final GREEN.

### Final amended covering GREEN

```powershell
$env:PYTHONUTF8='1'; $env:RUN_POSTGRES_INTEGRATION='1'
.venv/Scripts/python.exe -m pytest tests/test_security_audit.py tests/test_identity_sessions.py tests/test_oidc_identity.py tests/test_oidc_login.py tests/test_patient_vertical.py tests/test_security_server.py -q --cov=medical_rag.security_audit --cov-report=term-missing --basetemp=.pytest-tmp-audit-final3-20261102
```

Observed final result: **256 passed, 2 warnings in 84.84s**. New-module statement
coverage: **98% (166 statements, 3 missed)**, above the requested80% target.
The missing statements are internal invalid-action/cross-tenant validation and
the narrow re-authentication-denied fallback during failure recording; this is
not a claim of100% branch coverage.

The connected audit tests cover:

- Login→session→identity→health grant→create/append/read/list→withdraw→logout,
  exactly one event per logical request, verified operator/outcome/action and
  matching trusted server request UUID; concurrent threadpool requests have
  distinct correctly correlated UUIDs despite the same forged caller header.
- Title/content/provider-token/client-secret/CSRF/header/query/body/guessed-ID
  canaries absent from audit rows and captured sensitive error logs, static
  errors, missing/invisible resource404 uniformity, and two patient identities.
- Second-institution metadata isolation, RLS/no-scope and pooled-scope lifetime,
  runtime mutation denial, owner append-only DML rejection, metadata-only API,
  retained events after fictional account removal, and bounded pagination.
- Live auditor role/MFA/session/account checks, support/admin/platform/patient
  audit denial, separate adult-self permission, authorization before audit
  SELECT, audit read storage failure and audit read's own INSERT failure.
- Actual audit INSERT privilege revocation rolling back patient/consent writes
  and withholding private reads; no login cookies on audit failure; revoked
  logout staying revoked with cleared cookies on503; induced audit/patient DB
  exceptions never returned/logged with their private canary messages.
- Missing audit schema, required INSERT removal, excess mutation/server-field
  grants, current primary/startup constraints, existing patient lock/withdrawal
  and identity/session/OIDC/PKCE/Origin/CSRF regression tests.

Two intermediate covering runs exposed test compatibility/setup issues, not
silent audit opt-outs: a no-DB static-loader test expected404 from unknown
routes but now correctly gets503 when recording that404 is unavailable; it
also explicitly verifies legacy routes are absent. The new account-removal
test initially hit existing identity role/session/binding FKs; its fictional
owner setup now removes those dependants before account removal. The final
covering command above includes both corrected cases.

```powershell
.venv/Scripts/python.exe -m ruff check src/medical_rag/security_audit.py src/medical_rag/patient_service.py src/medical_rag/patient_api.py src/medical_rag/oidc_login.py src/medical_rag/security_server.py tests/test_security_audit.py tests/test_patient_vertical.py tests/test_security_server.py tests/test_oidc_login.py
git diff --check
```

Observed: ruff **All checks passed**, exit0; diff check exit0. Git reports the
existing LF→CRLF working-copy notices for frontend ESLint/package/tsconfig
files; these are warnings, not whitespace errors. Early ruff import-order and
intentional best-effort no-logging suppression findings were corrected before
the final check.

```powershell
# frontend
npm test
npm run lint
npm run build
npm run build:patient
```

Observed: **18 test files, 199 tests passed**; lint exit0; both builds exit0.
The patient DOM suite includes all prior terminal/stale-result regression
tests plus audit late-result/logout503 and forbidden-hash checks. The focused
audit parser/table suite adds18 cases including plain text, fixed-field copying,
malformed rows, empty/loading state and offset10000 boundary. Both independent
Vite builds were exercised; generated ignored outputs are not a deployment.

The two backend warnings are the existing Starlette TestClient/httpx and
Authlib/httpx migration deprecations. They remain documented, not hidden or
described as a warning-free run.

## Source and test files changed by Task4

Existing files extended (relative to the preserved Task3 baseline):

- `database/patient_runtime_grants.sql`
- `src/medical_rag/patient_service.py`
- `src/medical_rag/patient_api.py`
- `src/medical_rag/oidc_login.py`
- `src/medical_rag/security_server.py`
- `tests/test_patient_vertical.py`
- `tests/test_oidc_login.py`
- `tests/test_security_server.py`
- `frontend/src/patient/model.ts`
- `frontend/src/patient/PatientApp.tsx`
- `frontend/src/patient/patient.css`
- `frontend/src/test/patient-app.test.tsx`

New files:

- `database/migrations/0005_security_audit.sql`
- `src/medical_rag/security_audit.py`
- `tests/test_security_audit.py`
- `frontend/src/patient/audit.ts`
- `frontend/src/patient/AuditTable.tsx`
- `frontend/src/test/patient-audit.test.tsx`
- `docs/security/patient-audit-report.md` (this report)

Audited patient fixtures explicitly apply migration5 and grant its required
columns. Disposable owner-only reset temporarily disables only the audit
TRUNCATE trigger around fixture reset and restores it immediately. This is not
an application audit-disable path. Old standalone headless auth fixtures can
still run migrations1–3. No baseline snapshot was edited. Controller-owned
README/progress/future-provider brief were left alone; no `.env` read/edit,
staging, commit, deployment, legacy worker/API unlock or unknown-data assignment
was performed.

## Intentional limits and remaining gates

- This is append-only application-role separation, NOT protection from an
  owner/superuser changing schema, triggers, rows or permissions. RLS assumes
  the trusted application does not execute arbitrary caller SQL; transaction
  settings are not authentication against a malicious SQL client with runtime
  credentials. Preflight is a partial live check, not a complete privilege,
  SECURITY DEFINER, operational or compliance audit.
- Independently protected external transport/checkpoints, retention/archival,
  alerting, operational privilege review, capacity/abuse controls and legal
  policy remain production work. During audit outage, new sensitive use is
  denied; failed attempts cannot be durably recorded in the unavailable store.
- Best-effort cleanup of an unexposed issued session can itself fail when the
  database is unavailable; its raw token still never leaves the server and its
  existing bounded expiry applies. Logout revocation is not undone on audit
  failure. No implementation claims cancellation of already-authorized work.
- Stable ordering is server identity allocation order, not a claim of globally
  serialized commit order. Offset pagination is bounded but is not a snapshot
  across concurrent new events. Audit-read events become visible to later reads.
- No provider-side revocation/back-channel logout, hospital connection,
  clinical generation or production acceptance was added. Those require
  separate scoped work and institutional evidence. This report awaits
  independent specification and quality review before Task4 acceptance.
