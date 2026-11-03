# Provider logout test-clock repair report

2026-11-03. Narrow test-only Task10 implementation; independent specification
and quality review remains owned by the coordinating agent. No whole-branch,
hospital, clinical/legal or production acceptance is asserted.

## Scope, baseline and source freeze

Changed paths for this task are exactly:

- `tests/test_provider_logout.py`: only
  `test_wrong_signature_algorithm_missing_claims_and_time_bounds` changed.
- `tests/provider_logout_clock.py`: new test-only scoped clock helper.
- `tests/test_provider_logout_clock.py`: new focused pure regressions.
- `docs/security/provider-logout-test-clock-report.md`: this handoff report.

No additional existing file was edited, so no additional pre-edit capture was
needed. The coordinating agent's original capture remains at
`outputs/provider-logout-test-clock-baseline-20261103/tests__test_provider_logout.py`.
Both the pre-edit target and the preserved original have SHA256:
`6C266E7DC39E50D0C62FFC4F628EDB86C522B6BE45E615F38146840774081DAC`.
An AST comparison excluding the affected function returned
`Only affected test AST changed: True`, including unchanged shared helper
interfaces and all other inherited tests.

Frozen implementation source SHA256 (before handoff):

| Path | SHA256 |
| --- | --- |
| `tests/test_provider_logout.py` | `EAA9A7D0C846ADE5B5AE7B6A90A564BBB9155F04BD1A556DB48575A387F96199` |
| `tests/provider_logout_clock.py` | `CE1BB1A416C4285FF09386EA5ADE799EA3F8D818C2C09E2577A81A3A378F1AEF` |
| `tests/test_provider_logout_clock.py` | `64A2235FD60F2E3C2DAE0C3143F198BCF975FF9022FB9AE6DBF1CEF5D739D901` |

The report's own hash is supplied separately at handoff to avoid a self-hash.

## Root cause and repair

The deterministic pure reproduction replaces only the inherited test module's
imported clock reference. With a caller epoch of T, a claims sample at T+1
and expiration T+301 yields lifetime 300, not the intended 301. No sleeps,
probability loops, tolerance widening or original-test skip/xfail were added.

The amended actual-PostgreSQL HTTP test privately resolves `system` through
`request.getfixturevalue`, then samples one integer real epoch after fixture
readiness. Every payload iat/exp is derived explicitly from that epoch. The
small helper pins only `jwt.api_jwt.datetime` and
`medical_rag.provider_logout.time` to the same epoch through monkeypatch.
Global `time.time`, database readiness, asyncio and unrelated clocks remain
untouched. A pure test proves both clocks agree, global time remains untouched,
and the original references are restored on scope exit. The helper is shared
only so this precise clock setup and restoration are directly regression-tested;
it introduces no production abstraction or dependency.

All amended assertions consume precomputed boolean locals with static messages.
The pure privacy checks inspect only this affected function's AST and replay
each actual assertion through pytest assertion rewriting with synthetic token
and connection canaries. Failures retain their static case label and
`assert False` proof without sensitive values. No tokens, claims or connection
tuples are embedded in assertion operands or failure calls.

## Exact boundary evidence

Each row passed in both the pure real-verifier tests and the amended actual-PG
HTTP test. T is the one frozen epoch. Every HTTP payload has a fresh jti.
Every HTTP result also requires exactly one correlated mandatory audit record
with action `provider.logout`, resource `session`, no operator, and the expected
outcome/reason (`allowed`/`ok` or `denied`/`invalid_request`). No browser session
is required; accepted cases are allowed to create revocation fences.

| iat | exp | Expected HTTP |
| --- | --- | --- |
| T | T+120 | 200 |
| T | T+300 | 200 |
| T | T+301 | 400 |
| T+1 | T+120 | 400 |
| T-120 | T+120 | 200 |
| T-121 | T+120 | 400 |
| T | T | 400 |
| T | T-1 | 400 |
| T-1 | T-1 | 400 |

