# Task 8 actual MFA — independent task-scoped review

## Spec Compliance

- ❌ Issues found: the new pure composer checks can print fictional generated passwords and OTP seeds through pytest assertion rewriting on failure (`tests/test_keycloak_mfa_support.py:31-32`). This violates the brief's boolean-local/static-diagnostics requirement (`docs/security/actual-mfa-brief.md:101-103`); see Important I1. The actual-provider scenario assertions themselves follow the required pattern (`tests/test_keycloak_mfa.py:66-79`, `tests/test_keycloak_mfa.py:205-213`).
- ✅ The only functional production delta requests configured, deterministically sorted, space-separated nonessential ACR values and omits an empty set; it neither requests an essential claim nor changes verified-token or permission decisions (`src/medical_rag/oidc_login.py:312-317`, `tests/test_oidc_login.py:213-226`). Existing code/S256, nonce/state and max_age remain in that call and its unchanged client configuration (`src/medical_rag/oidc_login.py:300-317`).
- ✅ The shipped fragment has no deployment identity, users, credentials or clients; its native cookie/interactive flow has required password at level1 and required role/LoA/OTP at conditional level2, with the specified ACR mapping and zero level2 reuse age (`deploy/identity/keycloak-mfa-policy.json:2-31`). The composer loads that exact fragment and uses memory-only generated fictional credentials, explicit Base32 encoding and mature cryptography TOTP generation (`tests/keycloak_mfa.py:18-61`). Runtime policy composition is explicit opt-in, preserving the default password-only fixture (`tests/keycloak_runtime.py:193`, `tests/keycloak_runtime.py:295-296`).
- ✅ Actual scenarios distinguish accepted password from a pending OTP challenge and a completed app session; they cover signed accepted assurance plus current local auditor access, lower assurance despite the requested ACR, actual MFA without local auditor role, wrong/missing OTP, unenrolled staff, SSO re-challenge, next-request revocation and actual signed provider logout (`tests/test_keycloak_mfa.py:66-213`). The consumed-challenge replay test has a nonblocking evidence weakness described in M1; no measured native challenge-expiry claim is made (`docs/security/actual-mfa-report.md:166-171`).
- ✅ Reference and institutional boundaries are explicit: no automatic/real realm deployment, IdP classification is not a local business-role grant, staff MFA does not authorize patient content, and TOTP is not phishing-resistant or institutional certification (`deploy/identity/README.md:3-44`).
- ⚠️ Cannot verify cross-task guarantees from this delta alone: all patient operations reading current authoritative primary-PostgreSQL account/institution/roles/binding/age/verification/purpose consent, complete CSRF coverage, all JSON envelopes and the absence of every possible legacy mount. The changed production path preserves its existing session/verifier/cookie/origin implementation (`src/medical_rag/oidc_login.py:320-554`), and focused new tests deny staff patient-consent/content access (`tests/test_keycloak_mfa.py:91-92`); controller must retain earlier task/whole-branch gates rather than infer global certification from these cases.
- ⚠️ Reported provider/PG/frontend/broad-suite totals are implementation/controller evidence, not independently rerun here (`docs/security/actual-mfa-report.md:111-148`). The broad run predates the final diagnostic-only delta as disclosed. Browser E2E, real institutional enrollment/recovery/proofing, deployment and production acceptance remain separate gates (`deploy/identity/README.md:32-44`).

## Strengths

- The app remains an OIDC consumer: no local staff password/OTP verification, seed persistence, upstream-role authority, hard-coded ACR mapper or relaxed MFA authorization was introduced (`src/medical_rag/oidc_login.py:312-317`, `tests/keycloak_mfa.py:18-61`, `deploy/identity/keycloak-mfa-policy.json:2-31`).
- Positive and negative actual-provider tests pass through native provider forms and the existing verifier/session authority, not locally signed identity tokens; local permission checks are exercised independently of provider classification (`tests/test_keycloak_mfa.py:66-135`).
- Driver extensions keep bounded streaming, same-origin form actions and private challenge representation, while bad OTP values are actually posted to the provider rather than locally rejected (`tests/keycloak_forms.py:47-83`, `tests/keycloak_forms.py:107-148`).
- The evidence boundary is honest: provider traffic is real verified-HTTPS socket traffic while application HTTP is TestClient; the report distinguishes consumed challenges from measured time expiry (`tests/test_keycloak_mfa.py:1-5`, `docs/security/actual-mfa-report.md:166-171`, `docs/security/actual-mfa-report.md:183-187`).

