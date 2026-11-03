# Structured account support — approved-plan continuation

Prepared2026-11-03. Execute only after Task8 actual-MFA specification/quality
gate passes. This implements the approved institution-support view, not access
to patient content or a new identity-administration service. Preserve all
existing changes/.env; no commits/staging, original checkout edits, production
owner migrations, external messages, real accounts/hospitals or legacy unlock.
Root owns README/progress/readiness. Fictional-data development only.

## Scope and conservative implementation choice

First support cases are structured category/status records, not free-text
tickets or attachments. This directly enforces the approved default denial of
patient conversation/record/profile access for support staff. Do not add text,
title, contact details, patient IDs, conversation IDs, uploaded files, passwords,
tokens, hospital identifiers or model requests. Users choose a fixed problem
category; support can take a case and record a fixed handling outcome. Actual
account unlocking/password recovery/role or binding edits remain separate
trusted workflows, not implied by handling a case. No external notification.
Because opening a case requires a current session, this is not a recovery
channel for a completely locked-out user. Describe that limitation rather than
invent an institution contact, public account lookup or password-reset path.

An active authenticated account can open/read/cancel its own support requests,
independently of health-purpose consent and verified adult patient eligibility:
these records contain no health information. This includes an account lacking
patient authority. It does not authorize clinical resources, guardianship or
minor health processing. A session/account/institution revocation still blocks
the next request. Review support notices/retention institutionally before real
use; this implementation is not a legal basis or approved notice.

Only current local Role.SUPPORT with Action.SUPPORT_CASE_MANAGE can access its
institution's queue. Use existing is_allowed, including its MFA-required-role
combination rules; do not silently add/remove MFA policy. Org administrators,
auditors, knowledge/integration administrators and platform operators do not
inherit support permission. Multiple roles grant only their explicit scopes.
Never grant support access to patient content, role administration or bindings.

## Data and authority

Add database/migrations/0007_support_cases.sql, owner-run after0001..0006, for
medical_rag.support_cases. Fields: tenant_id, case_id UUID, requester_account_id,
category, status, assigned_account_id nullable, resolution_code nullable,
created_at, updated_at. Composite foreign keys bind requester and assignee to
the same tenant. Use categories login_help/account_help/usage_help; states
open/in_progress/resolved/cancelled; resolution codes guidance_provided/
referred_to_institution/not_reproducible. Database constraints enforce the
state/assignment/resolution combinations. New cases are open/unassigned with
no resolution. Terminal cases remain retained; no hard deletion endpoint.

Use server UUIDs, database timestamps and explicit tenant/owner predicates.
Keep immutable case/tenant/requester/category/created fields immutable after
insertion, runtime column grants minimal, no runtime DELETE/TRUNCATE/authority
table writes/owner/BYPASSRLS. FORCE RLS with transaction-bound support scope
defends owner versus current-institution queue access. Scope is constructed
only from the live AuthorizedTransaction principal and checked operation mode,
never request fields. Document the same trusted-app/arbitrary-SQL boundary as
existing patient RLS. Do not reuse a health patient scope to authorize support.

Add focused src/medical_rag/support_service.py. Reuse SessionStore and shared
mandatory AuditStore, not a detached Principal snapshot. Each operation has an
independent authoritative transaction. After waiting on a case row lock,
refresh current authority then recheck permission/resource/state in fresh SQL.
Owner cancel and staff take/resolve serialize on that row. Operations already
authorized are in flight, not immediately cancelled or push-cleared.

Owner may cancel open/in_progress own cases, even when assigned. Staff take is
open -> in_progress with assignee=current account, exactly one winner. Staff
resolve requires in_progress and assignee=current account -> resolved with a
valid fixed code. No takeover/reassignment, reopening or other user's cancel.
Visible invalid transitions return static409; missing/malformed/foreign cases
use the same404. Queue itself without current support permission returns403.

Bound new case admission: fictional-development cap20 created cases per rolling
24 hours and5 nonterminal cases per account. Cancellation does not reset the
24-hour count. Serialize admission with a transaction advisory lock derived
from tenant+account; query fresh counts after waiting, with bounded/indexed SQL.
Do not obtain serialization by granting UPDATE on identity_accounts. Static429
on exceeded admission, no case creation; audit with existing fixed denial
semantics. These development caps are not an institution-approved production
rate policy or a replacement for gateway/IP limits. Test concurrent admissions
from distinct sessions of one account cannot exceed them.

