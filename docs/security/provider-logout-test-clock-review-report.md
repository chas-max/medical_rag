# Task10 independent specification and quality review

## Spec Compliance

- ✅ Spec compliant for the supplied three-file exact working-tree snapshot, based on the captured original rather than a commit range. The inherited change is confined to `tests/test_provider_logout.py:219`; the two additional files are focused test-only support. No production policy or verifier edit appears in the package.
- ✅ Deterministic independent-clock reproduction demonstrates caller T / payload iat T+1 / exp T+301 giving a 300-second lifetime, without sleep or global-clock patching (`tests/test_provider_logout_clock.py:21`).
- ✅ The affected HTTP test obtains the credential-bearing fixture privately through `request.getfixturevalue`, samples one epoch after readiness, and derives every payload time explicitly from it (`tests/test_provider_logout.py:225`, `tests/test_provider_logout.py:228`, `tests/test_provider_logout.py:231`). Shared inherited helper signatures remain unchanged in the supplied full context.
- ✅ Clock repair patches only PyJWT's UTC datetime reference and the provider verifier's imported clock through monkeypatch (`tests/provider_logout_clock.py:10`, `tests/provider_logout_clock.py:17`). Agreement, untouched global `time.time`, and restoration of both references are explicitly tested (`tests/test_provider_logout_clock.py:76`, `tests/test_provider_logout_clock.py:92`).
- ✅ Exact HTTP boundaries are present: current iat with positive exp, lifetime 300 accepted, lifetime 301 rejected, future iat +1 rejected, age 120 accepted, age 121 rejected, exp equal to now rejected, and exp before now rejected (`tests/test_provider_logout.py:266`). The original nonpositive-lifetime case is also retained (`tests/test_provider_logout.py:282`). The same table exercises the real verifier, not a mocked acceptance decision (`tests/test_provider_logout_clock.py:101`).
- ✅ Wrong RSA signature, HS256 substitution and each required-claim deletion are still sent through the actual HTTP endpoint (`tests/test_provider_logout.py:247`, `tests/test_provider_logout.py:252`). Every result requires exactly one request-correlated audit row with the mandatory action/resource/operator/outcome/reason fields (`tests/test_provider_logout.py:236`). Fresh UUID jti values avoid boundary decisions being replaced by replay conflicts (`tests/test_provider_logout.py:231`).
- ✅ Assertions in the amended function use precomputed boolean names with static messages. The focused AST contract and synthetic pytest-rewritten execution replay the actual assertions, retain case labels and boolean proof, and check signed-token/database canaries are absent (`tests/test_provider_logout_clock.py:37`, `tests/test_provider_logout_clock.py:46`, `tests/test_provider_logout_clock.py:64`).
- ⚠️ Cannot independently establish execution chronology or the reported PostgreSQL/Ruff results from a source diff. The implementer records RED 3 failed / 1 passed, final pure 14 passed, actual-PG target 1 passed, full module 85 passed and scoped Ruff passing (`docs/security/provider-logout-test-clock-report.md:101`, `docs/security/provider-logout-test-clock-report.md:126`, `docs/security/provider-logout-test-clock-report.md:136`, `docs/security/provider-logout-test-clock-report.md:147`). Controller-owned frozen hash checks and fresh pure verification remain separate evidence.
- ⚠️ Preservation of unrelated worktree state, `.env`, RabbitMQ and the original checkout is not established by this scoped package. No such surfaces were inspected during this review. This is not whole-branch, hospital, clinical/legal or production approval.

## Strengths

- `tests/test_provider_logout.py:236`: One private predicate combines HTTP behavior with request-correlated mandatory audit evidence, without putting signed-token calls or credential structures into assertion expressions.
- `tests/test_provider_logout.py:266`: Exact accepted neighbors are tested alongside rejected limits; this prevents merely broadening a tolerance to hide the inherited race.
- `tests/provider_logout_clock.py:10`: The 18-line helper has one test-specific responsibility and uses monkeypatch-owned restoration rather than a new production abstraction.
- `tests/test_provider_logout_clock.py:46`: The privacy proof rewrites the actual amended assertions rather than a look-alike example, making it responsive to future unsafe assertion edits.

## Issues

### Critical (Must Fix)

- None found in this task snapshot.

### Important (Should Fix)

- None found in this task snapshot.

### Minor (Nice to Have)

- `docs/security/provider-logout-test-clock-report.md:152`: Reported test runs retain two existing dependency deprecation warnings (Starlette/httpx TestClient and Authlib/httpx), so output is not pristine. Track compatible dependency remediation in the final branch review; do not suppress warnings or expand this narrowly authorized test repair to update dependencies.

## Checks and limits

- Read the binding brief, implementation report and exact full-context review package; initial tool output truncated the inherited context, so only the omitted package hunk was recovered. Changed source files were not separately reread. A metadata-only package pass derived current-file line references.
- No source outside the package was inspected: no concrete unresolved cross-file risk required broadening the review. No tests were rerun because the code review raised no specific doubt unanswered by the reported focused runs; no source, index, HEAD or branch mutation was performed. Only this review report was added.

## Assessment

**Task quality: Approved.**

The repair addresses the test's competing time domains and assertion-rewrite disclosure risk while preserving the real verifier policy, HTTP path and mandatory auditing. The remaining Minor warning item is inherited dependency maintenance, not a blocker for this task-scoped gate.