Wrong RSA signature, HS256 substitution, and each deletion of iss, aud, iat,
exp, events, jti, or sub-without-sid also return HTTP400 with the correlated
mandatory denial audit. Separate static labels identify every missing claim.
Existing full-module tests of audit-storage failure and replay remain intact.

## RED / GREEN commands and results

All commands ran from
`C:/Users/楠凤/.codex/worktrees/medical-security/medical-rag-resume`.
The virtualenv was used directly; no install/update operation was run.
Each pytest basetemp below is explicit and under ignored `outputs/`.

RED, before editing the affected inherited test:

```powershell
$env:PYTHONUTF8='1'
./.venv/Scripts/python.exe -m pytest tests/test_provider_logout_clock.py -q --tb=no --basetemp=outputs/provider-logout-test-clock-red-pure-20261103
```

Exit1: **3 failed, 1 passed, 2 warnings in 3.20s**. The controlled rollover
reproduction passed. Failures correctly identified the credential-bearing
fixture argument, direct rewritten assertions, and synthetic signed-token
canary disclosure in the original first assertion. The canary contents and
failure diagnostic are deliberately not reproduced here.

Initial GREEN after repair:

```powershell
$env:PYTHONUTF8='1'
./.venv/Scripts/python.exe -m pytest tests/test_provider_logout_clock.py -q --tb=no --basetemp=outputs/provider-logout-test-clock-green-pure-20261103
```

Exit0: **14 passed, 2 warnings in 3.71s**. The first scoped Ruff attempt then
reported a loop-lambda binding and import-layout issue in the new pure tests;
both were repaired locally, without changing inherited code or dependencies.

Amended actual-PG test, run once:

```powershell
$env:RUN_POSTGRES_INTEGRATION='1'
$env:PYTHONUTF8='1'
./.venv/Scripts/python.exe -m pytest tests/test_provider_logout.py::test_wrong_signature_algorithm_missing_claims_and_time_bounds -q --tb=no --basetemp=outputs/provider-logout-test-clock-green-target-pg-20261103
```

Exit0: **1 passed, 2 warnings in 6.54s**; no skips.

Full inherited provider-logout module, run once:

```powershell
$env:RUN_POSTGRES_INTEGRATION='1'
$env:PYTHONUTF8='1'
./.venv/Scripts/python.exe -m pytest tests/test_provider_logout.py -q --tb=no --basetemp=outputs/provider-logout-test-clock-green-module-pg-20261103
```

Exit0: **85 passed, 2 warnings in 23.10s**; no skips/failures. `--tb=no`
protects legacy assertion diagnostics; no failures are downgraded or ignored.

Final pure verification after lint-only corrections, on frozen sources:

```powershell
$env:PYTHONUTF8='1'
./.venv/Scripts/python.exe -m pytest tests/test_provider_logout_clock.py -q --tb=no --basetemp=outputs/provider-logout-test-clock-final-pure-20261103
./.venv/Scripts/python.exe -m ruff check tests/test_provider_logout.py tests/provider_logout_clock.py tests/test_provider_logout_clock.py
```

Pytest exit0: **14 passed, 2 warnings in 3.79s**. Ruff exit0:
**All checks passed!** Ruff also passed before the full module run.

## Concerns and preserved boundaries

The two known dependency warnings are Starlette's httpx TestClient deprecation
and Authlib's httpx deprecation. They were not suppressed and no dependency
updates were attempted. Other inherited tests still contain direct assertions;
their broad privacy cleanup is intentionally outside this deliverable.

Docker server reported 29.8.0. After verification, `docker ps` showed only the
pre-existing RabbitMQ container; disposable PostgreSQL fixtures cleaned up their
own containers. User RabbitMQ, `.env`, original checkout, inherited changes and
legacy locks were not read/modified/removed by this task. No production source,
migration, permissions, TLS or role policy changed. No staging, commit, child
agent, external identity or external write was used. Actual Keycloak and frontend
suites were not rerun because no corresponding sources changed. Independent
specification/quality review and any readiness decision remain pending with
the coordinating agent.
