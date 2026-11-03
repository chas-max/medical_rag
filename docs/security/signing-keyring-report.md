# Task 7 implementer report — operator-approved signing-key overlap

Date: 2026-11-03. Status: implementation verified; independent specification
and quality review remain controller gates. No commits or staging performed.
Production source/config/runbook freeze: 2026-11-03T13:16:54+08:00.
Fixture/test source freeze: 2026-11-03T13:19:16+08:00. All source/test/config
hashes below are final. Final provider result is recorded below.

## Scope and behavior

`SigningKeys` copies and freezes one approved 1..8-entry RSA public-key ring,
or retains one legacy pinned RSA public key, never both/neither. Keys must be
actual RSA public objects of at least 2048 bits, unique by public numbers;
names are exact `[A-Za-z0-9_-]{1,128}` identifiers. It validates bounded JWT
protected headers, including duplicate-field rejection, and selects exactly
one local key. PyJWT still performs RS256 cryptographic and claims validation.
No token-supplied URLs, JWKS discovery, network key fetch, trial verification,
fallback, algorithm negotiation, private signing keys or application key
generation were introduced. Legacy optional bounded `kid` labels still work.

ID-token and strict provider-logout verification share this helper. Existing
issuer/audience/nonce/time/age/type/events checks remain. Embedded/remote key
headers and `crit` are rejected for both purposes. Local current-authority
checks, audit ordering, durable pending-login fences and replay semantics were
not relaxed. The touched ID verifier documentation now describes its connected
isolated-app role, without claiming production approval.

The strict trusted TOML loader accepts exactly one `public_key_file` or
`verification_key_files` mapping; checks complete ring bounds/names before
opening listed files; resolves relative paths from TOML; bounds each public
file at 65536 bytes; uses cryptography PEM parsing; and keeps all startup
errors static. Configuration rings are frozen and omitted from repr. Existing
operator-only provider CA configuration is preserved. Example comments and
the runbook describe deploy/reload of every verifier, independently approved
public material/fingerprints, deliberate provider switch and retirement, no
universal overlap interval, and separate compromise/session-revocation work.

## Actual provider boundary and operator authority

The official cached Keycloak image was
`quay.io/keycloak/keycloak:26.8.1@sha256:3f3c5f8e03c614774b4bd9173b88391fefa4153bfc010d8012d59ad825377332`.
The fixture also uses actual `postgres:18.6-alpine` and
`python:3.13-alpine` collector containers. Provider HTTPX/Authlib traffic uses
actual verified HTTPS sockets with the generated private CA, no `verify=False`,
environment proxy/trust overrides or global trust mutation. The patient app
HTTP boundary uses FastAPI TestClient at its exact HTTPS origin; this is not a
full browser/full-socket application end-to-end test. The PG-only HTTP suite
uses actual least-privilege PostgreSQL but a clearly synthetic signed IdP
transport. Actual-provider rotation never substitutes local JWT signatures.

The separate fictional `fixture-operator` confidential client uses standard
authorization-code/S256 PKCE and the exact HTTPS callback
`https://fixture-operator.invalid/callback`. Implicit, password/direct grant
and service-account flows are disabled for both patient/operator clients.
Only the disposable-realm `realm-management/manage-realm` role is assigned;
no realm-admin, master bootstrap identity or fake admin bearer is used.
The operator is kept out of `Runtime.users`, so the patient provisioning loop
never creates a local patient account for it. The native bounded form driver
and mature Authlib exchange obtain the real bearer in process memory only.

Bounded admin REST calls are restricted to that fixture realm's
`/admin/realms/<unique-fictional-realm>/components` and exact validated UUID
component subpaths: POST creates a uniquely named passive RSA/RS256 provider,
GET lists/inspects, PUT activates it, DELETE removes only the exact tracked
created ID/name/type/parent. Administrative Location headers are never followed.
The separate rotation provider fixture prevents active-key contamination of
the existing-provider regression fixture. Cleanup verifies ownership afresh;
even a caller mutation of the yielded component cannot redirect cleanup.

