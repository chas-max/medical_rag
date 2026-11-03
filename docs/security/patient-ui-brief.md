# Task 3: Usable same-origin patient interface

Read patient-vertical-brief.md Global constraints as binding requirements.
This task follows the live patient API implementation and review. It adds a
separate React/Vite entry; never alter or reuse the legacy chat App/router,
global settings, upload or memory API. Do not publish to cloud Sites.

## API contract

Every JSON response is {success,data,error}.

- GET /auth/session => {user_id,tenant_id,roles,csrf_token}.
- POST /auth/login =>303 fixed IdP. Submit same-origin HTML form (browser
  generates Origin); no JS cross-origin fetch following this redirect.
- POST /auth/logout requires X-CSRF-Token => null.
- GET /me => {user_id,tenant_id,roles,binding:{verified:boolean},permissions:
  {identity:true,health_consent:boolean,conversations:boolean}}.
- GET/POST/DELETE /me/consents/health => {notice:{version,text,
  validity_seconds},granted:boolean,expires_at:ISO|null}. POST exact
  {version,accepted:true}; DELETE records withdrawal, not deletion.
- GET /conversations?limit=20&offset=0 => {items:[{id,title,created_at}],
  limit,offset}; POST {title} => one such item.
- GET /conversations/{id} => one such item.
- GET /conversations/{id}/turns?limit=20&offset=0 => {items:
  [{id,content,created_at}],limit,offset}; POST {content} => one item.

The server fixes all ownership. Do not send patient/user/tenant/role fields.
Treat all server prose and stored content as plain text, never HTML/Markdown.

## Files and build

Create frontend/patient.html, frontend/vite.patient.config.ts and
frontend/src/patient/{main.tsx,PatientApp.tsx,client.ts,patient.css}. Split
consent/conversation views into focused components if needed. Add tests in
frontend/src/test/patient-*.test.tsx or .ts. Use current React19/Vite7/Vitest3
dependencies, no new dependency required. Add build:patient script to package,
with a dedicated dist-patient output; package.json keeps all existing scripts.
Update ESLint ignores for dist-patient and tsconfig to include new config.
No legacy API proxy in patient config, no insecure development-auth shortcut.

## Behavior and UX

Build a clean, responsive Chinese institutional portal, restrained teal/navy,
readable sans-serif, accessible labels, focus styles, status/alert regions.
Header clearly labels fictitious-data development and no diagnosis/model
generation. No imagery needed. Use external CSS; no inline styles/scripts,
unsafe-inline/eval or third-party fonts/scripts.

Initial loading is fail closed. If auth/session401 show institution login
form only; network503 shows static safe unavailable status and retry. Do not
use raw backend/exception messages as display text. Load /me after session.
Use server permissions for navigation. Health-consent navigation only if
permission true; conversations only if live permission true. An explicitly
requested forbidden view shows403 and never fetches its content.

Identity view shows own account/institution ID and translated role labels.
For staff roles, explain that management identity gives no patient-content
right and their business modules remain unavailable; do not invent functional
admin/support pages or advertise unimplemented endpoints. For unverified,
minor, guardian or unknown-age paths, state institution verification/rules
are required and show no patient content. Do not mislabel role claims as
hospital identity proof.

Consent view displays actual institutional notice/version in full, unchecked
explicit acknowledgment before enabling grant, and a separate withdraw action
when granted. Explain hospital records and external-model processing are not
included; withdrawal stops future purpose use but is not historical deletion.
After grant reload /me; after withdrawal immediately discard conversation and
turn data, switch to consent status and reload /me. Never auto-grant on login.

Conversations view offers create title, bounded paginated list, select and
view paginated turns, append plain-text record. Call it conversation records,
not functioning medical AI chat: no fabricated assistant responses or typing
indicators. No direct pipeline/model call. Disable duplicate submissions.
Keep ownership IDs out of displayed URLs/history. Do not persist tokens,
identities, notice decisions or patient content in localStorage/sessionStorage,
IndexedDB, service workers, analytics or console logs. CSRF only in component
memory; every fetch uses credentials:same-origin and cache:no-store.

On logout,401,403, expired/invalid session or authority refresh failure,
synchronously clear all patient content, draft titles/text, selected IDs and
CSRF before subsequent UI updates. Withdraw clears before waiting for network;
failure does not restore previously held content. Prevent late asynchronous
responses from repopulating cleared state: request generation token or abort
controllers. Tab blur/hide clears sensitive state; on refocus reauthenticate
and reload permissions before fetching data. Do not claim instantaneous
push revocation while idle: show refresh-on-return behavior explicitly.

## TDD and checks

Write tests first; run focused RED then GREEN. Use mocked fetch only for DOM
tests (real HTTP+PG belongs to backend slice); reports must distinguish this.
Test login, staff no patient fetch, missing patient access, explicit notice
checkbox, exact version mutation+CSRF, successful list/create/append,
withdraw/logout/401/403 clearing, delayed old response after logout denied,
tab-hide clear/refocus recheck, no unsafe storage/HTML, forbidden view403,
static error text and duplicate mutation disabled. Test client rejects
malformed envelopes/non-JSON/network failures without echoing server data.
Run npm test (baseline117), npm run lint, npm run build, npm run build:patient;
focus >80% coverage of new client and components.

Write report to docs/security/patient-ui-report.md with RED/GREEN, exact
commands/results, changed files, browser verification limits and concerns.
No commits/staging. Keep controller-owned launcher/README/auth CSP untouched;
controller will mount generated patient.html and /assets with self-only
script/style/connect CSP; form-action additionally allows only the fixed
trusted IdP origin for the required login redirect.
