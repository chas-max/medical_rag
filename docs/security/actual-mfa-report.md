# Task 8: actual second-factor assurance — implementation evidence

2026-11-03, isolated medical-security worktree. Implementation source frozen
2026-11-03 16:00 +08:00; results below are scoped reference-policy evidence,
not full-plan/production acceptance. Final combined provider regression passed
90 tests; report finalized 2026-11-03 16:05 +08:00. Independent review pending.

## Scope and production delta

The reusable public `deploy/identity/keycloak-mfa-policy.json` is loaded directly
by `tests/keycloak_mfa.py` into a unique fictional disposable realm. It has only
flows, authenticator configuration, browser binding, ACR mapping and staff role;
no users, credentials, clients, enabled realm, real callback or institution.
`deploy/identity/README.md` explains institutional review/enrollment boundaries.

Only production change: `OIDCLogin.start` passes deterministically sorted,
space-separated configured `mfa_acr_values` as Authlib's nonessential
`acr_values`, omitting the parameter for an empty in-memory accepted set.
There is no request override, essential claim, constant ACR mapper, new MFA
setting, verifier bypass, local OTP endpoint, provider-role business authority,
or change to accepted assurance/current local permissions/adult-self scope.
Code/S256, nonce/state, max_age, fixed issuer/client/callback and verified TLS
remain. The default actual-provider fixture is still password-only; it now
requests its unknown institutional ACR without gaining MFA assurance.

All four pre-existing edited files were already in the controller's
`outputs/actual-mfa-baseline-20261103` snapshot: `src/medical_rag/oidc_login.py`,
`tests/test_oidc_login.py`, `tests/keycloak_forms.py`, `tests/keycloak_runtime.py`.
No additional pre-existing file needed editing/capture. Other worktree changes,
.env files, production systems/accounts/DBs and user RabbitMQ were not touched.
No commits/staging, original checkout changes or legacy RAG/worker unlock.

## TDD and investigative history (honest failures, sanitized results only)

All Python commands use `.venv/Scripts/python.exe` from the worktree with
`$env:PYTHONUTF8='1'`. Integration selections explicitly set
`RUN_POSTGRES_INTEGRATION='1'` and/or `RUN_KEYCLOAK_INTEGRATION='1'` as below.
No selected actual-provider/integration run silently skipped infrastructure.

1. With `RUN_POSTGRES_INTEGRATION=1`:
   `-m pytest tests/test_oidc_login.py -k requests_only_sorted -q --basetemp=outputs/actual-mfa-red-acr`
   → **1 failed, 1 passed, 50 deselected**: sorted trusted ACR request absent.
   After minimal start change, same selector with
   `--basetemp=outputs/actual-mfa-green-acr --tb=short` → **2 passed**.
2. `-m pytest tests/test_keycloak_mfa_support.py -q --tb=short --basetemp=outputs/actual-mfa-red-support`
   → **13 failed**: public fragment/native OTP driver missing. After fragment
   and bounded driver implementation, `outputs/actual-mfa-green-support`
   → **13 passed**.
3. Pure selector `-k 'runtime_has or fixture_composer'`, base
   `outputs/actual-mfa-red-compose` → **2 failed, 13 deselected**: opt-in runtime
   hook/composer missing. After opt-in composition, full support suite at
   `outputs/actual-mfa-green-compose` → **15 passed**.
4. Pure selector `-k code_generation`, base `outputs/actual-mfa-red-code`
   → **2 failed, 15 deselected**: mature bounded-rollover utility missing.
   After implementation, encoding/code subset below → generator **2 passed**.