Primary pinned-version privilege sources inspected on 2026-11-03:
[ComponentResource.java](https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/services/src/main/java/org/keycloak/services/resources/admin/ComponentResource.java)
requires view-realm for inspection and manage-realm for mutation;
[RealmPermissions.java](https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/services/src/main/java/org/keycloak/services/resources/admin/fgap/RealmPermissions.java)
allows manage-realm to satisfy that view check. These checks authorize only
the bounded fictional-fixture operations above, not production administration.

Rotation test sequence:

1. Capture an actual old-key patient ID token and its original nonce by real
   code/S256 exchange; its normal app verifier first accepts the token.
2. Create a **passive** second RSA signing component, enroll only RSA/RS256/sig
   public JWKS entries over verified TLS into a manually constructed overlap
   app. This is fixture trust enrollment, not application discovery.
3. Actual old-key HTTP login succeeds; real provider logout is captured,
   proves the old actual `kid`, and revokes the local session via HTTP.
4. PUT switches the created provider active. Actual new-key HTTP login and
   actual provider logout succeed; the logout proves a different actual `kid`
   present in the overlap set and revokes the new local session.
5. The **same unexpired actual old ID token**, same original nonce and correct
   patient audience, passes the overlap app verifier immediately before
   removal, then fails a newly constructed current-only app verifier. This
   retirement leg is deliberately verifier-level: callbacks accept codes,
   not arbitrary ID tokens; no public bearer/token-verification endpoint exists.
   Actual current-only HTTP authentication still succeeds separately.

The fixture now serializes generated realm credentials only into a bounded
Docker stdin payload and the uniquely owned container's volatile tmpfs, then
atomically exposes the import file there. There is no host `realm.json`
writer/mount. Keycloak necessarily imports fictional credentials internally;
access/ID tokens remain process-memory-only. Focused tests cover bounds,
transport/exit failures with static errors and no credential-bearing command
arguments; the actual rotation also asserts absence of a host realm artifact.
Existing fixture TLS certificate files are test-only generated TLS material,
not upstream identity signing substitutes.

Task6 cleanup follow-up: every separately owned container and network is
attempted despite another inspect/remove failure; unverifiable or unowned
targets are never removed. An original failure survives with only the fixed
aggregated note `Disposable identity cleanup failed`; cleanup-only failure is
static. Sixteen added ownership/failure/original-failure combinations cover
container and network targets; the module also retains four earlier tests and
adds four bounded-import tests (24 total).

Four obsolete **fictional Task7 fixture credential import artifacts**, not
patient data or source snapshots, were permanently removed after exact absolute
workspace-target validation when the memory/tmpfs clarification was approved:

- `outputs/task7-rotation-red/test_actual_operator_rotation_0/realm.json`
- `outputs/task7-rotation-green1/test_actual_operator_rotation_0/realm.json`
- `outputs/task7-kc-final1/actual-keycloak0/realm.json`
- `outputs/task7-kc-final1/test_actual_operator_rotation_0/realm.json`

## TDD evidence and verification commands

All Python commands below ran from this isolated worktree in PowerShell with
`$env:PYTHONUTF8='1'` and the explicit `./.venv/Scripts/python.exe` (Python
3.13.9). Every pytest basetemp is inside this worktree's `outputs/` directory.
Actual-PG commands additionally set `$env:RUN_POSTGRES_INTEGRATION='1'`;
actual-provider commands set `$env:RUN_KEYCLOAK_INTEGRATION='1'`.
Actual-provider infrastructure was present; selected runs did not mock/skip it.

