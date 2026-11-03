# Task11 independent review — dedicated verified-TLS patient gateway

- **Task:** Task11, verified-TLS gateway for the isolated fictional-data patient application.
- **Review date:** 2026-11-03 (Asia/Singapore).
- **Reviewer role:** independent specification + quality reviewer. No production source, tests, deploy files, Compose files, `progress.md` or readiness docs were edited. This report is the only file written. Nothing was staged or committed; no image prune, resource deletion or `.env` access was performed.
- **Reviewed artifacts:** `docs/security/patient-gateway-brief.md` (requirements), `docs/security/patient-gateway-report.md` (implementer/root report), `docs/security/patient-gateway-review.diff` (60,919-byte snapshot package), the current worktree sources/tests/deploy/Compose files, and the retained output artifacts under `outputs/`.
- **Verdict — specification compliance:** **Compliant**. No Critical or Important non-compliance was found.
- **Verdict — code quality:** **Approved**. No Critical or Important quality defect was found; Minor findings below are follow-ups, not acceptance blockers.

## Source hashes checked

I recomputed SHA256 over the current worktree with
`Get-FileHash -Algorithm SHA256` and uppercased the results. All nine match the
report's "Final source freeze" and the review-diff header exactly.

| Current file | SHA256 | Match |
| --- | --- | --- |
| `src/medical_rag/patient_gateway.py` | `74BBD162F638F1FC5FF8008AF8DB3B53699EC5AE6FA9A9B187D69A8CB9CA168D` | yes |
| `tests/test_patient_gateway.py` | `4D077F6534EDD8ED5F28E0C1E71B368DC4315ECC8BD19AA8F48804ED380A99F0` | yes |
| `tests/patient_gateway_runtime.py` | `40638737FB7C3321567416650C0E2930ABBCD10FC96089645A5D75EE517B1647` | yes |
| `tests/fixtures/patient_gateway_synthetic.py` | `FC117245BBA4F826A2BB14A5F33FC83329C2BC9750A6BFCB395AA7B23826B390` | yes |
| `tests/keycloak_runtime.py` | `A248449D7DBF869C9C5CEFAF0A5A61C6B51D5F783F4AEF3B3A31DE8862BDE1EC` | yes |
| `deploy/patient-gateway/config.example.toml` | `FCF61CB5EBDD868B2622389197CE2E26A0D00B97CFA71BCFDED6F109D664FA27` | yes |
| `deploy/patient-gateway/start.sh` | `B5AD38905D89611EE453B05095EFC4B395992D364132FD904D78998C21119466` | yes |
| `deploy/patient-gateway/README.md` | `2F6583039F50F1B7135DB525AE7C0498C854DF266B38690DDD3F3878C050BDFB` | yes |
| `docker-compose.patient-gateway.yml` | `5B93BC66387B84F300F89F70FBF5976415F7425593D8E044819C918F92A0748E` | yes |

The diff's `tests/keycloak_runtime.py` hunk is exactly three non-critical
extensions: CA SubjectKeyIdentifier, leaf SubjectKeyIdentifier and leaf
AuthorityKeyIdentifier. Diffing the preserved pre-Task11 copy
(`outputs/patient-gateway-baseline-20261103/tests__keycloak_runtime.py`) against
the current file showed no other change. `tests/keycloak_runtime.py` is a test
helper; no production module imports it, and no test asserts extension lists.
The only other direct user is the integration-marked
`tests/test_keycloak_identity.py:236` (plus `keycloak_runtime` itself at line
215). No production certificate-validation code changed.

## Commands I ran and observed results

All commands were run from the worktree with the repository `.venv`. These are
my runs, not the implementer's; the report's recorded runs are separately
attributed and are not recast here.

1. **Pure loader/renderer/CLI suite with coverage**

   ```powershell
   .venv/Scripts/python.exe -m pytest tests/test_patient_gateway.py -q -m "not integration" --cov=medical_rag.patient_gateway --cov-report=term-missing --basetemp outputs/task11-review-unit-20261103
   ```

   Observed: `45 passed, 3 deselected, 1 warning in 0.86s`, exit 0.
   Coverage: `src\medical_rag\patient_gateway.py 97 stmts, 3 miss, 97%`
   (missing 104, 107, 217 — the two renderer revalidation raises and the
   `__main__` guard). The one warning is the pre-existing
   Starlette/TestClient/httpx deprecation.

