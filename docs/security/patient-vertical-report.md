# Patient authorization vertical slice — implementer report

Date: 2026-11-02. Scope: approved Task 1 only. Fictional development data.

## Outcome and files

The isolated OIDC application can now expose live self-patient identity,
purpose-specific health notice/grant/withdrawal and owned conversations/turns
through `create_patient_app(login, notice)`. No legacy application, experimental
snapshot repository, inference, RAG, worker, memory, document or model-settings
route is mounted or enabled. The `/` route remains absent for the separately
owned UI follow-on.

Files created:

- `database/migrations/0004_patient_access.sql`
- `src/medical_rag/patient_service.py`
- `src/medical_rag/patient_api.py`
- `tests/test_patient_vertical.py`
- `docs/security/patient-vertical-report.md`

Files changed:

- `src/medical_rag/identity_sessions.py`: authorized transaction/refresher,
  shared authentication implementation, bounded timeouts, updated scope docs.
- `tests/test_identity_sessions.py`: live transaction/refresh regression.

`oidc_login.py` required no modification. Existing uncommitted changes were
preserved; `.env`, `.env.example`, README, original D: checkout and legacy code
were not edited by this task. No staging, commit or reset was performed.
Most security files were already untracked, so ordinary `git diff --stat`
does not inventory this task; reviewers must use the supplied baseline and
working tree, not the index alone.

## Authority and transaction semantics

`SessionStore.authorized_transaction` opens an independent READ COMMITTED
read/write transaction against the configured primary, rejects connections
already in a transaction, runs the existing runtime-role/basic-primary check,
and holds the session row FOR UPDATE through the entire patient operation.
Session locking precedes a fresh authority/expiry statement. Authentication
continues to use the same code and preserves its existing call interface.
Local `lock_timeout=3s` and `statement_timeout=5s` match OIDC storage.

`AuthorizedTransaction.refresh()` repeats the account, institution,
session-version/expiry/CSRF, roles and verified-binding checks on that same
connection. It also compares the principal with the original operation
principal. No principal from a request/preflight or previous operation is
used as patient authority. The API performs an additional authentication
preflight so malformed body/path/query validation does not disclose received
inputs to unauthenticated callers; it discards that principal, and the service
reauthorizes inside its own transaction.

The patient scope is set transaction-locally from only the live verified
binding, using the existing transaction-ID/institution/patient convention.
Patient and content queries also contain explicit institution+patient
predicates. Content operations acquire a shared patient row lock; consent
grant/withdrawal acquire an exclusive patient row lock. Read/write policy
action is independent of the lock mode. After lock acquisition a fresh SQL
statement reads patient verification/age; the session authority is refreshed;
only then is latest health-purpose consent evaluated with database time.
Unverified identities, unverified bindings, all minors/unknown ages, guardians
and staff without self-patient permission remain denied. Staff can inspect
their own identity without receiving fabricated functional role features.

A withdrawal or verification/authority change committed before the waiting
operation's fresh check is observed. A content transaction already authorized
and holding its shared lock is in flight; withdrawal waits for it to finish.
This does not cancel streams/workers or undo already authorized operations.
Authority changes after the fresh statement snapshot likewise remain in flight.

## Schema and consent

Migration 0004 adds trusted `age_classification` (default `unknown`) and
`identity_verified` (default false). Runtime privileges in the integration
fixture are column-minimal for authority reads, SELECT on needed patient
columns, and UPDATE(patient_id) solely to permit PostgreSQL row locks. Runtime
has no UPDATE privileges for age/verification or authority tables. The
migration documents the required deployment grants; it does not assume a
production runtime-role name or create privileged deployment identities.

Consent events have composite institution/patient and institution/grantor
foreign keys, explicit purpose/version/grant state, expiry and server-recorded
timestamps. FORCE RLS uses the same transaction-bound patient scope as 0001.
Runtime gets SELECT/INSERT only. A trigger rejects updates/deletes even from
the trusted test administrator, and takes the exclusive patient lock before
assigning the next per-patient `event_order=max+1`. Unique
institution/patient/order prevents duplicates. The trigger overwrites supplied
order/time; it does not infer commit order from a sequence/default allocated
before a lock wait, or from a wall clock that can tie/move backwards. The
READ COMMITTED requirement is documented. History survives withdrawal/regrant.

The latest event for the patient+purpose is selected across **all versions**;
only then is exact current notice version, grant state, grantor, age,
verification and expiry checked by the existing consent policy. Older grants
are never resurrected after withdrawal, incompatible-version grant or expiry.
Accepting health information never creates a hospital-record/external-model
grant, and grants of those other purposes cannot authorize health content.

`HealthNotice` is immutable, requires actual nonempty trusted text and has no
default legal notice. Version is bounded to 128 characters, text to 64,000,
validity to 1..31,536,000 seconds. Withdrawal does not require an existing or
current grant and records another event; it does not promise record deletion.

## HTTP contract and limits

