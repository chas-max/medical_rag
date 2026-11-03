# Operator-approved signing-key overlap — next continuation

Prepared2026-11-03. Execute only after Task6 actual Keycloak/private-CA review
passes. This continues the approved key-lifecycle requirement; it is not a
new identity service or automatic provider discovery. Global constraints from
patient-vertical-brief.md remain binding. Preserve dirty changes and .env;
no commits/staging, hospital calls, real accounts, owner DB migrations or legacy
unlock. Root owns README/progress/readiness.

## Scope and design decision

Implement a bounded operator-approved public-key ring for routine provider
signing-key overlap, shared by ID-token and backchannel-logout verification.
Do not fetch a token-supplied URL/key or add dynamic JWKS discovery. A kid is
only an exact lookup into the immutable trusted ring, never a path, URL, SQL
identifier, or alternate trust source. This choice preserves the current
out-of-band approval boundary and requires deliberate deployment reloads.

Official primary references checked2026-11-03:

- https://openid.net/specs/openid-connect-core-1_0.html#Signing
- https://openid.net/specs/openid-connect-core-1_0.html#RotateSigKeys
- https://www.keycloak.org/docs/latest/server_admin/#_realm_keys

The protocol supports identifying signing keys by kid and an overlap period.
Keycloak documents active/passive signing-key lifecycle. The local, manually
approved ring here does not implement automatic JWKS refresh, key-compromise
response or a universal provider profile. Operator restart/rolling deployment
is required to activate a changed trust set. Public-key removal does not itself
revoke already-issued opaque local sessions.

## Interfaces and trusted configuration

Create focused src/medical_rag/signing_keys.py and tests/test_signing_keys.py.
Keep RSA signature validation in mature PyJWT; this helper only validates and
selects trusted key material. Adapt OIDCIdentityVerifier, ProviderLogoutVerifier
and LoginConfiguration with an optional verification_keys mapping. Existing
single-public_key callers keep their current meaning. Exactly one mode must
be configured: one pinned public key OR a nonempty named verification_keys
ring. Neither or both fail construction with a static trusted-config error.
The single public_key parameter can be None only when a valid ring is present.

Copy and freeze the trusted mapping at construction; caller mutations must
not alter active authority. Permit1..8 entries, each distinct kid matching
[A-Za-z0-9_-]{1,128}, each an actual RSA public key of at least2048 bits. Do not
accept private, EC, octet, certificate text or key URLs in the in-memory ring.
Public-key/path/claim material and the ring must be excluded from repr/errors.
Do not make cryptographic key generation or algorithm negotiation part of the
application.

Strict operator TOML [oidc] supports exactly one public_key_file OR a nested
verification_key_files mapping kid->public PEM file path. Relative paths use
the TOML directory. Each file read is bounded65536 bytes and uses mature
cryptography public-key parsing. The ring size/key-ID checks occur before
opening any listed files. Unknown/missing/both/wrong-type settings, invalid key
files, duplicate TOML keys, excessive entries and malformed identifiers produce
the same static StartupError, no path/PEM/parser disclosure. Retain Task6's
optional provider_ca_file; do not accept environment/request trust overrides.

In ring mode every accepted ID/logout JWT must have exactly one well-formed
known kid. Unknown, absent, null, empty or malformed kid fails, with no trial of
other ring keys and no fallback to legacy single-key verification. A token
signed by key A and labeled key B fails signature validation even if both are
approved. Retaining the same key under two trusted names is unnecessary; reject
duplicate public-key material to catch operator mistakes.

In legacy single-key mode preserve optional ordinary bounded kid as a label
for that one key; malformed present kid fails. Both modes reject crit and
embedded/remote key-selection headers (jku,jwk,x5u,x5c). Only RS256 remains
supported. Preserve all issuer/audience/nonce/age/registered time/type/events
checks, current-authority checks, audit semantics and provider-fence ordering.
Do not silently relax the strict logout profile or trust provider role claims.

## Connected verification

TDD before source changes. Tests must include both approved keys in both token
purposes, same kid with wrong signature, unknown/missing/malformed kid, invalid
algorithms/key headers, mutation of original mappings, invalid/mixed config,
file bounds and privacy of errors/repr. Add affected actual-PG HTTP login,
provider logout, pending-login fence and replay regressions in ring mode rather
than only helper/verifier demonstrations. Old single-key callers remain covered.

