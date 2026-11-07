# Security implementation progress

2026-11-02: Existing OIDC protocol, opaque sessions, purpose policy and scoped
repository experiments verified previously; legacy API/workers remain locked.
They were not yet an end-to-end patient application.

Task1: complete for the defined fictional-data slice. Fresh PostgreSQL
authority/consent connected to OIDC patient conversation APIs. Final168 tests
passed, API100%/service98%/sessions93% coverage; two dependency deprecations.
Task2: complete. /root/patient_vertical_review approved spec compliance and
code quality, no findings; static review, not independent production acceptance.
Task3: in progress: trusted startup and usable patient/identity UI, using both
patient-ui-brief.md and patient-startup-brief.md as one runnable-portal task.
Initial verification: backend178 including actual standby + TLS process;
frontend144 and both builds passed. Independent review found terminal-action
race (Important), stale read replacement and scoped-IPv6 CSP syntax gaps
(Minor). Fixes and re-review required; see patient-portal-review-findings.md.

Resume 2026-11-02: partial terminal-barrier/read-generation and scoped-IPv6
fixes were preserved after an interrupted implementation run. A fresh focused
fix task is validating the retained code, adding any missing regression cases
and rerunning amended tests. Task3 is not yet approved. Mandatory connected
audit and auditor UI requirements are recorded in patient-audit-brief.md;
implementation starts only after this review gate closes.

Controller resume verification: access_policy, consent_policy, security_lockdown,
public_error_safety and stream_error_safety tests: 189 passed, one existing
Starlette/httpx deprecation warning. Repeated with explicit worktree basetemp
(.pytest-tmp-patient-portal-policy-resume-20261102) to avoid the unrelated
system-temp pytest-current permission error seen during the first run's
atexit cleanup; repeated run exited without that cleanup error. This focused
run is not evidence for the concurrently amended portal race fixes.

Task3 resumed fix verification: frontend174 passed, patient-specific57 passed
(51 DOM +6 transport), frontend statement coverage96.92%; lint and both builds
exit0. Restored Docker Desktop hidden after engine was absent; actual startup/
OIDC PostgreSQL rerun91 passed with2 existing dependency warnings. Independent
re-review /root/portal_fix_review is pending; see patient-portal-fix-report.md.
Controller broad regression after amended source:
`python -m pytest -q -m 'not integration' --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking --basetemp .pytest-tmp-patient-portal-final-20261102`:
1349 passed,4 skipped,165 deselected,2 known dependency warnings in145.34s.
No pytest temp-cleanup error. Full unfiltered suite is not claimed passing.

Task3: complete for defined fictional-data portal/startup scope. Independent
/root/portal_fix_review approved spec and quality of all3 amended findings,
no Critical/Important/Minor findings. Review inspected amended source/tests,
not a second suite execution or real-IdP/browser lifecycle acceptance.
Task4: in progress, mandatory patient-app audit plus current-MFA auditor
metadata API/UI; requirements patient-audit-brief.md. Legacy stays locked.

Next connected continuation requirements: provider-logout-brief.md (not yet
implemented; execute only after Task4 review). Signed provider logout must
revoke local sessions and fence pending stale logins; it does not establish
real IdP interoperability or instantly clear an idle browser. Official protocol
was checked before writing those requirements; strict profile and institution
registration remain explicit integration gates. Key rotation is still pending.

Task4 implementation frozen: report patient-audit-report.md records256 passed
in actual-PG/auth/patient/startup covering regressions, audit98% statements,
frontend199 passed, lint/both builds/ruff/diffcheck exit0. Two existing backend
dependency warnings remain. Exact task-scoped package patient-audit-review.diff
prepared from preserved preTask4 snapshots plus6 new implementation/test files;
independent /root/patient_audit_review is in progress. Not yet accepted.

Resume handling: a new user continuation can leave an implementation subagent
interrupted. Check live agent state and use followup_task to resume that same
task; send_message alone does not restart an interrupted agent. Do not discard
retained source, repeat completed stages or silently count unfinished checks.

Controller broad regression on the frozen audited application:
`python -m pytest -q -m 'not integration' --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking --basetemp .pytest-tmp-patient-audit-broad-20261102`:
1350 passed,4 skipped,199 deselected,2 existing warnings in159.82s. No temp
cleanup error. Still not a passing unfiltered full suite or production gate.

Task4: complete for fictional-data development scope. Independent
/root/patient_audit_review approved both specification and quality, no
Critical/Important/Minor actionable findings. Checked actual transaction exit,
error/denial chain, request context, callback cleanup, logout cookies, RLS,
minimal grants and auditor UI; did not rerun suites. No production approval.
Task5: in progress, signed provider logout with session correlation, replay
protection and pending-login fences, provider-logout-brief.md. No real provider
registration or identity proofing is performed by this engineering stage.

