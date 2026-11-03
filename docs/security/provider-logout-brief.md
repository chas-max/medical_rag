# Provider-initiated session revocation — connected continuation

Execute only after mandatory patient-app audit passes independent review.
This implements the approved session-revocation requirement, not institutional
identity proofing or production acceptance. Keep all patient-vertical global
constraints, preserve existing changes and .env, and keep legacy APIs/workers
locked. No automatic database migration, real provider registration or external
message is authorized. No commits/staging.

## Outcome and protocol profile

Connect a signed institutional OIDC provider logout to current local sessions,
including a pending-login fence. Do not merely implement a token-verifier demo.
Use the current pinned trusted issuer/client/RSA key and mature PyJWT verifier;
do not fetch keys or trust token jku/jwk/x5u. Key rotation remains a separate
task. Official protocol references (checked 2026-11-02):

- https://openid.net/specs/openid-connect-backchannel-1_0.html (sections2.4-2.8)
- https://openid.net/specs/openid-connect-core-1_0.html (ID-token validation)

Add POST /auth/backchannel-logout to the protected application. It is the ONLY
new no-cookie/no-CSRF POST exception: exact trusted HTTPS Host still required;
no Origin is permitted for server-to-server requests, or the exact app Origin
if supplied; reject other/duplicate Origins. Do not exempt other /auth routes
or an arbitrary prefix. Ignore forwarded identity headers. No session cookies
are read, issued or changed by this route.

Use a bounded application/x-www-form-urlencoded body (max32768 bytes), exactly
one logout_token (max16384 characters), no duplicate form keys, no query
parameters. Ignore bounded unknown form parameters, as the protocol permits;
never use them as authority. Reject other media/encoding, invalid UTF-8 and
malformed percent encoding with static400. Use the standard JSON envelope and
the audited server-generated request UUID. Valid accepted/no matching sessions
returns200 with data:null; invalid tokens400; storage/audit errors503. Never
return account existence, counts, subject/sid/jti or token error details.

Verify RS256 signature against the same fixed trusted public key as ID tokens;
require iss exact configured issuer, aud exact configured client string, iat,
exp,jti,events and at least sub or sid. Require integer (not bool) NumericDates,
iat<=now, iat>=now-120, exp>now, exp>iat, exp<=iat+300. This is an intentionally
strict supported provider profile, not universal provider compatibility.
Require typ=logout+jwt; reject critical or embedded/remote key-selection
headers. Allow an ordinary bounded kid only as a label for the pinned key,
never as a URL/path or an alternative trust source. events must contain
http://schemas.openid.net/event/backchannel-logout with an object value.
nonce must be absent; ignore otherwise unknown vendor claims. This does not
disable registered JWT constraints: optional nbf, if present, must be an exact
integer NumericDate no later than now; keep mature not-before validation.
See https://datatracker.ietf.org/doc/html/rfc7519#section-4.1.5.
sub, if present, uses existing nonempty/trimmed <=255-character subject
constraints; this strict profile additionally rejects control characters
and unencodable UTF-8 surrogate code points in sub. sid and jti must be
nonempty trimmed strings <=255 characters, no control characters or surrogates.
Malformed present optional values deny; do not silently treat them as absent.
ID tokens must not be accepted as logout tokens or vice versa.

## Session correlation, replay and pending-login fence

Add focused provider_logout.py for validated claims/protocol processing, plus
migration0006_provider_logout.sql and tests/test_provider_logout.py. Keep SQL
revocation/session operations in a focused store or SessionStore extension;
do not duplicate current authority logic. Extend VerifiedIdentity with optional
provider_session_id and authenticated_at (repr=False) with backward-compatible
defaults for trusted headless test construction. Actual ID-token verification
always fills authenticated_at from verified auth_time and validates optional
sid. Store authenticated_at and SHA256(UTF-8 sid), never raw sid, on sessions.

Persist append-only provider logout events: server event UUID, trusted tenant
and issuer, hashed jti, nullable hashed sub and sid, verified issued-at/expiry,
DB receipt time. Unique tenant+issuer+jti hash. No raw JWT, sub/sid/jti,
patient/resource IDs or free-form JSON. Force institution RLS with a trusted
transaction-bound scope and explicit tenant predicates; append-only trigger;
runtime SELECT/INSERT only, no UPDATE/DELETE/TRUNCATE/ownership/BYPASSRLS.
Keep fences until a reviewed retention policy exists; do not delete them merely
because the short-lived JWT expires. This is not owner-proof audit storage.

