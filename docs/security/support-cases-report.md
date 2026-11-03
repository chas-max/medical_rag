# Task9 — structured account support implementation report

2026-11-03. Implementation handoff only; independent specification and quality
review remain required. Root owns broad nonintegration, browser QA, readiness
and plan/progress documents. No production, clinical, identity-proofing or legal
approval is implied.

## Implemented boundary

The isolated patient app now provides the seven approved `/support/cases` and
`/support/queue` routes. Account support is independent of adult-patient
eligibility and health-purpose consent. Queue authority uses current local
`is_allowed(SUPPORT_CASE_MANAGE)` including the existing combined-role MFA rule;
no role policy or MFA policy changed. No account recovery, role/binding edit,
generic account lookup, patient content, attachment, free-text ticket, external
notification or model call was added. The UI explains that fully locked-out
accounts cannot use this authenticated channel.

Schema7 is owner-run only. Cases have fixed categories, states and outcomes,
same-tenant requester/assignee foreign keys, immutable identity/category/created
fields, database timestamps, FORCE RLS and transaction-bound owner/queue scope.
Runtime INSERT is limited to tenant/case/requester/category; UPDATE is limited
to status/assignee/resolution. There is no runtime DELETE/TRUNCATE or identity
authority write grant. Existing fixture migrations, owner grant recipe and
startup preflight were updated. RLS assumes trusted application SQL and does
not protect against arbitrary runtime SQL or a schema/DDL owner.

Every operation obtains its own live SessionStore authorized transaction and
mandatory AuditStore. Case transitions lock the case, refresh live authority,
then re-read permission/resource/state in fresh SQL. Take has one winner;
resolve requires the current assignee; owner cancellation works even after
assignment. Denial audit reauthenticates after rollback. Mutation and minimal
audit commit together, and an audit outage fails closed. No case ID, category,
payload, path or requester field was added to audit storage.

Development admission caps are 20 created cases per rolling 24 hours and five
nonterminal cases per account. An advisory transaction lock derived from
tenant/account serializes distinct sessions, followed by live-authority refresh
and fresh indexed bounded count queries. Cancellation does not reset daily
admission. These are not institution-approved production rate limits or a
replacement for gateway/IP controls.

The focused SupportCases component/client parser provides create/list/detail/
cancel plus permission-gated queue/take/fixed resolution. Returned categories,
states, outcomes and UUIDs are validated; only public fields are copied. It
retains the parent blur clearing, terminal-action barrier and stale generation
guards. No storage, token URL, text/upload field or HTML injection was added.

## Pre-edit snapshots and scope

Root's 16 supplied originals remain in `outputs/support-cases-baseline-20261103`.
Before additional existing-target edits, apply_patch captured:

- `outputs/support-cases-baseline-20261103/tests__test_oidc_login.py`
- `outputs/support-cases-baseline-20261103/tests__test_identity_sessions.py`
- `outputs/support-cases-baseline-20261103/tests__test_oidc_login__pre-diagnostic-fix.py`

All source edits used apply_patch (format/import tools performed mechanical
formatting). Existing user edits were preserved. No .env file, legacy unlock,
commit/staging, original checkout, production DB, actual patient/hospital data,
IdP administration or institutional message was touched. Disposable fixture
PostgreSQL and actual fictional-provider containers only; no unrelated RabbitMQ
cleanup. No public server, publishing or source push was performed.

## RED and GREEN evidence

Commands run from repository root unless prefixed `frontend/`. Python commands
used `$env:PYTHONUTF8='1'`; actual PG commands additionally used
`$env:RUN_POSTGRES_INTEGRATION='1'`. Pytest used `.venv/Scripts/python.exe`.
Dependency warnings were the existing Starlette/httpx2 and Authlib/httpx2
deprecations, not suppressed by this implementation.

### TDD RED

1. `.venv/Scripts/python.exe -m pytest tests/test_support_cases.py -q --tb=short --basetemp=outputs/support-cases-red-tmp`
   — 1 failed, missing authenticated structured-case route; static boolean
   assertion, no credential-bearing assertion operands. First GREEN with
   `outputs/support-cases-green-tmp`: 1 passed.
2. Same module with `--basetemp=outputs/support-cases-red2-tmp` — 1 failed,
   18 passed: preflight did not reject broad support column mutation grants.
   GREEN with `outputs/support-cases-green2-tmp`: 19 passed.