2. **Selected actual/synthetic socket suite**

   ```powershell
   $env:RUN_PATIENT_GATEWAY_INTEGRATION='1'
   .venv/Scripts/python.exe -m pytest tests/test_patient_gateway.py -q --basetemp outputs/task11-review-socket-20261103
   ```

   Observed: `48 passed, 2 warnings in 37.57s`, exit 0 (45 pure + 3
   integration). Warnings: the existing Starlette/httpx and Authlib/httpx
   deprecations. No skips. Docker Desktop 29.8.0 (x86_64) was available and the
   three required images were already cached:
   `nginx:1.30.6-alpine@sha256:0e52bd054126cdc040b1276c0af347aeb02a41b9cfe305d6094767ac30e97949`,
   `postgres:18.6-alpine`, `python:3.13-alpine`. `frontend/dist-patient`
   existed with a real `patient.html` and two hashed assets. No image pull,
   prune or unrelated resource removal was performed.

3. **Focused unchanged-application regression**

   ```powershell
   .venv/Scripts/python.exe -m pytest tests/test_security_server.py tests/test_public_error_safety.py tests/test_stream_error_safety.py -q -m "not integration" --basetemp outputs/task11-review-app-20261103
   ```

   Observed: `73 passed, 14 deselected, 2 warnings in 8.78s`, exit 0. Warnings
   are the two existing dependency notices.

4. **Scoped Ruff (the five Python files named in the report)**

   ```powershell
   .venv/Scripts/python.exe -m ruff check src/medical_rag/patient_gateway.py tests/test_patient_gateway.py tests/patient_gateway_runtime.py tests/fixtures/patient_gateway_synthetic.py tests/keycloak_runtime.py
   ```

   Observed: `All checks passed!`, exit 0.

5. **Resource-cleanup verification after the selected socket run**

   ```powershell
   docker ps -a --filter "label=medical-rag.fixture" --format '{{.Names}} {{.Status}}'
   docker network ls --filter "label=medical-rag.fixture" --format '{{.Name}}'
   docker ps --format '{{.Names}} {{.Image}} {{.Status}}'
   ```

   Observed: no leftover containers and no leftover networks carrying the
   fixture label; the user's existing
   `medical-rag-resume-rabbitmq-1` (`rabbitmq:4.3.6-management`, healthy) was
   still running and untouched. The tests removed their generated
   `server.key`/`server.pem`/`ca.pem`; only public artifacts such as
   `wrong-ca.pem` and rendered `nginx-*.conf` files remained under the ignored
   review basetemp, which is expected.

6. **Generated-config inspection from the actual-application run**

   I read `outputs/task11-review-socket-20261103/test_actual_isolated_applicati0/nginx-c8ac3309.conf`.
   It contains `proxy_pass https://192.168.65.254:62062;`,
   `proxy_ssl_verify on`, `proxy_ssl_name localhost`, TLS 1.2/1.3,
   `proxy_next_upstream off`, `proxy_set_header Host 127.0.0.1:62045`,
   the fixed request-header allowlist, `proxy_pass_request_headers off`,
   `proxy_hide_header X-Gateway-Request-ID`, buffering/cache off and the five
   `/tmp` temp paths. This is direct evidence that the passing actual-app test
   used the rendered configuration, the Docker Desktop host-gateway IPv4 route
   and the real app port rather than a mock endpoint.

## What the actual-socket proof really exercises

- **Actual application chain.** `tests/patient_gateway_runtime.py:214-255`
  creates the real `security_server._application(config)` (real schema and
  privilege preflight), runs it under real uvicorn TLS with
  `proxy_headers=False`, and `tests/patient_gateway_runtime.py:169-211`
  applies all `0001`-`0007` migrations to a disposable PostgreSQL, creates a
  `NOSUPERUSER NOBYPASSRLS` runtime role, applies
  `database/patient_runtime_grants.sql` to it, and uses that non-owner role as
  the app DSN. The app's preflight would fail without the schema/grants, and
  the test passed.
- **Real path, not a mock.** The gateway container is reached over HTTPS at
  `https://127.0.0.1:<gateway_port>`; its upstream is the app's real TLS socket
  at the Docker host-gateway IPv4 address. The browser client never connects to
  the app directly. `tests/patient_gateway_runtime.py:263-304` checks real
  frontend/asset retrieval, 401, forged identity headers, own `/me` tenant,
  audit correlation by the app's `X-Request-ID`, wrong Host, wrong Origin,
  missing/valid CSRF, own/foreign conversation ownership, 403 on `/audit`, and
  closed legacy paths.
- **Synthetic transport is separate.** `tests/patient_gateway_runtime.py:307-385`
  uses an owned TLS-only `http.server` fixture, not the application, for the
  header allowlist, port-preserving Host, body framing, 100,000-byte boundary,
  SSE first-chunk timing, per-source-IP admission, pass-through statuses/302,
  and wrong-CA/wrong-name/unavailable 502 checks. `tests/fixtures/patient_gateway_synthetic.py`
  is never imported by the actual-app path. The report and README explicitly
  label this synthetic and state that fixture sessions are direct
  `SessionStore` sessions, not OIDC login.