Extend audit fixed enums/schema checks/action classifier for support create,
self-list/read/cancel, queue-list/take/resolve. Successful mutation and minimal
audit commit together; denial recording reauthenticates after rollback using
existing semantics. Audit outage denies sensitive use. No case IDs, requester
IDs, categories, payloads or paths added to the existing minimal audit event;
use server request UUID and fixed resource/action/outcome. Do not loosen the
current audit constraints or add privileged diagnostic fields.

Update database/patient_runtime_grants.sql and security_server preflight to
require schema7/grants/FORCE RLS and reject overly broad support mutation
privileges. Add owner-recipe smoke on actual PostgreSQL. Do not auto-migrate
production or assign legacy data. Existing fixtures must apply the new schema
and grant recipe consistently without broadening identity authority.

## HTTP contract

Reuse protected create_patient_app login/Origin/CSRF/static errors/no-store and
bounded strict JSON/pagination helpers. Add routes there to reuse its credential
and validation boundary rather than duplicate auth code. Body extra fields and
duplicate JSON keys fail statically. No request can specify account/tenant,
assignee, timestamps, status or resolution outside the dedicated transition.

- POST /support/cases body {category}; return the new own case.
- GET /support/cases bounded limit/offset; own cases only.
- GET /support/cases/{case_id}; own case only.
- DELETE /support/cases/{case_id}; cancel own nonterminal case, not deletion.
- GET /support/queue bounded limit/offset; current institution cases only.
- POST /support/queue/{case_id}/take body {}; take an open case.
- POST /support/queue/{case_id}/resolve body {resolution_code}; assigned only.

Use {success,data,error}. Case representation: id, category, status,
resolution_code, created_at, updated_at. Queue additionally includes necessary
requester_account_id and assigned_account_id (UUIDs only), never provider
issuer/sub, roles, patient linkage/profile or a patient listing. Queue API
provides all information needed to act; no support generic account lookup.
Pagination returns items/limit/offset with deterministic timestamp+UUID order,
limit1..100, offset0..10000. Authentication precedes body/path validation.
All mutations require the existing exact Origin and valid CSRF proof.

Extend /me effective permissions with support_self and support_queue only once
implemented. support_self means account support, not a health permission;
support_queue comes from current server-side is_allowed. Frontend navigation
uses these effective permissions, never upstream role claims or local guesses.

## Functional role interface

Extend dedicated frontend/src/patient interface with a support view, keeping
focused SupportCases component and support.ts parsing/client functions rather
than inflating PatientApp with table logic. Own requests show category selector,
create/list/detail/status/cancel; staff additionally see bounded queue/take/
fixed-resolution actions when support_queue is true. Role label may say
机构客服. Explain that no medical text/attachments are accepted and handling
does not unlock accounts/change roles/bindings. No text/upload field or link
to a patient conversation/profile. Unimplemented admin/knowledge/integration/
platform menus must not appear functional.

Retain PatientApp lifecycle guards: hide/clear on blur, expired identity/403,
logout/withdraw pending/failure; terminal-action barrier cannot be bypassed by
focus/hash/navigation. Invalidate stale reads after create/cancel/take/resolve;
previous queue/detail responses cannot restore stale state or overwrite current
permissions. No localStorage/IndexedDB/sessionStorage, token URLs or HTML
injection. Direct unauthorized support view shows403 and backend still denies.
Keep all existing patient consent/record/auditor behavior and separate builds.

## Verification and handoff

TDD before source changes, integration first. Actual PostgreSQL HTTP tests
cover own versus guessed account/case IDs, tenant mismatch, all roles, low-MFA
role combinations, state/race conflicts, owner cancel versus staff resolution,
post-lock role/account/institution revocation, CSRF/Origin, validation/privacy,
audit rollback/outage, RLS and grants. Tests must demonstrate no patient access
from support. Cover missing/foreign same404 and unsupported identity fields.

Frontend tests exercise functional owner/support views, direct403, stale data
after transition/revocation, failed logout/terminal barrier, blur clearing and
XSS/plain text. New support source coverage>80%, scoped Ruff, frontend tests/
lint/both builds, affected actual-PG/startup/audit regressions. Reuse one minimal
actual-provider MFA/startup smoke for schema/grant wiring; do not rerun every
unrelated expensive provider scenario after each UI edit. Root owns broad
nonintegration run with disclosed known500-anchor exclusion.

Capture pre-task originals, report exact files and source SHA256/freeze in
docs/security/support-cases-report.md with RED/GREEN commands/results and
boundaries. Do not hide dependency/legacy lint or inherited test diagnostic
debt. Independent specification+quality review gates completion; no whole-plan,
hospital integration, clinical/legal or production approval inferred.
