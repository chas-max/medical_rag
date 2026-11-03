# Production gate — NOT APPROVED

This repository must not process real patient information. A passing code
test suite is not hospital identity proofing or production acceptance.

## Implemented evidence and remaining work

| Area | Evidence in this worktree | Remaining gate |
| --- | --- | --- |
| Browser identity | OIDC code+S256 PKCE, encrypted one-use login, opaque sessions; signed provider logout/replay/pending-login fences; actual pinned Keycloak HTTPS/private-CA interoperability and routine operator-approved signing-key overlap/retirement; native password+OTP/SSO reference policy with independent task review | Real institutional registration/proofing and approved MFA enrollment/recovery/device policy; production IdP operations, client-secret/login-encryption-key lifecycle, compromise response and pending-account reactivation behavior; no idle-page push or in-flight cancellation |
| Local authority | Current account/institution/roles/verified binding, session versions; live recheck after patient-lock waits | Reviewed provisioning/invitation and identity-proofing workflows, administrative audit and least-privilege deployment |
| Patient resources | New isolated JSON conversation/turn service, primary PostgreSQL transaction scope and FORCE RLS | RAG generation, private memory, file/vector/reference/job isolation and migration of confirmed legacy ownership |
| Purpose consent | Versioned health-information notice, explicit grant/withdrawal, append-only event history; mandatory same-transaction operation audit | Institution-approved actual notice, hospital/external-model purposes, retention/erasure process, protected external audit transport |
| Minors/guardians | New API denies all minor/unknown/guardian paths | Authoritative verified relationship source, reviewed rules, expiry/revocation integration and role UI |
| Interfaces | Dedicated patient identity/consent/record interface, current-MFA institution auditor metadata API/UI, structured own-account support/current-role institution queue API/UI and direct-TLS startup; task-level verification/review recorded in reports | Functional institution-member, knowledge, integration and separate platform control-plane workflows; approved support notice/retention and separate inaccessible-account recovery |
| Operation audit | Minimal constrained events, append-only application-role separation, institution FORCE RLS, same-transaction success/independent denial recording; failures deny sensitive use; auditor API/UI reviewed | Independently protected transport/checkpoints, retention/archival, alerting, capacity controls and operational privilege review; not database-owner-proof |
| Hospitals/models | No real hospital binding or record reader exposed; no inference enabled by new API | Institution's formal identity mapping, protocol, credentials, network/field scope, approved suppliers and revocation behavior |
| Gateway/deployment | Dedicated verified-TLS gateway with strict bounded TOML, fixed nginx renderer/CLI, loopback-only published port, separate edge/internal-backend networks, exact Host, request-header allowlist, upstream chain/name verification, TLS1.2/1.3 and development per-IP admission; actual nginx HTTPS to actual isolated app over disposable PostgreSQL plus a separate synthetic transport suite and Compose config check; operator README written; independent task review Compliant/Approved with seven non-blocking Minor follow-ups | Institution production ingress/network policy, account/tenant quotas and approved rate policy, certificate issuance/rotation and revocation response, backup/monitoring/load/incident controls, independently deployed app container topology and full authenticated-browser acceptance |
| Acceptance | Disposable PostgreSQL, signed test-provider responses, actual fictional-account Keycloak protocol/TLS/logout, support isolation/admission/transition races and lock-wait tests; provider uses real sockets, app uses TestClient; separately labeled mock-only browser UI interactions | Real authenticated browser/full-socket/institutional integration, independent penetration review, clinical evaluation, privacy/legal review, restoration drills and responsible institutional sign-off |

Task-level reports contain exact commands and scope. They do not authorize
production. Existing single-user SQLite/Milvus/queue data is not silently
assigned to any new account or institution. The old application and workers
remain locked by default.

## External inputs needed for genuine institutional integration

1. Institution/deployment identity and data owner; approved infrastructure and
   authoritative primary PostgreSQL/secret-management configuration.
2. Trusted IdP issuer, client registration, exact HTTPS callback, keys/rotation
   and institution-approved authentication-strength/MFA mapping.
3. Hospital's official API specification, sandbox, patient identity-proofing
   and mapping protocol, verified guardian relationship source, allowed fields
   and authorization/withdrawal behavior. Never substitute a test binding.
4. Reviewed notice texts/versions, processing purposes, model data-flow approval,
   retention, deletion and support boundaries.
5. Named acceptance owners and independent clinical/security/privacy/legal
   evidence. Do not mark a gate passed because the corresponding team is
   unavailable or because a template/checklist exists.

Repository-only work above remains repository work, not an external blocker;
external inputs only block the specific real-system/acceptance step they own.