## Issues

### Critical (Must Fix)

- None identified in this task delta.

### Important (Should Fix)

- **I1 — New composer test can leak process-private credentials on failure.** `tests/test_keycloak_mfa_support.py:31-32` directly asserts `set(users)` and membership against credential-bearing user dictionaries populated by `compose_mfa`. With a missing expected user, pytest renders the `set(users)` call's argument; with an accidental seed on the unenrolled user, it renders that dictionary. Both include the generated fictional password and raw seed. Two focused synthetic assertion-rewrite experiments reproduced password and seed disclosure, printing only boolean results, never sensitive material. The AST privacy guard only scans `test_keycloak_mfa.py` (`tests/test_keycloak_mfa_privacy.py:11`, `tests/test_keycloak_mfa_privacy.py:22-43`), so its passing result does not protect these new checks. Precompute these two sensitive expressions into boolean locals, assert with static messages, and add a synthetic rewritten-failure check covering the support assertions. No production credential is implicated; the defect is the explicitly forbidden diagnostic exposure of disposable credentials/OTP material.

### Minor (Nice to Have)

- **M1 — Consumed-challenge replay proof drops browser proof first.** `tests/test_keycloak_mfa.py:167-172` clears application cookies before processing an old redirect, so a callback can be denied merely for a missing browser-bound OIDC cookie. The test would not detect some regressions in replay/consumption handling. Preserve the original pending login cookie privately before the first finish, restore it only for the stale callback attempt, and require rejection with that browser proof supplied; retain boolean/static assertions. This strengthens the consumed-challenge regression, not a new native time-expiry requirement.
- **M2 — Validation output is not pristine.** The final pure/provider/PG selections disclose inherited Starlette/Authlib deprecation warnings (`docs/security/actual-mfa-report.md:113`, `docs/security/actual-mfa-report.md:124`, `docs/security/actual-mfa-report.md:132`, `docs/security/actual-mfa-report.md:223-224`). Track the dependency migration separately; do not suppress or describe these runs as warning-free. No dependency upgrade is requested by this scoped review.

## Checks and boundaries

- Read authoritative brief, final implementation report, and the complete 2,457-line frozen review package once. Base is `outputs/actual-mfa-baseline-20261103`, not a meaningful Git commit range; source/index/HEAD/.env were not changed. No reported suite was rerun.
- Named focused unchanged-code check — **fixture authority and real-network boundary**: inspected `tests/test_keycloak_identity.py:31-103` and `tests/test_keycloak_rotation.py:28-40` because new tests import those fixtures/helpers. Confirmed the lower-assurance `admin` has an actual current local auditor role (`tests/test_keycloak_identity.py:60-68`), fixed private-CA provider configuration with no injected transport (`tests/test_keycloak_identity.py:78-97`), and signed logout capture via the same real browser/socket path (`tests/test_keycloak_rotation.py:28-40`). Password/OTP provider requests use the runtime's real verified TLS browser (`tests/keycloak_runtime.py:139-148`); TestClient handles only application HTTP (`tests/test_keycloak_mfa.py:48-49`).
- Named focused experiment — **pytest diagnostic privacy**: replayed the exact support assertion expressions with synthetic dictionaries through `_pytest.assertion.rewrite.rewrite_asserts`; membership assertion and `set(users)` assertion each produced `password_leaked=True, seed_leaked=True`. A pure-public policy comparison control produced both false. No suite or actual credential was involved, and only boolean experiment results were printed.
- Controller independently verified the ten source-manifest hashes before dispatch; this review did not repeat those checks. Only this authorized review artifact was persisted.
- Controller additionally reported a fresh actual-provider positive auditor and actual-MFA/no-local-role smoke: 2 passed, 2 dependency warnings, 45.05 seconds; cleanup retained only the pre-existing RabbitMQ container. This is controller evidence, not a reviewer rerun or production acceptance.

## Assessment

**Task quality: Needs fixes.**

