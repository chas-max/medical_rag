# Task11 report — verified TLS gateway for the isolated patient application

2026-11-03. Scope: one independently testable gateway integration increment for
the fictional-data patient application. This is not production deployment
approval and does not complete account-level rate policy, institutional
identity proofing, full browser E2E or hospital integration.

## Boundary

- The legacy `deploy/gateway/nginx.conf` and `docker-compose.gateway.yml`
  plaintext/fake-upstream experiment was not edited, redirected or unlocked.
- The isolated patient application and its verifier, middleware, session,
  consent, role, ownership and audit semantics were not changed.
- New: `src/medical_rag/patient_gateway.py` (bounded TOML loader, pure nginx
  renderer, exclusive-create CLI), `deploy/patient-gateway/`,
  `docker-compose.patient-gateway.yml`, `tests/test_patient_gateway.py`,
  `tests/patient_gateway_runtime.py`,
  `tests/fixtures/patient_gateway_synthetic.py`, this report and the operator
  README.
- `tests/keycloak_runtime.py` (shared fictional certificate helper) was amended
  test-only to emit SKI/AKI. No production certificate validation changed.

## Baseline

Task11 targets were absent when the task began:
`outputs/patient-gateway-baseline-20261103/new-targets.txt`. No pre-existing
target bytes were overwritten. Historical intermediate output directories are
not treated as acceptance evidence; in particular
`outputs/patient-gateway-socket-green-20261103` contains a **failed** run
(2 failed, 1 passed) recorded before the implementer exhausted workspace credit.

## Final source freeze (SHA256, uppercase)

```text
74BBD162F638F1FC5FF8008AF8DB3B53699EC5AE6FA9A9B187D69A8CB9CA168D  src/medical_rag/patient_gateway.py
4D077F6534EDD8ED5F28E0C1E71B368DC4315ECC8BD19AA8F48804ED380A99F0  tests/test_patient_gateway.py
40638737FB7C3321567416650C0E2930ABBCD10FC96089645A5D75EE517B1647  tests/patient_gateway_runtime.py
FC117245BBA4F826A2BB14A5F33FC83329C2BC9750A6BFCB395AA7B23826B390  tests/fixtures/patient_gateway_synthetic.py
A248449D7DBF869C9C5CEFAF0A5A61C6B51D5F783F4AEF3B3A31DE8862BDE1EC  tests/keycloak_runtime.py
FCF61CB5EBDD868B2622389197CE2E26A0D00B97CFA71BCFDED6F109D664FA27  deploy/patient-gateway/config.example.toml
B5AD38905D89611EE453B05095EFC4B395992D364132FD904D78998C21119466  deploy/patient-gateway/start.sh
2F6583039F50F1B7135DB525AE7C0498C854DF266B38690DDD3F3878C050BDFB  deploy/patient-gateway/README.md
5B93BC66387B84F300F89F70FBF5976415F7425593D8E044819C918F92A0748E  docker-compose.patient-gateway.yml
```

## RED/GREEN history

The implementation followed test-first for the pure surface and integration-first
for the sockets, but the retained artifact history is incomplete: the original
RED stdout directories `outputs/patient-gateway-red-20261103` and
`outputs/patient-gateway-unit-red-20261103` are empty. The executable transcript
recorded:

- RED: `pytest tests/test_patient_gateway.py -q` failed at
  `find_spec('medical_rag.patient_gateway') is None` before the module existed.
- RED: 45 pure tests failed before the renderer existed.
- GREEN: 45 pure tests passed (`0.81s`), 3 integration deselected.
- Integration RED (implementer, then exhausted credit):
  `2 failed, 1 passed` — gateway readiness and
  `invalid input syntax for type timestamp with time zone: "adult"`.
- Root resume reproduced the same failures and found four independent causes:

1. **Read-only rootfs temp paths.** nginx creates compiled default temp
   directories under `/var/cache/nginx`; the container rootfs is read-only and
   `/var/cache/nginx` was not a tmpfs. `nginx -t` showed
   `mkdir() "/var/cache/nginx/fastcgi_temp" failed (30: Read-only file system)`,
   so the container exited before readiness. The renderer now pins
   `fastcgi_temp_path`, `uwsgi_temp_path` and `scgi_temp_path` under the bounded
   writable `/tmp` tmpfs, alongside the existing client/proxy temp paths.
2. **Fixture SQL drift.** `patient_subjects` gained
   `age_classification` and `identity_verified` in migration `0004`; the socket
   fixture still inserted four positional values. The insert now names
   `(tenant_id, patient_id, age_classification, identity_verified)`.
3. **Test CA/leaf missing key identifiers.** The current OpenSSL rejects the
   fictional leaf with `certificate verify failed: Missing Authority Key
   Identifier`, which made every readiness probe look like a gateway failure.
   The test helper now emits CA/leaf SKI and leaf AKI. This is test material
   only; the application's certificate verification policy is unchanged.
4. **Docker Desktop dual-stack host route.** nginx resolved
   `host.docker.internal` alternately to IPv4 and to an unreachable IPv6 host
   address, producing a static `502` for one request after another succeeded.
   The fixture now resolves the host route to its IPv4 address through an owned
   one-shot `python:3.13-alpine` container and uses that address as the explicit
   upstream. It also binds the real application to the configured loopback host
   for the socket test. Neither change adds application routes or bypasses
   identity.

A timing-dependent rate-limit assertion was also made deterministic
(sequential 80-request burst; the first 429 body is checked). The final renderer
tests additionally assert the three fixed temp paths, TLS 1.2/1.3, retry-off,
`server_tokens off` and the absence of private-key markers.

## Final verification (2026-11-03, this worktree, fictional data only)