On first valid event, atomically persist its fence and revoke matching local
sessions. sub-only matches the institution's exact issuer+subject account(s);
sid-only matches issuer+sid hash; both claims use conjunction, not OR. Revoke
only sessions with authenticated_at<=logout iat (legacy NULL counts as older).
No local active-role/MFA requirement applies to the signed provider authority;
revocation must also work for locally disabled accounts/institutions. It grants
no patient-data access. Missing targets are indistinguishable successful no-ops.

Duplicate jti with identical claims is idempotent200, with no second revocation
of a later freshly authenticated session. Reuse with different target/time
claims is static400. Record each HTTP attempt safely through mandatory audit.

Serialize ALL session issuance and provider-fence insertion per institution
with the same PostgreSQL transaction advisory lock, acquired before reading
fences or inserting sessions. This may be coarse-grained because login/logout
DB operations are short; token exchange must not hold it. A current primary
transaction and bounded existing lock/statement deadlines remain mandatory.
After acquiring it, a fresh statement rejects issuance when a matching logout
event has iat>=verified auth_time. Unknown auth_time denies if a relevant fence
exists. A new verified auth_time strictly later than the fence may issue; same
second remains denied intentionally. A pending code exchange must not restore
old authority after logout, regardless of which request started first. Do not
depend on an UPDATE snapshot or a stale pre-transaction fence query.

All actual SessionStore uses must require the new schema/grants; update affected
headless fixtures rather than silently bypassing fences. Reuse current trusted
tenant/issuer context, not request account/tenant fields. No patient binding is
created by provider logout or by successful reauthentication.

## Audit, startup and UI boundaries

Record the provider as an external authority, operator_id=null, not as the
patient who happened to be logged out. Use fixed action provider.logout,
resource_type session, static bounded reason/outcome enums. Integrate into
the existing request context, one event per logical request. Extend audit
constraint values through migration6 as needed, not unchecked text.

Revocation/fence persistence must survive an audit outage, just as an already
revoked browser logout must. After committing the revocation, record audit
before emitting200; audit failure emits503 with no private data. Retry of the
same committed event is idempotent and still attempts audit before success.
Database failure before revocation commit emits503 and never reports success.
All failures are static and never log provider token/claims or SQL exceptions.

Extend least-privilege grant recipe and trusted startup required schema/column
grants/RLS/denied mutation diagnostics to migration6. Update owner-run setup
instructions; do not create privileged roles or apply migrations automatically.
Existing browser terminal barrier remains unchanged. The next request after
provider revocation must fail current authentication; idle-page push clearing
is NOT implemented by a webhook and must not be advertised as instantaneous.
The IdP must register/support the exact endpoint and strict token profile.

## TDD and verification contract

Start with failing integration tests, then minimal implementation. Signed test
provider tokens and actual disposable PostgreSQL with least runtime grants:

- HTTP login->consent/content works, signed subject logout->old cookie401;
  fresh verified login later works. sub/sid/both target exactly intended issuer,
  institution/account/session; no target existence/count is returned.
- Pending login on each side of the advisory-lock order cannot reactivate old
  auth_time. Include sid-only event before any local matching session exists.
- Replayed event does not revoke a later new session; duplicate-jti retarget
  denied; wrong issuer/audience/signature/algorithm/type/time/events, nonce,
  missing target, malformed present sid/sub/jti, duplicate/oversized form deny.
- No-cookie provider POST works; TLS/Host/other Origin protections unchanged;
  browser logout/grant and other POST routes still require Origin+CSRF.
- Audit INSERT loss leaves provider-revoked session/fence revoked, emits503;
  storage failure returns503; retry after audit recovery is safe and auditable.
- No raw provider claims/tokens, guessed IDs or arbitrary fields in public
  errors/audit/logs; same server requestUUID ties safe events and responses.
- Force RLS, pooled transaction scope, append-only and startup missing6/broad
  privilege rejection; existing auth/patient/audit/startup/lockdown regressions.

Run new module coverage >80%, affected actual PostgreSQL regressions, ruff,
frontend tests/lint/both builds (no UI change is required). Report exact RED/
GREEN and limitations to docs/security/provider-logout-report.md; independent
spec+quality review required before the task is complete. No real provider,
hospital proofing, penetration or clinical/legal/production approval claimed.