5. With actual-provider flag, first
   `-m pytest tests/test_keycloak_mfa.py -k password_otp_signed -q --tb=short --basetemp=outputs/actual-mfa-first-provider`
   → **1 failed, 9 deselected**: real password reached OTP form, but provider
   rejected generated code. This was an implementation discovery, not a clean
   feature-missing RED. Official 26.8.1 `OTPCredentialModel.getDecodedSecret`
   treats absent encoding as literal UTF-8. The fixture imported Base32 text;
   explicit supported `secretEncoding=BASE32` was missing. Pure encoding
   contract added first; `-k 'fixture_composer or code_generation'`, base
   `outputs/actual-mfa-red-encoding` → **1 failed, 2 passed, 14 deselected**.
   Corrected fixture credential metadata only; no validator changes.
6. Actual full suite, base `outputs/actual-mfa-provider-v2` → **5 passed,
   5 failed**. Repeated same-timestep codes correctly failed Keycloak's native
   single-use policy; later cases shared the module fixture credential.
   Added pure `-k never_reuses`, base `outputs/actual-mfa-red-singleuse`
   → **1 failed, 17 deselected** before bounded fresh-step generation.
   Keep single-use policy unchanged; generator records only last issued step
   in private fixture memory and waits at most 30.05 seconds, then fails
   statically if clock did not advance. Near-rollover wait is under 3.05 sec.
   No unbounded retries, OTP-window widening or reusable-code switch.
7. Actual selector `-k 'unenrolled or sso_reauthentication' -q -s --tb=short`,
   base `outputs/actual-mfa-diagnose-boundaries` → **2 failed, 8 deselected**:
   tests had assumed rejection meant no redirect. Safe boolean-only diagnostics
   established unenrolled users redirect to the provider (not app callback);
   the native required-action path is asserted. Consumed challenge can likewise
   redirect without issuing a usable application session. Assertions now check
   the actual session/callback boundary, not an invented no-redirect contract.
   No public flow or application contract change was needed.
8. Actual full suite, base `outputs/actual-mfa-provider-v3` → **10 passed**,
   221.57 sec. This preceded final proof-hardening below.
9. Pure `-k wrong_code`, base `outputs/actual-mfa-red-wrong-code`
   → **1 failed, 18 deselected**, then mature-utility candidate generation
   added. Six-digit invalid code is selected outside five surrounding timestep
   candidates (covering native default +/-1 window with boundary margin), and
   is submitted to actual Keycloak, not rejected by a local format validator.
10. Privacy AST proof initially found two missing checks and one harness
    NameError; fixed the harness without weakening the tested checks.
    `-m pytest tests/test_keycloak_mfa_privacy.py -q --tb=short --basetemp=outputs/actual-mfa-red-privacy-clean`
    → **3 failed, 2 passed**: missing Base32/used-OTP checks and whole-form
    comparison missed a raw logout JWT. After actual-check hardening, support
    plus privacy suite at `outputs/actual-mfa-green-privacy-wrong-code`
    → **24 passed**. AST replays use synthetic inputs only and prove static
    boolean-local failures do not render sensitive values.
11. Initial ACR RED's default pytest rendering inherited a legacy `system`
    argument tuple containing an ephemeral disposable PG password. That
    credential-bearing diagnostic is deliberately not reproduced or packaged.
    No OTP/seed/token/provider page was emitted. Added a static regression
    contract (`-k new_acr_red`, base `outputs/actual-mfa-red-acr-diagnostic`)
    → **1 failed, 5 deselected**; new ACR test now resolves the fixture via
    `request.getfixturevalue`, keeping that tuple out of argument rendering.
    Targeted integration rerun at `outputs/actual-mfa-acr-diagnostic-green`
    → **2 passed, 50 deselected**. This is test-only post-freeze hardening;
    other inherited legacy fixture diagnostic debt remains out of scope.

## Final verification commands/results

- Pure support/privacy:
  `-m pytest tests/test_keycloak_mfa_support.py tests/test_keycloak_mfa_privacy.py -q --tb=short --basetemp=outputs/actual-mfa-pure-refreeze`
  → **37 passed**, 1 inherited Starlette deprecation warning.
