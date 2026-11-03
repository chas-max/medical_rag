# Patient authorization vertical slice

Status: approved implementation continuation, 2026-11-02.

## Global constraints

- Keep the legacy API and workers locked. Do not mount legacy chat, memory,
  model settings, documents, vectors or jobs on the new application.
- Authentication alone is not patient authorization. Every patient operation
  must read current server-side account, institution, roles, verified binding,
  patient verification/age and required-purpose consent from the authoritative
  primary PostgreSQL database. Never accept these from request fields.
- Patients see only their own bound patient resources. Administrators,
  support and platform roles have no implicit access. Guardians, minors and
  unknown age remain denied in this slice.
- Browser cookies remain Secure, HttpOnly, __Host-, SameSite=Lax. Mutations
  require the exact trusted Origin and one valid X-CSRF-Token header. Reads
  require a valid session; do not expose patient data in URLs, logs or errors.
- All JSON responses use {success, data, error}. Missing and another patient's
  sensitive resource produce the same public response. Public errors are
  static; do not return Pydantic inputs, SQL errors, patient text or tokens.
- This is fictional-data development, not production acceptance. Never claim
  genuine hospital identity verification, real-IdP interoperability, clinical
  safety or legal acceptance from these tests.
- Preserve all pre-existing uncommitted changes. Do not commit, reset, stage
  unrelated files, edit .env, or change the original D: checkout.

## Task 1: Connect live patient authority and conversations to OIDC

Implement with TDD. Owned files: identity_sessions.py refactor;
database/migrations/0004_patient_access.sql; new patient_service.py and
patient_api.py; focused new/changed tests. Do not change README or the legacy
application. Existing oidc_login.py can receive the smallest necessary hook,
but the new app should reuse create_auth_app(login) and its protections.

### Session transaction

Expose a documented internal authorized transaction context manager from
SessionStore. Keep an independent READ COMMITTED transaction and the locked
session row open while the patient operation executes. Existing authenticate
must preserve its interface and semantics by using the same implementation.
Allow a live authority refresh on that same connection after patient-lock
waits, and reject if binding/role/session/account/tenant changed. Never cache
Principal across operations. Keep the lock-first, fresh-statement expiry
checks. Add bounded lock/statement timeouts consistent with OIDC storage.

### Schema

Add trusted age_classification (adult, under_14, older_minor, unknown;
default unknown) and identity_verified (default false) to patient_subjects.
Only the trusted institution provisioning role may modify these values;
runtime may have UPDATE of a harmless subject key solely for PostgreSQL row
locking, but must not get UPDATE of verification or age columns.

Add purpose-specific append-only patient consent events. Include institution,
patient, grantor account, purpose, notice version, explicit grant/withdrawal,
server timestamps and expiry. Composite FKs enforce institution/patient and
institution/account relationships. The latest event per patient+purpose
determines current consent (a newer withdrawal or new version must not expose
an older grant). Serialize all event mutations using the patient row lock;
do not rely on sequence numbers alone to imply commit ordering. FORCE RLS
with the same transaction-bound app.patient_scope convention as migration0001.
Runtime SELECT/INSERT only on this table. No hospital/external-model grant is
implicitly created by accepting health-information notice.

### Live service

Use SessionStore's current authorized transaction, not the disconnected
snapshot repository interface. Set patient_scope exclusively from the live
verified binding. Apply existing access_policy and consent_policy. Patient
and conversation queries include explicit institution+patient predicates
as well as RLS. UUIDs are server-generated. Use a shared patient row lock for
content operations; exclusive patient row lock for consent grant/withdraw.
After lock acquisition, read current patient verification/age, refresh
account/session authority, and then read latest consent with DB time. A
withdrawal committed before the operation's fresh authorization check denies
that operation. Operations already authorized and holding shared locks are
in flight; withdrawal waits for their transaction to finish. Do not describe
this as cancellation of streams or workers. Verification changes must also
be observed after row-lock waits.

