Spec compliance: Issues found — the functional support interface can remain permanently loading after repeated navigation to its already-active view. The reviewed database, authorization, audit and HTTP implementation otherwise matches the Task9 brief.

## Scope and evidence

- Task9 specification and quality gate only, against `docs/security/support-cases-brief.md`, then the implementation report and supplied full-context dirty-worktree review package. This is not a commit-range, whole-branch, deployment or production approval.
- The package manifest contains 26 source paths: eight new and eighteen existing. The five reconciliation paths (`access_policy.py`, `conftest.py`, `test_security_server.py`, `patient-audit.test.tsx`, `patient-client.test.ts`) show only trailing blank-line removal in the package, consistent with the reported normalized-content accounting; they add no behavioral changes. The controller separately confirmed all 26 current hashes match the manifest.
- Reported suites were not rerun. The controller supplied fresh 31-test actual-PostgreSQL/privacy and nine-test support frontend results. The implementer reports 256 affected PostgreSQL tests, the actual-provider MFA/startup smoke, 208 frontend tests, source coverage above 80%, scoped Ruff, lint and both builds. These execution claims are distinguished from the code inspection and focused reproduction below.

## Strengths

- `src/medical_rag/support_service.py:51` constructs owner/queue scope from the live authorized principal and calls the existing `is_allowed` for queue authority. `src/medical_rag/access_policy.py:79` retains the existing MFA-required role set and combined-role rule; support gains neither patient access nor identity administration.
- `src/medical_rag/support_service.py:69` serializes admission by tenant/account advisory lock, refreshes authority after waiting, then uses bounded fresh counts for both caps. `tests/test_support_cases.py:177` and `tests/test_support_cases.py:359` exercise distinct-session concurrent admission limits.
- `src/medical_rag/support_service.py:123` locks the case, refreshes authority and scope, then rereads resource/state in fresh SQL before enforcing owner cancellation, single-winner take and assigned-only resolution. `tests/test_support_cases.py:248` covers post-lock authority changes and `tests/test_support_cases.py:285` covers transition races.
- `database/migrations/0007_support_cases.sql:15` supplies composite tenant/account foreign keys and state constraints; `database/migrations/0007_support_cases.sql:27` enforces immutable fields; `database/migrations/0007_support_cases.sql:40` enables FORCE RLS with transaction-bound support scope. `database/patient_runtime_grants.sql:52` limits runtime insert/update columns, and `src/medical_rag/security_server.py:118` rejects forbidden support privileges.
- `src/medical_rag/security_audit.py:207` commits successful case changes with the existing minimal audit insertion; `src/medical_rag/security_audit.py:227` reauthenticates denial attribution after rollback. The fixed support action/resource additions do not add case, category, requester or payload fields to audit storage. `tests/test_support_cases.py:198` checks audit-outage rollback and read denial.
- `src/medical_rag/patient_api.py:213` reuses authenticated credentials and strict bounded JSON for the seven support routes. `frontend/src/patient/support.ts:43` validates structured values and copies only public fields; `frontend/src/patient/SupportCases.tsx:170` communicates the authenticated-channel and fixed-category limits without adding medical text or uploads.

## Issues

### Critical

None found in the reviewed Task9 change.

### Important

- **[P2] Re-selecting support while its first load is pending strands both lists.** `frontend/src/patient/PatientApp.tsx:225` increments the global generation even when `next` is already `support`. `setView(next)` keeps the same component mounted, while `frontend/src/patient/SupportCases.tsx:31` starts its loader only when `[queue, read]` changes. Those dependencies remain unchanged. The existing responses therefore fail `live()` and are discarded, but no replacement read starts; both lists remain “正在读取支持请求…” and detail/queue actions never appear. A normal double click on the navigation item can trigger this on a slow response. Make same-view navigation a no-op before invalidation, or explicitly restart/remount the support loader. Add a regression that holds the initial response, re-selects support and then completes the response.

  Focused verification: an in-memory jsdom harness compiled the actual PatientApp, SupportCases, model and support source reconstructed from the review diff, with synthetic request responses. It rendered `#support`, held the initial own-list request, clicked the active “账户支持” navigation button, then fulfilled both list responses. Observed calls were exactly `/auth/session`, `/me`, `/support/cases?limit=20&offset=0`, `/support/queue?limit=20&offset=0`; both loading messages remained and the detail action was absent. No source files were written by the harness, and no reported suite was rerun.

### Minor

- **[P3] Known dependency warnings remain in verification output.** `docs/security/support-cases-report.md:70` identifies the inherited Starlette/httpx2 and Authlib/httpx2 deprecations; the final affected run also reports two warnings. Track the dependency migration separately and retain explicit attribution rather than treating the output as pristine. This is disclosed existing debt, not a Task9 security regression or an additional blocker.

## Named-risk checks and remaining boundaries