Browser QA: built anonymous/error landing checked in IAB at default viewport,
static preview only (no auth backend or real IdP). A controlled local two-origin
CSP test reproduced blocked POST303 with form-action self; adding only the
fixed destination origin restored navigation. Correct patient HTML policy
must therefore permit the fixed trusted IdP origin, not arbitrary targets.
This is not proof of actual institutional provider interoperability.

Task5 implementation frozen for independent /root/provider_logout_review:
provider-logout-report.md records376 affected actual-PostgreSQL tests passed,
then85 provider tests after two added subject-control cases (application source
unchanged), new verifier100% statement coverage. Frontend199 tests, scoped
Ruff, lint and both builds passed. Full-repository Ruff still has41 findings
in untouched legacy code; it is not clean. No production acceptance claimed.
Controller broad frozen-source regression, excluding the same known unrelated
500-question anchor-mapping failure:
`python -m pytest -q -m 'not integration' --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking --basetemp .pytest-tmp-provider-logout-broad-20261102`:
1352 passed,4 skipped,290 deselected,2 existing warnings in152.08s. Task5 is
not accepted until specification and quality review both pass.

Resume2026-11-03: previous provider-review agent was no longer present and no
final verdict was persisted; Task5 remains pending, not silently accepted.
/root/provider_logout_review_resume is reviewing the same frozen source and
will persist provider-logout-review-report.md. The review package's duplicated
subject-control test line was removed; its normalized new-test section now
matches the actual515-line file. No application change from this correction.
Docker Desktop was absent after the interruption; checked the installed path
and restored it hidden. Existing RabbitMQ is preserved, pinned Keycloak digest
is still cached. PreTask6 snapshots:outputs/keycloak-integration-baseline-20261103.

2026-11-03 Task5: complete for strict-profile fictional-data development.
/root/provider_logout_review_resume approved specification and quality, no
Critical/Important issues. Persisted provider-logout-review-report.md. Root
fresh actual-PG provider run:85 passed,2 existing warnings in23.46s;61/61
statements100%. Minor follow-ups: dependency/legacy lint noise (separate
cleanup, do not hide); add exact owner-grant-recipe provider revoke/fence/replay
smoke in next deployment-validation task. Cross-task boundaries: prior Task4
audit review complete; legacy lock regressions passed; .env was not read or
edited by these tasks. No whole-branch or production approval inferred.
Task6: in progress, actual disposable Keycloak protocol interoperability and
operator-owned private CA, keycloak-integration-brief.md. No real accounts or
hospital identity proofing, no production IdP deployment. Keep Task5 snapshots.

Next engineering requirements prepared, not implemented or accepted:
signing-keyring-brief.md. Bounded operator-approved overlap for the same ID/
logout trust set, not token-directed key discovery. Execute only after Task6
review. Client-secret/encryption-key lifecycle and production incident controls
remain separate gates; public-key removal alone does not revoke local sessions.

Task6 partial milestone (not acceptance): implementer observed clean TLS RED
6failed/7passed, then13passed with2 existing dependency warnings. Operator CA
helper/config/transport implemented; actual pinned Keycloak fixture and native
form tests written and first actual run started. These are intermediate
results; exact final commands/counts and protocol findings belong in the
pending keycloak-integration-report.md. Do not count Task6 complete or replace
actual IdP execution with signed-test responses if an agent is interrupted.

Task6 source frozen; implementer report keycloak-integration-report.md records
367 affected actual-PG regressions passed, final29 selected passed (10 actual
Keycloak+15CA+4infra), TLS21/21 statements100%, frontend199/lint/bothbuilds and
scopedRuff passed. Two dependency warnings remain. Real logout profile worked
without changing production token validators. Test transport boundary remains
real provider sockets + application TestClient, not browser/full-socket E2E.
Exact owner-grant provider revoke/fence/replay smoke added, closing Task5's
coverage-only Minor follow-up. Unique fixture containers/network cleaned,
existing RabbitMQ preserved. Task6 independent /root/keycloak_interop_review
pending, package keycloak-integration-review.diff. No task acceptance yet.
Controller frozen-source broad regression:
`python -m pytest -q -m 'not integration' --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking --basetemp .pytest-tmp-keycloak-broad-20261103`:
1371 passed,4 skipped,300 deselected,2 existing warnings in117.66s. Known500
anchor failure remains excluded; no unfiltered full-suite passing claim.