3. `npm --prefix frontend test -- --run src/test/patient-support.test.tsx` — 4 failed:
   support view/navigation missing. Initial GREEN support + existing app suite:
   62 passed (4 support, 58 existing). Two intermediate text-matcher failures
   were diagnosed as combined category/status text; the displayed transitions
   were already correct. Matchers were corrected without relaxing behavior.
4. `.venv/Scripts/python.exe -m pytest tests/test_support_cases.py::test_validation_authentication_origin_csrf_uniform_missing_and_minimal_audit -q --tb=short --basetemp=outputs/support-cases-red-delete-tmp`
   — 1 failed: DELETE ignored extra body fields. Reused the bounded strict JSON
   helper with optional empty input for DELETE; extra/duplicate keys now fail.
   GREEN with `outputs/support-cases-green-delete-tmp`: 1 passed.
5. After Docker became unavailable, root approved a test-only diagnostic fix.
   `.venv/Scripts/python.exe -m pytest tests/test_support_cases_privacy.py::test_failed_disposable_database_start_hides_subprocess_credentials -q --tb=short --basetemp=outputs/support-cases-start-privacy-red-tmp`
   — 1 failed safely: the boolean negative diagnostic detected inherited
   CalledProcessError argument exposure without printing synthetic credentials.
   The shared fixture now uses check=False and static pytest.fail(pytrace=False)
   on Docker start failure. No production behavior or grants changed.

### Successful verification

- `.venv/Scripts/python.exe -m coverage run --source=medical_rag.support_service -m pytest tests/test_support_cases.py -q --tb=short --basetemp=outputs/support-cases-green3-tmp`
  — 26 passed. Expanded rolling-cap suite with `outputs/support-cases-final-tmp`
  — 27 passed. Support + two initial privacy tests with
  `outputs/support-cases-coverage-final-tmp` — 29 passed. Subsequently added
  deterministic roleless-owner cancellation after staff assignment:
  `.venv/Scripts/python.exe -m pytest tests/test_support_cases.py::test_requester_can_cancel_an_assigned_case_without_takeover -q --tb=short --basetemp=outputs/support-cases-roleless-tmp`
  — 1 passed.
- Final combined recovered-Docker PG/coverage run: 256 passed; command/result
  below. The last narrowed exception catch in the privacy regression was
  independently rechecked by the final three-test privacy command as well.
- `.venv/Scripts/python.exe -m pytest tests/test_support_cases_privacy.py -q --tb=short --basetemp=outputs/support-cases-private-final-tmp`
  — 3 passed, including negative rewritten assertions and simulated private
  subprocess-start failure. Fixture arguments are repr-hidden wrappers and
  support assertions operate only on boolean locals with static messages.
- With `RUN_KEYCLOAK_INTEGRATION=1` and `RUN_POSTGRES_INTEGRATION=1`:
  `.venv/Scripts/python.exe -m pytest tests/test_keycloak_mfa.py::test_actual_password_otp_signed_assurance_allows_only_current_auditor -q --tb=short --basetemp=outputs/support-cases-actual-mfa2-tmp`
  — 1 passed in 62.53s. Real fictional password/OTP, signed assurance, real
  verified-HTTPS provider sockets, current local auditor and patient denial;
  schema7 and owner runtime recipe applied by the actual-provider fixture.
  Application HTTP is TestClient, not full-browser authenticated E2E. An earlier
  invocation without the opt-in flag skipped and is not counted as validation.
- `npm --prefix frontend test -- --coverage --coverage.include=src/patient/support.ts --coverage.include=src/patient/SupportCases.tsx --coverage.reportsDirectory=../outputs/support-cases-frontend-coverage`
  — 208 passed, 20 files. New support.ts: 100% statements/lines/branches/functions.
  SupportCases.tsx: 98.4% statements/lines, 91.66% branches, 100% functions.
  Combined new UI source: 98.97% statements/lines, 95.79% branches, 100% functions.
  Includes all 58 existing PatientApp lifecycle tests plus seven functional
  support tests and two strict parser tests. Functional owner/support actions,
  direct403, bounded pagination, stale detail after cancellation, revoked queue,
  blur clearing and failed-logout terminal barrier are exercised in jsdom.
- `npm --prefix frontend run lint`, `npm --prefix frontend run build`,
  `npm --prefix frontend run build:patient` — all exit0. Both separate builds preserved.
