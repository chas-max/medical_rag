# Reference staff OTP authentication policy (Keycloak 26.8.1)

`keycloak-mfa-policy.json` is a **fragment**, not an enabled realm or a deployment
command. It contains no institution, client, callback, account, password, OTP
seed, recovery code or secret. Do not import it into an existing/real realm
without the institution's IAM/security review and a separate authorized rollout.
The actual disposable-provider tests compose this exact file, not a parallel
test-only policy, into a uniquely owned fictional realm.

The native browser flow has a cookie/interactive structure, level1 conditional
LoA with required username/password, and a conditional staff level2 subflow.
Its role condition, LoA condition and OTP form are all REQUIRED. Realm role
`medical-staff-mfa` selects staff; it is an IdP policy classification, **not** an
application role. A configured native OTP is not itself the condition for entry:
unenrolled staff cannot silently take a password-only shortcut. Keycloak's
enrollment page is not an authenticated application session, and the application
does not handle enrollment or see OTP secrets. Level2 has `loa-max-age=0` so SSO
must challenge for OTP again on subsequent level2 authentication.

`urn:medical-rag:reference:otp` maps to LoA2. The application requests its trusted
accepted ACR values nonessentially, but independently verifies the signed token,
issuer/audience/nonce/authentication age and accepted ACR. Merely requesting an
ACR is never proof of MFA. Patients without staff classification can obtain
level1 identity, never accepted level2 assurance. A locally assigned auditor
without staff classification stays low-assurance and cannot use `/audit`.
Conversely provider classification plus actual MFA without the current local
auditor role cannot grant `/audit`. This does not change MFA-required local
roles (`org_admin`, `integration_admin`, `auditor`, `platform_operator`) or
silently change `knowledge_admin`/`support` policy. Staff MFA does not grant
patient consent/content absent the existing verified adult self-patient scope.

An institution must separately review identity proofing, role classification,
enrollment authorization, recovery and lost-device handling, revocation,
monitoring, key rotation, session lifetimes and approved assurance semantics.
Retain exact callbacks, confidential code+S256, disabled implicit/direct/service
account grants and verified provider TLS. Do not add a constant ACR mapper,
trust upstream business-role claims, or store OTP seed/password/recovery data
in the application. Reference TOTP is not phishing-resistant WebAuthn and this
fragment is not a complete production IAM service or institutional certification.

The tests exercise native forms over actual verified HTTPS sockets and provider
signatures; application HTTP uses FastAPI TestClient plus real disposable
least-privilege PostgreSQL. They are not full browser/network E2E, institutional
enrollment/recovery evidence, or authorization to unlock legacy RAG/workers.

Primary policy/model references:

- [Keycloak server administration: step-up authentication](https://www.keycloak.org/docs/latest/server_admin/)
- [26.8.1 native conditional LoA implementation](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/authentication/authenticators/conditional/ConditionalLoaAuthenticator.java)
- [26.8.1 native role condition](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/authentication/authenticators/conditional/ConditionalRoleAuthenticatorFactory.java)
- [26.8.1 OTP credential encoding model](https://github.com/keycloak/keycloak/blob/26.8.1/server-spi/src/main/java/org/keycloak/models/credential/OTPCredentialModel.java)
