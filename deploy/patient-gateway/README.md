# Dedicated verified-TLS patient gateway (reference only)

This directory configures a **separate** gateway for the isolated patient
application served by `medical_rag.security_server`. It is not production
deployment approval and does not replace the institution's ingress, network
policy, certificate lifecycle, monitoring or identity registration.

It is intentionally distinct from the legacy `deploy/gateway/nginx.conf` and
`docker-compose.gateway.yml` experiment. That old configuration still uses a
plaintext HTTP fake upstream; do not point it at the patient application and do
not treat it as a substitute for this gateway.

## Files

| Path | Purpose |
| --- | --- |
| `config.example.toml` | Placeholders only; no institution-approved values |
| `start.sh` | Fixed container command; startup diagnostics stay private |
| `../../src/medical_rag/patient_gateway.py` | Bounded TOML loader, pure renderer and CLI |
| `../../docker-compose.patient-gateway.yml` | Gateway-only, loopback-bound hardened container |

The renderer does not read `.env`, the legacy `Settings`, the network or the
filesystem. It validates an explicit operator file and prints only static
messages, including on failure.

## 1. Provision the operator files

Create a private directory outside the repository containing:

- the gateway certificate chain (PEM);
- the gateway private key (PEM);
- the CA bundle that signed the application certificate (PEM).

The gateway container runs as UID/GID `101:101` on a read-only root filesystem.
Make the mounted files readable by that user without making them world-readable
where the host supports POSIX permissions. The private key must never be copied
into the repository, a rendered nginx configuration, a Compose environment
variable or a log. The container receives only read-only bind mounts.

The application certificate must be valid for the configured
`upstream_tls_name` (the verification DNS name), and its chain must verify
against `upstream_ca`. The public gateway certificate must be valid for the
public origin; clients must trust its issuer explicitly. Do not disable
verification or use test certificates for real data.

## 2. Write the gateway configuration

Copy `config.example.toml` to the private directory and replace every
placeholder. All six fields are required and unknown or duplicate keys are
rejected:

```toml
public_origin = "https://patient.example.org:8443"
upstream_url = "https://patient-app:8443"
upstream_tls_name = "patient.internal.example.org"
tls_certificate = "/run/tls/gateway.pem"
tls_key = "/run/tls/gateway.key"
upstream_ca = "/run/tls/upstream-ca.pem"
```

- `public_origin` is one canonical ASCII DNS, IPv4 or bracketed IPv6 authority
  with an optional non-default port. It has no userinfo, path, query, fragment,
  wildcard or control character. This exact authority is required in the HTTP
  `Host` header before any request is proxied.
- `upstream_url` is one explicit HTTPS `host:port`, resolved at gateway start.
  It is not a variable URI, Unix socket or user-controlled target.
- `upstream_tls_name` is the DNS name used for upstream SNI and certificate
  hostname verification. It is independent of the public HTTP authority.
- The three mount paths are absolute POSIX container paths with a fixed safe
  character set. They are configuration values, not key material.

The TOML file is an operator input, not a secret store. Protect it because it
discloses topology, but never place certificate or key contents in it.

## 3. Render the fixed nginx configuration

Use a new output path each time; the CLI creates it exclusively and refuses to
overwrite an existing file:

```powershell
.\.venv\Scripts\python.exe -m medical_rag.patient_gateway `
  --config C:\secure\patient-gateway\gateway.toml `
  --output C:\secure\patient-gateway\nginx.conf
```

Success prints only `Patient gateway configuration written.` Failure prints only
`Patient gateway configuration failed.` and never echoes the received values,
paths or generated configuration. Do not pipe a failure message into a shell
command or retry with looser validation.

Rendered output uses fixed directives, not a generic template language. Review
the generated file before use. Re-rendering after a configuration or certificate
change is a separate operator action; the gateway does not reload itself.

## 4. Deploy only the gateway container

Set explicit environment variables and disable Compose's implicit `.env`
loading; the gateway must not consume the legacy application dotenv:

```powershell
$env:COMPOSE_DISABLE_ENV_FILE = '1'
$env:PATIENT_GATEWAY_CONFIG = 'C:\secure\patient-gateway\nginx.conf'
$env:PATIENT_GATEWAY_CERT = 'C:\secure\patient-gateway\gateway.pem'
$env:PATIENT_GATEWAY_KEY = 'C:\secure\patient-gateway\gateway.key'
$env:PATIENT_GATEWAY_UPSTREAM_CA = 'C:\secure\patient-gateway\upstream-ca.pem'
$env:PATIENT_GATEWAY_PORT = '8443'
docker compose -f docker-compose.patient-gateway.yml up -d
```