Task6: complete for defined fictional-data interoperability scope.
/root/keycloak_interop_review approved spec+quality, no Critical/Important;
persisted keycloak-integration-review-report.md. Minor ledger: cleanup should
continue after individual owned-resource errors (close during Task7 fixture
extension); known dependency/legacy lint noise remains separate cleanup.
Root independently reran selected actual-provider suite:
`RUN_KEYCLOAK_INTEGRATION=1; python -m pytest tests/test_keycloak_identity.py tests/test_provider_tls.py tests/test_keycloak_runtime.py -q --tb=short --cov=medical_rag.provider_tls --cov-report=term-missing --cov-fail-under=81 --basetemp outputs/task6-root-verify-20261103`:
29 passed,2 existing warnings in73.28s,21/21 statements100%; no skips.
All12 reported source hashes matched before that result; Docker afterwards
only preserved user RabbitMQ, no fixture-labeled network. Unchanged legacy
locks and authority internals remain covered by prior reviews/regressions;
this task does not re-certify the whole branch or historical environment.
No institutional proofing/MFA/production approval. Task7 signing-key overlap
next, signing-keyring-brief.md; no implementation or acceptance counted yet.

Task7: in progress, bounded operator-approved signing-key overlap shared by
ID/logout verification; no dynamic discovery/compromise-response claim.
PreTask7 snapshots outputs/signing-keyring-baseline-20261103 (20 files).
Implementer will close Task6's nonblocking fixture cleanup robustness follow-up
while extending actual-provider rotation coverage. No existing legacy unlock.

Task7 partial milestone, not acceptance: implementer observed45expected ring
interface/header RED failures then97helper/ID passes; strict config RED1fail/
14pass then147pass/14opt-in-skipped. Actual-PG and real-provider rotation still
pending final evidence. Cleanup robustness RED8 missing-helper failures ->
12focused passes. Actual official provider started and reached missing operator
helper RED; native code+S256/manage-realm-only fixture operator now being
tested. No production signature relaxation or external IdP mutation.
Future actual staff MFA requirements actual-mfa-brief.md prepared, not started;
native password+OTP/LoA proof is separate from previous negative-MFA checks.

Known validation boundaries: isolated tests use signed test-provider responses;
Task6 also exercises actual disposable Keycloak with fictional accounts, not an
actual institutional IdP. No hospital contract/identity-proofing protocol,
guardian implementation, production deployment, penetration/clinical/legal
acceptance. Legacy data not assigned to any new patient. Existing default500
anchor-mapping test is a pre-existing unrelated failure and must not be
silently represented as passing.

Task7 continuation: actual-PG regressions and real-provider rotation reached
intermediate GREEN; final source freeze/report/review still pending. Coverage
exposed fresh-login fixture waits based on the earlier authentication second
instead of the later verified logout event. Fix only the test's timing; retain
the conservative server fence. Authorized bounded stdin import into owned
container tmpfs to remove the inherited host realm.json credential artifact;
provider-internal import is necessarily allowed. Tokens stay process-memory
only. Require privacy/import-transport and cleanup tests before acceptance.

Controller frozen-production-source broad regression2026-11-03:
`python -m pytest -q -m 'not integration' --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking --basetemp .pytest-tmp-signing-keyring-broad-20261103`:
1476 passed,5 skipped,304 deselected,2 existing warnings in98.96s, exit0.
The same known unrelated500-question failure remains explicitly excluded.
Task7 remains pending final source report and independent spec+quality review.

Task7 final source frozen2026-11-03T13:19:16+08:00. Report records327
affected actual-PG/helper passes (signing helper100%),37 actual-Keycloak/fixture
passes (operator86%,runtime97%),frontend199/lint/bothbuilds. Root checked16
source hashes all match; exact package signing-keyring-review.diff includes
8 modified existing +8 new source/test/runbook files (report separate).
Normalization-only snapshot CRLF differences in pyproject/conftest are not
source changes. Independent /root/signing_keyring_review now reviewing.
Root fresh actual rotation+helper verification:
`RUN_KEYCLOAK_INTEGRATION=1; python -m pytest tests/test_keycloak_rotation.py tests/test_signing_keys.py -q --tb=short --cov=medical_rag.signing_keys --cov-report=term-missing --cov-fail-under=81 --basetemp outputs/task7-root-verify-20261103`:
69 passed,2 existing warnings in72.65s,50/50 statements100%,exit0. Docker
afterward only preserved user RabbitMQ. This does not remove review gate or
establish real institutional MFA/proofing/production acceptance.

Task7 first independent review persisted signing-keyring-review-report.md:
spec issues / quality needs fixes, no Critical, one Important I1. Synthetic
pytest-rewrite probes proved failed real-provider ring membership/count and
kid equality assertions render ring/header identifiers. No demonstrated raw
token leak from the currently truthy-or-static-raise verifier contract.
Dispatch /root/signing_privacy_fix for boolean/static diagnostic assertions
and synthetic negative-output regression, then actual rotation rerun and
independent re-review. Task7 not complete; do not start Task8 yet.
Minor M1 remains known dependency/legacy lint maintenance. Reviewer's unchanged
authority/cookies/legacy/TDD/production unverifiables retain prior task gates
and required whole-branch review; no new cross-task approval inferred.

