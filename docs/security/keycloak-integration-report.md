# Task6 implementer report — actual disposable Keycloak and operator CA

Date: 2026-11-03. Implementer source frozen; independent spec/quality reviews
and controller-owned broad regression/readiness updates remain separate gates.
No commits or staging. Existing dirty changes, `.env`, original D: checkout,
legacy locks and the existing RabbitMQ service were preserved.

## Outcome and trust boundaries

The isolated patient factory, existing confidential OIDC client, pinned RSA
identity/logout verifiers, session store, patient authorization and mandatory
audit code work with actual Keycloak 26.8.1. No production verifier correction
or relaxation was necessary. The actual provider suite contains ten scenarios
and uses no mock upstream transport, token response, signature or authentication
implementation. Generated certificates sign TLS infrastructure only; Keycloak
creates/signs the upstream ID and logout tokens.

Exact official image used, locally inspected and run with `--pull=never`:

```text
quay.io/keycloak/keycloak:26.8.1@sha256:3f3c5f8e03c614774b4bd9173b88391fefa4153bfc010d8012d59ad825377332
```

Supporting cached test images: `python:3.13-alpine`, `postgres:18.6-alpine`.
This is a test version pin, not a claim of indefinite security support.

Provider authorization forms, token exchange, verification-key retrieval and
provider-to-collector backchannel delivery use actual verified HTTPS sockets.
The application routes/callback use Starlette TestClient at the exact configured
`https://medical.example.org` origin; this is **not** a real browser or
full-socket application E2E test. The actual unchanged captured form is submitted
to the protected production backchannel route, which verifies it again.
The collector is fixture transport, not a substitute production logout service.

## Implementation

- `LoginConfiguration.provider_ca_file: Path | None` is excluded from repr.
  Trusted TOML allows only that optional extra OIDC key and resolves it relative
  to the operator TOML. Existing strict no-CA configurations still work.
- A focused helper reads at most 65537 bytes and rejects missing, empty,
  malformed, non-ASCII, non-certificate trailing data and bundles above 65536
  bytes. It builds a new `SSLContext(PROTOCOL_TLS_CLIENT)` with hostname
  verification, `CERT_REQUIRED` and TLS >=1.2. Explicit private trust loads only
  bounded PEM content; no environment or global trust mutation is used.
- No-CA behavior uses the same explicit certifi public bundle as HTTPX
  `verify=True, trust_env=False`. Certifi is now a direct runtime dependency.
  OpenSSL environment-driven default-path loading is deliberately not used.
- Explicit CA validation happens during trusted startup and on every transport
  construction. A later invalid/missing CA cannot fall back to public or insecure
  trust. Public startup/login errors remain static; no CA paths/bytes leak.
- HTTPX/Authlib token transport retains no redirects, bounded response handling,
  `trust_env=False` and its existing 5-second request/3-second connection and
  10-second overall exchange bounds. Existing explicit MockTransport tests remain
  separate coverage, never used by the actual provider suite.
- The uniquely named/labeled disposable fixture creates an HTTPS-only Keycloak
  realm/client, generated fictional passwords and TLS CA, a private-network TLS
  collector and an independent PostgreSQL database with migrations 0001-0006 and
  the exact owner-run `patient_runtime_grants.sql` recipe. Containers expose
  only loopback host ports. Cleanup checks the exact ownership label before
  removing each fixture container/network; there is no broad Docker cleanup.
- Test-only `start-dev`/H2 and dynamic provider hostname handling are confined
  to the disposable fixture. They are not production deployment examples.
  Confidential code flow requires S256 PKCE and exact callback/post-logout URLs;
  implicit, direct password-grant and service-account flows are disabled.
- The bounded native HTMLParser driver accepts the actual password form and
  Keycloak's anonymous logout-confirmation form. It permits exactly one matching
  POST form, bounded fields/actions/redirect, same-origin action and bounded
  streaming page bytes before UTF-8 decoding. It never follows arbitrary
  redirects or bypasses certificate validation.
- Actual scenarios cover initial consent denial/no automatic purpose grant,
  explicit fictional notice grant, own conversation/turn reads and writes,
  second-identity non-disclosure, wrong password, disabled/unprovisioned local
  accounts, unverified patient binding, local administrator/auditor without real
  MFA, wrong-CA handshake and exchange denial, and invalid/missing CA changed
  after login start. Upstream role claims do not grant local roles.
- Actual normal user logout produces a signed provider backchannel form;
  unchanged delivery revokes the old local cookie, safely audits without raw
  upstream claims, permits later real reauthentication and preserves it on
  identical replay. The current strict logout profile accepted actual provider
  output unchanged; no synthetic token was introduced.
- Task5's follow-up now exercises append/revoke/pending-login fence/identical
  replay/later-new-auth survival under the exact owner grant-recipe role in
  `test_owner_grant_recipe_supports_real_runtime_operations`.

