# Real second-factor assurance — connected continuation

Prepared2026-11-03. Execute after Task7 signing-key overlap independently
passes. Continue the approved mandatory staff-MFA requirement with actual
Keycloak password+OTP authentication, not a hard-coded ACR mapper. Preserve
patient-vertical global constraints and existing changes/.env; no commits,
staging, production imports, real accounts, hospital calls or legacy unlock.
Root owns README/progress/readiness. This is a tested reference policy and
fictional-provider evidence, not institutional MFA certification.

## Deliverable and boundary

Add a reusable public Keycloak authentication-policy fragment plus an actual
disposable-provider suite that reaches the existing auditor API after genuine
password+OTP and denies password-only/bad-OTP use. Keep MFA verification in
Keycloak and cryptographic claim validation in existing mature PyJWT. The
application must never collect/store staff OTP seeds, passwords or recovery
codes. No new password/OTP-verification endpoint or local MFA boolean setting.

Create deploy/identity/keycloak-mfa-policy.json (flow/config/ACR mapping/role
fragment only; no users, credentials, secrets, clients, enabled realm or real
callback). Create deploy/identity/README.md explaining manual institutional
review/enrollment and reference-policy boundaries. Tests must load this same
fragment into an otherwise uniquely owned disposable realm, not a divergent
test-only policy while shipping an untested example. Import/deployment into
an existing or real realm is not authorized by this task.

Focused test support: tests/keycloak_mfa.py may compose the fragment and
fictional credentials into existing runtime provisioning; tests/test_keycloak_mfa.py
owns actual scenarios; extend bounded native form driver for an OTP challenge.
Existing default fixtures retain their password-only behavior. Keep actual
provider/network ownership and private-CA trust controls. Existing application
code should not change unless actual evidence exposes a concrete defect;
report the defect to controller before altering an assurance/permission contract.

Controller preflight found one concrete omission in OIDCLogin.start: its
create_authorization_url call sends state/nonce/code_verifier/max_age but no
acr_values, although the trusted accepted set is configured. Authorized narrow
production change for this task: TDD tests in tests/test_oidc_login.py must
first demonstrate the missing nonessential acr_values request. Then send the
space-separated deterministically sorted configured mfa_acr_values through
Authlib (omit when the in-memory accepted set is empty). Do not request an
essential claim, hard-code the reference ACR into the app, accept a request
override, or turn the requested value into evidence of successful MFA. The
existing verified-token/current-role authorization must still decide access.
Preserve code/S256, max_age, issuer/client/callback and all existing protections.
No other production change is authorized without reporting concrete evidence.

## Reference policy

Use supported Keycloak26.8.1 native conditional authentication levels. Fixed
ACR urn:medical-rag:reference:otp maps to LoA2. A separate realm role
medical-staff-mfa selects the required staff challenge; it is an IdP policy
classification, NEVER a local business-role grant. Existing MFA-required roles
(org_admin, integration_admin, auditor, platform_operator) must still require
a verified accepted MFA ACR independently. This task does not silently change
the approved role policy for knowledge_admin/support. An
IdP staff classification without a local role grants no auditor/admin access;
a local auditor without that provider classification must still be denied if
only password assurance is returned.

Reference flow: native cookie/interactive browser structure, level1 required
username/password; conditional staff level2 with role condition REQUIRED,
conditional-level-of-authentication REQUIRED and native auth-otp-form REQUIRED.
Level2 loa-max-age=0 prevents treating a previous completed challenge as a new
OTP challenge. Do not make OTP merely ALTERNATIVE or condition it only on
whether an OTP is already configured; unenrolled staff must not silently bypass
it. Patients without the staff classification may authenticate at level1 but
must never receive the accepted level2 ACR. This is nonessential acr_values
behavior: lower assurance may authenticate identity, not staff business access.

Primary references checked2026-11-03 (interpret actual26.8.1 behavior through
tests; do not infer a constant claim proves the flow):