Authoritative RED commands before the corresponding implementation:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_signing_keys.py -q --tb=no --basetemp=outputs/task7-pytest-red2
# 45 failed, 11 passed, 1 dependency warning: missing verification_keys interface
# and missing ID protected-header restrictions.
./.venv/Scripts/python.exe -m pytest tests/test_signing_key_config.py -q --tb=short --basetemp=outputs/task7-pytest-config-red2
# 1 failed, 14 passed, 2 dependency warnings: approved ring rejected at startup.
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_runtime.py -q --tb=short --basetemp=outputs/task7-cleanup-red
# 8 failed, 4 passed, 1 dependency warning: cleanup_owned absent.
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_rotation.py -q --tb=short --basetemp=outputs/task7-rotation-red
# Actual infrastructure ran; 1 failed, 2 warnings, 34.89s: operator helper absent.
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_operator.py -q --tb=short --basetemp=outputs/task7-operator-red
# 1 failed, 1 warning: operator helper absent.
./.venv/Scripts/python.exe -m pytest tests/test_signing_keys.py -q --tb=short --basetemp=outputs/task7-selector-red
# 4 failed, 64 passed, 1 warning: shared selector parsed before local input bound.
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_operator.py -q --tb=short --basetemp=outputs/task7-operator-ownership-red
# 1 failed, 1 passed, 2 warnings: preexisting lookalike component not rejected.
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_runtime.py -q --tb=short --basetemp=outputs/task7-import-red
# 4 failed, 20 passed, 1 warning: memory/tmpfs import helper absent.
```

Initial test-harness attempts are not claimed as clean feature evidence:
`task7-pytest-red1` used PyJWT's encoder for malformed `kid` values (47 failures,
9 passes); malformed-header signing was corrected in test code before the
authoritative RED above. `task7-pytest-config-red` also hit Windows long
parameter-generated path errors (1 failure, 13 passes, 2 errors); bounded static
parameter IDs were substituted before its authoritative RED. No application
implementation was justified by those harness errors.

GREEN progression:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_signing_keys.py tests/test_oidc_identity.py -q --tb=short --basetemp=outputs/task7-pytest-green1
# 97 passed, 1 warning, 1.49s.
./.venv/Scripts/python.exe -m pytest tests/test_signing_keys.py tests/test_signing_key_config.py tests/test_oidc_identity.py tests/test_security_server.py -q --tb=short --basetemp=outputs/task7-pytest-green2
# 147 passed, 14 opt-in PG skips, 2 warnings, 4.30s. Later actual-PG run below includes these tests.
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_runtime.py -q --tb=short --basetemp=outputs/task7-cleanup-green
# 12 passed, 1 warning, 0.68s (before network/import test expansion).
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_operator.py tests/test_keycloak_rotation.py -q --tb=short --basetemp=outputs/task7-rotation-green1
# 2 passed, 2 warnings, 71.58s, actual provider rotation.
./.venv/Scripts/python.exe -m pytest tests/test_signing_keys.py -q --tb=short --cov=medical_rag.signing_keys --cov-report=term-missing --basetemp=outputs/task7-selector-green
# 68 passed, 1 warning, 1.14s; helper 98% (config-handoff property covered in combined run).
./.venv/Scripts/python.exe -m pytest tests/test_signing_key_http.py tests/test_oidc_login.py tests/test_provider_logout.py tests/test_security_server.py -q --tb=short --basetemp=outputs/task7-pg-green1
# 188 passed, 2 warnings, 50.28s; actual PG, ring and legacy HTTP regressions.
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_operator.py tests/test_signing_keys.py tests/test_signing_key_config.py tests/test_keycloak_runtime.py -q --tb=short --basetemp=outputs/task7-unit-green
# 97 passed, 2 warnings, 2.64s.
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_runtime.py -q --tb=short --basetemp=outputs/task7-import-green
# 24 passed, 1 warning, 0.58s.
```

