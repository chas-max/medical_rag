# Signed provider logout implementation report

Date:2026-11-02. Implementation and self-review complete; independent
specification and quality review remain required before task acceptance.
Fictional data/test IdP only. No real provider registration, hospital call,
identity proofing, penetration test, production/clinical/legal approval or
universal IdP interoperability is claimed.

## Implemented boundary

The mandatory-audited protected application connects exact
`POST /auth/backchannel-logout` to current SessionStore revocation and durable
pending-login fences. No-cookie/no-Origin server POST is permitted only on
this exact route, under the existing exact HTTPS Host. A supplied Origin must
be the trusted application Origin once. No cookie is read, set or cleared by
this endpoint; forwarded identity does not supply authority. Experimental
headless auth without AuditStore returns503 before accepting provider
authority. Other browser auth/patient mutation protections are unchanged.

`provider_logout.py` performs bounded UTF-8 URL-encoded form parsing (32768
bytes, exactly one token of at most16384 characters, no duplicate keys/query,
strict percent escapes/media/encoding). Bounded unknown fields are ignored,
including standard bare flags. Unknown custom JWT extensions are ignored;
registered `nbf` remains subject to PyJWT validation and, if present, must be
an exact integer. This follows the controller's explicit registered-claim
clarification, not a bypass of mature JWT validation.

The verifier uses the same configured issuer/client/pinned RSA key as ID
tokens, RS256 only, strict audience string and explicit logout+jwt type. It
rejects critical/remote/embedded key-selection headers, nonce, malformed
present target identifiers and invalid timestamps/events. A bounded simple
kid is a label only, never a key lookup. ID and logout token types cannot be
interchanged. Errors are static400/503 envelopes with no target counts or
existence details; protected-app responses retain server request UUIDs.
Controller clarification explicitly approves rejecting control characters
and UTF-8 surrogate code points in sub as well as sid/jti as part of this
bounded strict provider profile. Interior NUL/control and surrogate subjects
have explicit400 tests; they never reach PostgreSQL as invalid text.

VerifiedIdentity adds optional provider_session_id/authenticated_at with
backward-compatible defaults and hidden representations. Actual ID-token
verification fills authenticated_at from verified auth_time and validates a
present sid. Sessions persist only sid's SHA256 UTF-8 digest, never raw sid.

Migration6 adds append-only provider_logout_events with generated UUID/DB
receipt time, trusted institution/issuer, SHA256 jti/sub/sid, verified iat/exp,
and unique institution+issuer+jti hash. No token, raw sub/sid/jti, patient or
resource ID, or free-form JSON is stored. Forced institution RLS uses an
assigned transaction ID bound to the trusted tenant scope; SQL also has
explicit tenant predicates. Runtime receives SELECT and column-minimal
INSERT only; event UUID/time and all mutation/truncation remain denied.
Owner-run migration and grant instructions are in
`docs/security/provider-logout-setup.md`; no migration was applied outside
disposable test databases.

## Transaction, replay, and audit semantics

ALL SessionStore.issue calls and provider fence insertions acquire the same
deterministic per-institution PostgreSQL transaction advisory lock. They use
an independent primary READ COMMITTED transaction, existing3s lock/5s
statement deadlines, and a fresh fence statement after acquiring the lock.
Token exchange does not hold this lock. Schema6 SELECT/column requirements
also apply to headless SessionStore transactions, with no compatibility
switch that bypasses fences.

First receipt atomically inserts the fence and revokes exact issuer/account
subject and/or sid-hash matches. Both claims mean conjunction. Only sessions
whose verified auth_time is no later than event iat are revoked; legacy NULL
counts as older. Local account/institution active state, roles or MFA do not
constrain signed provider revocation. Missing targets are successful no-ops.
Issuance matching a fence rejects unknown auth_time or auth_time<=fence iat;
strictly later verified authentication may issue. Sid-only fences work before
any local matching session exists. Both advisory-lock orders and a genuinely
paused HTTP code exchange are tested on actual PostgreSQL.

Identical committed jti replay returns before any second UPDATE. Different
target/iat/expiry reuse is400. Fences are never deleted on JWT expiry; retention
requires a separately reviewed policy. A repeated token must still meet its
strict validity window, even when its event is already persisted.