The Compose file deploys **only** `patient-gateway`. It does not deploy the
patient application, PostgreSQL, the identity provider or the legacy RAG
services. The gateway publishes one loopback port by default and joins:

- `edge` - the published edge network;
- `backend` - a separate internal network with no backend published port.

Deploy the patient application independently over TLS and attach it to the
`backend` network with a network alias equal to the upstream hostname:

```powershell
docker network connect <compose-project>_backend <patient-app-container> --alias patient-app
```

Start the application before the gateway: nginx resolves the configured
upstream hostname at startup. If the application is recreated with a different
address, restart the gateway. Do not expose `unsafe-local-demo`, the legacy API
or the workers through this gateway. No Docker socket is mounted.

## 5. Routing, headers and bounds

Only the patient application's current paths are routed: `/auth/...`, `/me`,
`/me/consents/health`, `/conversations...`, `/audit`, `/support/...`,
`/patient.html`, `/assets/...` and `/`. Unknown paths and the legacy `/ui`,
`/api`, `/settings` and `/documents` paths are answered by the gateway and never
reach the application.

The application remains authoritative for authentication, sessions, roles,
ownership, consent, MFA and audit. The gateway does not validate identity
headers and does not create a session. It forwards only `Cookie`, `Origin`,
`Content-Type`, `Accept`, `Accept-Language` and `X-CSRF-Token`, plus its own
`Host` and correct request framing. It strips `Authorization`, `Forwarded`,
`X-Forwarded-*`, `X-User-ID`, `X-Tenant-ID`, `X-Role` and arbitrary identity
headers. Application `X-Request-ID` values are preserved for audit correlation;
the gateway's independent trace is `X-Gateway-Request-ID` and any upstream
header of that name is discarded. The two identifiers are not a shared trace.

Request bodies are bounded to 100,000 bytes with a 128 KiB memory buffer.
Response buffering and proxy cache are off, HTTP/1.1 is used and upstream reads
may last 180 seconds so a future streaming endpoint is not buffered. The
current patient application does not expose clinical inference streaming;
synthetic SSE tests prove transport capability only.

Development admission is per connection source IP: 5 requests/second with a
burst of 20, at most 32 concurrent connections, answering 429 with a static
gateway body. Forwarded addresses and cookies are never used as rate keys this
stage. **This is not institution-approved rate policy and not account-level
rate limiting.** Account and tenant quotas remain a separate gate.

## 6. TLS behavior

The gateway listens with TLS 1.2/1.3 only and verifies the upstream certificate
chain and hostname with the explicitly mounted CA, SNI and verification name.
There is no HTTP fallback, insecure retry or certificate-verification bypass.
Wrong CA, wrong verification name or an unavailable upstream produce only a
static `502 GATEWAY_UNAVAILABLE` response.

Certificate rotation, revocation response, private-key protection, backup and
production ingress policy are operator-owned processes. This reference does not
automate them. Restart the gateway after replacing mounted files, and review
the rendered configuration whenever the public origin or upstream changes.

## 7. Verification

Prerequisites: Docker with the pinned `nginx:1.30.6-alpine`, disposable
`postgres:18.6-alpine` and `python:3.13-alpine` images cached, a built
`frontend/dist-patient`, and Python dependencies from `.[dev]`.

```powershell
# Pure loader/renderer/CLI tests and coverage.
.\.venv\Scripts\python.exe -m pytest tests/test_patient_gateway.py -q -m "not integration" `
  --cov=medical_rag.patient_gateway --cov-report=term-missing

# Actual nginx HTTPS -> actual isolated application/PG plus synthetic transport.
$env:RUN_PATIENT_GATEWAY_INTEGRATION = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_patient_gateway.py -q
```

The selected integration run fails when required infrastructure is unavailable;
it must not silently become a mock or skip success. The tests create only
uniquely labeled disposable networks/containers and remove them after checking
the ownership label. They reuse cached images and do not prune other resources.
Generated test certificates are self-signed fictions trusted explicitly by the
test client; no real keys, accounts, DNS names or production certificate store
are used.

The actual-application test issues fixture sessions directly through the
session store. That is not an OIDC browser login and does not prove full
identity-provider interoperability; the separate Keycloak tests cover protocol,
not a full authenticated browser E2E.

## 8. Explicit non-claims

This gateway is repository engineering evidence, not production acceptance. It
does not provide institution-approved rate limits, real hospital identity
proofing, network segmentation review, certificate automation, monitoring,
backup, incident response, load testing, clinical validation or legal/privacy
approval. Do not process real patient information through it until those gates
are independently completed and signed off.
