# Verified TLS gateway for the isolated patient application

Approved-plan continuation, prepared2026-11-03. Execute only after Task10
validation-repair review passes. This is an independently testable gateway
integration increment, not production release or the entire deployment plan.
Preserve all edits/.env/userRabbitMQ/original checkout; no commits/staging,
real identities/medical data, public deployment, certificate-store edits,
insecure TLS fallback, legacy unlock or cloud publishing. Root owns README,
progress/readiness and review packages. Capture any existing target before edit.

## Boundary and architecture

The existing deploy/gateway/nginx.conf and docker-compose.gateway.yml are a
legacy HTTP/fake-upstream experiment. Leave them separate; do not silently
redirect that configuration to a different service or unlock old APIs. Add a
dedicated operator-configured verified-TLS gateway for security_server's
isolated application. The application remains independently authoritative and
continues direct TLS, exact public Host/Origin, own request IDs, no proxy-header
trust, current sessions/roles/ownership/consents and mandatory audit. Do not
change its verifier, middleware or identity semantics to accommodate a proxy.

Use focused src/medical_rag/patient_gateway.py for a bounded strict TOML
configuration loader and pure nginx renderer/CLI. It must not import legacy
Settings/dotenv or read .env. Create deploy/patient-gateway/config.example.toml,
a focused operator README and docker-compose.patient-gateway.yml. Reuse the
existing pinned nginx image/digest and nonroot101:101, read-only rootfs,
cap_dropALL, no-new-privileges and bounded tmpfs pattern. Certificate/key/CA
files are operator-controlled read-only mounts; their contents must never be
embedded in rendered configuration, environment values, CLI arguments or logs.
Safe configured mount paths are required in configuration and mount arguments.
Do not copy private
keys into the repo or claim templates install certificates/network policy.

## Configuration and nginx requirements

Use explicit fields for one public HTTPS origin, one upstream HTTPS target,
upstream certificate verification DNS name, the gateway certificate/key and
upstream CA mount paths. Public origin must be canonical ASCII DNS/IPv4 or
bracketed IPv6, optional valid nondefault port, no userinfo/path/query/fragment,
wildcards or controls. Upstream is a single explicit HTTPS host:port, not a
variable URI, Unix socket, redirect or user-supplied request target. Keep the
HTTP Host authority including its nondefault public port separate from upstream
TLS SNI/verification name. Reject all ambiguous/unknown/duplicate TOML keys,
overlarge/empty input, raw newline/quote/semicolon/braces/$ injection and invalid
ports/names. Mount paths must be explicit safe absolute POSIX paths with no
control/whitespace/nginx syntax, traversal or interpolation. Examples contain
only placeholders, never institution-approved claims.

Renderer uses fixed directives, not a generic nginx templating engine. Expose
load/render functions with no side effects; CLI prints only static errors and
does not print received configuration, paths or generated config on failure.
Write rendered config only to an explicitly named output with exclusive-create
semantics; never overwrite an existing config by default. Do not use shell
execution to interpolate configuration. Success may print a static completion
message; deployment/reload is a separate operator action, not automatic.

Nginx must verify the upstream chain and hostname (`proxy_ssl_verify on`,
explicit trusted CA, SNI and verification name), TLS1.2/1.3 only, no insecure
retry/fallback. Reject a missing/wrong public Host before upstream dispatch;
forward the configured public authority including port. Do not accept a
forwarded host/protocol/tenant/user/role/authentication assertion. Use a fixed
request-header allowlist for actual app needs (Cookie, Origin, Content-Type,
Accept, Accept-Language, X-CSRF-Token; nginx must retain correct body framing).
Strip Authorization and arbitrary identity/Forwarded/X-Forwarded-* headers;
the isolated app uses opaque session cookies, not an upstream bearer identity.
No access log or request/response/header/query/body logging. Static error log
level only; no token-bearing startup diagnostics.