An expanded PG coverage run (`task7-pg-final`, same arguments as final2 below)
exposed two new-test timing assumptions: 325 passed, 2 failed, 2 warnings,
53.95s; helper coverage 100%. The fresh-login test waited past the *initial*
authentication second, but the later logout could be issued in a newer second
under coverage overhead. The unchanged real fence correctly denied that
authentication. Test-only waits now use the verified logout event's exact
`issued_at`; pending-exchange test received the same correction. No fence or
verification source was changed to make these tests pass.

Final actual-PG/helper regression command:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_signing_keys.py tests/test_signing_key_config.py tests/test_signing_key_http.py tests/test_oidc_identity.py tests/test_oidc_login.py tests/test_provider_logout.py tests/test_security_server.py tests/test_provider_tls.py -q --tb=short --cov=medical_rag.signing_keys --cov-report=term-missing --basetemp=outputs/task7-pg-final2
# 327 passed, 2 known dependency warnings, 57.52s; signing_keys.py 50 statements, 0 misses, 100%.
```

The earlier full affected actual-provider regression command used
`--cov=tests/keycloak_operator.py` incorrectly (file path instead of importable
module). It still ran all actual scenarios: 33 passed, 2 dependency warnings,
156.27s, signing helper 84% for provider-only paths, plus a separate explicit
`CoverageWarning: Module tests/keycloak_operator.py was never imported`.
No fixture-helper coverage claim is based on that attempt. Corrected final
provider/module coverage command and result:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_identity.py tests/test_keycloak_rotation.py tests/test_keycloak_operator.py tests/test_keycloak_runtime.py -q --tb=short --cov=medical_rag.signing_keys --cov=keycloak_operator --cov=keycloak_runtime --cov-report=term-missing --basetemp=outputs/task7-kc-final2
# 37 passed, 2 known dependency warnings, 145.37s; no skips/failures.
# signing_keys 84% for actual-provider-only paths (100% in the combined PG/helper run).
# keycloak_operator: 134 statements, 19 misses, 86%; keycloak_runtime: 159 statements, 4 misses, 97%.
# Combined three-module coverage: 343 statements, 31 misses, 91%.
```

Scoped lint, run from worktree:

```powershell
./.venv/Scripts/python.exe -m ruff check src/medical_rag/signing_keys.py src/medical_rag/oidc_identity.py src/medical_rag/provider_logout.py src/medical_rag/oidc_login.py src/medical_rag/security_config.py tests/test_signing_keys.py tests/test_signing_key_config.py tests/test_signing_key_http.py tests/keycloak_runtime.py tests/keycloak_operator.py tests/test_keycloak_runtime.py tests/test_keycloak_operator.py tests/test_keycloak_rotation.py tests/test_keycloak_identity.py
# All checks passed. Import formatting was mechanically corrected with scoped --fix.
git diff --check
# Exit 0. Existing frontend eslint.config.mjs/package.json/tsconfig.json CRLF notices remain.
```

Frontend commands from `frontend/` (no frontend source was changed by Task7):

```powershell
npm test
# 18 test files, 199 tests passed, 28.50s.
npm run lint
# tsc --noEmit && eslint .; exit 0.
npm run build
# tsc + Vite; 63 modules; exit 0, build 1.09s.
npm run build:patient
# tsc + patient Vite config; 33 modules; exit 0, build 705ms.
```

Subsequent changes were fixture/test-only, not frontend source. Known Python
dependency warnings are the Starlette HTTPX deprecation and Authlib HTTPX
deprecation; neither was suppressed or represented as pristine output. The
previously documented 41 unrelated legacy Ruff findings remain untouched;
the controller owns the one broad legacy regression run after source freeze.

## Changed files and SHA-256

Existing files modified: `src/medical_rag/oidc_identity.py`,
`src/medical_rag/provider_logout.py`, `src/medical_rag/oidc_login.py`,
`src/medical_rag/security_config.py`, `tests/keycloak_runtime.py`,
`tests/test_keycloak_runtime.py`, `tests/test_keycloak_identity.py`,
`deploy/patient/config.example.toml`.

