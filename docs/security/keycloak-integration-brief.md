# Actual identity-service interoperability (fictional accounts only)

Execute only after provider logout (Task5) passes independent review. Continue
the approved mature identity-component requirement: do not implement passwords
or claim institutional patient proofing from an identity-service login.
Global constraints in patient-vertical-brief.md apply; preserve .env and dirty
changes, no commits/staging, real accounts, hospital calls or legacy unlock.

## Deliverable

Add an operator-configured private-CA option for the real token endpoint and
an actual Keycloak interoperability integration suite. It must exercise the
existing patient factory/OIDC client/audit/provider-logout/session code, not a
second auth implementation or a mocked token response. This stage does NOT
deploy a production IdP, certify MFA policy or replace hospital identity proofing.

Use the already downloaded official image exactly:
quay.io/keycloak/keycloak:26.8.1@sha256:3f3c5f8e03c614774b4bd9173b88391fefa4153bfc010d8012d59ad825377332

Official references checked2026-11-02:

- https://www.keycloak.org/getting-started/getting-started-docker
- https://www.keycloak.org/server/containers
- https://www.keycloak.org/server/configuration-production
- https://www.keycloak.org/server/supported-configurations
- https://www.keycloak.org/docs/latest/server_admin/

Do not use latest or arbitrary untrusted images. Keycloak image is cached;
python:3.13-alpine,postgres:18.6-alpine are also cached for test infrastructure.
Version is a verified test pin, not a guarantee of indefinite security support.

## Trusted TLS configuration

Extend LoginConfiguration with optional provider_ca_file: Path|None (repr=False),
and strict trusted TOML [oidc] optional provider_ca_file. Old configs retain
system/public-CA verification. Resolve operator paths relative to TOML. Validate
an explicitly supplied PEM trust bundle at startup, bounded65536 bytes; missing,
empty, malformed or oversized files fail with the existing static StartupError.
No request-driven CA/issuer/key/endpoint values and no environment-based trust
override. Do not read old .env or modify global/system certificate stores.

When supplied, build a fresh mature SSLContext from bounded trusted PEM content
for token-endpoint HTTPX transport; hostname verification and CERT_REQUIRED
remain enabled, minimum TLS1.2. Do not set verify=False/check_hostname=False or
trust_env=True anywhere. If the file later becomes invalid/unreadable, login
fails closed with static errors, never a silent public-CA/insecure fallback.
Do not leak paths/CA bytes/provider bodies in responses, repr or logs. Keep the
helper focused (provider_tls.py if needed), reuse it for future key retrieval.
Existing signed-provider tests may keep their explicitly injected MockTransport;
the ACTUAL Keycloak suite must not patch the client, verifier or token response.

## Actual disposable provider suite

Create tests/test_keycloak_identity.py, marked integration and explicitly gated
RUN_KEYCLOAK_INTEGRATION=1. When selected, missing Docker/images is a test failure,
not a hidden skip or replacement by fake identity. Actual app DB remains an
independent disposable PostgreSQL with migrations1-6, current runtime recipe and
fictional owner-provisioned adult/self binding. Never use the user's databases.

Keep test infrastructure focused: tests/keycloak_runtime.py may own disposable
containers/network/CA/realm provisioning and cleanup; tests/keycloak_forms.py
may own the bounded native form driver; tests/fixtures/keycloak_collector.py
may be the small stdlib TLS collector served only inside the test fixture.
Keep assertions/scenarios in tests/test_keycloak_identity.py, not one huge
test-plus-server module. These helper paths are test-only, not production APIs.

Start one uniquely named/labeled disposable Keycloak realm/client/users fixture
with generated fictional credentials and test CA/certificate files. Test-only
start-dev/H2 is permissible ONLY inside this explicit disposable fixture, never
in a production recipe or suggested as production. HTTPS only, public port bound
127.0.0.1, actual certificate validation using the fixture CA. No global trust
installation or security-interstitial bypass. No real account/SMTP registration.
Use bounded readiness polls and per-request/connect deadlines. Cleanup only
verified fixture-owned containers/networks/tmpfs; preserve other Docker services.

Import a unique realm with a confidential OIDC client, code flow+S256 PKCE,
exact HTTPS callback and post-logout URLs (no wildcard), no implicit/direct
password-grant/service-account flow. Do not fake MFA by assigning a constant
acr mapper. A patient login can use actual fictional password authentication;
an admin without real assurance must remain denied. Proving actual OTP/WebAuthn
assurance/policy is a distinct remaining gate, not inferred from a signed claim.

Fetch the actual realm's public verification key via verified HTTPS/operator
fixture provisioning and configure the existing pinned RSA verifier. The suite
must not create/sign its own upstream ID/logout token or stub the token endpoint.
Actual Keycloak does both. Local SQL records are explicitly fictional and not
hospital identity verification; IdP role claims must not grant local privileges.

Drive native login-form semantics over actual verified HTTPS with HTTPX and a
bounded stdlib HTMLParser: initial app POST /auth/login->real authorization
page->fictional credential form POST->real code redirect->existing callback.
Application callback may use TestClient on the exact configured HTTPS origin;
report this transport boundary honestly (not real browser/full-socket E2E).
Existing application token exchange must contact actual Keycloak over TLS using
the new trusted-CA option. No real credentials or bodies/token-bearing URLs in
logs or failure reports.

Then read session/identity, grant fictional-purpose consent, create/read own
record and verify a second fictional identity cannot read it. Wrong password,
wrong CA and an unprovisioned/disabled local account fail without new session
cookies. An active account with an unverified patient binding may authenticate
for its identity view, but must not gain health-consent/patient-content access.
Provider login alone does not auto-grant purpose consent. Local roles and MFA
checks remain authoritative, not the Keycloak role claims.

Trigger a REAL Keycloak logout using its normal user logout confirmation or a
fixture-only actual admin API. Capture its actual backchannel HTTPS form using
a small disposable TLS collector on a private Docker network (stdlib Python
image is acceptable; no request/body logging). A capture collector is test
transport, not the production logout implementation. Deliver the captured
unchanged signed form to the actual protected app backchannel route and verify
old local cookies fail401, audit metadata has no raw upstream claims, replay
does not undo a subsequent fresh login, and a later actual reauthentication
can work. No synthetic/hand-signed logout token in this suite. If a captured
provider profile does not match requirements, report concrete claims privately
and resolve the protocol conflict; do not silently weaken validation.

## Verification and handoff

TDD for trusted-CA behavior first; run actual provider suite to expose real
interop failures, implement only required corrections while preserving trust.
Run selected actual Keycloak tests, actual PG auth/provider/audit/startup
regressions, TLS-helper coverage>80%, ruff, frontend tests/lint/both builds.
Test CA-mismatch denies actual TLS, not only a mocked exception. Also test
bounded/invalid CA files and existing strict no-CA configurations.

Close Task5's nonblocking deployment-validation follow-up: extend the existing
tests/test_security_server.py owner-grant-recipe test with an actual runtime
provider append/revoke/fence/identical-replay sequence under that exact recipe
role. Preserve later-new-auth survival. This is test coverage, not new authority.

Update deploy/patient/config.example.toml with an optional PUBLIC trust-bundle
file path and no secrets. Write exact commands/results/image digest/transport
boundaries to docs/security/keycloak-integration-report.md. Root owns README/
progress/readiness. Independent spec+quality review gates task completion.
Do not claim this is production identity deployment, genuine patient proofing,
real institutional MFA, clinical/legal acceptance or full-plan completion.
