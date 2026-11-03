"""Operator-approved trust selection, never token-directed key discovery."""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from medical_rag.oidc_identity import InvalidIdentity, OIDCIdentityVerifier
from medical_rag.provider_logout import (
    EVENT,
    InvalidProviderLogout,
    ProviderLogoutVerifier,
)


@pytest.fixture(scope="module")
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


def verifiers(keys, **settings):
    config = dict(issuer="https://fictional.invalid/realm", audience="patient", **settings)
    return (OIDCIdentityVerifier(**config, mfa_acr_values=frozenset()),
            ProviderLogoutVerifier(**config))


def token(key, purpose, headers):
    now = int(time.time())
    claims = {"iss": "https://fictional.invalid/realm", "aud": "patient", "sub": "fictional",
              "iat": now, "exp": now + 120}
    if purpose == 0:
        claims.update(nonce="fixture-nonce", auth_time=now)
    else:
        claims.update(jti="fixture-event", events={EVENT: {}})
    encoded = jwt.encode(claims, key, algorithm="RS256")
    payload = encoded.split(".")[1]
    header = jwt.utils.base64url_encode(json.dumps(
        {"alg": "RS256", "typ": "JWT" if purpose == 0 else "logout+jwt"} | headers
    ).encode()).decode()
    message = (header + "." + payload).encode()
    signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return message.decode() + "." + jwt.utils.base64url_encode(signature).decode()


def verify(verifier, purpose, encoded):
    return verifier.verify(encoded, **({"expected_nonce": "fixture-nonce"} if purpose == 0 else {}))


@pytest.mark.parametrize("purpose", [0, 1])
def test_overlap_accepts_both_exact_names_and_freezes_caller_mapping(keys, purpose):
    ring = {"old": keys[0].public_key(), "new": keys[1].public_key()}
    verifier = verifiers(keys, public_key=None, verification_keys=ring)[purpose]
    ring.clear()
    for name, key in zip(("old", "new"), keys, strict=True):
        assert verify(verifier, purpose, token(key, purpose, {"kid": name}))


@pytest.mark.parametrize("purpose", [0, 1])
@pytest.mark.parametrize("headers", [{}, {"kid": None}, {"kid": ""}, {"kid": "unknown"},
                                    {"kid": "../old"}, {"kid": "old new"}, {"kid": 1},
                                    {"kid": "a" * 129}, {"kid": "new"},
                                    *[{"kid": "old", name: []} for name in ("crit", "jku", "jwk", "x5u", "x5c")]])
def test_ring_rejects_missing_unknown_malformed_and_mislabeled_keys(keys, purpose, headers):
    verifier = verifiers(keys, public_key=None,
                         verification_keys={"old": keys[0].public_key(), "new": keys[1].public_key()})[purpose]
    with pytest.raises((InvalidIdentity, InvalidProviderLogout)):
        verify(verifier, purpose, token(keys[0], purpose, headers))


@pytest.mark.parametrize("purpose", [0, 1])
@pytest.mark.parametrize("headers", [{"kid": None}, {"kid": "../key"},
                                    *[{name: []} for name in ("crit", "jku", "jwk", "x5u", "x5c")]])
def test_legacy_retains_optional_label_but_rejects_key_selection_headers(keys, purpose, headers):
    verifier = verifiers(keys, public_key=keys[0].public_key())[purpose]
    with pytest.raises((InvalidIdentity, InvalidProviderLogout)):
        verify(verifier, purpose, token(keys[0], purpose, headers))


@pytest.mark.parametrize("purpose", [0, 1])
def test_legacy_accepts_absent_or_bounded_label(keys, purpose):
    verifier = verifiers(keys, public_key=keys[0].public_key())[purpose]
    for headers in ({}, {"kid": "ordinary-label"}):
        assert verify(verifier, purpose, token(keys[0], purpose, headers))


@pytest.mark.parametrize("case", ["neither", "both", "empty", "too-many", "bad-name", "duplicate", "private", "ec", "weak", "pem"])
def test_invalid_trusted_ring_is_static_and_private(keys, case):
    ring = {"old": keys[0].public_key()}
    public = None
    if case == "neither":
        ring = None
    elif case == "both":
        public = keys[0].public_key()
    elif case == "empty":
        ring = {}
    elif case == "too-many":
        ring = {str(i): keys[0].public_key() for i in range(9)}
    elif case == "bad-name":
        ring = {"SECRET/PATH": keys[0].public_key()}
    elif case == "duplicate":
        ring["new"] = keys[0].public_key()
    elif case == "private":
        ring["old"] = keys[0]
    elif case == "ec":
        ring["old"] = ec.generate_private_key(ec.SECP256R1()).public_key()
    elif case == "weak":
        ring["old"] = rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key()
    else:
        ring["old"] = "SECRET-PEM"
    for verifier in (OIDCIdentityVerifier, ProviderLogoutVerifier):
        with pytest.raises(ValueError, match="^Invalid trusted OIDC configuration$"):
            verifier(issuer="https://fictional.invalid/realm", audience="patient",
                     public_key=public, verification_keys=ring,
                     **({"mfa_acr_values": frozenset()} if verifier is OIDCIdentityVerifier else {}))


@pytest.mark.parametrize("value", [None, 4, "", "x" * 16385], ids=["null", "integer", "empty", "bound"])
def test_shared_selector_bounds_before_parsing(keys, value, monkeypatch):
    from medical_rag.signing_keys import SigningKeys

    selected = SigningKeys(keys[0].public_key())
    reached = []
    monkeypatch.setattr(jwt, "get_unverified_header", lambda _: reached.append(True))
    with pytest.raises(ValueError):
        selected.select(value)
    assert reached == []


@pytest.mark.parametrize("purpose", [0, 1])
@pytest.mark.parametrize("alg", ["HS256", "RS384", "none"])
def test_ring_does_not_negotiate_algorithm(keys, purpose, alg):
    verifier = verifiers(keys, verification_keys={"old": keys[0].public_key()})[purpose]
    with pytest.raises((InvalidIdentity, InvalidProviderLogout)):
        verify(verifier, purpose, token(keys[0], purpose, {"kid": "old", "alg": alg}))


@pytest.mark.parametrize("purpose", [0, 1])
def test_duplicate_kid_and_reprs_do_not_disclose_trust(keys, purpose):
    from medical_rag.signing_keys import SigningKeys

    trust = SigningKeys(verification_keys={"PRIVATE-CANARY": keys[0].public_key()})
    assert "PRIVATE-CANARY" not in repr(trust)
    verifier = verifiers(keys, verification_keys={"old": keys[0].public_key()})[purpose]
    original = token(keys[0], purpose, {"kid": "old"})
    kind = "JWT" if purpose == 0 else "logout+jwt"
    head = jwt.utils.base64url_encode(
        ('{"alg":"RS256","typ":"' + kind + '","kid":"old","kid":"old"}').encode()).decode()
    payload = original.split(".")[1]
    message = (head + "." + payload).encode()
    signature = keys[0].sign(message, padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises((InvalidIdentity, InvalidProviderLogout)):
        verify(verifier, purpose, message.decode() + "." + jwt.utils.base64url_encode(signature).decode())
