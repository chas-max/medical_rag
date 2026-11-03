# Task3 review — fixes required

Independent reviewer /root/patient_portal_review: spec and quality need fixes.
No Critical; one Important and two Minor. Actual read-only in-memory DOM/code
probes, not merely report assertions; no original source mutation by reviewer.

1. Important: pending logout/withdraw can be superseded by focus/hash refresh.
   PatientApp.tsx129-136,203-224. Delay logout, blur/focus, reload still-valid
   session/me and conversation; successful logout callback becomes stale and
   is discarded, leaving private records visible. Terminal requests need a
   barrier independent of view generations. No automatic revalidation or
   navigation may restore content while terminal action is unresolved. Always
   handle terminal completion/failure across blur/hide/hash/focus; keep inactive
   windows cleared. Failed terminal action must not silently restore content
   through automatic focus/hash; require explicit user revalidation.
2. Minor: initial/page reads share generation with mutations. Delay initial
   turns GET, append and reload new saved page, then resolve old GET: stale page
   overwrites updated data. Sequence/invalidate conflicting reads or prohibit
   them; test initial and paginated response ordering. No DB loss or cross-user
   leak demonstrated, but old reads must not replace post-mutation results.
3. Minor: _authorization_origin stringifies scoped IPv6 without rejecting the
   scope identifier. Example https://[::1%25;upgrade-insecure-requests;]/authorize
   emits semicolons in CSP source. Reject scoped IPv6 and test; this is a strict
   trusted-config/CSP syntax gap, not a demonstrated unauthenticated injection.

Fix all with RED/GREEN, covering frontend patient-app/client tests plus lint,
both builds and focused coverage. Cover CSP fix with tests/test_security_server.py
and tests/test_oidc_login.py; run exact amended tests before re-review. Append
evidence and limitations to existing reports. Do not change legacy behavior,
weaken Origin/CSRF or broaden identity trust. No commits or staging.