```text
$env:RUN_PATIENT_GATEWAY_INTEGRATION='1'
.venv/Scripts/python.exe -m pytest tests/test_patient_gateway.py -q
48 passed, 2 warnings in 37.04s, exit 0
```

45 pure + 3 integration (synthetic transport, actual application/PG, dedicated
Compose configuration). The two warnings are the pre-existing Starlette/httpx
and Authlib/httpx dependency notices.

```text
.venv/Scripts/python.exe -m pytest tests/test_patient_gateway.py -q -m "not integration" \
  --cov=medical_rag.patient_gateway --cov-report=term-missing
45 passed, 3 deselected, 1 warning in 0.94s
src/medical_rag/patient_gateway.py  97 stmts  3 miss  97%
```

```text
.venv/Scripts/python.exe -m pytest tests/test_security_server.py \
  tests/test_public_error_safety.py tests/test_stream_error_safety.py \
  -q -m "not integration"
73 passed, 14 deselected, 2 warnings in 8.98s
```

```text
.venv/Scripts/python.exe -m pytest -q -m "not integration" \
  --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking \
  --basetemp outputs/task11-broad-20261103
1586 passed, 5 skipped, 347 deselected, 2 warnings in 122.96s, exit 0
```

The broad run keeps the known pre-existing 500-question anchor-mapping failure
explicitly deselected; it is not represented as a passing unfiltered suite.

```text
.venv/Scripts/python.exe -m ruff check \
  src/medical_rag/patient_gateway.py tests/test_patient_gateway.py \
  tests/patient_gateway_runtime.py tests/fixtures/patient_gateway_synthetic.py \
  tests/keycloak_runtime.py
All checks passed.
```

The full-repository Ruff backlog remains a separate pre-existing debt and is
not represented as clean here.

## Actual sockets versus synthetic transport

**Actual application chain (must not be read as OIDC login).** A real nginx
container serves HTTPS with the rendered configuration; it proxies over
verified TLS to a real uvicorn instance created by
`security_server._application`, against a disposable PostgreSQL with all
`0001`-`0007` migrations and `patient_runtime_grants.sql` applied as the
non-owner runtime role. The test asserts:

- patient frontend and real asset retrieval;
- missing session → 401;
- forged `Authorization`/identity/tenant/role headers → 401 (not trusted);
- current fixture-issued session → own `/me` identity and tenant;
- own conversation create/read, foreign-owner conversation → 404
  `RESOURCE_NOT_FOUND`;
- wrong Host → 400 before upstream dispatch; wrong Origin → 403;
- retained Cookie and CSRF consent mutation succeeds; missing CSRF is denied;
- patient role on `/audit` → 403;
- `/ui`, `/api`, `/settings`, `/documents`, `/docs`, `/unknown` and
  `/me/unknown` are gateway 404s and never reach the application;
- application `X-Request-ID` remains single and audit-correlated; the
  gateway trace is independent; the built-in Server header has no version.

Fixture sessions are issued directly by `SessionStore`; they are **not** real
OIDC authorization-code login. Full authenticated-browser E2E and institutional
IdP registration remain open; the separate Keycloak suites cover protocol
interoperability.

**Synthetic TLS-only upstream.** A bounded owned fixture inspects the gateway
transport without application identity: exact request-header allowlist (Cookie,
Origin, Content-Type, Accept, Accept-Language, X-CSRF-Token only), stripped
Authorization/Forwarded/X-Forwarded-*/identity headers, correct request framing,
port-preserving Host, 100000-byte boundary and static 413, SSE first-chunk
delivery before completion, per-source-IP 5r/s burst-20 development admission
with static 429, pass-through 401/403/404/409/429 bodies and provider 302
Location, and static 502 for wrong CA, wrong verification name and unavailable
upstream. This is transport capability evidence only; the application does not
offer clinical inference streaming.

## Ownership and cleanup

Every added network/container carries `medical-rag.fixture=<unique owner>` and
cleanup checks that exact label before removal, attempts all owned resources and
fails with a static diagnostic otherwise. The one-shot host-address resolver is
`--rm` and labeled. Cached images are reused; no image prune or unrelated
resource removal is performed. The user's existing RabbitMQ container was
preserved across a Docker Desktop restart. Test certificates/keys live only in
the ignored pytest output directory and are removed after the run.

## Remaining gates

- Institution-approved account/tenant rate and quota policy; the gateway only
  has per-connection-source-IP development admission.
- Production ingress/network topology, certificate issuance, rotation,
  revocation and private-key custody; no automatic reload or discovery.
- Independently deployed application/PostgreSQL/IdP container topology and
  resource limits; the Compose file deploys only the gateway.
- Full authenticated-browser OIDC E2E, real institutional identity proofing,
  MFA enrollment/recovery, hospital record/model integration and guardian rules.
- Monitoring, backup/restore, log protection, load testing, penetration/clinical/
  privacy/legal review and named institutional acceptance.

No production gateway, account-level limits, hospital integration or whole-plan
completion is claimed.

## Post-review status (2026-11-03)

`docs/security/patient-gateway-review-report.md` independently records
specification **Compliant** and quality **Approved**, with no Critical or
Important findings. The reviewer reproduced the pure 45/97% coverage, the full
48-passed socket run with no skips, the 73 focused application tests, scoped
Ruff and all nine source hashes; it left seven Minor non-blocking follow-ups
(retained historically: RED capture, a weaker wrong-Host body assertion,
config-only auth/support/TLS-1.1/concurrency coverage, grant wording,
`/patient.html` route semantics, config-vs-Compose mount coupling, and small
test/operability nits). No source change was made after the reviewed freeze.
Task11 is accepted **only** for the tested fictional-data development scope;
the production and institutional gates above remain open.