- Checked the concrete risk that `refresh()` might leave the cached tenant/account or permission snapshot usable after a lock wait: the single focused unchanged-code inspection of `src/medical_rag/identity_sessions.py:72` shows it rejects any changed principal, while `src/medical_rag/identity_sessions.py:218` retains the session lock and uses fresh READ COMMITTED authority queries. This supports the scope reuse and post-lock checks above; it also rules out a stale `/me` support permission caused by a successful refresh returning changed roles.
- Checked the new parent/component generation contract with the focused pending-load reproduction above. The remaining terminal barrier and blur clearing are preserved in `frontend/src/patient/PatientApp.tsx:55`, `frontend/src/patient/PatientApp.tsx:107` and `frontend/src/patient/PatientApp.tsx:287`; the task tests cover blur, failed logout, stale detail and queue 403 at `frontend/src/test/patient-support.test.tsx:113`, `frontend/src/test/patient-support.test.tsx:137`, `frontend/src/test/patient-support.test.tsx:176` and `frontend/src/test/patient-support.test.tsx:188`.
- **Cannot verify from this diff alone:** execution of the reported RED-before-source sequence, complete test/build output, measured coverage and exact source freeze. The controller should retain the reported artifacts and its hash/test verification. The supplied controller evidence is acknowledged, not recast as independently rerun evidence.
- **Cannot verify from this diff alone:** the full unchanged login/Origin/CSRF/no-store boundary and actual-provider socket implementation. The task routes reuse that boundary, and the task adds HTTP denial tests at `tests/test_support_cases.py:119`; the controller should retain the affected auth/startup regression and actual-MFA smoke evidence. Synthetic browser interaction and TestClient are not full authenticated browser/socket E2E proof.
- **Cannot verify from this diff alone:** the disclosed unchanged provider-logout timing failure, institutional notice/retention approval, production migration or deployment. The controller must disposition the existing timing-test debt separately. Real institutions, production operations, legal/clinical acceptance and recovery workflows remain outside this task gate.

## Assessment

**Task quality: Needs fixes.**

The service and database boundary is focused and backed by meaningful PostgreSQL concurrency, revocation, grant and audit tests. Fix the reproducible support-navigation loading race and run a focused regression before accepting Task9; no whole-branch or production certification is implied.

## Final independent re-review — P2 correction

**Spec compliance: Spec compliant within the Task9 scope reviewed.** This final verdict supersedes the initial “Issues found” verdict above. The Important/P2 navigation defect is closed; the original strengths and stated verification boundaries continue to apply.

- Reviewed the appended fix report and the exact two-file pre-fix/current package `docs/security/support-cases-fixes-review.diff`. Its final source hashes are PatientApp `75c0573cf543b6fc93b81590ffdbe18d01cb5477570510929f51446f8650b715` and patient-support tests `9ddc21747fcfb9561506789bc10a67cfe6240c604c5f795b41d5e2e6ebbed7dc`. The backend, database, SupportCases component and support client are unchanged by this correction.
- **Important/P2 closed:** `frontend/src/patient/PatientApp.tsx:227` now returns for support-to-support navigation before incrementing the generation. The existing terminal/pending/active guard at line 214 and permission checks at line 215 still execute first. The mounted loader therefore retains a live generation when the active support navigation item is clicked again; cross-view navigation, other views, mutation invalidation, blur clearing and terminal-action handling retain their existing behavior.
- **Regression quality:** `frontend/src/test/patient-support.test.tsx:114` parameterizes the initial own-list and queue waits. Each scenario waits until its response is actually pending, clicks support twice, releases the response, then requires detail rendering, removal of loading indicators and a functional queue take transition. These are observable behavior checks that directly cover the previously reproduced failure, rather than assertions mirroring the new conditional.
- **Validation evidence:** the fix report records both parameterized cases failing before the production change, then 85 focused tests passing, all 210 frontend tests passing, support source coverage above 80%, lint and both builds passing without frontend warnings. No concrete doubt requiring another reviewer test run arose from the fix package; reported suites were not rerun. The controller's separate final focused verification remains its own evidence. Earlier PostgreSQL evidence remains applicable because this correction changes no backend source.

### Remaining issues

- **Critical:** none found.
- **Important:** none remain from this task review.
- **Minor/P3 remains open:** the previously attributed Starlette/httpx2 and Authlib/httpx2 dependency warnings are unchanged. The disclosed provider-logout timing-test debt also remains a separate controller-owned disposition; this UI correction neither fixes nor conceals it.
- **Cannot verify from this diff alone:** the reported execution history and full artifacts, unchanged authenticated browser/socket behavior, institutional notices and retention, or any production deployment. Retain the controller verification and original boundaries; this approval does not establish authenticated full-browser E2E or institutional, clinical, legal or production acceptance.

**Final task quality: Approved.** The minimal correction resolves the reproduced loader race while preserving the existing lifecycle and permission guards, with meaningful regressions for both initial loading phases. This approval is the Task9 specification/quality gate only.
