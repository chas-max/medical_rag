# Task 7 independent specification and quality review

### Spec Compliance

- ❌ Issues found: actual-provider rotation failure diagnostics can expose the approved ring/header identifiers. `tests/test_keycloak_rotation.py:53` and `tests/test_keycloak_rotation.py:82` place the sensitive mapping directly inside rewritten pytest assertions, contrary to `docs/security/signing-keyring-brief.md:47` (ring excluded from repr/errors) and `docs/security/signing-keyring-brief.md:121` (static fixture failures/no header diagnostics). See Important I1.
- ✅ The production-side task requirements otherwise match the diff: exactly one legacy/ring mode, bounded names and 1..8 RSA keys, >=2048-bit validation, duplicate-material rejection, defensive copying, immutable mapping handoff, exact single-key selection, duplicate protected-header rejection, RS256-only policy, and rejection of remote/embedded/critical headers are implemented in `src/medical_rag/signing_keys.py:15`, `src/medical_rag/signing_keys.py:36`, `src/medical_rag/signing_keys.py:51`, and `src/medical_rag/signing_keys.py:58`.
- ✅ Both existing verification purposes use the shared selector without changing their claims/profile checks (`src/medical_rag/oidc_identity.py:60`, `src/medical_rag/oidc_identity.py:71`, `src/medical_rag/provider_logout.py:55`, `src/medical_rag/provider_logout.py:64`); login configuration carries the same frozen trust to both (`src/medical_rag/oidc_login.py:114`, `src/medical_rag/oidc_login.py:124`, `src/medical_rag/oidc_login.py:314`).
- ✅ Strict TOML mode exclusivity and complete name/cardinality checks precede listed key-file reads; bounded PEM loading and the existing static startup boundary remain (`src/medical_rag/security_config.py:79`, `src/medical_rag/security_config.py:82`, `src/medical_rag/security_config.py:117`, `src/medical_rag/security_config.py:149`).
- ✅ The retirement proof is valid: the identical actual old ID token and original nonce pass the overlap verifier immediately before removal, then fail the newly constructed current-only verifier; current-key HTTP authentication is tested separately (`tests/test_keycloak_rotation.py:88`, `tests/test_keycloak_rotation.py:89`, `tests/test_keycloak_rotation.py:93`, `tests/test_keycloak_rotation.py:98`). This is correctly a verifier-level retirement check, not arbitrary-token HTTP authentication. The upstream token comes from the native code/S256 exchange rather than a local signature (`tests/keycloak_operator.py:53`, `tests/keycloak_operator.py:78`).
- ✅ The fictional operator's imported authority is only realm-management/manage-realm; confidential standard-flow/S256 configuration disables implicit, direct/password, and service-account grants; it is kept separate from patient provisioning (`tests/keycloak_runtime.py:227`, `tests/keycloak_runtime.py:272`, `tests/keycloak_runtime.py:275`, `tests/test_keycloak_identity.py:50`). Native admin operations use the fixed disposable-realm component endpoint; cleanup validates canonical ID, tracked name, parent, provider type and provider ID, without following Location (`tests/keycloak_operator.py:87`, `tests/keycloak_operator.py:113`, `tests/keycloak_operator.py:130`, `tests/keycloak_operator.py:146`). A dedicated provider fixture prevents cross-scenario signing-key contamination (`tests/test_keycloak_rotation.py:24`).
- ✅ Realm credential enrollment uses bounded Docker stdin into container tmpfs, not a host realm-import file (`tests/keycloak_runtime.py:49`, `tests/keycloak_runtime.py:354`, `tests/keycloak_runtime.py:378`). Cleanup attempts each separately verified owned target, aggregates only fixed diagnostics, and preserves the original failure (`tests/keycloak_runtime.py:171`); focused tests exercise failure/ownership combinations and bounded import privacy (`tests/test_keycloak_runtime.py:47`, `tests/test_keycloak_runtime.py:78`).
- ✅ Operator example/runbook document deliberate reload, independent material/fingerprint approval, overlap-before-switch, institution-approved retirement windows, separate compromise/session revocation, and no production acceptance (`deploy/patient/config.example.toml:25`, `docs/security/signing-keyring-runbook.md:8`, `docs/security/signing-keyring-runbook.md:18`, `docs/security/signing-keyring-runbook.md:28`, `docs/security/signing-keyring-runbook.md:39`).
- ⚠️ Cannot verify from this task diff alone: the entire unchanged primary-PostgreSQL patient authorization implementation, all cross-route cookie/CSRF behavior, absence of legacy exposure across the complete application, historical Task6 approval/TDD execution, and production/institutional operating controls. Controller should retain the prior gates and full-branch review. This change only adds key-selection wiring; it does not itself establish those cross-task guarantees. Existing full-context auth protections remain visible in `src/medical_rag/oidc_login.py:398`, `src/medical_rag/oidc_login.py:439`, and `src/medical_rag/oidc_login.py:531`.

### Strengths