Continuation interruption2026-11-03: /root/signing_privacy_fix errored with
`Your workspace is out of credits. Ask your workspace owner to refill in order
to continue.` This is an external workspace execution/quota gate, not a code
test failure or production acceptance. Pre-fix rotation snapshot exists at
outputs/task7-privacy-baseline-20261103/test_keycloak_rotation.py; rotation
source still matches original a82dcddae1b5abdaec9675b2768df1b97c57a9a76b661b785659bfda6ebf05fd.
Partial tests/test_keycloak_rotation_privacy.py created by fixer is UNVERIFIED;
no completed RED/GREEN or fix report from that agent. Preserve it and inspect
on resume, do not count it as passing or reimplement Tasks1-6. Task7 I1 remains
open; required independent re-review not passed. Task8 not started. Ten source
baselines prepared at outputs/actual-mfa-baseline-20261103 for future Task8;
actual-mfa-brief.md is requirements only. Resume focused privacy TDD/fix and
review once execution credit is restored, not a new full-plan restart.

Resume2026-11-03T15:25+08:00: same fixer successfully resumed; previous quota
error is historical, not assumed current. Clean privacy RED now observed:
7 failed/1 known warning in1.24s before rotation edits, using actual assertion
AST with synthetic canaries only. Focused fix/GREEN/real rotation/re-review
remain pending. Docker29.8.0 available; same isolated branch retained.

Task7 I1 fix source frozen15:28:52+08:00, report appended (historical hashes
remain historical). Fixer recorded original/final-harness RED7 failures,
GREEN76 including7privacy +1actualrotation +68signing cases,2 known warnings
in88.66s; scopedRuff passed. Production hashes unchanged. Root generated
signing-keyring-privacy-review.diff, checked final2 test hashes, fresh focused
privacy7passed/1warning2.74s. Same independent reviewer resumed for I1 re-review;
Task7 not accepted yet. No new broad/full/frontend rerun for test-only fix.
Task8 preflight found OIDCLogin.start has no acr_values request. Brief now
authorizes only TDD minimal nonessential sorted configured request parameter,
not treating requested strength as verified strength; 11 preTask8 baselines
captured. No Task8 source implementation started before Task7 gate.

Task7 complete for routine operator-approved overlap/fictional-data development:
same independent reviewer appended spec compliant + quality Approved, I1
closed, no Critical/Important. Minor dependency/legacy lint debt remains.
Cross-task authority/cookie-CSRF/legacy guarantees rely on unchanged source,
prior accepted task reviews and broad regressions, not new production proof;
whole-branch review and institutional acceptance still outstanding. Task6
cleanup Minor follow-up closed by independently reviewed best-effort owned
resource cleanup. Task8 actual-mfa-brief.md can now execute. Do not redoTask7.

Task8 in progress: /root/actual_mfa_impl, actualMFAreferencepolicy+nativeOTP
verification, localrole/metadataaudit boundaries and nonessential trusted
acr_values request. 11 existing snapshots captured before Task8. No actual
institutional MFA/enrollment/recovery certification or production acceptance
will be inferred from fictional-provider tests. Root owns final broad run.

Task8 partial evidence, not acceptance: trusted nonessential ACR request RED
1fail/1pass thenGREEN2; public policy/nativeOTPdriver RED13 thenGREEN13;
opt-in runtime composerRED2 thenGREEN2. Same publicJSON loaded by opt-in
fixture, default runtime policy unchanged. First actual password+OTP test now
running; actual LoA/non-bypass/SSO/revocation and final regressions/review pending.

Task8 actual-flow intermediate: cold password login reached native OTP page;
first OTP failure traced to fictional credential import encoding (26.8.1
requires explicit BASE32 rather than treating supplied Base32 as literal UTF-8).
Fixture encoding contract RED added then fixed; first real scenarios now
passing. No application authorization/assurance weakening. Exact final source
references/counts and remaining negative/SSO cases belong in pending report.

Task8 actual suite intermediateGREEN10 in221.57s, no flow/authorization
conflict. Cold andSSO login challenge nativeOTP; password-only/localrole and
MFA/no-localrole cannot accessaudit; currentrole/accountdisable/providerlogout
next-request checks passed. Prior fixture failures included BASE32encoding,
single-use OTP timestepreuse and incorrect no-redirect assumptions. Unenrolled
staff redirect to provider required-action, not appsession; stale scenario is
consumedchallenge, NOT elapsed-time expiry. These boundaries remain disclosed.
Final privacy/wrongcode proof improved (RED3->GREEN5, RED1->GREEN1); pure36
passes, helper100%/wholeforms83%/combined88%. Source frozen before final real
provider/actualPG/frontend regressions; root broad run concurrently, not accepted.

