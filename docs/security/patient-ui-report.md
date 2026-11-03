# Patient portal UI implementation evidence — 2026-11-02

## Scope and boundaries

Dedicated same-origin React entry only: `frontend/patient.html`,
`vite.patient.config.ts`, `src/patient/{main.tsx,PatientApp.tsx,client.ts,model.ts,patient.css}`.
Minimal existing metadata edits: package `build:patient` script, tsconfig config
inclusion, ESLint/generated-directory ignore, root `.gitignore` dedicated output.
No legacy App/router/API/model/worker implementation changed by this task.
Sites skill was used in its explicit local-only mode: no cloud registration,
publication, source synchronization or commits. No imagery or dependencies added.

The portal is fictional-data development, not medical AI: it records plain text,
does not run a model, and does not attest hospital identity or legal/clinical
acceptance. Staff have only self-identity; all navigation uses live server
permissions. Unknown/minor/guardian/unverified paths stay closed. Notice content
comes from the institution's API, never a bundled legal notice/default consent.

## RED/GREEN

1. Wrote DOM/transport tests before behavior. Initial absent imports were
   corrected to minimal empty component/throwing transport scaffolds. Executed
   `npm test -- --run src/test/patient-client.test.ts src/test/patient-app.test.tsx`:
   the six client tests failed with `Not implemented`; component tests failed
   because required login/identity/content views did not exist. Implemented the
   tested behavior, then focused GREEN: 18 passed.
2. Controller identified mutation-selection lifecycle and paginated ordering
   risks. Added a regression before fixes. Executed
   `npm test -- --run src/test/patient-app.test.tsx`: 1 failed, 12 passed,
   expected conversation selection to be disabled during mutation. Fixed busy
   navigation/selection, duplicate mutation guard, and server-page reloads after
   creation/append instead of client-side page mixing. GREEN: 13 passed.
3. Added malformed payload, successful withdrawal, visibility/late-result and
   safe retry coverage. Focused final GREEN: 27 passed (21 DOM + 6 transport).

All these frontend tests use mocked HTTP responses in jsdom. They test the real
components and transport boundary but **are not real browser/HTTP/PG evidence**.
Backend/actual TLS evidence is in `patient-startup-report.md` and the earlier
patient vertical report.

## Commands and observed results

Executed from `frontend`:

- `npm test`: 17 files, **144 passed** (existing baseline 117 + new 27).
- `npm run lint`: exit 0 (TypeScript + ESLint).
- `npm run build`: exit 0; legacy build remains separate.
- `npm run build:patient`: exit 0; dedicated `dist-patient/patient.html` and
  hashed self-hosted JS/CSS, no legacy API proxy.
- `npm run test:coverage -- --run src/test/patient-client.test.ts src/test/patient-app.test.tsx --coverage.include=src/patient/**`:
  27 passed; statements/lines **96.67%**, branches **92.59%**, functions
  **89.18%**. `client.ts`: 100% all; `model.ts`: 100% statements/lines/functions,
  97.05% branches; `PatientApp.tsx`: 97.88% lines, 90.76% branches, 89.28%
  functions. Bootstrap `main.tsx` is deliberately not imported in DOM tests
  (0% bootstrap coverage, included in the aggregate).

## Sensitive-state lifecycle

CSRF exists only in a component ref. No storage APIs, service workers,
analytics, console logging, unsafe HTML or Markdown rendering. Requests always
use same-origin cookies and no-store; no ownership fields are sent. Login is
a same-origin native HTML POST, not a JS fetch to the IdP.

Generation invalidation discards late completions after logout, withdrawal,
blur/hide, failures and navigation. Clearing removes identity, CSRF, notice,
title/content drafts, selected IDs, lists and turns before subsequent network
updates. Withdrawal clears before waiting; failures never restore old data.
401/403 and authority refresh errors fail closed. Returning to the page rechecks
session then `/me`; it does not claim instantaneous idle push revocation.
Mutation buttons disable duplicates; list/turn mutations reload a real server
page at offset zero to avoid inserting records into unrelated paginated pages.