Restrict routing to the isolated application's current auth/me/conversations/
audit/support/patient.html/assets paths; unknown and old /ui,/api,/settings,
/documents paths must not reach upstream. Do not add health-data or inference
routes. Public paths still rely on the application for authentication policy.
Use {success,data,error} static gateway-generated HTTP errors without reflecting
inputs; distinguish gateway-unavailable/body-limit/rate-limit failures. Do not
rewrite application401/403/404/409/429 bodies or provider302 destinations.

Use bounded100000-byte request bodies, memory buffer at least128k and temporary
storage/concurrency bounds. Preserve streaming readiness: response buffering
and proxy cache off, HTTP1.1, empty Connection, upstream read timeout180s.
Synthetic SSE capability tests do not imply the app offers clinical streaming.
Keep per-connection-source-IP development admission at5r/s burst20,429 on limit;
never derive keys from forwarded headers or unverified cookies/account IDs.
This is not institution-approved rate policy or account-level rate limiting.

Keep application X-Request-ID unchanged for audit correlation; expose the
independent gateway-generated trace as X-Gateway-Request-ID, discarding any
upstream header of that name. Do not create duplicate/conflicting X-Request-ID
headers or claim end-to-end shared tracing while app IDs remain independent.
Responses must not reveal server version, TLS private paths or upstream
exception text; an unversioned built-in nginx Server header is not a version
disclosure. Do not add third-party header modules solely to remove that name.
Production ingress/network access and certificate rotation
remain explicit operator gates.

Compose binds gateway only to loopback by default, no backend published port,
and uses an isolated internal backend network distinct from the edge network.
Document how an independently deployed TLS app joins that network, with no
claim the gateway-only compose file deploys the application/database/IdP.
No unsafe-local-demo connection and no Docker socket mount.

## TDD, actual sockets and verification

Integration-first RED before implementation. New tests/test_patient_gateway.py
and focused fixture/helper files may use existing disposable PG/runtime
infrastructure; do not expand old gateway fake proof into authenticated E2E.
Any added Docker resources need unique ownership labels and bounded cleanup of
only owned resources. Cached images must be reused; do not prune/delete others.
No real keys/logins or production DNS; generated test certificates trusted
explicitly by the test client, never verify=False or system-store changes.

Required proof includes actual nginx HTTPS socket to the actual isolated
application over verified HTTPS and actual disposable PostgreSQL. Application
may run in an owned subprocess/container using its real entrypoint or real
application factory; no fake application routes or authentication bypass.
Use operator-equivalent trusted fictional config/authority fixtures. Test
frontend/static retrieval, missing session401, a current fictional session's
own identity/resource access and foreign-owner denial, forged identity headers,
wrong Host/Origin, retained Cookie/CSRF, and closed legacy paths. A fixture-issued
session is not real OIDC login: disclose that boundary. Existing actual IdP
tests separately cover protocol; full authenticated-browser E2E remains open.

Negative TLS cases must prove wrong CA, wrong verification name or unavailable
upstream yield only static gateway failures and do not downgrade to HTTP or
skip certificate verification. Separately use an owned bounded TLS-only test
upstream to inspect stripped headers/body framing, development limits and SSE
first-chunk delivery before stream completion; label this synthetic capability
proof, not real app inference. Test port-preserving Host, configuration injection,
exclusive output behavior and failure diagnostic privacy without secret reprs.

Run scoped Ruff, pure renderer/config tests with newsourcecoverage>80%, actual
gateway/PG socket tests and Compose config checks. Existing app unchanged: run
only focused affected startup/Host/CSRF tests, not all expensive Keycloak and
frontend suites after every gateway edit. Missing selected infrastructure is
a failure, not a silent mock/skip success. Root owns later broad regression.

Write docs/security/patient-gateway-report.md with exact paths/SHA256freeze,
RED/GREEN commands/results, actual socket versus synthetic distinctions,
coverage, ownership cleanup and remaining account-rate/deployment gates.
Independent specification+quality review is required. Do not claim production
gateway, account-level limits, hospital integration or whole-plan completion.