Trusted immutable notice configuration includes a nonempty bounded version,
nonempty actual notice text and bounded validity seconds (up to one year).
No default pre-approved legal text. A missing notice must fail construction.
The service supports status, explicit grant of the exact current notice,
withdrawal independent of whether consent is current, and owned conversation
create/list/get plus append/list turns. Keep modules focused. Query duplication
with the old experimental repository is acceptable; do not refactor or open
the old repository in this task. Bound lengths and list sizes; no unbounded
query or memory result. Grant history must survive withdrawal/regrant.

### API

create_patient_app(login, notice) reuses the protected headless OIDC app,
adding only new routes:

- GET /me: current account, institution, roles and binding state plus effective
  navigation permissions. Never fabricate a verified patient from a body/claim.
  Staff may see their own identity without gaining patient data access. Do not
  advertise unimplemented role features as functional patient permissions.
- GET /me/consents/health: actual current notice and current health consent
  status for the verified adult self, even before grant.
- POST /me/consents/health: explicit version + accepted:true, exact current
  trusted notice only; server fixes purpose, user, patient and timestamps.
- DELETE /me/consents/health: record withdrawal, stop new health content reads
  and writes; do not promise deletion of historical records.
- GET/POST /conversations; GET /conversations/{id};
  GET/POST /conversations/{id}/turns.

All content routes require current health-information consent. Conversation
and turn POST bodies accept only title/content respectively, with strict types,
bounds and extra fields forbidden. No client-supplied ownership IDs. Path IDs
must be UUID; malformed or invisible IDs give a uniform static resource404.
Use bounded pagination. Write operations require one CSRF header. Missing,
duplicate or invalid CSRF is denied with no mutation. Avoid returning received
bad bodies in request-validation errors. Keep all auth cookies and no-store
headers. No model inference or RAG generation is enabled in this slice.

### Tests and verification

Use disposable PostgreSQL18 with a genuinely unprivileged runtime role,
column-minimal authority permissions, actual RLS, and fictitious data. Test:

1. Full HTTP login start/callback with nonce-signed test IdP transport, then
   current identity, consent notice/grant, own create/get/list/turns.
2. Two adult patients and a second institution; guessed conversation IDs and
   injected patient/tenant/user/role fields never cross isolation boundaries.
3. No grant, withdrawal, expired/latest-version mismatch deny content;
   regrant works and retains event history. No extra purpose is granted.
4. Admin/support/platform role without self-patient permission denied;
   unverified binding/identity, all minor and unknown ages denied.
5. Account/tenant disable, role/binding changes, session logout/expiry deny
   next access. Include real patient-lock wait tests for withdrawal,
   verification changes and authority changes, with deterministic coordination.
6. Strict payload, malformed ID, wrong/duplicate CSRF, wrong Origin and
   unexpected DB exception produce static envelopes with no sensitive echo.
7. Old routes absent on new app and still locked on legacy app.

Run focused RED before implementation, GREEN after, covering existing session
and OIDC tests as regressions. Target >80% coverage for new modules, and run
ruff and git diff --check. Do not rerun the whole unrelated legacy suite after
each edit. Report exact commands and output, not estimated pass counts.

Write full implementer report to docs/security/patient-vertical-report.md:
TDD evidence, files, commands/results, authority/lock semantics and remaining
limitations. Return only status and concise test/concern summary to controller.

## Follow-on tasks

2. Independently review Task1 against this brief, fix findings, re-review.
3. Add a trusted startup factory and minimal same-origin patient/identity UI
   for this actual API (no mock identity, no local tokens). Reuse the deployed
   HTTPS origin and provide institutional config guidance. Independently test
   and review. Role pages beyond identity must wait for actual backend scopes.
4. Continue authenticated audited institution workflows and worker isolation;
   hospital protocols and verified guardian rules require institution input.
   Production acceptance remains a separate evidence-backed gate.