- Trust validation/selection is a focused 76-line helper, not a second JWT cryptography stack. It leaves RSA verification to PyJWT and has no network/discovery or token-directed authority (`src/medical_rag/signing_keys.py:31`, `src/medical_rag/signing_keys.py:58`).
- Tests exercise both token purposes, mutable caller mappings, wrong-key labels, malformed/absent/unknown IDs, bad modes/material, forbidden headers/algorithms, pre-open configuration checks, and file bounds rather than asserting helper internals alone (`tests/test_signing_keys.py:53`, `tests/test_signing_keys.py:65`, `tests/test_signing_keys.py:88`, `tests/test_signing_key_config.py:53`, `tests/test_signing_key_config.py:69`).
- Actual-PG HTTP tests preserve logout ordering, pending-exchange fencing, and replay behavior under each approved key (`tests/test_signing_key_http.py:79`, `tests/test_signing_key_http.py:97`). Actual-provider tests separately exercise old/new-key login and real provider logout, with an honest TestClient-versus-provider-socket boundary (`tests/test_keycloak_rotation.py:62`, `tests/test_keycloak_rotation.py:72`, `tests/test_keycloak_rotation.py:77`, `tests/test_keycloak_rotation.py:83`).
- Cleanup is materially safer than the baseline: one inspect/remove failure cannot skip remaining owned resources, and ownership failure cannot authorize deletion (`tests/keycloak_runtime.py:171`, `tests/test_keycloak_runtime.py:47`).

### Issues

#### Critical (Must Fix)

- None found in this task scope.

#### Important (Should Fix)

- **I1 — Sensitive ring/header values leak through assertion failure explanations.** `tests/test_keycloak_rotation.py:53` asserts `len(overlap) == 2 and old_kid in overlap`; `tests/test_keycloak_rotation.py:82` asserts membership in that same actual-provider ring. If the provider publishes an unexpected count or signing selection is wrong, pytest's assertion rewriting prints the mapping, including enrolled key identifiers and public-key object representations. The equality at `tests/test_keycloak_rotation.py:69` also prints actual header kid values on mismatch. These are real failure paths of the scenario under test, not evidence that passing output leaked. The brief explicitly requires the ring to stay out of errors and fixture failures to be static. Compute boolean checks outside assertions and assert only those booleans with fixed messages, without sensitive call operands/mappings in rewritten expressions; apply the same privacy pattern consistently to actual-provider assertions. Add a synthetic failure-output regression that proves ring/header/token/nonce canaries are absent. A successful actual-provider rerun alone cannot verify this negative-path property.

#### Minor (Nice to Have)

- **M1 — Existing dependency warning debt remains visible.** `docs/security/signing-keyring-report.md:237` reports the Starlette/HTTPX and Authlib/HTTPX deprecations. They are pre-existing, disclosed, and not caused by this patch; they do not block this task independently. Track compatible dependency maintenance separately and do not suppress warnings merely to obtain clean output. The unrelated legacy Ruff findings (`docs/security/signing-keyring-report.md:240`) are outside this task gate, not a new Task7 defect.

### Checks and scope

- Reviewed the authoritative brief, implementer report, and full-context task diff. Initial tool output was truncated, so the omitted sections were completed using bounded diff slices; no changed source file was reread separately and no broader source crawl was performed. A focused diff-line mapping pass supplied exact source references. No git commands, `.env` access, source/index edits, or reported-suite reruns were performed.
- Accepted the controller's exact-source hash verification and reported frozen-source suite evidence as provenance, not as an independent execution claim: broad 1476 passed / 5 skipped / 304 deselected with the known unrelated anchor failure excluded; affected actual-PG/provider/helper, lint and frontend checks are recorded in the implementer report. Controller subsequently reported a fresh actual rotation/helper run: 69 passed, two known warnings, helper 50/50 statements covered. The review verdict is based on code and the focused diagnostic experiment below, not implementer testimony alone.
- Ran an in-memory synthetic pytest assertion-rewrite experiment using this checkout's pytest runtime, no files or actual upstream values. A failing ring-count expression rendered `len({'HEADER_KID_CANARY': 'PUBLIC_KEY_CANARY'})`; a failing membership expression rendered the same complete mapping. A failing kid equality rendered the two kid strings, not the JWT. This confirms I1 without deliberately failing a real-token test. The initial synthetic JWT used invalid base64url signature padding; correcting that harness input produced the diagnostic results above.
- The same experiment showed a direct `assert verifier.verify(token, expected_nonce=nonce)` would render its arguments **if the verifier returned false**. The current verifier contract instead returns a truthy `VerifiedIdentity` or raises static `InvalidIdentity`, so the reviewed code does not establish a reachable token leak from `tests/test_keycloak_rotation.py:49` or `tests/test_keycloak_rotation.py:88` alone. Avoid claiming one; moving these calls outside assertions is sensible privacy hardening alongside I1.

### Assessment

**Task quality: Needs fixes.**

**Reasoning:** The production trust-ring implementation is focused, bounded, and well tested, and the actual-provider retirement proof and native operator authority are substantively valid. The actual-provider test's non-static failure explanations violate an explicit privacy bound and must be corrected before this task gate can approve.

## I1 narrow-fix re-review — frozen 2026-11-03T15:28:52+08:00

### Spec Compliance