Transport HTTPX/httpcore URL loggers are disabled only in the actual-test scope
to prevent authorization/callback query capture. The collector has no request or
body logging. Docker errors (including timeout/OSError) are converted to static
messages with underlying exception context suppressed. Privacy assertions use
booleans to avoid pytest rewriting printing tokens/cookies on a failure.
Generated fixture credentials/keys remain in ignored `outputs/task6-*` temporary
test directories, not source/config examples; captured upstream tokens stay in
memory and are not written to reports or files.

## TDD and execution evidence

All commands below ran from the worktree root unless `frontend` is specified.
Python commands use this worktree's `./.venv/Scripts/python.exe` and
`$env:PYTHONUTF8='1'`. Every basetemp is worktree-owned, not system temp.

### Trusted-CA RED before production changes

```powershell
$env:PYTHONUTF8='1'
./.venv/Scripts/python.exe -m pytest tests/test_provider_tls.py -q --tb=short --basetemp outputs/task6-tls-red2
```

Result: **6 failed, 7 passed, 2 known dependency warnings**, exit1. Expected
failures were missing `medical_rag.provider_tls`, missing configuration field,
and refusal of the new optional TOML field. This is the feature RED. The seven
pre-existing negative/strict-loader behaviors were already passing. The first
attempt used automatic bytes parameter IDs and caused Windows path-length setup
errors (6 failed/6 passed/2 errors); named bounded IDs fixed that test harness
issue before the clean RED above. Command of that superseded attempt:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_provider_tls.py -q --basetemp outputs/task6-tls-red
```

### Initial trusted-CA GREEN

```powershell
$env:PYTHONUTF8='1'
./.venv/Scripts/python.exe -m pytest tests/test_provider_tls.py -q --tb=short --basetemp outputs/task6-tls-green
```

Result: **13 passed, 2 known dependency warnings**, exit0. Subsequent edge
coverage added inclusive byte-bound/bundle and direct wrong-type checks; the
final helper suite has 15 tests and 100% statement coverage below.

### Actual provider interoperability iterations (not feature RED)

Each command used `$env:PYTHONUTF8='1'; $env:RUN_KEYCLOAK_INTEGRATION='1'`:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_identity.py -q --tb=short --basetemp outputs/task6-keycloak-first
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_identity.py -q --tb=short --basetemp outputs/task6-keycloak-second
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_identity.py -q --tb=short --basetemp outputs/task6-keycloak-third
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_identity.py -q --tb=short --basetemp outputs/task6-keycloak-final
```

1. First: **8 setup errors**, 38.01s, exit1: fixture incorrectly inserted local
   role `admin`, which the schema rightly rejects. Corrected fixture to
   `org_admin`; this was not a production auth failure/RED.
2. Second: **5 passed, 3 failed**, 61.73s, exit1. Two test assertions used the
   nonexistent `/audit/events` instead of existing `/audit`. Native Keycloak
   logout confirmation has an anonymous POST form rather than the assumed ID.
   Corrected assertions/parser semantics, keeping same-origin/single-form
   constraints. Real login, wrong-password, wrong-CA and local-account denial
   had already passed. No production validation was changed.
3. Third: **8 passed**, 67.55s, exit0: actual profile compatibility proven.
4. Hardened final: **8 passed**, 71.08s, exit0 after bounded streaming/native
   form and diagnostic-privacy improvements. Two warnings on each run.

### Final selected actual provider + helper/gating coverage

```powershell
$env:PYTHONUTF8='1'
$env:RUN_KEYCLOAK_INTEGRATION='1'
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_identity.py tests/test_provider_tls.py tests/test_keycloak_runtime.py -q --tb=short --basetemp outputs/task6-keycloak-final2 --cov=medical_rag.provider_tls --cov-report=term-missing
```

Result: **29 passed, 2 warnings**, 73.61s, exit0; no skips, no failures.
This is **10 actual-provider scenarios +15 CA tests +4 infrastructure privacy/
explicit-gate tests**. `provider_tls.py`: **21 statements, 0 missed, 100%**.
When explicitly selected, absent Docker/images fail; there is no fake-provider
fallback or skip for missing infrastructure. The gate itself is unit-tested.

### Actual PostgreSQL auth/provider/patient/audit/startup regressions

```powershell
$env:PYTHONUTF8='1'
$env:RUN_POSTGRES_INTEGRATION='1'
./.venv/Scripts/python.exe -m pytest tests/test_oidc_login.py tests/test_oidc_identity.py tests/test_identity_sessions.py tests/test_provider_logout.py tests/test_security_server.py tests/test_security_audit.py tests/test_patient_vertical.py tests/test_provider_tls.py tests/test_keycloak_runtime.py -q --tb=short --basetemp outputs/task6-pg-final --cov=medical_rag.provider_tls --cov-report=term-missing
```

Result: **367 passed, 2 warnings**, 103.08s, exit0; no skips/failures.
Provider helper coverage **100% (21/21)**. This includes the exact owner-recipe
follow-up and actual least-privilege/RLS/primary/startup/provider/audit regressions.
One subsequently added explicit missing-Docker gate test was separately verified:

```powershell
$env:PYTHONUTF8='1'
./.venv/Scripts/python.exe -m pytest tests/test_keycloak_runtime.py -q --tb=short --basetemp outputs/task6-runtime-unit
```

Result: **4 passed, 1 warning**, 0.51s, exit0 (also included in final29 above).

### Scoped lint, formatting and frontend

```powershell
$env:PYTHONUTF8='1'
./.venv/Scripts/python.exe -m ruff check src/medical_rag/provider_tls.py src/medical_rag/oidc_login.py src/medical_rag/security_config.py tests/test_provider_tls.py tests/test_keycloak_identity.py tests/keycloak_runtime.py tests/keycloak_forms.py tests/fixtures/keycloak_collector.py tests/test_keycloak_runtime.py tests/test_security_server.py
git diff --check
```

Both exit0; Ruff: **All checks passed**. Initial scoped checks found import
ordering/SIM117/overbroad test catch, corrected before the final check. Only new
files were Ruff-formatted. `git diff --check` reports only Git's pre-existing
CRLF conversion warnings on frontend config/package files, no whitespace error.
The controller's existing full-repository Ruff baseline has **41 untouched
issues**; this report does not claim full-repository lint cleanliness or suppress
those findings. A broad unrelated legacy suite is controller-owned post-freeze.

From `frontend`, each command executed separately:

```powershell
npm test -- --reporter=dot
npm run lint
npm run build
npm run build:patient
```

All exit0. Tests: **18 files/199 tests passed**, 42.88s. Lint: clean. Main build:
63 modules, Vite 1.80s; patient build:33 modules, Vite715ms. No frontend source
edits were made by this task.

Known unsuppressed Python dependency warnings: Starlette's HTTPX TestClient
deprecation and Authlib's HTTPX deprecation. Both are existing dependency-level
warnings, not test failures; migration to another HTTP client is not this task.

## Changed files and frozen SHA256

New source/helper/test/report files and the five changed existing files are
listed below. README/progress/readiness are controller-owned and untouched by
this implementer. Report itself is excluded from the self-referential hash list.

```text
src/medical_rag/provider_tls.py      D24ECADF6098BCD40611E71BADB65310AA407213ECC554D80826E8FCB0344F4F
src/medical_rag/oidc_login.py        6505FBDD597F4026894C365EBF6FBFD78BB9374C719C95CFB7A9D93DFA514B25
src/medical_rag/security_config.py   5BD0ABD20C7ADFC0B923CBA1577DC7798FFE9455D07110122D266FEA4C84488C
pyproject.toml                      BF6D5C27CA7A3FFC5AFC2F732F16C0D92CC8A4BD237DCB33E07FA2EFFF635715
deploy/patient/config.example.toml  7477BAA2FA2327AB3F46C76B351FB8BF7C84E0A1560CEA9EA1E2031BE30F948F
tests/test_provider_tls.py          B3E9C79095E426D6FC46B5E6D56E4C5217740B89580DB46AFC0DC7E55DA52714
tests/test_keycloak_identity.py     45F52FD838F654C88AEB6B030B9C0358A2772DC2CC5A1405BD40F2216F824C25
tests/keycloak_runtime.py           39665E14B206F747D41338579B66F4AEF8BEC17734239A7B88393241BADBF8D5
tests/keycloak_forms.py             A3D61424281DF98AC1D5D8D5084128D502D242E1CEE163C000C7C45A1F383471
tests/fixtures/keycloak_collector.py 7C613F9AE7338F15D6311EA2351C80BC27EBF0A614B98C3EFA55E9CF86EE1FA3
tests/test_keycloak_runtime.py      2F6D2F276E459A82954480E69050ED9E2C185CA9352D77490E5172B07537079F
tests/test_security_server.py       62D3C8058650DEA6DCB8DE69A7031A8CB32A66438A15751DD156CF1436DA9563
docs/security/keycloak-integration-report.md (this report)
```

## Self-review, limits and remaining gates

Self-review corrected explicit public-CA loading to avoid OpenSSL environment
trust overrides, bounded streamed HTML/readiness before parsing, corrected actual
native form semantics without weakening production validation, made Docker
timeout/OSError paths static, and made failure-path privacy assertions safe.
Affected tests were rerun after these changes; the final29 and scoped lint are
green. Source hashes identify the frozen review inputs.

This proves interoperability with the pinned disposable provider, not production
identity deployment, genuine patient/hospital proofing, institutional OTP or
WebAuthn/MFA policy, guardian/minor workflows, clinical/legal acceptance or full
plan completion. Local fictional SQL adult/self bindings are owner-provisioned,
not verified by the identity provider. No constant ACR mapper is used; the admin
with local auditor role still has `mfa_verified=false` and audit access is denied.
No real account, hospital connection, external model call, SMTP registration,
global certificate installation or legacy API/worker unlock occurred.

Key retrieval here is verified fixture provisioning and a single pinned RSA
key; automated discovery/rotation remains a separate task. Real institution MFA,
production Keycloak HA/storage/TLS/proxy operations, certificate/key lifecycle,
institutional identity proofing and independent review/acceptance remain gates.
