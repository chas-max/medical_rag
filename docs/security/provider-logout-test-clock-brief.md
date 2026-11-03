# Provider logout timing regression — validation repair

2026-11-03. Execute after Task9 support review passes. This is a narrow repair
of an inherited security acceptance test, not a change to authentication policy.
User already requested continued security implementation and verification.
No production code, migrations, permissions, TLS settings or role policy edits.
Preserve all existing changes, .env, original checkout and RabbitMQ. No commits,
staging, real identities, external writes or broad cleanup. Fictional data only.

## Observed defect and root-cause hypothesis

Task9 expanded PostgreSQL run produced336passes and one failure in unchanged
tests/test_provider_logout.py::test_wrong_signature_algorithm_missing_claims_and_time_bounds;
the isolated test also failed. The test samples `now` once, while each call to
`claims()` samples time again, and the verifier/JWT library use advancing clocks.
Consequently a token intended to have301second lifetime may actually have300
seconds after a clock rollover, and a future-issued token can become current.
The assertion also directly evaluates a call containing a signed JWT, so pytest
can render that token on failure. Do not copy credential-bearing diagnostics.

## Required deliverable and allowed structure

Modify only the affected test and add focused test-only helper/regression code
if needed. Keep existing tests/test_provider_logout.py helper signatures used
by other modules unchanged. Root captures its pre-task original. New helper
may be tests/provider_logout_clock.py with a dedicated test module if a helper
is genuinely needed; do not introduce a general time abstraction or dependency.

First reproduce the independent-time-sample error deterministically with
synthetic values/controlled clocks, using a boolean/static failure diagnostic.
Show that301seconds relative to the earlier caller sample can become300seconds
relative to the later payload iat. Then repair the test's time domain, not the
verifier: use one explicit epoch for payloads and verifier/JWT time within the
test. Scope clock changes to the specific test, restore with monkeypatch, and
do not patch global time.time (fixture readiness, asyncio, database and other
threads must not be affected). PyJWT's UTC clock and provider_logout.time must
agree. The epoch may be sampled once from current real time so real database
replay storage remains meaningful; all assertions must use that fixed epoch.

Exercise exact acceptance/rejection boundaries, not a wider safety margin:

- current iat with positive expiration and expiration=iat+300 is accepted;
- exp=iat+301 is rejected;
- iat=now+1 is rejected;
- iat=now-120 with still-valid expiration is accepted;
- iat=now-121 is rejected;
- exp=now and exp<now are rejected.

Keep wrong RSA signature, HS256 substitution and every required-claim deletion
rejected through the actual HTTP endpoint with mandatory-audit behavior. Use
fresh jti values or otherwise avoid replay conflicts changing which boundary
is tested. Accepted boundary cases may revoke the fictional account's sessions;
do not weaken verification to work around this. Provider logout itself does
not require a browser session.

In the amended test, retrieve inherited credential-bearing system fixture via
request.getfixturevalue and private local handling rather than exposing its
tuple as a parametrized test argument. Assertions must operate on precomputed
boolean locals and static messages. Do not embed post(), signed(), JWT values,
private dictionaries or connection tuples inside assert/pytest.fail. Preserve
debuggable static case labels without outputting tokens or claims. Add a
focused synthetic pytest assertion-rewrite failure check proving amended
assertion failures cannot expose a signed-token canary. Do not broaden this to
rewriting every inherited test in the repository.

## Verification and handoff

Follow TDD. Record RED command/result for the deterministic mismatch or privacy
regression before changing the affected test, then GREEN. No sleeps, repeated
high-count probability loops or xfail/skip of the original test. Do not suppress
known dependency warnings or change tolerances/policy to obtain green results.

Run the focused pure clock/privacy tests; run the amended actual-PG test once,
then the full tests/test_provider_logout.py module once with
RUN_POSTGRES_INTEGRATION=1, PythonUTF8=1 and explicit ignored workspace basetemp.
Use --tb=no on inherited module failures to avoid credential diagnostics; a
failure remains failure. Scoped Ruff must pass. Do not rerun actual Keycloak
or frontend suites because this deliverable changes no corresponding source.

Write docs/security/provider-logout-test-clock-report.md with exact changed
paths/SHA256, RED/GREEN commands/results, evidence for each boundary, and any
concerns. Freeze source before handoff. Independent specification + quality
review is required. No whole-branch, hospital, clinical/legal or production
approval is implied. Root owns review package, progress and readiness updates.