- Coverage command before the final diagnostic-only test was added:
  `COVERAGE_FILE=outputs/.coverage-actual-mfa`,
  `-m pytest tests/test_keycloak_mfa_support.py tests/test_keycloak_mfa_privacy.py --cov=keycloak_mfa --cov=keycloak_forms --cov-report=term-missing -q --tb=short --basetemp=outputs/actual-mfa-support-final`
  → **36 passed**; `keycloak_mfa.py` **100% (38/38 statements)**,
  whole existing+extended `keycloak_forms.py` **83% (74/89)**, combined **88%**.
  New driver paths are covered; uncovered old password/logout helpers are
  exercised additionally by the actual-provider regression below.
- Actual final provider / prior actual-Keycloak / rotation / fixture checks:
  both integration flags 1, `COVERAGE_FILE=outputs/.coverage-actual-mfa-final`,
  `-m pytest tests/test_keycloak_mfa.py tests/test_keycloak_mfa_support.py tests/test_keycloak_mfa_privacy.py tests/test_keycloak_identity.py tests/test_keycloak_rotation.py tests/test_keycloak_runtime.py tests/test_keycloak_operator.py tests/test_keycloak_rotation_privacy.py --cov=keycloak_mfa --cov=keycloak_forms --cov-report=term-missing -q --tb=short --basetemp=outputs/actual-mfa-provider-final`
  → **90 passed**, 2 known dependency deprecation warnings, 358.59 sec;
  helper **100%**, whole driver **97%**, combined **98%**. All ten hardened
  actual MFA cases passed, as did default password-only/unknown requested-ACR
  Keycloak identity cases, real rotation/key overlap and prior fixture/privacy
  cases. The separate pure diagnostic test was added after collection and
  independently rerun in the final 37-passed pure suite above.
- Actual PG auth/audit/startup/keyring affected regressions, PG flag 1:
  `-m pytest tests/test_oidc_login.py tests/test_oidc_identity.py tests/test_identity_sessions.py tests/test_security_audit.py tests/test_security_server.py tests/test_provider_logout.py tests/test_signing_keys.py tests/test_signing_key_config.py tests/test_signing_key_http.py -q --tb=short --basetemp=outputs/actual-mfa-pg-regressions`
  → **389 passed**, 2 dependency deprecation warnings, 114.12 sec. Includes
  real least-privilege PG/startup/TLS-process gates and keyring HTTP cases.
- Scoped Ruff:
  `-m ruff check src/medical_rag/oidc_login.py tests/keycloak_runtime.py tests/keycloak_forms.py tests/keycloak_mfa.py tests/test_keycloak_mfa.py tests/test_keycloak_mfa_support.py tests/test_keycloak_mfa_privacy.py tests/test_oidc_login.py`
  → **All checks passed**. Earlier scoped run found import ordering/unused
  test locals; corrected without behavior changes. No repo-wide lint claim.
- From `frontend`: `npm test` → **199 passed / 18 files**;
  `npm run lint` → **exit 0**; `npm run build` → **exit 0**;
  `npm run build:patient` → **exit 0**. Existing legacy frontend remains
  distinct from the dedicated patient build; neither was functionally changed.
- Controller reports its broad nonintegration initial-freeze regression run
  exited 0: **1519 passed, 5 known skips, 316 deselected**, 2 known warnings,
  205.32 sec, retaining the known 500-anchor exclusion. It started before the
  final diagnostic-only pure case and integration ACR argument-repr hardening;
  it is **initial-freeze regression plus targeted final-delta evidence**, not
  a claim all tests were collected after the final manifest. Controller owns
  acceptance, independent final-delta checks and review packaging.

## Actual protocol findings and proof limits

- Cold staff login accepts native username/password, then issues an actual
  OTP challenge before any application session/callback. Correct fictional
  OTP produces provider-signed accepted ACR, consumed through existing mature
  PyJWT verification/session authority, and current local auditor gets `/me`
  audit permission and `GET /audit`. Consent/conversations remain denied without
  current self-patient role and binding. No raw provider claims are persisted or
  exposed by a debugging API.