Provider revocation/fence commits BEFORE the audit transaction. Audit INSERT
loss returns503 but cannot restore sessions or remove fences. Identical retry
after audit recovery is safe and auditable. Storage failure before commit
returns503, never a success. Each protected HTTP attempt uses the existing
mandatory request context, fixed action provider.logout/resource session,
null operator (external provider authority), static outcome/reason enums and
one server-generated request UUID. Neither submitted cookies nor raw provider
claims/tokens enter audit or public errors/logs.

## TDD evidence and verification

All Python integration commands used PowerShell, worktree root,
`.venv/Scripts/python.exe`, `$env:PYTHONUTF8='1'` and
`$env:RUN_POSTGRES_INTEGRATION='1'`. Docker server version was29.8.0;
fixtures launched real disposable `postgres:18.6-alpine` with unprivileged
runtime roles. Worktree-local --basetemp avoided system-temp permissions.
No test below skipped PostgreSQL. Known warnings are Starlette TestClient's
httpx deprecation and Authlib's httpx migration notice, not application errors.

Initial exact RED:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_provider_logout.py -q --basetemp outputs/provider-logout-pytest-red
```

Result:3 failed,2 warnings,7.16s. No-Origin signed provider POST was403 and the
audit-outage attempt did not revoke the old session. This was before the new
module, migration or integration code. First GREEN with the same file plus
`--tb=short --basetemp outputs/provider-logout-pytest-green1`:3 passed,2
warnings,7.43s.

Subsequent exact RED commands all used `-q --tb=short`:

| Test selection / basetemp under outputs | Observed result before corresponding change |
| --- | --- |
| `tests/test_provider_logout.py`, `provider-logout-pytest-red2` | 1 failed,63 passed,2 setup errors,2 warnings,19.55s. Unknown-claim behavior was exposed; huge auto-generated parameter IDs also hit Windows path limits. Short stable IDs fixed test setup. Controller later clarified nbf is registered, not unknown. |
| `tests/test_security_server.py -k 'schema6_preflight or owner_grant_recipe'`, `provider-logout-pytest-red3` | 8 failed,1 passed,40 deselected,2 warnings,7.82s. Missing schema6/broad grants were not checked; old owner recipe lacked new session columns. |
| `tests/test_provider_logout.py -k 'unencodable or standard_urlencoded'`, `provider-logout-pytest-red4` | 4 failed,70 deselected,2 warnings,6.96s. Surrogate identifiers gave503; standard bounded unknown form flags were rejected. |
| `tests/test_provider_logout.py -k 'headless_unconfigured'`, `provider-logout-pytest-red5b` | 1 failed,74 deselected,2 warnings,6.10s. Experimental headless accepted unaudited provider revocation. An earlier test setup used conftest's wrong default Origin; explicit empty headers corrected that before accepting this RED. |
| `tests/test_provider_logout.py -k 'registered_nbf'`, `provider-logout-pytest-red6` | 5 failed,75 deselected,2 warnings,7.47s. Added after controller clarification, before restoring standard not-before checks plus exact integer validation. |
| `tests/test_provider_logout.py -k 'past_nbf or direct_verifier or correlation_is_verified'`, `provider-logout-pytest-red7` | 1 failed,2 passed,80 deselected,2 warnings,7.53s. Malformed media token with an embedded space was accepted; parser now preserves token syntax. |

Intermediate GREEN: provider+startup files114 passed/2 warnings/30.93s;
first affected regression363 passed/2 warnings/113.64s with98.33% module
coverage. Later provider-only GREEN80 passed/2 warnings/29.32s with98.33%.

Final exact affected regression:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_provider_logout.py tests/test_identity_sessions.py tests/test_oidc_identity.py tests/test_oidc_login.py tests/test_patient_vertical.py tests/test_security_audit.py tests/test_security_server.py tests/test_security_lockdown.py -q --tb=short --cov=medical_rag.provider_logout --cov-report=term-missing --cov-fail-under=81 --basetemp outputs/provider-logout-pytest-regression2
```

Result:376 passed,2 warnings,112.48s. provider_logout.py61 statements,
0 missed,100.00% coverage. This includes full HTTP login/consent/content/
provider logout, old-cookie rejection, actual fresh HTTP reauthentication,
sub/sid/both conjunction, cross institution/issuer isolation, disabled local
authority, legacy NULL cutoffs, unknown auth-time fences, conflicting replay,
later session survival, hostile claims/headers/body/transport, current
authority/patient locking regressions, audit-loss persistence/retry, storage
failure, owner/runtime append-only checks, RLS/pool scope, schema6 startup and
least-privilege owner-recipe operations. Existing recovery-primary/startup and
legacy lockdown tests were retained and passed.