## Specification and quality assessment

I checked the brief's requirements against the implementation and the runs
above. The following are present and independently supported:

- bounded strict TOML loading (8,192-byte ceiling, exact six-field set, duplicate
  and unknown keys rejected, type/ASCII/control/quote/newline/semicolon/brace/`$`
  checks, canonical URL reconstruction, IPv4/IPv6 handling, port rules, safe
  absolute POSIX mount paths, traversal rejection);
- pure `load_configuration`/`render_nginx` and an exclusive-create CLI that
  prints only static diagnostics and never echoes inputs, paths or rendered
  output; failure leaves no output file;
- fixed nginx directives (no generic template/shell interpolation); upstream
  chain + name verification, SNI, CA mount, TLS 1.2/1.3 only, no retry/fallback;
- exact `$http_host` authority check including a non-default port, separation
  from upstream TLS name, and no proxy-header trust;
- fixed request-header allowlist (Cookie, Origin, Content-Type, Accept,
  Accept-Language, X-CSRF-Token) with `proxy_pass_request_headers off`;
  Authorization/Forwarded/X-Forwarded-*/identity headers stripped; correct
  request framing;
- route restriction to current auth/me/conversations/audit/support/patient.html/
  assets/root paths; static gateway error bodies for invalid/not-found/
  body-limit/rate-limit/unavailable; application 401/403/404/409/429 bodies and
  provider 302 redirects left untouched;
- 100,000-byte body limit, 128 KiB buffer, bounded temp/concurrency settings,
  streaming readiness (buffering/cache off, HTTP/1.1, empty Connection, 180 s
  read timeout);
- `5r/s` burst 20 per `$binary_remote_addr`, 429 static body, no forwarded/
  cookie/account rate keys;
- independent `X-Gateway-Request-ID` from `$request_id`, upstream
  `X-Gateway-Request-ID` hidden, application `X-Request-ID` preserved and
  audit-correlated; unversioned nginx `Server`;
- Compose loopback-only publish, no backend publish, internal backend network
  distinct from edge, no Docker socket, no `unsafe-local-demo`, hardened
  read-only/nonroot/cap-drop/no-new-privileges/tmpfs container; operator
  README documenting independent app attachment and all non-claims;
- ownership labels and cleanup of only owned disposable networks/containers;
  cached-image reuse; no real identities, medical data or production DNS;
- scoped Ruff clean and 97% source coverage, both reproduced.

The application and legacy gateway show no Task11 edits in the review diff.
`deploy/gateway/nginx.conf` still has its pre-Task11 (2026-11-01) timestamp and
the legacy HTTP/fake-upstream experiment is left separate. The focused
application regression passed.

## Findings

**No Critical findings. No Important findings.**

### Minor

1. **RED-before-implementation evidence is not independently verifiable.**
   `outputs/patient-gateway-red-20261103/` and
   `outputs/patient-gateway-unit-red-20261103/` contain only empty per-test
   artifact directories, with no stdout/log capture. The report is honest about
   this (`docs/security/patient-gateway-report.md:50-57`), but the claimed
   first RED at `find_spec('medical_rag.patient_gateway') is None` is not
   reproducible from retained artifacts, and the retained test now imports the
   module lazily in `gateway_module()` (`tests/test_patient_gateway.py:22-25`)
   with `find_spec` only inside the integration test
   (`tests/test_patient_gateway.py:157`). Why it matters: the brief requires
   integration-first RED and exact RED results; the satisfied GREEN behavior is
   independently verified, but the RED process claim rests on testimony.
   Follow-up: retain RED logs for future increments.

2. **Wrong-Host socket assertion does not prove gateway rejection before
   dispatch.** `tests/patient_gateway_runtime.py:285` asserts only
   `status_code == 400`; the real app could itself return 400, so the test does
   not distinguish the gateway's static `GATEWAY_INVALID_REQUEST` body
   (`src/medical_rag/patient_gateway.py:139,151`) or prove that the upstream was
   never contacted. The renderer check is correct and nginx `return` in the
   rewrite phase precedes proxying, but the socket test is weaker than the
   requirement "reject ... before upstream dispatch." Follow-up: assert the
   static body code and/or an app-side non-observation marker.

