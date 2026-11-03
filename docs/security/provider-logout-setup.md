# Owner-run provider logout setup (fictional development only)

This is an operator checklist, not an automatic migration or approval to
register a real provider. Keep the legacy API/workers locked. Do not put any
provider token, secret, medical content or callback query in access/body logs.

1. Using the independently approved owner connection, apply migrations0001
   through0006 in order. An existing migration5 deployment needs
   `database/migrations/0006_provider_logout.sql`. Review and back up the
   database first; the application does not migrate, create roles or backfill
   unverified provider correlation. Do not change `.env` for this workflow.
2. Review the dedicated runtime role and inherited/PUBLIC permissions, then
   run the updated owner recipe with an explicitly quoted role variable:
   `psql [approved owner connection options] -v runtime_role=patient_runtime -f database/patient_runtime_grants.sql`.
   This recipe grants required columns but does not revoke pre-existing broad
   authority. Runtime must not own tables/schema, bypass RLS, or mutate/truncate
   provider events. Event UUID/receipt time are database-generated, not runtime
   insertable columns. The new session columns are `authenticated_at` and
   `provider_sid_hash`; the latter is SHA256 of UTF-8 sid, never the raw sid.
3. Start only the dedicated trusted patient factory with the institution's
   current configuration. Preflight requires schema6, the new column grants,
   event SELECT/INSERT, forced institution RLS and denied mutation privileges.
   The provider endpoint cannot enable authority on experimental headless auth
   without mandatory audit. No secrets or schema errors are printed publicly.
4. A separately authorized IdP registration must use exactly the configured
   app HTTPS origin plus `/auth/backchannel-logout`. Direct TLS and exact Host
   remain required; forwarded user/tenant headers are not trusted. The IdP
   must support the strict profile in `provider-logout-brief.md`: pinned RSA
   RS256, exact issuer/client string, `typ=logout+jwt`, bounded fresh integer
   timestamps, required expiry/jti/events, no nonce, and sub and/or sid. This is
   not universal OIDC interoperability. No dynamic key lookup is performed;
   key rotation and any broader provider profile are separate reviewed tasks.
5. Send exactly one URL-encoded `logout_token`, no query parameters, with the
   specified bounds/UTF-8. Cookies and CSRF are neither needed nor read or
   changed. Origin may be absent, or exactly the trusted app origin once. This
   exception applies only to this exact POST route. Other auth/patient browser
   mutations keep their existing Origin/CSRF protections.
6. Treat200/null as accepted without target disclosure.400 is a static invalid
   request/token/conflicting replay.503 is a static storage/audit failure:
   revocation may already have committed. Retry the identical signed event
   within its supported validity window. An identical committed jti is a no-op
   for revocation but still requires a new successful audit before200. A
   newly authenticated session must have verified auth_time strictly later
   than the fence; equal-second authentication is intentionally denied.

Provider fences are append-only and are retained after JWT expiry until a
reviewed retention policy exists. Existing sessions have NULL auth time/sid
until replaced; NULL auth time counts as older, but an absent historical sid
cannot be reconstructed. Subject-targeted logout can revoke these older
sessions; sid-only logout can match only a stored verified sid hash. Signed
logout applies even if local account/institution authority is disabled.

The next protected request after revocation fails authentication. A webhook
does not push-clear an idle page or cancel an already-authorized operation.
No patient binding, consent, patient-data access or clinical/legal/production
acceptance is created by logout or reauthentication. Complete independent
specification and quality review before accepting this task.