All new JSON responses use `{success,data,error}` and reuse the protected
headless application's exact-origin/HTTPS/Host checks, secure HttpOnly
`__Host-` SameSite=Lax cookies, no-store and security headers. Every mutation
requires exactly one valid session-bound `X-CSRF-Token`. Missing, duplicate or
invalid proof causes no mutation. Error bodies are static; validation does not
return Pydantic inputs, received patient text, tokens or database exceptions.

Routes implemented: GET `/me`; GET/POST/DELETE `/me/consents/health`;
GET/POST `/conversations`; GET `/conversations/{id}`;
GET/POST `/conversations/{id}/turns`. `/me` exposes current own account,
institution, roles, verified-binding boolean and effective permissions, not a
client-supplied patient or an ownership identifier. Conversations permission
requires current live health consent; health-consent permission requires the
verified adult self even before grant.

Consent POST accepts only `{version,accepted:true}`. Conversation/turn POST
accept only strict `{title}` / `{content}`, respectively. Extra fields are
forbidden, including ownership/tenant/user/role/purpose injection; duplicate
JSON keys are rejected. Title max 200 characters, turn content max 16,000,
whitespace-only values rejected, request body bytes bounded before JSON parse
at 100,000. IDs are server UUIDs; malformed/missing/invisible resource IDs use
the same static resource404. Lists use limit default20/max100, offset
default0/max10000, deterministic ordering and bounded SQL LIMIT/OFFSET.

## TDD and verification evidence

All commands below ran in the supplied worktree using `.venv/Scripts/python.exe`.
PostgreSQL runs set `$env:RUN_POSTGRES_INTEGRATION='1'`. Docker29 launched
disposable `postgres:18.6-alpine` containers via the existing OIDC/session
fixtures; the new fixture uses a genuinely unprivileged NOSUPERUSER NOBYPASSRLS
runtime with actual RLS and restricted column grants. The IdP is a nonce-signed
RS256 test transport, not a real external provider.

### RED evidence

1. `.venv/Scripts/python.exe -m pytest tests/test_patient_vertical.py -q`
   before patient modules existed: **1 failed, 1 warning in 1.21s**;
   `find_spec('medical_rag.patient_service') is not None` failed. An unrelated
   pre-existing Windows pytest-current atexit PermissionError followed;
   subsequent commands used explicit disposable basetemp paths.
2. `.venv/Scripts/python.exe -m pytest tests/test_identity_sessions.py -k authorized_transaction -q --basetemp D:/temp-openclaw/patient-vertical-session-red2`
   before refactor: **1 failed, 39 deselected, 1 warning in 5.13s**;
   `hasattr(store,'authorized_transaction')` failed after real session issuance.
3. Functional patient tests were authored before core implementation. The first
   focused functional command,
   `.venv/Scripts/python.exe -m pytest tests/test_patient_vertical.py -k full_oidc -q --basetemp D:/temp-openclaw/patient-vertical-api-red`,
   produced **40 deselected, 2 warnings, 1 error in 6.55s** at missing migration
   0004. This was missing implementation/setup evidence, **not** claimed as a
   meaningful functional assertion RED.
4. During tightening, the independently raised read-versus-write-action concern
   got a dedicated failing regression before its fix:
   `.venv/Scripts/python.exe -m pytest tests/test_patient_vertical.py -k content_mutations_authorize -q --basetemp D:/temp-openclaw/patient-vertical-write-red`:
   **1 failed, 43 deselected, 2 warnings in 5.04s**. With write explicitly denied
   by policy, POST returned200 instead of403. `_subject` now takes an explicit
   action independently of shared/exclusive locking; this test is green in the
   final combined run. Supplemental edge/coverage checks exercise already
   implemented behavior and are not misrepresented as separate RED cycles.

### Intermediate runs / corrections

- Session refactor GREEN:
  `.venv/Scripts/python.exe -m pytest tests/test_identity_sessions.py -q --basetemp D:/temp-openclaw/patient-vertical-session-green`:
  **40 passed, 1 warning in 11.91s**.
- First full patient run:
  `.venv/Scripts/python.exe -m pytest tests/test_patient_vertical.py -q --basetemp D:/temp-openclaw/patient-vertical-first-green`:
  **33 failed, 8 passed, 2 warnings in 11.60s**. Root cause was the legacy
  conftest default Origin being inherited by the new TestClient; fixed the
  focused fixture to use explicit empty headers, without weakening production
  origin checks.
- `.venv/Scripts/python.exe -m pytest tests/test_patient_vertical.py -xq --basetemp D:/temp-openclaw/patient-vertical-second-green`:
  **41 passed, 2 warnings in 18.80s**.
- Initial combined regression/coverage command (same coverage flags as final,
  omitting legacy-lockdown file and using patient-vertical-regression basetemp):
  **134 passed, 2 warnings in 38.80s**. Import sorting happened while coverage
  was running, producing unreliable shifted source line mappings for API
  coverage (69%); therefore this was not used for the final coverage claim.

### Final complete verification (no source edits during coverage run)