## Browser verification and remaining gates

Controller opened the actual built anonymous/error landing in IAB through a
local static preview. Layout and literal fictional/nonproduction disclaimers
were visible; unavailable status was expected because the static preview has
no authentication backend. This is **only anonymous/error landing visual QA**,
not a real IdP login, functional browser flow, or responsive-size test.

Controller's two-origin native-form A/B browser check showed Chromium blocks
the fixed IdP303 redirect with only `form-action 'self'`. Controller approved
the necessary correction: allow self plus only the trusted configured HTTPS
authorization origin. Script/style/connect remain self-only; arbitrary request
headers cannot change that source. This is a local browser mechanics check,
not real institutional IdP interoperability evidence.

Audit transport, retention/key-rotation/back-channel revocation code and future
guardian/business interfaces are remaining engineering work outside this slice.
Real IdP registration, institution-approved notices/MFA/guardian rules, actual
hospital protocols and independent clinical/legal/production acceptance require
external institutional input. Do not infer these from mocked form tests.

## Task3 review fixes — authoritative amended verification, 2026-11-02

Resumed the interrupted uncommitted changes; preserved unrelated work and the
existing terminal-action barrier, mutation-generation invalidation and tests.
The barrier is separate from ordinary view generations: focus/hash/navigation
cannot revalidate while logout/withdrawal is pending, failures require explicit
user revalidation, and completion is retained while the window is inactive.
Successful withdrawal rechecks session and `/me` and opens consent status only.
Added actual focus tracking: visibility alone cannot unlock an unfocused window,
and an initially unfocused window starts locked. Initial/list/turn/pagination
responses from before a mutation cannot replace the post-mutation server page.

RED evidence from this resumed run (commands executed from `frontend`):

- `npm test -- --run src/test/patient-client.test.ts src/test/patient-app.test.tsx`:
  **2 failed, 37 passed**. Existing interrupted focus regressions observed
  unwanted `/auth/session` and `/me` calls on visibility-only/initial-unfocused
  paths. This was before the focus-state implementation.
- To verify the already-partial fixes, temporarily disabled only the terminal
  refresh barrier and mutation generation increment with `apply_patch`, then
  ran `npm test -- --run src/test/patient-app.test.tsx -t 'survives focus|old .*cannot overwrite'`:
  **6 failed, 27 skipped**. Pending logout/withdrawal triggered extra requests;
  all four initial/page list/turn cases replaced the newly saved page with
  the old response. Both original guards were restored before GREEN.
- Extended terminal failures to both actions, active/inactive completion, and
  401/403/503/network/malformed-JSON outcomes. The inactive-return tests now
  assert visibility alone stays locked after blur and an actual focus event
  is required; no assertion allowing visibility-only unlocking was retained.

Final commands/results against the amended source and tests:

- `npm test -- --run src/test/patient-client.test.ts src/test/patient-app.test.tsx`:
  **57 passed** (51 DOM lifecycle tests + 6 transport tests).
- `npm test`: **174 passed, 17 files** (117 existing + 57 patient).
- `npm run lint`: exit0.
- `npm run build`: exit0; `npm run build:patient`: exit0.
- `npm run test:coverage -- --run src/test/patient-client.test.ts src/test/patient-app.test.tsx --coverage.include=src/patient/**`:
  **57 passed**; statements/lines **96.92%**, branches **92.41%**, functions
  **95%**. `PatientApp.tsx`: lines98.07%, branches90.9%, functions96.77%;
  `client.ts`:100% all. Aggregate includes unimported bootstrap `main.tsx` at0%.

These frontend regressions remain mocked-fetch/jsdom evidence, not an actual
browser or real IdP functional flow. No new browser acceptance is claimed.
No legacy source, storage behavior, Origin/CSRF controls or identity trust was
changed by these review fixes; independent re-review remains the next gate.