Controller Task8 initial-freeze broad regression:
`python -m pytest -q -m 'not integration' --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking --basetemp .pytest-tmp-actual-mfa-broad-20261103`:
1519 passed,5 skipped,316 deselected,2 existing warnings in205.32s,exit0.
During that run implementer hardened only new integration-marked ACR test
argument repr and added one pure privacy guard case; root will verify final
pure delta separately, not claim initial collection included the later case.
ACR fixture's initial expected-failure diagnostics could render an ephemeral
fictional PG credential via inherited tuple repr; new test uses private
request.getfixturevalue access, no credential-bearing diagnostic copied to
report/package. No actual patient/production credential involved. Final
targeted ACR2 and pure37 passed per implementer. ActualPG389 andfrontend199/
lint/bothbuilds passed; finalprovider/report/review still pending.

Task8 final report frozen actual-mfa-report.md: combined90passed,0skips,
358.59s, helper100%/forms97%/combined98%; actualPG389, frontend199/lint/builds
as recorded. Root exactpackage actual-mfa-review.diff4existing+6newfiles,
10sourcehashes match. Fresh finalpure37 passed1warning2.10s, helper100%/
forms83%/combined88%. Fresh rootactual positive-auditor andMFA/no-localrole
2passed2warnings45.05s; only originalRabbitMQ afterward. Independent
/root/actual_mfa_review pending. No taskacceptance yet. Future structured
support-cases-brief.md prepared only; execute after Task8review passes.

Task8 independent review persisted actual-mfa-review-report.md: spec issues/
Needsfixes, noCritical, ImportantI1 new pure composer set(users)/seed-membership
assertions expose fictional password/seed on failure. Reviewer synthetic probes
confirmed both. Production/nativeMFA policy otherwise aligned. MinorM1 consumed
challenge replay dropped browserproof beforecallback; M2 warningdebt retained.
Fresh /root/actual_mfa_privacy_fix assigned I1+targetedM1 test-only correction,
negative-outputTDD, realSSO/replay rerun then independent re-review. No Task8
acceptance or Task9 implementation before gate. Prior cross-task authority/
CSRF/legacy unverifiables retain earlier reviewed guarantees and whole-branch
review requirement, not inferred production certification.

Task8 test-only fix intermediate: 3 source snapshots captured byte-identical
under outputs/task8-fix-baseline-20261103/tests. CleanRED4fail6deselect for
actual composer diagnostic leak and stale-proof shape; pureGREEN41 with1 known
warning, scopedRuff passed. Default pytest rewrite covered explicitly because
active set-diff repr hook can hide one call-argument leak. Original pending
browser cookie and real first callback now retained privately for explicit
replay after consumption, no authenticated cookie; native consumed-provider
challenge retained. Actual targetedSSO/replay run pending, no acceptance.
FutureTask9 16 source snapshots outputs/support-cases-baseline-20261103;
support-cases-brief.md is requirements only, not implemented/accepted.

Task8 complete for tested fictional native password+OTP reference policy:
/root/actual_mfa_review appended spec compliant +qualityApproved. I1 andM1
closed; onlyM2 inheriteddependencydeprecationdebt remains. Finalfixpure41pass
1warning, actualSSO/replay1pass2warnings59.52s; rootfreshpure41pass1warning
2.46s and3finaltesthashesmatch. Production/policy/helpersunchanged byfix.
Cross-task authoritativepatientdata/cookieCSRF/legacy gates retainpriorreview
andunchangedcode; finalwholebranchreviewstillrequired. No institutionalIAM,
enrollment/recovery, measuredproviderchallengeexpiry, fullbrowserE2E or
clinical/legal/production acceptance. Do not redoTask8. Task9 structured
support-cases-brief.md next;16baselinefilesready. ExistingrealclinicalRAG/
memory/vector/jobs/hospital/model paths remain locked/not connected.

Task9 started /root/support_cases_impl, requirements support-cases-brief.md.
16preTask9originals outputs/support-cases-baseline-20261103. Structured
nonclinicalaccountsupport cases +currentRole.SUPPORT queue +functionalUI,
schema7/minimalgrants/preflight/mandatoryaudit/tenant-ownerRLS, noauthority
writes orpatientdataaccess; developmentadmissioncaps20/24h and5nonterminal.
Sites skill read with local-preview reference: existinglocalcheckout only,
skiphosting/registration/sync/publish. No Site/externaldeployment authorized.
Root owns broadverification/reviewpackages/README/readiness; userRabbitMQ and
legacylocks preserved. No sourcechange/testacceptance counted yet.