3. **Several allowed routes and one TLS boundary are config-only, not
   socket-proven.** The regex at `src/medical_rag/patient_gateway.py:156`
   routes `/auth/*` and `/support/*` (which match the current app routes at
   `src/medical_rag/oidc_login.py:447-531` and
   `src/medical_rag/patient_api.py:213-246`), but the actual-app test
   (`tests/patient_gateway_runtime.py:263-304`) exercises only `/`, assets,
   `/me`, conversations and `/audit`; the synthetic test
   (`tests/patient_gateway_runtime.py:307-385`) exercises `/me` and `/assets`.
   There is also no socket test that a TLS 1.1 client is rejected, nor a
   concurrency-limit test. Why it matters: a future typo in the auth/support
   alternations or a regression in `limit_conn` would not be caught by an
   end-to-end test. The current regex was manually checked against the app
   routes and is correct.

4. **Report wording overstates how the runtime-role grants were applied.**
   `docs/security/patient-gateway-report.md:136-138` says migrations and
   `patient_runtime_grants.sql` were applied "as the non-owner runtime role."
   In `tests/patient_gateway_runtime.py:193-211`, migrations and grants are
   applied by the `postgres` admin connection, then a `gateway_runtime`
   `NOSUPERUSER NOBYPASSRLS` role is created and used as the application DSN.
   The functional boundary is correct; the report sentence should say the
   grants were applied *to* the non-owner role.

5. **`/patient.html` is a routed path but not an application route.**
   `src/medical_rag/patient_gateway.py:156` forwards `/patient.html`, and
   `deploy/patient-gateway/README.md:131-135` lists it among current paths, but
   `security_server._application` serves the patient HTML at `/` and mounts only
   `/assets` (`src/medical_rag/security_server.py:239-247`). A request to
   `/patient.html` would therefore be an application 404 passed through, not a
   gateway 404. No exposure results, and the brief itself names `patient.html`;
   this is a documentation/route-semantics mismatch worth clarifying.

6. **Configurable mount paths are not coupled to the fixed Compose mount
   targets.** The renderer accepts any validated absolute POSIX path for
   `tls_certificate`/`tls_key`/`upstream_ca`
   (`src/medical_rag/patient_gateway.py:83-86`), while
   `docker-compose.patient-gateway.yml:27-41` mounts the operator files only at
   `/run/tls/gateway.pem`, `/run/tls/gateway.key` and
   `/run/tls/upstream-ca.pem`. The example config matches those targets, but an
   operator who changes the config paths without also editing Compose gets a
   fail-closed nginx startup failure. Why it matters: operator footgun, not a
   security defect; the README should state the targets must stay aligned.

7. **Minor test/operability nits.** `tests/test_patient_gateway.py:12` defines
   an unused `ROOT`. `deploy/patient-gateway/start.sh:3` runs nginx in the
   foreground without `exec`, so `sh` stays PID 1 and does not forward SIGTERM;
   the test's `docker stop --time 1` still succeeds, but shutdown may become a
   force-kill. The Compose config test
   (`tests/patient_gateway_runtime.py:406`) checks that all mounts are
   read-only and `create_host_path: false`, but would also accept a read-only
   Docker-socket mount; the actual file visibly has no socket mount.

## Boundaries — what I did not verify

- No real OIDC/IdP browser login, institutional identity proofing, MFA
  enrollment/recovery, guardian rules, or real patient data. The passing
  actual-app test uses fixture-issued `SessionStore` sessions, as the report and
  README disclose; the separate Keycloak suites were **not** run here.
- No production DNS, certificate issuance, chain rotation/revocation, private-key
  custody, HSTS/ingress policy, monitoring, backup, incident response, load or
  penetration testing.
- No account-level or tenant rate/quota policy; only the development
  per-source-IP admission was exercised.
- No production deployment, hospital/clinical integration, frontend test suite,
  or whole-repository regression suite. Only the three focused application files
  were run.
- No end-to-end Compose deployment with a separate application container
  attached to the `backend` network; the Compose topology was verified
  declaratively by the passing `docker compose config` test, while the actual
  app socket used the Docker Desktop host-gateway IPv4 route (disclosed in the
  report).
- I did not verify the implementer's claimed RED run history (see finding 1);
  I did not recast any implementer/root run as my own.
- Docker Desktop restart history and RabbitMQ continuity across it were not
  observed by me; I only verified that the RabbitMQ container was healthy and
  untouched at review time.

## Conclusion

No Critical or Important defect was found; the specification is met by the
current source and the independently reproduced pure, socket, Compose and
focused-application runs. Task11 can be treated as accepted for fictional-data
development; the Minor findings above (especially retaining RED evidence and
tightening the wrong-Host and auth/support socket assertions) should be tracked
as follow-ups and do not require a blocking fix before this increment is used
with fictional data.