New files: `src/medical_rag/signing_keys.py`, `tests/test_signing_keys.py`,
`tests/test_signing_key_config.py`, `tests/test_signing_key_http.py`,
`tests/keycloak_operator.py`, `tests/test_keycloak_operator.py`,
`tests/test_keycloak_rotation.py`, `docs/security/signing-keyring-runbook.md`,
this report. No existing file outside the controller's original 20-source
baseline set was modified. All original source snapshots were preserved.
README/progress/readiness/review-package and brief clarification belong to
the controller, not this implementer.

SHA-256 (report itself excluded to avoid a self-referential digest):

```text
src/medical_rag/signing_keys.py c049104015f45ecbf2ed833ace313c9723985b6f5999914b3f3c36b7e8073f6f
src/medical_rag/oidc_identity.py 9d036905d3c1194160137e2ad959a926065b380390e8803868014d1c08a6b529
src/medical_rag/provider_logout.py 1311d39c558dc5378e09b1795dcc85efe2d886a23282bef8112ee87b898443af
src/medical_rag/oidc_login.py 2fcfa21929431bd2a186215017db45b0906f5d05af3aa14d199b34513e84b405
src/medical_rag/security_config.py 06e67ca8fa734a765473b078b031de74294f3a2b2e16de132fe4126cdd95ead0
tests/test_signing_keys.py d1b3f5b05af694c35825b18467074ea1f6178d10faded21a9eb9a4bb76fb15a7
tests/test_signing_key_config.py e4be6b8efe59506ecebd3ac0fe76e0a39a2733f44eac64ac07af0adbae058146
tests/test_signing_key_http.py 08b8e4b8e663af0bfc5fc546d5642211d48422c2c6f5f9dcc773ce068f557f06
tests/keycloak_runtime.py bad358cf152b7d4f711884206a17ffbb7f03ed41dc38fb4bfd400580bf09f64c
tests/keycloak_operator.py 8014f2d500c6b6dde8354c692557972dfd529138c3870720308031b79a622b7b
tests/test_keycloak_runtime.py 194ab4e6d0888b7b6afbd7fbdc2fe37af4dd1c3f708095a49bbc09b548688b7c
tests/test_keycloak_operator.py 821264b059381ff9b58a2679e0cfee50f47fb1c8328523824d98fdb5572bdd1a
tests/test_keycloak_rotation.py a82dcddae1b5abdaec9675b2768df1b97c57a9a76b661b785659bfda6ebf05fd
tests/test_keycloak_identity.py 0f4110a673561365c8084021297e30c575480034451e16f89034de5539466c65
deploy/patient/config.example.toml 11f5aaf73b1e2bf0ee300020562b46dd6e4e03121824446f21ccfd35768c7e67
docs/security/signing-keyring-runbook.md 411fe7fad3fd6d4d51d6d04c64ce8ea33d4e16d6ba0a0f3d46b8669e16b1d473
```

## Limits and handoff

This is routine overlap, not emergency compromise response, automatic discovery,
client-secret/login-encryption-key rotation, real-institution onboarding, MFA
certification, clinical safety, legal acceptance or production medical-data
approval. Trust removal does not revoke already-issued opaque local sessions;
institution/session-version revocation remains separate. All provider/PG
resources were unique disposable fictional fixtures; existing RabbitMQ was
preserved. No `.env`, original D: checkout, real account/system/message,
production setting or legacy unlock was touched. Required independent reviews
must still approve this frozen implementation before the controller declares
Task7 complete.

## Review I1 follow-up — test-only failure-diagnostic privacy

Completed verification 2026-11-03T15:28:52+08:00. The two test sources below
are frozen at their final hashes. This follow-up addresses only Important I1
in `signing-keyring-review-report.md`; that review report was not edited.
Production trust/configuration code, verifier semantics, fixture authority,
and all existing token/nonce/retirement checks are unchanged.