Task9 partial milestones, NOT acceptance: first actualPG HTTP routeRED then
GREEN1; backend now26actualPG support passes incl roles/MFA combinations,
states, distinct-session admission, post-row-lock role/account/tenant revoke,
take and cancel-vs-resolve races, auditrollback/minimalgrants/FORCERLS/owner
recipe. Additional pre-edit snapshots tests__test_oidc_login.py and
tests__test_identity_sessions.py captured in samebaseline. Frontend initial
RED4 missing-view failures, then4support+58existingPatientApp passes, lintpass;
stale-read/parser/rolling-cap race and finalregressions stillpending. Source
NOT frozen; report/review/broad/rootQA remain pending. Never claimcompletedUI
or production support workflow from intermediate tests.

Task9 controller verification2026-11-03: frozen-collection broad command
`python -m pytest -q -m 'not integration' --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking --basetemp .pytest-tmp-support-root-broad-20261103`
finished1526passed,5skipped,344deselected,2existingwarnings in191.19s,exit0.
Known500-anchor exclusion remains; later fixture-diagnostic-only delta is not
included in this collection. Implementer reported frontend208passed20files,
lint/bothbuilds, newUI98.4%lines/91.66%branches/100%functions; supportclient100%.
Actual-MFA startupsmoke1pass62.53s. FinalPGrerun pending after Docker recovery:
engine was absent, recovered by starting existing per-user Docker Desktop
hidden; server29.8.0 verified. Setup failures are not business-test results.
Inherited provider logout301second boundary failure remains separately open.

Root synthetic-browser UI check used actual dist-patient bundle on owned
loopback8779 server, with explicit fictional-preview banner and no real
backend/identity/medical data. Observed owncreate/detail and queue take/resolve
visible transitions, only identity/support navigation, no health/text/upload
controls. Screenshot outputs/support-cases-visual-proof.jpg; mock script
outputs/support_preview.py. Owned preview tabclosed and serverstopped. This is
visual/interaction evidence only, NOT actualauth/isolation/full-socketE2E or
production acceptance. Independent Task9review/report stillpending.

Task9 implementer finalhandoff docs/security/support-cases-report.md:
recoveredDocker finalactualPG256pass2knownwarnings124.38s; service84/86stmts98%.
ScopedRuffpassed. Approved fixture-start diagnostic-only fix has cleanRED1
andGREEN3privacy; no productionauthoritychange. Root freshfinalsupport28+
privacy3=31passed2knownwarnings28.35s, frontendfocused9passed2files24.38s.
Rootverified26SHA256entries match currentexactfiles. Of18existingbytechanges,
5normalizeidentically (lineendingsonly),13implementationmodified;8newfiles.
Package docs/security/support-cases-review.diff26fullcontextfiles348221bytes;
/root/support_cases_review independent spec+quality nowpending. NoTask9acceptance.
Nextpreparedmaintenance brief provider-logout-test-clock-brief.md addresses
observedinheritedtestclock/diagnostic issue only after Task9gate; notstarted.

Task9 independent reviewer reproduced Important frontend same-view navigation
race in in-memory jsdom without checkoutwrites: while initialsupport own/queue
reads pending, clicking active supportnav increments parentgeneration but leaves
SupportCases mounted/effectdependencies unchanged. Oldcompletiondiscarded and
noreplacementload starts, leaving loadingforever. Root confirmed codepath;
finalreviewreportpending, Task9NOTaccepted. Pre-fix PatientApp/supporttest
snapshots outputs/support-cases-fix-baseline-20261103. FreshTDDfix/re-review
required before nextmaintenance; no weakening of stale-read/revocationbarriers.

Task9 persistedreview support-cases-review-report.md: specissues/qualityNeedsfixes,
ImportantP2sameviewsupportnavstall only, noCritical. MinorP3existingdependency
warningsretained. Cross-taskcannotverify items retain prioridentity/cookieCSRF/
TLSreview plus finalaffectedPG256/actualMFA1/root31/hashverification; notexpanded
to productionapproval. Fresh /root/support_navigation_fix ownsonlyPatientApp+
supportUITest. RED2 reproduced with separatependingown andqueue responses before
productionedit. Narrowguard afterexistingpermission/terminal/pending/activechecks
planned; Task9notaccepted, GREEN/finalfrontend/re-reviewpending.

Task9P2fix sourcefrozen: RED2pendingown/queue then GREEN85focused, fullfrontend
210/20files, UI98.97%lines95.79%branches, lint/bothbuilds passed perfixreport.
Root inspected support-only no-op afterexistingguards+permissioncheck, noother
navigation/backendchanges. Package support-cases-fixes-review.diff2fullcontext
files35956bytes, finalhashesmatchfixreport; original26packageinitialscope kept.
/root/support_cases_review re-reviewpending, rootfreshnew2regressionsrunning.
Do notacceptTask9 beforeboth independentverdicts. MaintenanceTask10notstarted.