```powershell
$env:RUN_POSTGRES_INTEGRATION='1'
.venv/Scripts/python.exe -m pytest tests/test_patient_vertical.py tests/test_identity_sessions.py tests/test_oidc_login.py tests/test_security_lockdown.py -q --cov=medical_rag.patient_api --cov=medical_rag.patient_service --cov=medical_rag.identity_sessions --cov-report=term-missing --basetemp D:/temp-openclaw/patient-vertical-final-verify
```

Exact summary: **168 passed, 2 warnings in 47.93s**, exit0.

```text
Name                                   Stmts   Miss  Cover   Missing
src/medical_rag/identity_sessions.py        95      7    93%   70,87-88,91,158,198-199
src/medical_rag/patient_api.py             114      0   100%
src/medical_rag/patient_service.py         130      2    98%   83,100
TOTAL                                    339      9    97%
```

The two warnings are installed Starlette TestClient and Authlib httpx-to-httpx2
deprecation warnings. They were not suppressed and are not failures. No real
IdP or production claim is inferred from the fake-provider test transport.

Final lint command:

```powershell
.venv/Scripts/python.exe -m ruff check src/medical_rag/identity_sessions.py src/medical_rag/patient_service.py src/medical_rag/patient_api.py tests/test_patient_vertical.py tests/test_identity_sessions.py
```

Output: **All checks passed!**, exit0. Import-only formatter fixes were applied
before the final run. `git diff --check`: **no output, exit0**. Because owned
security files are untracked, supplemental `git -c core.autocrlf=false diff
--no-index --check BEFORE AFTER` checks compared session code with the supplied
flat baseline `D:/temp-openclaw/medical-patient-vertical-baseline-20261102/identity_sessions.py`
and each other owned file with `NUL`. A pre-existing trailing blank line in the
touched session test file was removed after the final test run (whitespace
only). Final supplemental checks have no whitespace-error output (exit1 is
the expected no-index difference status). Initial checks used nonexistent
nested baseline paths; these were corrected to the actual flat baseline.
The unchanged `oidc_login.py` and supplied baseline also have identical SHA256
hashes: `BEFAD14EC056D9CF74FE2426E5A3CE9FEA92B31ED328ACEFF4D9AB7F39E1C66B`.

## Focused evidence covered

The focused suite includes full HTTP login start/callback, signed nonce,
current identity and actual notice, grant, own create/get/list/append/list;
two separately authenticated adult patient sessions and a second institution;
guessed IDs, uniform404, ownership/authority injection and actual RLS scope
expiry; no grant, withdrawal, regrant history, expired latest grant, latest
incompatible version, cross-purpose separation; staff/guardian denial,
unverified binding/patient and every nonadult classification; next-access
account/tenant/role/binding/logout/expiry invalidation; and static strict-body,
CSRF/Origin/duplicate-header, malformed-ID and unexpected-storage-error checks.

Real PostgreSQL patient-lock waits are coordinated by polling
`pg_stat_activity.wait_event_type='Lock'` for a unique application_name before
committing the trusted mutation. Waiting operations deny after withdrawal,
patient verification/age changes, account/tenant disable, binding change and
session expiry. A separate Event-coordinated real content transaction holds a
shared patient lock while a second session's withdrawal demonstrably waits;
the content completes, withdrawal commits, and the next content call denies.
Server order/time overwrite and append-only triggers are exercised as well.
Existing session and OIDC regressions and legacy API/worker lockdown tests are
included, not the unrelated full legacy functional suite.

## Remaining limitations / acceptance boundaries

- This is fictional-data development, not verified hospital identity proofing,
  real-IdP interoperability, clinical safety, legal notice acceptance or
  production acceptance. Institutional configuration and actual approved notice
  text are required; construction fails without a valid notice.
- Deployments must apply migration0004 using a trusted owner/provisioner and
  independently install/audit least privileges. The migration does not name or
  create production runtime/provisioning roles. The basic runtime diagnostic
  is intentionally not a full privilege/SECURITY DEFINER audit; privileged SQL
  administrators can still alter schema/disable triggers/RLS.
- UI, trusted startup factory, proxy/TLS integration, institutional account and
  consent provisioning workflows, audited role administration, external-model
  approval, record protocols, guardian/minor rules, worker isolation and
  operational retention/cleanup policies remain controller-owned follow-ons.
  Existing experimental gateway behavior was not modified or accepted here.
- Upstream server/proxy access logging must remain disabled/redacted as already
  required by OIDC; this application does not configure external loggers.
  HTTPS origin/Host are exact; proxy trust is not broadened by this task.
- Withdrawal stops newly authorized health content operations but retains
  historical records and waits for already authorized transactions. It does
  not implement cancellation, clinical generation, streaming or a worker API.
- List pagination is deliberately bounded OFFSET pagination, not an audit or
  bulk export/history endpoint. General rate limits, real audit transport,
  backup/erasure/legal retention and operational request deadlines need
  deployment review. Dependency deprecation warnings remain as noted above.