Before editing, `tests/test_keycloak_rotation.py` was snapshotted through
`apply_patch` to
`outputs/task7-privacy-baseline-20261103/test_keycloak_rotation.py`.
Both original source and snapshot had exactly the same SHA-256:
`a82dcddae1b5abdaec9675b2768df1b97c57a9a76b661b785659bfda6ebf05fd`.
This snapshot contains test source only, no runtime credentials.

### Fix and privacy-check contract

All 16 assertions in the actual rotation scenario now assert only boolean
locals with fixed messages. Sensitive comparisons, verifier calls and HTTP
operations occur before the rewritten assertion; no ring, header, token,
nonce, body or response operand appears in a rewritten assertion expression.
The extra indentation in the operator context was corrected without changing
the scenario order or its same-token/same-nonce retirement proof.

`tests/test_keycloak_rotation_privacy.py` reads the actual scenario AST without
importing or executing its real-provider fixture. It enforces the boolean-local
and static-message shape across all 16 assertions. Six negative cases extract
the actual assertion and, after the fix, its immediately preceding actual
boolean computation. They run that source through this environment's pytest
assertion rewriter using only synthetic ring material, old/new header IDs,
a synthetic JWT/token marker and nonce. The rendered short failure diagnostic
uses `showlocals=False`, matching pytest's ordinary no-local-dump diagnostics.
Each check must actually fail, retain its actual fixed message and `assert False`,
and exclude every canary. This checks real assertion source rather than an
unrelated safe demonstration; reverting the rotation source makes it RED.
The scoped `S102` annotation permits only this checked-in-AST execution with
synthetic inputs, not untrusted code or any real fixture credentials.

Concrete count/header/membership/key-set diagnostics are covered. Two cases
explicitly label false-returning verifier stubs as defensive hardening only:
the real verifier returns a truthy verified identity or raises static
`InvalidIdentity`. This follow-up does not claim a reachable real-token leak
from the existing verifier assertions. No actual credentials were deliberately
made to fail, and no synthetic signatures replaced the actual-provider run.

### Exact RED/GREEN evidence

Commands ran in this worktree with `./.venv/Scripts/python.exe` and
`$env:PYTHONUTF8='1'`; all pytest basetemps stayed under `outputs/`.

```powershell
$env:PYTHONUTF8='1'
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_rotation_privacy.py -q --tb=short --basetemp=outputs/task7-privacy-red-20261103
# Before any rotation-source edit: exit 1; 7 failed, 1 known warning, 1.24s.
# One assertion-shape failure and six rendered-diagnostic canary failures.

./.venv/Scripts/python.exe -m pytest tests/test_keycloak_rotation_privacy.py -q --tb=short --basetemp=outputs/task7-privacy-green-20261103
# After boolean/static fix: exit 0; 7 passed, 1 known warning, 2.71s.
```

The regression was then strengthened to require the actual fixed message and
`assert False`, and to check both the complete synthetic token and its payload
marker. Its final version was also run against the byte-identical baseline
snapshot, without reverting or executing the actual rotation source:

```powershell
./.venv/Scripts/python.exe -c "import sys; from pathlib import Path; sys.path.insert(0, 'tests'); import test_keycloak_rotation_privacy as privacy; import pytest; privacy.ROTATION_SOURCE = Path('outputs/task7-privacy-baseline-20261103/test_keycloak_rotation.py'); raise SystemExit(pytest.main(['tests/test_keycloak_rotation_privacy.py', '-q', '--tb=short', '--basetemp=outputs/task7-privacy-final-harness-baseline-red-20261103']))"
# Final harness / original source: exit 1; 7 failed, 1 known warning, 2.52s.
# The same six canary failures remain; no harness errors.

$env:RUN_KEYCLOAK_INTEGRATION='1'
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_rotation_privacy.py tests/test_keycloak_rotation.py tests/test_signing_keys.py -q --tb=short --basetemp=outputs/task7-privacy-provider-green-20261103
# Final source: exit 0; 76 passed, 2 known warnings, 88.66s; no skips/failures.
# 7 privacy checks + 1 actual-provider rotation + 68 signing-key regressions.

./.venv/Scripts/python.exe -m ruff check tests/test_keycloak_rotation.py tests/test_keycloak_rotation_privacy.py
# Exit 0; All checks passed!

git diff --check -- tests/test_keycloak_rotation.py tests/test_keycloak_rotation_privacy.py docs/security/signing-keyring-report.md
# Exit 0. The source files are already untracked in this task checkout, so
# scoped Ruff and their explicit hashes also verify the new-file content.
```

