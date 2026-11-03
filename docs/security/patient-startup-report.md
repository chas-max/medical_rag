# Trusted patient startup evidence — 2026-11-02

## Delivered scope

`security_config.py` loads bounded strict trusted TOML and secret/notice files;
`security_server.py` exports `load_patient_application(Path)` and direct-TLS
`main(argv=None)`. Operator example: `deploy/patient/config.example.toml`.
Owner-run psql grant recipe: `database/patient_runtime_grants.sql`, with quoted
`runtime_role` variable. Startup never migrates, creates accounts/roles,
verifies patients, grants consent or reads cwd `.env`/legacy Settings.

Minimal approved factory propagation adds `patient_html=False` through
`create_patient_app` and `create_auth_app`. Headless auth default CSP remains
`default-src 'none'; frame-ancestors 'none'`. Patient policy is a fixed trusted
factory choice, never request-controlled. Only `/` patient HTML and `/assets`
are mounted in addition to isolated auth/patient endpoints; docs/OpenAPI,
legacy `/ui`, chat, jobs, memory, settings and arbitrary config files stay absent.

Controller's actual browser A/B native-form redirect probe demonstrated that
strict self-only form-action blocks the required IdP303 in Chromium. Approved
correction permits **only self plus the fixed configured HTTPS authorization
origin** for form-action; script/style/connect stay self-only. A safe DNS/IP
authority serializer prevents CSP source injection. Tests first: CSP-origin and
wildcard/semicolon/quote/backslash rejection assertions gave **5 failed,
34 deselected** with `-k 'patient_form_csp or unsafe_authority'` using
`--basetemp=D:/temp-openclaw/patient-csp-red2`, then implemented correction.

## TDD evidence

- Added tests first and minimal failure scaffolds. Initial unbounded pytest
  parameter IDs triggered a Windows long-path fixture error; fixed test IDs
  and used explicit short disposable basetemp, not an implementation workaround.
- RED command:
  `$env:RUN_POSTGRES_INTEGRATION='1'; .venv/Scripts/python.exe -m pytest tests/test_security_server.py -q --basetemp=D:/temp-openclaw/patient-startup-red-pg --tb=short`
  Result: **5 failed, 20 passed**, failing valid config/app/CLI/actual least-role
  startup because implementation was absent.
- Implemented strict loading, actual DB preflight, build mounts and direct TLS.
  Added actual grant-recipe and actual recovery evidence. Early standby tests
  failed infrastructure setup (container-port lookup, replication HBA and
  default spread checkpoint); fixed only disposable-test infrastructure.
- Added required-DML/FORCE-RLS/remote-transport/help regressions before fixes:
  `$env:RUN_POSTGRES_INTEGRATION='1'; .venv/Scripts/python.exe -m pytest tests/test_security_server.py -q -k 'required_dml or unsafe_remote or help' --basetemp=D:/temp-openclaw/patient-startup-security-red --tb=short`
  Result: **5 failed, 28 deselected**. Implemented positive DML and RLS checks,
  remote verify-full requirement, multihost/hostaddr rejection, successful help.
- Expanded focused GREEN before final TLS-process smoke: **33 passed**.

## Real DB versus loader doubles

Pure mount/CLI tests may replace `preflight` to isolate loading and invocation.
They do not claim DB evidence. Actual disposable PostgreSQL18.6-alpine tests
reuse the reviewed vertical fixture and genuinely unprivileged runtime role:

- Valid primary+active tenant startup succeeds.
- Superuser, authority column INSERT/UPDATE, patient verification UPDATE,
  consent UPDATE/DELETE, missing INSERT rights, wrong/inactive institution,
  missing migration column, disabled FORCE RLS all deny startup.
- Owner-run grant recipe supports actual session issue, grant consent,
  conversation create, turn append/list, login transaction create/consume.
  Migrations1-4 have no identity/sequence needing grants; consent ordering is
  allocated by the invoker trigger, not an invented sequence.