- `.venv/Scripts/python.exe -m ruff check src/medical_rag/support_service.py src/medical_rag/patient_api.py src/medical_rag/patient_service.py src/medical_rag/security_audit.py src/medical_rag/security_server.py tests/test_support_cases.py tests/test_support_cases_privacy.py tests/test_oidc_login.py tests/test_identity_sessions.py tests/test_patient_vertical.py tests/test_security_audit.py`
  — all checks passed after final test-only diagnostic delta.

### Disclosed intermediate/inherited failures

- First affected PG run (support/patient/audit/identity/startup) was 198 passed,
  one failure: inherited exact `/me` expectation still denied the newly
  implemented support role's queue permission. Corrected only that expectation;
  health permissions remain false.
- Expanded command adding OIDC and provider logout was 336 passed, one failure
  in unchanged `test_provider_logout.py::test_wrong_signature_algorithm_missing_claims_and_time_bounds`.
  Its caller snapshots wall-clock `now`, then claims() independently chooses
  iat, so a nominal 301-second maximum-lifetime negative can become 300 seconds
  at a rollover. Isolated repro using `--tb=no` also failed (1 failed). No
  provider/logout source or policy was changed; no JWT or credential-bearing
  diagnostics are copied here. Root owns disposition of this inherited debt.
- After runtime resume Docker's named pipe was absent: final confirmation
  attempts were setup failures, not application failures (support: 2 pure
  privacy passes + 28 setup errors; affected: 51 passes, 172 setup errors,
  two tests depending on manual fixture creation failed). The failed coverage
  attempt overwrote coverage data with no-data and is not valid coverage.
  Root recovered Docker29.8.0; valid final combined evidence is recorded below.

## Final combined result

Final targeted PostgreSQL command:

`$env:PYTHONUTF8='1'; $env:RUN_POSTGRES_INTEGRATION='1'; $env:COVERAGE_FILE='outputs/support-cases.coverage'; .venv/Scripts/python.exe -m coverage run --source=medical_rag.support_service -m pytest tests/test_support_cases.py tests/test_support_cases_privacy.py tests/test_patient_vertical.py tests/test_security_audit.py tests/test_identity_sessions.py tests/test_security_server.py tests/test_oidc_login.py -q --tb=no --basetemp=outputs/support-cases-recovered-final-tmp`

Result: 256 passed, two known dependency warnings, exit0 in 124.38s. This includes
28 support integration cases, three privacy regressions, and 225 affected
patient/audit/identity/startup/OIDC cases. Final service coverage re-read after
this successful run: 86 statements, two missed, 98% (84/86); misses are defensive
invalid SessionStore construction and unknown internal transition operation.
Coverage data is `outputs/support-cases.coverage`; frontend coverage is
`outputs/support-cases-frontend-coverage`. Root's broad nonintegration, optional
synthetic browser QA and independent reviews are separate gates, not claimed as
authenticated end-to-end evidence by this implementation report.

## Source freeze manifest (SHA256)

Production source froze before root broad/browser QA. The only later approved
test delta was static Docker-start diagnostic normalization and its regression
test; both hashes below supersede the first freeze. The full root review scope
contains eight new and eighteen existing files (26 total): thirteen existing
files were implementation-modified; the other five existing files are included
to reconcile the exact byte-baseline review package. Those five (access_policy,
conftest, test_security_server, patient-audit.test, patient-client.test) have
different byte hashes from supplied snapshots but unchanged normalized content
(CRLF to LF, trailing whitespace removed); this implementer did not edit them.
This report and ignored snapshots/results are excluded from the source manifest.