- https://www.keycloak.org/docs/latest/server_admin/ (step-up and ACR/LoA)
- https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/services/src/main/java/org/keycloak/authentication/authenticators/conditional/ConditionalLoaAuthenticatorFactory.java
- https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/services/src/main/java/org/keycloak/authentication/authenticators/conditional/ConditionalLoaAuthenticator.java
- https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/services/src/main/java/org/keycloak/authentication/authenticators/conditional/ConditionalRoleAuthenticatorFactory.java
- https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/services/src/main/java/org/keycloak/authentication/authenticators/browser/OTPFormAuthenticatorFactory.java

If the combined conditional flow does not emit the required level ONLY after
actual OTP success, stop with the concrete structural conflict and propose a
supported provider-flow correction. Do not add a hard-coded claim mapper,
trust an upstream role as MFA, mark a local account verified, or relax backend
MFA checks to make tests pass. Keep disabled direct grants, implicit and service
accounts, exact callbacks, code+S256, pinned trust and mandatory audit.

## Real verification and privacy

Generate fictional OTP seeds only in fixture process memory and import through
Keycloak's supported credential model using Task7's bounded stdin/container
tmpfs import. No host realm-import credential artifact or seed in arguments,
environment, reports or logs. Generate test codes with
existing mature cryptography TOTP utility; verification remains the actual
provider. Native bounded verified-HTTPS forms must prove password acceptance
followed by an OTP challenge before any app session can be issued. No locally
signed upstream JWT, MockTransport, patched verifier or fabricated token.
Handle timestep boundaries with a bounded deterministic strategy; do not sleep
unboundedly or weaken OTP validation. Do not log seed, OTP, password, token,
callback query or provider page. Static failure messages/boolean privacy
assertions retain prior test hygiene. Precompute sensitive-expression results
into boolean locals before pytest assertions so assertion rewriting cannot
render a token, nonce, seed, provider page or trusted-key mapping on failure.

TDD policy structure/driver behavior and actual-provider scenarios:

- Patient password-only login returns identity/self functionality with false
  mfa_verified and no staff/auditor permission, despite the requested ACR.
- Staff password without completed OTP, wrong OTP, missing/unconfigured OTP,
  stale challenge and guessed callback cannot obtain a staff-capable session.
  Bad OTP must be rejected by actual Keycloak, not merely local format checks.
- Correct password+OTP with actual signed accepted ACR and current local auditor
  role permits /me audit permission and GET /audit. It still denies patient
  consent/content absent a current self-patient role and binding.
- Correct actual MFA without local auditor role denies /audit. Conversely,
  local auditor with only low-assurance provider authentication denies /audit.
- Reauthentication requires the real second factor again per the reference
  level2 policy; do not rely only on the first login's claim value. No assertion
  of immediate cancellation of already-authorized operations or idle-page push.
- Current local role revocation/account disable blocks the next auditor access
  after actual-MFA login; actual provider logout invalidates old local cookies
  via the existing signed route. No new public patient-data capability.
- Audit/public errors/logs contain no credentials, OTP/seed, raw upstream tokens
  or medical content. Same server request-ID and static metadata remain.

Prove actual upstream assurance with the existing verifier/session authority;
do not persist raw provider claims or expose them as a debugging API. Explain
the application TestClient versus provider real-socket boundary. Browser/real
institution enrollment, recovery, account-proofing, device lifecycle and
institution-approved assurance policy remain separate gates. Reference TOTP is
not phishing-resistant WebAuthn and is not a complete production IAM service.

## Verification/handoff

Report full RED/GREEN commands and protocol results to
docs/security/actual-mfa-report.md. New pure policy/driver support coverage>80%,
actual selected provider-MFA tests without hidden infrastructure skips,
existing actual-Keycloak/keyring cases, affected actual-PG auth/audit/startup
regressions, scoped Ruff and frontend tests/lint/both builds. Source hashes and
exact filelist for independent task review. Known dependency/legacy lint noise
must stay disclosed. Never count this as full-plan/production acceptance.