Reuse the actual disposable Keycloak fixture after Task6. Through fixture-only
native administrative operations create a second RSA signing provider, fetch
its public key using existing verified TLS fixture provisioning, and exercise
actual login/actual provider logout under the two-key approved ring. No local
test signature substitutes in this actual-provider scenario. Test deliberate
removal of the retired key from a newly constructed app: an unexpired captured
old upstream token is rejected while current-key authentication still works.
Document exactly which leg uses TestClient versus actual provider sockets.
For the captured-token retirement check, prove the same actual token first
passes with the overlap set and the correct audience/nonce/time, then fails
after key removal; otherwise a nonce/audience failure could be a false positive.
This check may invoke the newly constructed app's existing verifier directly,
because the public callback accepts codes, not arbitrary ID tokens. Do not add
a public bearer/token-verification endpoint. Keep the actual HTTP login/logout
scenarios separate and report this verifier-level boundary honestly.
If actual provider administrative rotation needs a materially new contract,
ask controller before broadening the fixture; never weaken verification to
make a fixture pass.

Authorized fixture-only rotation support: tests/keycloak_operator.py may own
native code+S256 PKCE authentication for a dedicated fictional realm operator
and its bounded actual admin REST calls. Import a separate confidential
fixture-operator client with exact HTTPS callback
https://fixture-operator.invalid/callback, standard flow only, no implicit,
direct password grant or service-account flow. Assign only the fictional
operator realm-management client role manage-realm within the disposable realm;
do not grant realm-admin, master roles or patient-data authority. This role
also satisfies the view check needed for component inspection in26.8.1. Keep
that identity out of patient application provisioning.
No bootstrap master administrator, password-grant workaround or fake admin
bearer is required. The helper may obtain a real code via the existing bounded
native form driver and exchange it over verified TLS with mature Authlib.
Keep access/ID tokens in fixture process memory only. Generated fixture
credentials must not be written to a host realm-import file: pass the bounded
import through Docker stdin into the uniquely owned container's volatile tmpfs.
Keycloak necessarily imports these fictional credentials into its disposable
runtime; this is not a prohibition on provider-internal processing. Cover
transport-failure privacy and absence of host credential artifacts with focused
tests. Use static failures and no URLs/bodies/headers in logs. Administrative rotation
applies only to the uniquely owned disposable realm, never external systems.
Use a separate uniquely owned provider fixture for rotation, or restore its
original signing component in finally so unrelated existing-provider scenarios
cannot inherit a changed active key. Verify created component ID/type/parent
before removal; never follow an unvalidated administrative Location URL.
Fixture JWKS provisioning may select only RSA RS256 signing entries, not
encryption/unsupported keys, over the existing verified TLS connection. It is
fixture trust enrollment, not application network discovery.
Pinned-version primary references for these fixture privileges:
https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/services/src/main/java/org/keycloak/services/resources/admin/ComponentResource.java
and
https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/services/src/main/java/org/keycloak/services/resources/admin/fgap/RealmPermissions.java.

Provide deploy/patient/config.example.toml comments illustrating mutually
exclusive single-key/ring modes with public-file paths only. Write an operator
routine-overlap runbook: independently authenticate new public material and
fingerprints, deploy overlap to every verifier instance first, switch provider
signing, validate both ID/logout paths, retire after institution-approved token
and rollout windows, deploy removal and verify rejection. No universal safe
time promise and no automatic production edits. Emergency compromise requires
separate trust removal plus existing trusted institution/session-version
revocation and independent incident handling; routine overlap is not adequate.
Client-secret and login-encryption-key rotation remain separate work.

Close Task6's nonblocking fixture-cleanup follow-up while modifying runtime
support: cleanup must attempt every separately verified owned resource even
if one inspect/remove fails. Never remove unverifiable/unowned resources.
Preserve the original test failure, attach only fixed aggregated cleanup
diagnostics when necessary, and use static failure if cleanup alone failed.
Cover failure/ownership combinations with focused helper tests; no broad
Docker cleanup. Correct outdated module documentation that claims touched
ID-verification code is unwired; it is connected but not production approval.

Report exact RED/GREEN commands/results, changed files and limitations in
docs/security/signing-keyring-report.md. New-helper coverage>80%, actual PG
affected regressions, actual Keycloak rotation scenario, scoped Ruff and
frontend tests/lint/both builds. Do not hide known dependency/legacy lint noise.
Independent task specification+quality review gates completion. No real
institution, MFA certification, production identity deployment or production
medical-data acceptance is established by these tests.