Task9 COMPLETE for fictional-data development: independent finalreview appended
specCompliant +qualityApproved; ImportantP2closed, MinorP3dependencydebtretained.
Rootfreshfinal2pendingown/queue regressionspassed7unselected4.84s,2hashesmatch;
coverageartifactread84/86service98%. Priorroot31PG/9UI/broad1526 andimplementer
256PG/actualMFA1/finalfrontend210 remain scoped asabove. No executionclaim
extrapolated to fullsuite/realbrowserauth/production. Cross-taskunverifiables
resolved by retainedpriorreview+affectedauth/startup/CSRFtests+actualMFA/hash
evidence, not institutionalacceptance. README/readiness/schema7examplecomment
updated; legacyremainslocked. NextTask10 provider-logout-test-clock-brief.md;
pre-taskoriginalSHA2566C266E7DC39E50D0C62FFC4F628EDB86C522B6BE45E615F38146840774081DAC
in outputs/provider-logout-test-clock-baseline-20261103. Test-onlymaintenance
stillrequires TDD andindependentreview; broaderengineeringnotcomplete.

Task10 started /root/provider_logout_clock_fix, requirements
provider-logout-test-clock-brief.md. Test-only deterministicclock+negative
diagnostic repair; productionverifier/policyunchanged. Pre-task originalready;
TDD/actualPGmodule/scopedRuff/report/independentreview pending. No completed
medical/hospital/remainingroleworkflows inferred from this maintenance step.

Task10 frozenreport provider-logout-test-clock-report.md: deterministicrollover
repro+privacyRED3fail1pass, pureGREEN14pass; amendedactualPGtarget1pass and
wholeproviderlogoutmodule85pass23.10s,2existingwarnings; scopedRuffpassed.
Onlyexistingaffectedtestfunction+2newtestfiles; productionpolicyunchanged.
Package provider-logout-test-clock-review.diff fullcontext3sources; independent
/root/provider_logout_clock_review pending. Rootfinalpure/hashverification
running. Task10notaccepted. Nextpatient-gateway-brief.md preparedonly, retains
oldHTTPexperiment separately andrequires verifiedTLS->actualapp sockets/PG;
no productiondeployment/accountlimits/full-browserauth inferred; notstarted.

Task10 COMPLETE: provider-logout-test-clock-review-report.md independently
speccompliant+qualityApproved, noCritical/Important. Minor2existingwarnings
retained. Rootfreshpure14passed2warnings3.26s, all3sourcehashesmatch; reviewed
productionverifierunchanged. Earlier85PGmodule/1target actualresults retained;
rootdoesnotrecastreportedrunsasown. Specificinheritedclockfailure closed by
test-onlyfixedepoch+actualassertdiagnosticproof, nottolerancechange. README
updated. No wholebranch/hospital/clinical/legal/productionapproval. NextTask11
patient-gateway-brief.md, verifiedTLSgateway->actualisolatedapp/PGincrement;
accountlimits/fullauthbrowser/infrastructuredeployment remainseparategates.

Task11 started /root/patient_gateway_impl, fullrequirements patient-gateway-brief.md.
Dedicated strictoperatorTOML->nginxTLSrenderer/compose, actualnginxTLS->actualapp
verifiedTLS+disposablePGsocketproof, separateheader/SSEcapabilitytests. Existing
legacyHTTPgateway untouched, no applicationauthweaken/proxyidentitytrust.
Account-levelratepolicy/fullOIDCbrowserE2E/productionnetworkdeploy separate.
TDD/freeze/report/coverage/independentreview pending; rootowns laterbroad/docs.
No Task11acceptance; do notredoTask9/10 completedgates.

Task11 resume2026-11-03T19:25+08:00 after workspace credit exhaustion. The last
implementer integration run was NOT green despite its output directory name:
2 failed/1 passed (gateway readiness and patient_subjects insert). Root did not
restart Tasks1-10; fixed the retained source/fixtures:
1. Production renderer now sets fastcgi/uwsgi/scgi temp paths under the writable
   tmpfs. Read-only rootfs otherwise made nginx exit at startup
   (mkdir /var/cache/nginx/fastcgi_temp: Read-only file system).
2. Fixture patient_subjects insert uses explicit columns and the current
   age_classification/identity_verified fields.
3. Shared fictional certificate helper adds SKI to CA/leaf and AKI to leaf;
   current OpenSSL rejected the leaf with Missing Authority Key Identifier,
   which surfaced as a misleading readiness failure. Test-only change; no
   production cert/validation change.