The native policy, narrow production request change and actual-provider/application separation are well scoped. I1 is a concrete unmet privacy requirement in newly added test support; fix and narrowly verify its failure diagnostics before accepting Task 8. This is not whole-branch, institutional MFA or production acceptance.

## Final scoped re-review — I1/M1 fix wave (2026-11-03)

### Spec Compliance

- ✅ **Spec compliant for the reviewable Task 8 delta; I1 resolved.** Both formerly credential-bearing expressions are evaluated into boolean locals and asserted with fixed messages (`tests/test_keycloak_mfa_support.py:31-34`). There is no longer a credential-bearing operand for pytest assertion rewriting at these checks.
- ✅ Regression protection extracts the actual support-source assertion/assignment pairs, requires both sensitive checks to have boolean-local/static-message shape, and replays missing-user and unexpected-unenrolled-seed failures using only synthetic credentials (`tests/test_keycloak_mfa_privacy.py:23-82`). The synthetic test temporarily disables only pytest's comparison-format hook, not any authentication or verifier, to exercise default rewritten diagnostics; it checks password, seed text, hex and Base32 absence with boolean/static outer assertions (`tests/test_keycloak_mfa_privacy.py:59-82`).
- ✅ **M1 resolved.** The actual scenario retains the original pending OIDC browser cookie before first completion and the exact first callback, clears authenticated cookies, and replays that callback with the original browser proof explicitly supplied. It requires 401, no newly issued session and unauthenticated `/me` (`tests/test_keycloak_mfa.py:152-177`). The consumed native challenge attempt also supplies original proof if it produces an application callback, and requires no usable new session (`tests/test_keycloak_mfa.py:178-184`). This closes the missing-browser-proof ambiguity without claiming measured native time expiry.
- ✅ A focused source-shape guard protects the original-cookie replay call from silently losing its explicit browser proof (`tests/test_keycloak_mfa_privacy.py:85-97`). The substantive protection is the actual callback/session assertions, not this shape guard alone (`tests/test_keycloak_mfa.py:174-184`).
- ⚠️ The initial cross-task and institutional/production limitations remain unchanged. This follow-up changes only three test files, not the production ACR request, native policy, runtime, OTP generator or application authorization contracts (`docs/security/actual-mfa-report.md:233-244`). Controller retains prior task reviews and the required whole-branch gate.

### Strengths

- The fix directly removes both demonstrated diagnostic disclosure paths and ties negative regression coverage to the actual assertions instead of copied examples (`tests/test_keycloak_mfa_support.py:31-34`, `tests/test_keycloak_mfa_privacy.py:41-82`).
- Replay evidence now separates a consumed local callback with valid original browser proof from a consumed native-provider challenge; neither is overstated as an elapsed native challenge-timeout measurement (`tests/test_keycloak_mfa.py:174-184`, `docs/security/actual-mfa-report.md:265-278`).

### Open Issues

- **Critical:** None.
- **Important:** None. I1 closed.
- **Minor:** M1 closed. M2 remains: disclosed inherited Starlette/Authlib dependency-deprecation warnings, unchanged and not suppressed (`docs/security/actual-mfa-report.md:284-309`). Track separately; no dependency migration is required for this task-scoped acceptance.

### Checks and Assessment

- Read the appended fix-wave report and complete 615-line full-context fix package once. No broader crawl, test rerun, new experiment, source/index/HEAD/.env modification or production change was performed; only this authorized review artifact was appended.
- Implementer evidence: clean privacy/replay RED 4 failed; final pure GREEN 41 passed; focused actual-provider SSO/replay 1 passed with no infrastructure skips; scoped Ruff clean; pure support coverage helper 100%, whole driver 83%, combined 88% (`docs/security/actual-mfa-report.md:280-305`). These are reported runs, not reviewer reruns.
- Controller evidence: fresh final pure suite 41 passed with one inherited warning in 2.46 seconds, and all three final test hashes matched. Earlier actual-positive/current-auditor and actual-MFA/no-local-role smoke evidence remains as recorded above. No new broad-suite or whole-provider-suite rerun is claimed for the test-only fix.

**Final task quality: Approved.**

Both actionable findings are closed with narrow, test-only changes and proportionate negative/actual-provider evidence. The only open issue is nonblocking inherited warning debt; approval applies to Task 8, not whole-branch, institutional MFA or production acceptance.
