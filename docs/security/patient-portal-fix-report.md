# Task3 patient portal review fixes — 2026-11-02

Status: all three findings implemented and verified; ready for independent
re-review. No production, clinical, legal or real-IdP acceptance is claimed.

## Scope and self-review

Read `patient-portal-review-findings.md` as the authoritative requirements,
plus both UI/startup briefs and reports and the vertical global constraints.
Resumed and preserved the previous implementer's uncommitted partial fixes.
Only these implementation/test paths are in the focused fix package:

- `frontend/src/patient/PatientApp.tsx`
- `frontend/src/test/patient-app.test.tsx`
- `src/medical_rag/oidc_login.py`
- `tests/test_security_server.py`

Appended evidence to the existing `patient-ui-report.md` and
`patient-startup-report.md`. Full current versions of the four focused files
are packaged in `patient-portal-fixes-review.diff` for independent review.
No commits/staging, `.env` reads/edits, legacy unlocking, dependency changes,
or unrelated source edits were performed by this resumed fix task.

1. Terminal actions now retain a barrier independent of view generations.
   Blur/hide clears all sensitive state; focus/hash/navigation cannot revalidate
   pending actions. Completion/failure is retained while inactive. Logout
   success remains login-only; withdrawal success revalidates `/auth/session`
   then `/me` and reloads consent, not conversations. Terminal failures remain
   closed under automatic events; an explicit retry/revalidation is required
   (401 offers institution login only). Added focus tracking so visibility alone
   cannot unlock a blurred or initially unfocused window.
2. Mutation start invalidates conflicting initial/paginated reads before its
   canonical server-page reload. Delayed list/turn responses cannot overwrite
   post-create/append data. Four tests cover initial/page x list/turn ordering.
3. Scoped IPv6 is rejected by trusted authorization-origin serialization,
   including a semicolon-bearing scope and an ordinary interface scope. The
   headless CSP remains unchanged; Origin/CSRF and trust boundaries are intact.

Self-review traced terminal completion independently of `perform`/generation,
automatic versus explicit revalidation, inactive visibility/focus ordering,
read/mutation invalidation and source serialization. Sensitive state stays
component-memory-only; no unsafe HTML/storage/console paths were introduced.

## RED/GREEN and final checks

Exact commands and detailed evidence are appended to the respective reports.
Observed RED before final implementation/restoration:

- Initial focused run: **2 failed, 37 passed**, exposing unfocused-window reads.
- Narrowly disabled the partial terminal refresh guard + mutation generation:
  **6 failed, 27 skipped**; extra pending-terminal reads and four stale pages.
- Narrowly disabled scoped-IPv6 guard: **2 failed, 4 passed, 35 deselected**;
  both scoped hosts incorrectly allowed app creation. Restored all guards.

Final amended frontend checks, from `frontend`:

- `npm test -- --run src/test/patient-client.test.ts src/test/patient-app.test.tsx`:
  **57 passed** (51 DOM, 6 client).
- `npm test`: **174 passed, 17 files**.
- `npm run lint`, `npm run build`, `npm run build:patient`: **exit0 each**.
- `npm run test:coverage -- --run src/test/patient-client.test.ts src/test/patient-app.test.tsx --coverage.include=src/patient/**`:
  **57 passed**; statements/lines96.92%, branches92.41%, functions95%.
  PatientApp lines98.07%, client100% all; bootstrap main remains unimported.

Final backend command, from repository root:

`$env:PYTHONUTF8='1'; $env:RUN_POSTGRES_INTEGRATION='1'; .venv/Scripts/python.exe -m pytest tests/test_security_server.py tests/test_oidc_login.py -q --basetemp=D:/temp-openclaw/patient-portal-fixed-pg --tb=short`

Result: **91 passed, 2 existing deprecation warnings in22.39s**, no skips/errors.
An earlier attempt had42passed/49setup-errors because Docker was stopped;
controller recovered the daemon before this successful real-PG rerun. No
mocked infrastructure or skipped integration checks are counted as PG evidence.

`.venv/Scripts/python.exe -m ruff check src/medical_rag/oidc_login.py tests/test_security_server.py`:
**All checks passed**. `git diff --check`: exit0 (existing CRLF notices only).

## Limits and next gate

Frontend is mocked-fetch/jsdom evidence, not real browser lifecycle or actual
institutional IdP interoperability. Backend tests do use disposable PostgreSQL
and actual direct TLS; they do not establish deployment or hospital acceptance.
No unresolved implementation/resource blocker remains in this focused scope.
Independent re-review must precede the next audit/workflow task. Existing
institutional, browser, audit transport, retention, credential rotation and
back-channel revocation limits remain as documented in the prior reports.