After the controller's explicit subject-control clarification, no application
source changed. Two explicit interior-control sub cases were added, and the
final provider suite was rerun:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_provider_logout.py -q --tb=short --cov=medical_rag.provider_logout --cov-report=term-missing --cov-fail-under=81 --basetemp outputs/provider-logout-pytest-final
```

Result:85 passed,2 warnings,22.52s;61 statements/0 missed/100.00% coverage.
Scoped Ruff and git diff --check were then rerun successfully. Application
source is frozen for independent review.

Additional commands/results:

```powershell
.venv/Scripts/python.exe -m ruff check src/medical_rag/provider_logout.py src/medical_rag/identity_sessions.py src/medical_rag/oidc_identity.py src/medical_rag/oidc_login.py src/medical_rag/security_audit.py src/medical_rag/security_server.py tests/test_provider_logout.py tests/test_identity_sessions.py tests/test_oidc_login.py tests/test_patient_vertical.py tests/test_security_server.py
git diff --check
```

Both exit0; scoped Ruff:All checks passed. Git emitted only pre-existing
frontend LF/CRLF notices. Broad `.venv/Scripts/python.exe -m ruff check src
tests --output-format concise` exits1 with41 findings in untouched legacy
evaluation/service/store/tools/workflow and their tests. None is in changed
provider/auth/session/patient/startup files; these were not refactored as part
of this task. Full-repository Ruff is therefore NOT claimed green.

From frontend:

- `npm test`:18 files/199 tests passed,65.83s, exit0.
- `npm run lint`:TypeScript+ESLint exit0.
- `npm run build`:exit0,63 transformed modules, Vite3.00s.
- `npm run build:patient`:exit0,33 transformed modules, Vite936ms.

## Changed files (this task only)

- New `src/medical_rag/provider_logout.py`.
- New `database/migrations/0006_provider_logout.sql`.
- New `tests/test_provider_logout.py`.
- `src/medical_rag/identity_sessions.py`:mandatory schema6, shared advisory
  serialization, fresh issuance fence query, correlation columns and atomic
  append/revoke/replay handling.
- `src/medical_rag/oidc_identity.py`:verified sid/auth-time correlation and
  ID/logout type separation.
- `src/medical_rag/oidc_login.py`:same pinned verifier configuration, exact
  transport exception, bounded provider route, audit-after-commit integration.
- `src/medical_rag/security_audit.py`:fixed provider action/session resource and
  request classification; existing mandatory audit mechanism retained.
- `src/medical_rag/security_server.py`:schema6 columns/grants/RLS/denied
  mutation preflight extension.
- `database/patient_runtime_grants.sql`:new column-minimal grants.
- `tests/test_identity_sessions.py`, `tests/test_oidc_login.py`,
  `tests/test_patient_vertical.py`:mandatory schema6 disposable fixtures,
  correct new table grants, owner-only append-history test resets.
- `tests/test_security_server.py`:schema6 missing/broad-authority checks.
- `deploy/patient/config.example.toml`:owner-provisioned migration range1-6.
- New `docs/security/provider-logout-setup.md` and this report.
- `docs/security/provider-logout-brief.md`:controller-approved explicit
  subject-control/surrogate constraint clarification only (the controller
  independently clarified registered nbf behavior).

No commits/staging, `.env` reads/edits, original D: checkout changes, legacy
unlocks, external provider registration/calls or privileged non-test role
creation occurred. Existing output baseline snapshots and replaced__ files
were untouched. README/progress/readiness remain controller-owned.

## Self-review and remaining limitations

Reviewed route specificity/cookie independence, pinned trust/type separation,
private-data boundaries, append-only RLS/least grants, BOTH lock orders/fresh
statements, replay no-second-UPDATE, auth_time cutoff, audit outage commit
ordering and mandatory source/factory audit preservation. The module remains
focused; SQL session authority lives in the existing SessionStore.

The webhook stops the next protected request, not in-flight operations or an
idle page via push. Real IdP registration/profile conformance and pinned-key
rotation are not implemented or certified. No missing historical sid is
fabricated. Equal-second auth remains denied intentionally. Fence retention
and operational capacity/DoS controls need separate reviewed policy; this is
not owner-proof audit storage. Runtime remains trusted for session integrity
and must not execute arbitrary user SQL. Broad repository Ruff has the41
untouched findings described above. Independent spec+quality review is the
remaining task-acceptance gate.
