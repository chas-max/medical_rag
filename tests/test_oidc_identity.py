"""Identity token checks use signed, fictional test identities only."""

from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from medical_rag.oidc_identity import InvalidIdentity, OIDCIdentityVerifier


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(signing_key):
    return OIDCIdentityVerifier(
        issuer="https://identity.example.org/realms/hospital",
        audience="medical-rag",
        public_key=signing_key.public_key(),
        mfa_acr_values=frozenset({"urn:hospital:loa:mfa"}),
    )


@pytest.fixture
def claims():
    now = datetime.now(UTC)
    return {
        "iss": "https://identity.example.org/realms/hospital",
        "aud": "medical-rag",
        "sub": "idp-subject-1",
        "nonce": "one-time-nonce",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "auth_time": int(now.timestamp()),
        "acr": "urn:hospital:loa:mfa",
    }


def test_valid_id_token_identifies_subject_and_configured_mfa(verifier, signing_key, claims):
    token = jwt.encode(claims, signing_key, algorithm="RS256")
    identity = verifier.verify(token, expected_nonce="one-time-nonce")
    assert identity.issuer == claims["iss"]
    assert identity.subject == claims["sub"]
    assert identity.mfa_verified is True


def test_unknown_assurance_does_not_grant_mfa(verifier, signing_key, claims):
    claims["acr"] = "untrusted-provider-value"
    token = jwt.encode(claims, signing_key, algorithm="RS256")
    assert verifier.verify(token, expected_nonce="one-time-nonce").mfa_verified is False


def test_stale_provider_authentication_is_rejected(verifier, signing_key, claims):
    claims["auth_time"] = int((datetime.now(UTC) - timedelta(minutes=11)).timestamp())
    token = jwt.encode(claims, signing_key, algorithm="RS256")
    with pytest.raises(InvalidIdentity):
        verifier.verify(token, expected_nonce="one-time-nonce")


@pytest.mark.parametrize("change", ["issuer", "audience", "nonce", "expired", "future", "missing_sub", "missing_auth_time", "missing_iat", "missing_exp", "multiple_audiences", "wrong_azp", "future_auth_time"])
def test_invalid_claims_are_rejected(verifier, signing_key, claims, change):
    if change == "issuer":
        claims["iss"] = "https://attacker.example"
    elif change == "audience":
        claims["aud"] = "other-client"
    elif change == "nonce":
        claims["nonce"] = "other-login"
    elif change == "expired":
        claims["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "future":
        claims["iat"] = datetime.now(UTC) + timedelta(minutes=2)
    elif change == "missing_sub":
        del claims["sub"]
    elif change == "missing_auth_time":
        del claims["auth_time"]
    elif change == "missing_iat":
        del claims["iat"]
    elif change == "missing_exp":
        del claims["exp"]
    elif change == "multiple_audiences":
        claims["aud"] = ["medical-rag", "other-client"]
    elif change == "wrong_azp":
        claims["azp"] = "other-client"
    else:
        claims["auth_time"] = int((datetime.now(UTC) + timedelta(minutes=2)).timestamp())
    token = jwt.encode(claims, signing_key, algorithm="RS256")
    with pytest.raises(InvalidIdentity):
        verifier.verify(token, expected_nonce="one-time-nonce")


def test_other_signing_key_is_rejected(verifier, claims):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(claims, key, algorithm="RS256")
    with pytest.raises(InvalidIdentity):
        verifier.verify(token, expected_nonce="one-time-nonce")


@pytest.mark.parametrize("field", ["iat", "exp", "auth_time"])
@pytest.mark.parametrize("value", [True, "1700000000", None])
def test_non_numeric_date_claims_are_rejected(verifier, signing_key, claims, field, value):
    claims[field] = value
    token = jwt.encode(claims, signing_key, algorithm="RS256")
    with pytest.raises(InvalidIdentity, match="^Invalid identity token$"):
        verifier.verify(token, expected_nonce="one-time-nonce")


def test_provider_roles_do_not_become_local_permissions(verifier, signing_key, claims):
    claims["roles"] = ["org_admin", "patient", "platform_operator"]
    identity = verifier.verify(jwt.encode(claims, signing_key, algorithm="RS256"),
                               expected_nonce="one-time-nonce")
    assert not hasattr(identity, "roles")
    assert not hasattr(identity, "patient_id")


def test_hmac_signed_token_is_rejected(verifier, claims):
    token = jwt.encode(claims, "a-fake-test-only-secret-with-at-least-32-bytes", algorithm="HS256")
    with pytest.raises(InvalidIdentity, match="^Invalid identity token$"):
        verifier.verify(token, expected_nonce="one-time-nonce")


def test_deeply_nested_untrusted_header_returns_only_generic_error(verifier):
    header = b'{"x":' + b'[' * 6000 + b'0' + b']' * 6000 + b'}'
    encoded = urlsafe_b64encode(header).rstrip(b'=').decode('ascii')
    with pytest.raises(InvalidIdentity, match="^Invalid identity token$"):
        verifier.verify(encoded + ".e30.signature", expected_nonce="one-time-nonce")


@pytest.mark.parametrize("nonce", ["", None, "other-login"])
def test_missing_or_wrong_expected_nonce_is_rejected(verifier, signing_key, claims, nonce):
    token = jwt.encode(claims, signing_key, algorithm="RS256")
    with pytest.raises(InvalidIdentity):
        verifier.verify(token, expected_nonce=nonce)


@pytest.mark.parametrize("issuer", ["http://identity.example.org", "https://identity.example.org?x=1", "https://name:secret@identity.example.org", "https://identity.example.org/#fragment"])
def test_issuer_configuration_must_be_unambiguous_https(signing_key, issuer):
    with pytest.raises(ValueError):
        OIDCIdentityVerifier(issuer=issuer, audience="medical-rag", public_key=signing_key.public_key(), mfa_acr_values=frozenset())


@pytest.mark.parametrize("age", [0, -1, 3601, True, "600"])
def test_invalid_authentication_age_configuration_is_rejected(signing_key, age):
    with pytest.raises(ValueError):
        OIDCIdentityVerifier(issuer="https://identity.example.org", audience="medical-rag",
                             public_key=signing_key.public_key(), mfa_acr_values=frozenset(),
                             max_authentication_age_seconds=age)


def test_weak_rsa_public_key_is_rejected():
    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    with pytest.raises(ValueError):
        OIDCIdentityVerifier(issuer="https://identity.example.org", audience="medical-rag",
                             public_key=key.public_key(), mfa_acr_values=frozenset())