- Patient and locally assigned low-assurance auditor can authenticate identity
  despite nonessential requested ACR; session MFA stays false, audit false/403.
  Patient adult verified self-consent functionality remains. Correct real MFA
  with provider staff classification but no local role still denies audit.
- Missing/wrong OTP are posted to the actual provider; no callback/session.
  Classified unenrolled staff receive the provider's required-action redirect,
  not application callback; test does not finish enrollment or collect its seed.
  A guessed callback cannot manufacture a session.
- Same provider SSO browser must see OTP again (`loa-max-age=0`), including
  native rejection of replayed prior OTP and success only after a fresh code.
  Reposting a **consumed** challenge cannot obtain a usable new application
  session. This is not a time-expired native challenge test; no claim of a
  newly measured provider challenge timeout is made. Existing PG protocol
  expiry/freshness regressions remain separate evidence.
- Local role removal/account disable blocks the next auditor request. Actual
  provider logout is captured over verified TLS, posted into existing signed
  backchannel route, and old MFA local cookies fail. This proves request-time
  revocation, not cancellation of already-authorized operations or idle-page
  push notification.
- Actual privacy checks inspect audit/public output/captured logs against
  password, seed hex and actual Base32 encoding, old session, decoded raw logout
  JWT, and the OTP actually submitted. Assertions precompute boolean locals and
  use static messages. Server request IDs remain present; existing PG audit
  regressions additionally prove ID correlation and fixed metadata.
- Real provider sockets/signatures/private-CA TLS; application HTTP is fixed
  HTTPS TestClient. No local JWT signing, patched verifier, fake MFA claim or
  MockTransport in actual scenarios. Synthetic transport tests exist only for
  pure bounded-driver behavior, never as upstream-authentication evidence.
- Official 26.8.1 image remains digest-pinned. Credentials/OTP seeds exist only
  in fixture memory and bounded stdin/container tmpfs import, not a host realm
  import artifact, CLI args, environment or report. Ownership-checked cleanup
  preserves unrelated RabbitMQ. Final cleanup inspection showed only the
  pre-existing `medical-rag-resume-rabbitmq-1` container. Institutional IAM review, identity proofing,
  browser E2E, enrollment/recovery, devices and real deployment are separate
  gates. TOTP is not phishing-resistant WebAuthn or a complete IAM service.