- Recovery test creates an owned temporary Docker network and bounded tmpfs
  streaming standby with `pg_basebackup -c fast`. It verifies actual
  `pg_is_in_recovery() = true`, then `load_patient_application` denies it through
  an actual unprivileged connection. No mocked recovery bit is counted as DB
  evidence. Standby/network are removed; only the test's own disposable primary
  receives a temporary replication HBA rule, and the fixture removes it.

## Security and operation limits

Config/public key/notice reads are <=64KiB, DSN/client secret <=16KiB, encryption
key exactly32 raw bytes. Paths resolve relative to config. PEM parsing and
existing LoginConfiguration validate keys/OIDC parameters. TLS cert+key are
loaded before Uvicorn; encrypted unsupported keys do not prompt. Exceptions,
repr and public CLI failures contain no DSN/key/path/SQL details.

DB connections explicitly bound connect3s/lock3s/statement5s and primary guards.
DSNs must explicitly supply host/database/user/password; multihost, hostaddr
override and service indirection are denied. Non-loopback hosts require
`sslmode=verify-full` with institution-managed certificate trust. Plaintext
loopback PG is only for fictional isolated development/test data; do not treat
that allowance as production transport approval. Listener binds a configured
explicit IP (example127.0.0.1), rejects wildcards, and warns on nonloopback.

Uvicorn uses direct TLS, no proxy headers/access log/server header, no debug,
concurrency100 and keepalive5s. Static and auth responses keep no-store and
security headers. No logging of request/response bodies or callback queries.
Old nginx HTTP-upstream/Host-port behavior is not integrated.

Preflight is deliberately a partial privilege/schema diagnostic, not a full
database audit: SECURITY DEFINER functions, arbitrary privileged memberships,
full policy definitions and network/OS/container protections still need
independent audit. Audit transport, credential-rotation/back-channel logout
code and future guardian/business UI are remaining engineering work, not solely
external blockers. Institution provisioning, real provider registration,
approved rules, actual hospital protocols and independent production acceptance
require external institutional input. The grant recipe does not silently revoke old
inherited/PUBLIC grants, create roles or claim a full audit.

## Verification commands/results

- `.venv/Scripts/python.exe -m ruff check src/medical_rag/security_config.py src/medical_rag/security_server.py src/medical_rag/oidc_login.py src/medical_rag/patient_api.py tests/test_security_server.py`: all checks passed (before final TLS-test formatting).
- `git diff --check`: exit0; Git warned existing frontend files will receive
  CRLF on future Git writes, with no whitespace errors.
- `$env:RUN_POSTGRES_INTEGRATION='1'; .venv/Scripts/python.exe -m pytest tests/test_security_server.py tests/test_oidc_login.py tests/test_identity_sessions.py tests/test_patient_vertical.py --cov=medical_rag.security_server --cov=medical_rag.security_config --cov-report=term-missing -q --basetemp=D:/temp-openclaw/patient-startup-regression --tb=short`:
  **172 passed**, two existing Authlib/Starlette httpx deprecation warnings;
  security_config100%, security_server94%, combined96% statement coverage.
  (Run preceded final explicit literal-IP hostaddr guard and added TLS smoke;
  fresh focused verification below is authoritative for those changes.)

Actual TLS-process test initially failed because the test child lacked explicit
src-layout module resolution, then exposed Authlib's import-time migration
warning with package paths. Child now has explicit `PYTHONPATH=src`, **normal
warning settings**, and launcher narrowly filters only that known Authlib
message after Authlib installs its own category filter. No blanket warnings
suppression is used. Actual trusted-certificate TLS request passed200/no-store,
no Uvicorn Server header, and empty process stdout/stderr. Focused command:
`$env:RUN_POSTGRES_INTEGRATION='1'; .venv/Scripts/python.exe -m pytest tests/test_security_server.py -q -k real_direct_tls --basetemp=D:/temp-openclaw/patient-direct-tls-final --tb=short`:
**1 passed, 38 deselected**, two existing suite import deprecation warnings.