An initial scoped Ruff run flagged only `S102` for the intentional synthetic
assertion-rewrite execution; the narrow explanatory annotation above resolved
it. No dependency warning was suppressed. The privacy-only runs show the known
Starlette/HTTPX warning; the actual-provider run also shows known Authlib/HTTPX
deprecation debt. No broad legacy/frontend suite was repeated for this test-only
fix; earlier broad evidence and its limitations remain as recorded above.

The provider rerun used the already-cached official pinned image
`quay.io/keycloak/keycloak:26.8.1@sha256:3f3c5f8e03c614774b4bd9173b88391fefa4153bfc010d8012d59ad825377332`
and the existing uniquely owned disposable fictional provider/PG/collector
fixtures. Verified TLS sockets, real native code/S256 exchange, real upstream
signatures and TestClient application boundary are unchanged. After cleanup,
`docker ps` showed only the same pre-existing RabbitMQ container
`7ed6d01d2567` / `medical-rag-resume-rabbitmq-1`; it was not modified. No host
realm-import file was generated. The actual run's only generated files are
fixture TLS `ca.pem`, `server.pem`, and `server.key` in
`outputs/task7-privacy-provider-green-20261103/test_actual_operator_rotation_0/`.

### Incremental file list and final source freeze

Existing files modified by this follow-up:

- `tests/test_keycloak_rotation.py`
- `docs/security/signing-keyring-report.md` (append-only evidence)

Exact newly authored files in this follow-up:

- `tests/test_keycloak_rotation_privacy.py`
- `outputs/task7-privacy-baseline-20261103/test_keycloak_rotation.py` (source snapshot)

Final test SHA-256, verified again at 2026-11-03T15:28:52+08:00:

```text
tests/test_keycloak_rotation.py bfdfd2899937f1ead16765ed77ab14bbf485c8e7475df401ce0ee4cabdf5d472
tests/test_keycloak_rotation_privacy.py 540aaf4655bcd903e23b0965745f14548548f4af63956ebbfa8601f6bf19f9b4
outputs/task7-privacy-baseline-20261103/test_keycloak_rotation.py a82dcddae1b5abdaec9675b2768df1b97c57a9a76b661b785659bfda6ebf05fd
```

The five production trust/configuration hashes were rechecked and still
exactly match the previous source freeze:

```text
src/medical_rag/signing_keys.py c049104015f45ecbf2ed833ace313c9723985b6f5999914b3f3c36b7e8073f6f
src/medical_rag/oidc_identity.py 9d036905d3c1194160137e2ad959a926065b380390e8803868014d1c08a6b529
src/medical_rag/provider_logout.py 1311d39c558dc5378e09b1795dcc85efe2d886a23282bef8112ee87b898443af
src/medical_rag/oidc_login.py 2fcfa21929431bd2a186215017db45b0906f5d05af3aa14d199b34513e84b405
src/medical_rag/security_config.py 06e67ca8fa734a765473b078b031de74294f3a2b2e16de132fe4126cdd95ead0
```

No `.env`, original D: checkout, owner database, real account/hospital/system,
production setting, legacy unlock, staging or commit was touched. This is
test-diagnostic remediation, not new production approval. Independent I1
re-review and the controller's task gate remain required.