Primary implementation references (not copied source):
[native LoA](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/authentication/authenticators/conditional/ConditionalLoaAuthenticator.java),
[role condition](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/authentication/authenticators/conditional/ConditionalRoleAuthenticatorFactory.java),
[OTP credential encoding](https://github.com/keycloak/keycloak/blob/26.8.1/server-spi/src/main/java/org/keycloak/models/credential/OTPCredentialModel.java),
[single-use OTP provider](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/credential/OTPCredentialProvider.java),
[native OTP/enrollment action](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/authentication/authenticators/browser/OTPFormAuthenticator.java).

## Frozen source manifest (SHA256)

Paths are relative to the isolated worktree. Four existing files modified;
six source/policy/docs files added. This report is the additional new handoff
artifact and intentionally excluded from its own hash manifest.

| File | SHA256 |
| --- | --- |
| src/medical_rag/oidc_login.py | BE6741F426E654342C35E27A60F2A1535C28D9E5B48098D50670C50FF337BA42 |
| tests/test_oidc_login.py | 824221DAF563A873937BEDE318AC41009497F767E529FA0B58EA962D8C90AE12 |
| tests/keycloak_runtime.py | D93DE5FCA9F76C013B6F784FFE0FC588A1500B209272132DAC9EE9036B593413 |
| tests/keycloak_forms.py | F45425CFBCF85323C13921FB326719F036B215ACB956213F8B45C58A6A7A561F |
| tests/keycloak_mfa.py | 320FC4533F814F5F55B559EE7E543C050FBB107D55E4C3120E53B02D3A4F095D |
| tests/test_keycloak_mfa.py | A305BA16ECBB7376B30A7AF6467EE8C3D0FA75E182612FCEE9BB2247E07528E0 |
| tests/test_keycloak_mfa_support.py | 5DC8C539C26CCDCDF6F8611536D08D701F09447B8F8531170F3493F6870193E5 |
| tests/test_keycloak_mfa_privacy.py | 505A22FC5B7CD7C4DC7F27C79BC008B77D981A9F086367015C86C981BDD50ABB |
| deploy/identity/keycloak-mfa-policy.json | 4898476BA92B196B1C049222D7E718124069EADFCDEA747AE1221A1E0E98282E |
| deploy/identity/README.md | 5AF0800192213AADB23CE1F1B5F9FCA9D51F083896EFA41ABD06565F0EAF38D2 |

## Remaining concerns / handoff

Dependency deprecations (Starlette HTTPX TestClient, Authlib HTTPX) remain
disclosed; no dependency migration was authorized. Inherited legacy diagnostic
fixture argument debt outside the new test remains; no credential-bearing RED
body is included here. Real institutional MFA certification, enrollment and
time-expired native challenge behavior are not claimed. Implementation needs
independent scoped review/controller gate before acceptance; production remains
blocked and legacy RAG/workers remain locked.

## Review fix wave — I1 and narrow M1 (2026-11-03)

Test-only source frozen at 2026-11-03 16:27 +08:00. This appendix supersedes
the three corresponding test hashes above, not prior verification history.
Independent scoped re-review remains required; this is not acceptance.

Before any edits, the exact three pre-fix files were captured via apply_patch
under outputs/task8-fix-baseline-20261103/tests/. SHA256 verification matched
the original manifest for all three (support 5DC8C539..., privacy
505A22FC..., actual scenario A305BA16...). Only these three tests and this
appendix were changed. No production/helper/runtime/policy changes, .env
reads/edits, staging, commits, original checkout changes, real accounts or
institutional deployment. All seven other original manifest entries were
rehashed and remain identical, including production oidc_login.py.

I1: both credential-bearing composer assertions now precompute booleans and
use static messages. New privacy tests extract those actual support-source
assertions, not copied examples, and replay them through pytest assertion
rewriting using synthetic canaries only. Missing-user and unexpected seed on
unenrolled-user failures must both fail without printing password, seed bytes,
hex or Base32. The structural guard covers these support assertions alongside
the existing actual-scenario guard. No production privacy helper was added.

The synthetic harness disables only pytest's custom _reprcompare hook for
its own case, so default rewritten diagnostics are exercised as well: an active
set-diff hook can hide the set(users) call arguments. No actual authentication
or verifier is patched. Initial RED before this harness refinement was 4 failed,
6 deselected, 1 warning at outputs/task8-fix-red-privacy; missing-user failed
the static-message requirement rather than detecting disclosure because that
set-diff hook hid arguments. The subsequent clean RED below detects disclosure
for **both** actual source expressions and is the meaningful regression RED.
All outer failing assertions report only boolean locals/static diagnostics;
neither synthetic canaries nor actual credential-bearing failure bodies were
printed or persisted.

M1: the real scenario privately retains the original pending
__Host-medical-oidc cookie before first completion and the actual captured
first callback. After successful first and SSO second completions, all local
cookies are cleared. The exact first callback is replayed with that original
browser proof explicitly supplied and no authenticated local cookie: it must
return 401, issue no session and leave /me at 401. Therefore rejection is
not attributable merely to absent browser proof. The original consumed native
provider-challenge submission remains; a redirect must either be to the trusted
provider origin or an app callback rejected with original browser proof, and
neither may yield a new local session. A focused source-shape test prevents
dropping the explicit proof from the first-callback replay. These are separate
local consumed-callback and native consumed-provider-challenge evidence, **not**
a measured provider time-expiry test.

Exact new verification commands (PowerShell, worktree root):

1. Clean RED, before either support/scenario fix:
   `$env:PYTHONUTF8='1'; .venv/Scripts/python.exe -m pytest tests/test_keycloak_mfa_privacy.py -k 'composer or consumed_callback' -q --tb=short --basetemp=outputs/task8-fix-red-privacy-clean`
   -> **4 failed, 6 deselected, 1 inherited Starlette warning**, 1.43 sec.
   Failures were sensitive-check shape, synthetic missing-user disclosure,
   synthetic unexpected-seed disclosure and missing replay browser proof.
2. GREEN after minimal test fixes:
   `$env:PYTHONUTF8='1'; .venv/Scripts/python.exe -m pytest tests/test_keycloak_mfa_support.py tests/test_keycloak_mfa_privacy.py -q --tb=short --basetemp=outputs/task8-fix-green-pure`
   -> **41 passed, 1 inherited Starlette warning**, 2.67 sec.
3. Real provider, no infrastructure skips:
   `$env:PYTHONUTF8='1'; $env:RUN_KEYCLOAK_INTEGRATION='1'; .venv/Scripts/python.exe -m pytest tests/test_keycloak_mfa.py::test_actual_sso_reauthentication_requires_otp_again_and_stale_challenge_denied -q --tb=short --basetemp=outputs/task8-fix-actual-replay`
   -> **1 passed, 2 inherited Starlette/Authlib warnings**, 59.52 sec.
   Native password/OTP/SSO, signed verifier/session authority and uniquely
   owned disposable PostgreSQL fixture were used. Original cookie/callback,
   OTP and credentials remained memory-only, outside rendered assertion operands.
   Existing mature TOTP utility, bounded fresh-step wait and native single-use
   policy were unchanged. No manufactured upstream JWT or mocked verifier.
4. Scoped lint:
   `$env:PYTHONUTF8='1'; .venv/Scripts/python.exe -m ruff check tests/test_keycloak_mfa_support.py tests/test_keycloak_mfa_privacy.py tests/test_keycloak_mfa.py`
   -> **All checks passed**, exit 0.
5. Final focused pure coverage:
   `$env:PYTHONUTF8='1'; $env:COVERAGE_FILE='outputs/.coverage-task8-fix'; .venv/Scripts/python.exe -m pytest tests/test_keycloak_mfa_support.py tests/test_keycloak_mfa_privacy.py --cov=keycloak_mfa --cov=keycloak_forms --cov-report=term-missing -q --tb=short --basetemp=outputs/task8-fix-green-coverage`
   -> **41 passed, 1 inherited Starlette warning**, 2.61 sec;
   composer/TOTP **100% (38/38)**, whole driver **83% (74/89)**,
   combined **88% (112/127)**.

No broad/provider-whole-suite/PG-whole-suite/frontend rerun is claimed for this
test-only delta. M2 dependency-warning debt is deliberately retained and not
suppressed. `docker ps --format '{{.Names}}'` after the real case showed only
the preserved pre-existing medical-rag-resume-rabbitmq-1 container.

Final fix source manifest (SHA256; report excluded from its own hash):

| File | SHA256 |
| --- | --- |
| tests/test_keycloak_mfa_support.py | 3C09259FCEFD3E7FF475A8B66650502A53DF614A8F0A0FB0406F147CC8757370 |
| tests/test_keycloak_mfa_privacy.py | 0C375E64A3EDEC3D4C6B7F928052D7F4A84F4A9160E98089514138A12A0A7031 |
| tests/test_keycloak_mfa.py | 193F45AA83F29B76FCB753BC5A0DA33B7FA29FAE306639DFF7BDA9C4DA8AC023 |