4. Actual-app socket test resolves Docker Desktop host.docker.internal to IPv4
   through an owned one-shot python container and uses that address; nginx no
   longer alternates to unreachable IPv6. Fixture-issued sessions remain
   explicitly not OIDC login.
5. Rate-limit assertion made deterministic (sequential burst, first 429 body
   checked) instead of a timing-dependent extra request. Added temp-path,
   TLS-version, proxy_next_upstream and no-private-key renderer assertions.

Final retained source verification: `pytest tests/test_patient_gateway.py -q`
with RUN_PATIENT_GATEWAY_INTEGRATION=1: 48 passed, 2 existing dependency
warnings in 37.04s, exit0 (45 pure + 3 integration incl dedicated Compose config).
Pure-only coverage: 97% (3 missed CLI/except lines), 45 passed, 3 deselected,
1 warning in 0.94s. Focused unchanged-app startup/Host/CSRF:
test_security_server + public/stream error safety 73 passed, 14 deselected,
2 warnings in 8.98s. Scoped Ruff clean for the 5 Python files. Actual nginx
HTTPS -> actual uvicorn TLS app + disposable PostgreSQL + real migrations/
grants passed frontend/static, missing-session401, own identity/resource,
foreign-owner404, forged identity, wrong Host400/Origin403, Cookie/CSRF,
audit403 and closed legacy paths. Synthetic TLS upstream passed header
allowlist/body framing/100000 bound/SSE first chunk/per-IP admission and
wrong-CA/wrong-name/unavailable 502 fail-closed. No real IdP/browser login,
production DNS/certs, account-level limits or deployment approval inferred.
Report/operator README/root README/readiness written; independent
specification+quality review pending. Task11 not yet accepted.

Task11 review package frozen: docs/security/patient-gateway-review.diff
(9 files, full context, 60919 bytes; reconstructed pre-Task11 view only for the
three test-certificate key-identifier additions in tests/keycloak_runtime.py).
Independent /root/patient_gateway_review assigned against the brief, report,
current source and hashes; it owns the verdict and
patient-gateway-review-report.md. No acceptance inferred while pending.
Root broad frozen-source regression2026-11-03:
`pytest -q -m 'not integration' --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking --basetemp outputs/task11-broad-20261103`:
1586 passed,5 skipped,347 deselected,2 existing warnings in122.96s,exit0.
Known500-anchor exclusion retained; no unfiltered full-suite claim.

Task11 COMPLETE for fictional-data development: patient-gateway-review-report.md
independently records specification Compliant and quality Approved, no Critical
or Important findings. Reviewer reproduced pure45/97% coverage, full48 passed
with no skips in37.57s, focusedapp73 passed, scoped Ruff clean and all9 source
hashes; Docker resources cleaned, RabbitMQ untouched. Seven Minor follow-ups
(retained, source frozen; no production change): (1) historical RED directories
have no captured stdout, so the RED claim rests on testimony; (2) wrong-Host
socket test asserts400 but not the static gateway body/no-dispatch; (3) auth/
support routes and TLS1.1/limit_conn are config-only, not socket-proven;
(4) report wording says grants were applied "as" the runtime role instead of
"to" it; (5) /patient.html is routed but the app serves HTML only at "/";
(6) config mount paths are not coupled to fixed Compose targets; (7) unused ROOT
in the pure test, start.sh does not exec nginx, Compose test would accept a
read-only Docker-socket mount (actual file has none). These are non-blocking
and must not be silently represented as fixed. No source change after the
reviewed freeze; accepted only for fictional-data development, not production.



2026-11-05 患者入口 ChatGPT 式问答改造（本轮未走独立复审流程）：
登录即会话界面 + 左下账户菜单；可选 `[chat]` 患者鉴权 `POST /chat`，
预检后调用本机公开资料 RAG 答案服务，持久化前二次校验，按 user/assistant
保存 turn 与引用；撤回期间的回答丢弃。迁移 0008 新增 role/citations/ordinal
与 chat.ask 审计 action，授权脚本/启动预检同步。HTTP 冲突真实根因是 Authlib
1.9 内部使用 httpx2，而 `oidc_login.py` 传入 httpx v1 transport 触发断言；
已把 OIDC 及其 mock transport 测试迁移到 httpx2，并钉住 httpx/httpx2/openai
版本。验证：后端非集成 1622 passed/5 skipped；患者实际 PostgreSQL 57 passed；
认证/患者/支持组合 284 passed；前端 215 passed，lint 与两个构建通过；
演示 verify.py 14/14，verify_chat.py 真实回答 1 条引用。仍是虚构演示：
`[chat]` 指向本机 unsafe-local-demo `/answers`，无流式、账号级限流、医院 IdP/
模型审批、临床或生产验收；旧应用保持独立和默认锁定。