- ✅ **Spec compliant for this task gate; I1 closed.** Actual-provider ring cardinality/membership and old/new header checks now compute booleans before asserting only those locals with fixed messages (`tests/test_keycloak_rotation.py:55`, `tests/test_keycloak_rotation.py:56`, `tests/test_keycloak_rotation.py:75`, `tests/test_keycloak_rotation.py:76`, `tests/test_keycloak_rotation.py:95`, `tests/test_keycloak_rotation.py:96`). The original-key-set check follows the same pattern (`tests/test_keycloak_rotation.py:123`, `tests/test_keycloak_rotation.py:124`). All 16 reviewed assertions use this privacy shape; operations and sensitive operands are not passed to pytest's rewritten assertion expression.
- ✅ The actual check semantics are preserved, not replaced with vacuous booleans: ring size remains exactly two with the original key, old logout still identifies that key, new logout still identifies a distinct approved key, and original trust remains exactly the captured old key (`tests/test_keycloak_rotation.py:55`, `tests/test_keycloak_rotation.py:75`, `tests/test_keycloak_rotation.py:95`, `tests/test_keycloak_rotation.py:123`). Existing real provider login/logout acceptance and session-revocation comparisons remain present in the full-context fix diff.
- ✅ The retirement proof still verifies the same actual captured token with its original nonce immediately before removal, then expects `InvalidIdentity` from the newly constructed current-only verifier; actual current-only HTTP authentication remains separate (`tests/test_keycloak_rotation.py:106`, `tests/test_keycloak_rotation.py:107`, `tests/test_keycloak_rotation.py:114`, `tests/test_keycloak_rotation.py:119`, `tests/test_keycloak_rotation.py:120`). The `bool(...)` wrapper does not change the real truthy-identity/static-exception contract. No signature substitute, new bearer endpoint, or production/fixture trust change is introduced.
- ⚠️ Scope remains task-only, not whole-branch or production approval. The controller reports the prior cross-task authorization/isolation gates resolved using prior reviews, unchanged hashes, and legacy regression evidence; those unchanged systems were not reopened by this narrowly scoped re-review.

### Strengths

- The privacy regression is tied to the actual scenario AST: it reads but does not import/run the real-provider test, extracts each assertion with its immediately preceding computation, and enforces the boolean-local/fixed-message shape across all 16 assertions (`tests/test_keycloak_rotation_privacy.py:26`, `tests/test_keycloak_rotation_privacy.py:57`).
- Six synthetic negative cases execute those actual extracted expressions through the installed pytest assertion rewriter, require an actual assertion failure, and reject key/header/token/nonce canaries from the rendered short failure output (`tests/test_keycloak_rotation_privacy.py:81`, `tests/test_keycloak_rotation_privacy.py:97`, `tests/test_keycloak_rotation_privacy.py:100`, `tests/test_keycloak_rotation_privacy.py:103`, `tests/test_keycloak_rotation_privacy.py:106`, `tests/test_keycloak_rotation_privacy.py:110`). Requiring the real fixed message and `assert False` prevents a blank/irrelevant diagnostic from satisfying the test (`tests/test_keycloak_rotation_privacy.py:112`, `tests/test_keycloak_rotation_privacy.py:115`).
- The regression accurately labels false-returning verifier stubs as defensive hardening rather than claiming the original verifier could return false (`tests/test_keycloak_rotation_privacy.py:82`). Its `exec` scope is checked-in AST with synthetic inputs only, not arbitrary provider code or credentials (`tests/test_keycloak_rotation_privacy.py:103`).

### Issues

#### Critical (Must Fix)

- None.

#### Important (Should Fix)

- None remaining. Historical I1 above is resolved by this follow-up.

#### Minor (Nice to Have)

- Historical M1 remains nonblocking existing dependency-deprecation debt; the follow-up neither creates nor suppresses it. No new Minor finding.

### Checks and scope

- Read the appended I1 implementer-report section and the complete two-test-file privacy-fix diff; a focused diff-to-source line-reference pass supplied citations. Did not reread changed source separately, crawl unrelated code, access `.env`, invoke git, or modify source/index state. Only this append to the authorized review report was written.
- Did not rerun reported suites or repeat the earlier diagnostic experiment. The final regression itself directly addresses that experiment's concrete failure mode. Reported evidence reviewed: original/final-harness baseline both 7 failed with canary failures; final privacy/actual-provider/signing run 76 passed with two known warnings; scoped Ruff passed. Controller independently reported final hashes matched and privacy-only 7 passed with one known warning. These are attributed execution results, not executions performed by this reviewer.
- Failure-output coverage is explicitly ordinary short pytest output with `showlocals=False` (`tests/test_keycloak_rotation_privacy.py:106`); this is not a claim that enabling arbitrary local-variable dumps or external secret-bearing logging becomes safe.

### Assessment

**Task quality: Approved.**

**Reasoning:** The narrowly scoped fix removes the concrete sensitive assertion introspection without weakening actual-provider overlap, logout, or same-token retirement validation. The regression exercises real assertion source and verifies meaningful synthetic failure output; no Critical or Important finding remains for Task7.