| Path | SHA256 |
| --- | --- |
| database/migrations/0007_support_cases.sql (new) | ae8592217fbed6c0d5e310cb196ec9369d400eb658a5d1872b3f311ffbc14f3a |
| database/patient_runtime_grants.sql | 73b676867408a55eefb9a45a6a08e2fd5a88b3b270206ba9945ec0867eb6318c |
| src/medical_rag/access_policy.py | e057178506d3a00d68d0e6f1a8be58ed4326e4ea6abac49e4eff630885ef8dbf |
| src/medical_rag/support_service.py (new) | 7f469411da3bf06d08047e5ec1fcefa72fe0eccd3e4a5764f01e796184d98bc1 |
| src/medical_rag/patient_api.py | b2edbbb595b222ab4819930197eec3cb24a00d4f8a7e5c02dc61b455a2b9e41a |
| src/medical_rag/patient_service.py | 22f1be8c88dea0b09128d4596468d6c3f3e591752070366f779e75f9fe0b2b8c |
| src/medical_rag/security_audit.py | 04f80c8fdf823fc74e03bbe6415feacd16ddcc00622404fd67ad76304e2f5864 |
| src/medical_rag/security_server.py | ffefd032d3f46b1dc2683ddc635a5cabec10ba0df80281240e9d3b612b53077f |
| tests/test_support_cases.py (new) | e9fc01746fb325be405eaa89dfe327860bb396c2e585f8029d112b2e3f73ce77 |
| tests/conftest.py | 234739d2c558c4d56e69d586a1e39d9d1c931efd8a558d1a1cd738a88d212946 |
| tests/test_security_server.py | 62d3c8058650dea6dcb8de69a7031a8cb32a66438a15751dd156cf1436da9563 |
| tests/test_support_cases_privacy.py (new) | 861b913784f99534e0b582faafab355e1ee14ebee74ebcb222eeba74c00d1f0b |
| tests/test_oidc_login.py | e835ae4c37e3d9d23b7c39a2f5711c937cc68072958a5d8d792687292262fdfd |
| tests/test_identity_sessions.py | 7dc1f4faef9e7591386f427f06394f550750fe089d8cbd163da9c7c7bbcd2c4c |
| tests/test_patient_vertical.py | 080abfb8293a0952d095ded24a7e01e09a2cb574c29832fb40c73d6387f54f23 |
| tests/test_security_audit.py | 1956f57958c4b7b9e6e6248dd6dfc9de2203c2cd9c9df3bd855b0750b9e7c4ec |
| frontend/src/patient/support.ts (new) | 611ce9d7d7fb9570d25c01bd5e50ac6a3e49303817d6ced5a07eae66a28179f2 |
| frontend/src/patient/SupportCases.tsx (new) | 7ff27349466b379092e391e24f369bca80041be4b96135b872b924e3f8309801 |
| frontend/src/patient/model.ts | aaebcb91c843680e11bbdba9e07494b47cddffe75fe3c2ecc27d6ecc0b21a7f8 |
| frontend/src/patient/PatientApp.tsx | 9df302b3c8576ab4d98a86e282eefba3022365f0d923a3e80bdc5f034ddaaf3a |
| frontend/src/patient/patient.css | 5cf127e4345696c1464cfe434673b0715608cc5b51ebef3f8098f8abbb68aba4 |
| frontend/src/test/patient-support.test.tsx (new) | 717080968952d0d115226ade949e8b5bb20baf95075c1d146cc2850f36c331e0 |
| frontend/src/test/patient-support-client.test.ts (new) | d7f262bda739712d35856949bac34174ec84bdbf5596a552d5ce530068d63f85 |
| frontend/src/test/patient-app.test.tsx | 495596275324f5dfec789e783bd858477f9c17a8f3218173d8830091d4706062 |
| frontend/src/test/patient-audit.test.tsx | 9ab5f7cd869626457164393bf9044ee375cf5ca31474d2d9741db76868474630 |
| frontend/src/test/patient-client.test.ts | 1e69b97f931334fb235c3cfd2c5fead8c1e3a94fddd5d33f114f3de81781eac1 |

## Remaining limitations

No institutional support notice or retention policy is approved by this code.
Terminal cases remain retained without a deletion endpoint. Admission caps are
development-only. Already-authorized operations are in flight; revocation is
checked on the next authoritative statement/request, not push cancellation.
The authenticated UI cannot recover a completely inaccessible account. Real
hospital integration, production migration, external notification, account
unlock/password recovery, guardianship/minor processing, and clinical/legal
acceptance remain out of scope. Independent specification and quality review
must pass before Task9 is accepted.

## P2 active-support navigation fix — 2026-11-03

Implementation status: DONE_WITH_CONCERNS, pending independent re-review; this
does not accept Task9. Only `frontend/src/patient/PatientApp.tsx` and
`frontend/src/test/patient-support.test.tsx` changed in this fix, plus this
appended report. The supplied pre-fix originals in
`outputs/support-cases-fix-baseline-20261103` were hash-verified against the
working files before editing: PatientApp SHA256
`9df302b3c8576ab4d98a86e282eefba3022365f0d923a3e80bdc5f034ddaaf3a`;
patient-support test SHA256
`717080968952d0d115226ade949e8b5bb20baf95075c1d146cc2850f36c331e0`.