Final authoritative regression after all changes:
`$env:RUN_POSTGRES_INTEGRATION='1'; .venv/Scripts/python.exe -m pytest tests/test_security_server.py tests/test_oidc_login.py tests/test_identity_sessions.py tests/test_patient_vertical.py --cov=medical_rag.security_server --cov=medical_rag.security_config --cov-report=term-missing -q --basetemp=D:/temp-openclaw/patient-startup-verified --tb=short`:
**178 passed, 2 warnings in59.57s**. Includes39 startup tests, actual primary,
least-role grant operations, actual streaming standby denial and actual direct
TLS subprocess with normal warnings/empty stdout+stderr. Coverage:
security_config **100%**, security_server **94%**, combined **96%** statements.
Final focused ruff command above: **All checks passed**. Final
`git diff --check`: exit0 (only CRLF-normalization notices).

No production acceptance, real hospital identity, real-IdP interoperability,
clinical safety or legal acceptance is claimed. Controller owns README,
independent code review and browser/CSP redirect validation.

## Task3 scoped-IPv6 review fix — 2026-11-02

Preserved the interrupted `_authorization_origin` guard and its two scoped
IPv6 tests. Both ordinary scoped IPv6 (`[fe80::1%25eth0]`) and a scope containing
CSP semicolons (`[::1%25;upgrade-insecure-requests;]`) are rejected before an
app can emit that trusted-config value as a CSP source. This is a strict
configuration/CSP syntax correction, not evidence of unauthenticated injection.
Headless CSP, trusted IdP source selection, Origin/CSRF and identity trust are
unchanged.

Verified RED by temporarily removing only the scope-ID guard with `apply_patch`:
`$env:PYTHONUTF8='1'; .venv/Scripts/python.exe -m pytest tests/test_security_server.py -q -k unsafe_authority --basetemp=D:/temp-openclaw/patient-portal-ipv6-red --tb=short`:
**2 failed, 4 passed, 35 deselected**, both expected `ValueError` assertions
failed with DID NOT RAISE. Restored the original guard before GREEN.

First requested full focused backend attempt:
`$env:PYTHONUTF8='1'; $env:RUN_POSTGRES_INTEGRATION='1'; .venv/Scripts/python.exe -m pytest tests/test_security_server.py tests/test_oidc_login.py -q --basetemp=D:/temp-openclaw/patient-portal-fixed --tb=short`:
**42 passed, 49 setup errors, 2 warnings**. This was a resource failure: Docker
Desktop's Linux engine pipe was absent and every `docker run` fixture failed;
it was not a passing PG run or a demonstrated code regression. Controller
started the installed Docker Desktop hidden and confirmed server29.8.0.

Authoritative rerun after daemon recovery:
`$env:PYTHONUTF8='1'; $env:RUN_POSTGRES_INTEGRATION='1'; .venv/Scripts/python.exe -m pytest tests/test_security_server.py tests/test_oidc_login.py -q --basetemp=D:/temp-openclaw/patient-portal-fixed-pg --tb=short`:
**91 passed, 2 warnings in22.39s**, no skips/errors. Includes actual disposable
PostgreSQL runtime/startup/OIDC, recovery-denial and direct-TLS subprocess tests,
plus scoped-IPv6 regression tests. The two existing Starlette/Authlib httpx
deprecation warnings remain visible; no broad warning suppression was added.

`.venv/Scripts/python.exe -m ruff check src/medical_rag/oidc_login.py tests/test_security_server.py`:
**All checks passed**. `git diff --check`: exit0, only existing frontend CRLF
normalization notices. No commits/staging or edits to `.env`/legacy sources.
All previously documented production/browser/real-IdP limitations still apply.
