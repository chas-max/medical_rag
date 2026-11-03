# Routine institutional signing-key overlap

This is a manual operator-approved trust set for the isolated patient app.
It is not automatic discovery, incident response, or production acceptance.
Only independently approved RSA public keys (at least 2048 bits) are accepted;
the app only verifies RS256. No private signing keys belong in this app.

1. Independently authenticate the provider's new public material and its exact
   `kid` using institution-approved channels. Compare independently obtained
   public-key fingerprints (for example SHA-256 over DER SubjectPublicKeyInfo),
   record approval evidence, and check old/new material is distinct. Never use
   a JWT header URL or unverified token as the trust source.
2. Replace the single `public_key_file` setting with `verification_key_files`,
   mapping the exact old and new `kid` to public PEM files. Exactly one mode is
   allowed; a ring contains 1..8 names matching `[A-Za-z0-9_-]{1,128}`. Paths
   resolve relative to the trusted TOML. Keep optional `provider_ca_file`
   independently approved; TLS trust and signing trust are separate.
3. Deploy/reload the overlap set to **every** ID-token and backchannel-logout
   verifier instance before switching provider signing. Inventory all versions
   and drain/reload stragglers. Existing processes never observe file/mapping
   updates automatically. Validate old/new configuration fingerprints without
   logging PEMs, credentials, ID/logout tokens, claims, or callback queries.
4. Switch provider active signing through its institution-approved operator
   process. Validate real authorization-code/S256 login and real signed
   backchannel logout with both approved keys, including fresh login after
   logout and safe replay. Preserve issuer, audience, nonce, age, registered
   time, authority and pending-login fences. Unknown/missing `kid` fails shut.
5. Retain overlap for institution-approved upstream token lifetime, pending
   flow, delivery/retry, clock and rollout windows. There is no universal safe
   retirement interval. Confirm no provider path still signs with the retired
   key and all instances support the current key before approving removal.
6. Deploy/reload removal to every verifier. Prove a still-valid retired-key
   token with correct audience/nonce first passes the overlap verifier, then
   fails the new verifier; verify current-key HTTP login/logout still works.
   This direct verifier check is not a public bearer-token endpoint. Verify
   unknown/mislabeled keys fail without fallback. Record per-instance rollout
   and approvals; make no automatic production edits from this runbook.

Removal prevents future ID/logout verification under the retired key; it does
not revoke already-issued opaque local sessions. Emergency key compromise
needs separate approved trust removal **and** trusted institution/session-version
revocation plus independent incident handling. Routine overlap alone is not
an adequate compromise response. Client-secret rotation and login-encryption-key
rotation are separate procedures. These fictional fixtures do not establish
real institution/MFA certification, clinical safety or legal acceptance.

Protocol/provider references:
[OIDC signing](https://openid.net/specs/openid-connect-core-1_0.html#Signing),
[OIDC rotation](https://openid.net/specs/openid-connect-core-1_0.html#RotateSigKeys),
[Keycloak realm keys](https://www.keycloak.org/docs/latest/server_admin/#_realm_keys).