Root cause: navigating to the already-mounted support view incremented the
parent generation, invalidating its initial loader, without changing either
SupportCases effect dependency or starting a replacement load. The fix returns
for support-to-support navigation only, after the existing terminal/pending/
active and permission checks and before generation invalidation. Other views'
same-view navigation behavior and cross-view stale-read invalidation remain
unchanged. Blur clearing, logout/withdraw terminal barriers, mutation guards,
current permission checks and backend authorization are unchanged.

Two parameterized regressions hold the initial own-list or queue response,
click the active support navigation twice, then fulfill the response. They
require actual owner detail to open, loading indicators to disappear and the
queue take action to expose the completion action. They use only the existing
fictional UUID/structured fixtures and no real data or credentials.

### Fix RED/GREEN and validation

Commands run from repository root; all source/report edits used apply_patch.

- RED, before any production edit:
  `npm --prefix frontend test -- --run src/test/patient-support.test.tsx -t 'reselecting active support'`
  — exit1, 2 failed / 7 skipped, one failed file. Both failed because
  `查看详情` was absent after fulfilling the held response; initial output
  showed both lists still loading. After making the test handler resume normal
  responses after the held initial request (so later mutation reloads are not
  also held), the same command with `$env:DEBUG_PRINT_LIMIT='500'` again
  produced exit1, 2 failed / 7 skipped, for the same expected missing detail.
  DEBUG_PRINT_LIMIT shortened synthetic DOM diagnostics only.
- GREEN:
  `npm --prefix frontend test -- --run src/test/patient-support.test.tsx src/test/patient-app.test.tsx src/test/patient-audit.test.tsx`
  — exit0, 85 passed across three files: 9 support (including both new cases),
  58 existing app lifecycle and 18 audit tests.
- Whole frontend, run once after this fix:
  `npm --prefix frontend test -- --coverage --coverage.include=src/patient/support.ts --coverage.include=src/patient/SupportCases.tsx --coverage.reportsDirectory=../outputs/support-cases-fix-frontend-coverage`
  — exit0, 210 passed across 20 files. SupportCases: 98.4% statements/lines,
  91.66% branches, 100% functions. support.ts: 100% in all categories. Combined:
  98.97% statements/lines, 95.79% branches, 100% functions. Coverage artifacts
  are in `outputs/support-cases-fix-frontend-coverage`.
- `npm --prefix frontend run lint`, `npm --prefix frontend run build`,
  `npm --prefix frontend run build:patient` — each exit0. No frontend test,
  lint or build warnings were emitted. `git diff --check` also exited0;
  Git separately noted existing LF/CRLF conversion warnings on unrelated
  frontend config files, with no source rewrite performed.

### Fix source freeze and boundaries

Source freeze verified at 2026-11-03T18:11:09+08:00; no further source edits
after validation. These two hashes supersede only their entries in the earlier
26-path Task9 manifest; every other Task9 source remains outside this fix.

| Path | SHA256 |
| --- | --- |
| frontend/src/patient/PatientApp.tsx | 75c0573cf543b6fc93b81590ffdbe18d01cb5477570510929f51446f8650b715 |
| frontend/src/test/patient-support.test.tsx | 9ddc21747fcfb9561506789bc10a67cfe6240c604c5f795b41d5e2e6ebbed7dc |

Unchanged adjacent source was also rehashed: SupportCases.tsx remains
`7ff27349466b379092e391e24f369bca80041be4b96135b872b924e3f8309801`;
support.ts remains
`611ce9d7d7fb9570d25c01bd5e50ac6a3e49303817d6ced5a07eae66a28179f2`.
No backend/database/Keycloak changes or reruns were needed for this UI-only fix.
Existing edits, .env, legacy locks and original checkout were preserved; no
staging, commits, dependency upgrade, external messages or production operation.

Concerns ledger remains open: the inherited Starlette/httpx2 and Authlib/httpx2
dependency warnings and unchanged provider-logout wall-clock timing-test debt
retain the attribution recorded above. They were not fixed or reclassified by
this change. jsdom integration coverage is not authenticated browser/socket E2E
proof. Independent specification/quality re-review and root-owned readiness
updates remain required; Task9 and production/clinical/legal acceptance are not
claimed by this fix handoff.
