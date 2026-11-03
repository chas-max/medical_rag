"""Connected isolated-app OIDC verification, not production identity approval.

The public key, issuer, audience, assurance values and one-use login nonce
must originate in trusted server configuration and a secure login transaction.
This module does not fetch keys, manage sessions, establish patient bindings,
or check that an account, institution or consent is still active. It is not
an authorization service and must not be used alone to authorize medical data access.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from secrets import compare_digest
from time import time
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from .provider_logout import valid_identifier
from .signing_keys import SigningKeys


class InvalidIdentity(Exception):
    """An ID token or its expected login nonce is invalid."""


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """A verified provider subject, not a local account or patient binding."""

    issuer: str
    subject: str
    mfa_verified: bool
    provider_session_id: str | None = field(default=None, repr=False)
    authenticated_at: int | None = field(default=None, repr=False)


class OIDCIdentityVerifier:
    """Verify RS256 ID tokens with a public key pinned by trusted configuration."""

    def __init__(self, *, issuer: str, audience: str, public_key: RSAPublicKey | None = None,
                 mfa_acr_values: frozenset[str], max_authentication_age_seconds: int = 600,
                 verification_keys: Mapping[str, RSAPublicKey] | None = None) -> None:
        try:
            parsed = urlsplit(issuer)
            valid_issuer = (parsed.scheme == "https" and bool(parsed.hostname)
                            and not parsed.username and not parsed.password
                            and not parsed.query and not parsed.fragment)
            _ = parsed.port
        except (ValueError, TypeError):
            valid_issuer = False
        if (not valid_issuer or not isinstance(audience, str) or not audience.strip()
                or audience != audience.strip() or not isinstance(mfa_acr_values, frozenset)
                or type(max_authentication_age_seconds) is not int
                or not 1 <= max_authentication_age_seconds <= 3600
                or not all(type(value) is str and value and value == value.strip()
                           for value in mfa_acr_values)):
            raise ValueError("Invalid trusted OIDC configuration")
        self._issuer = issuer
        self._audience = audience
        self._keys = SigningKeys(public_key, verification_keys)
        self._mfa_acr_values = mfa_acr_values
        self._max_authentication_age_seconds = max_authentication_age_seconds

    def verify(self, token: str, *, expected_nonce: str) -> VerifiedIdentity:
        """Validate a token against a server-side, single-use login transaction."""
        try:
            if (type(token) is not str or not 0 < len(token) <= 16384
                    or type(expected_nonce) is not str or not 0 < len(expected_nonce) <= 256):
                raise ValueError("Invalid token or nonce")
            claims = jwt.decode(
                token, self._keys.select(token), algorithms=["RS256"], issuer=self._issuer,
                audience=self._audience,
                options={"require": ["iss", "aud", "sub", "nonce", "iat", "exp", "auth_time"],
                         "strict_aud": True},
            )
            if (jwt.get_unverified_header(token).get("typ") == "logout+jwt"
                    or "http://schemas.openid.net/event/backchannel-logout" in claims.get("events", {})
                    or ("sid" in claims and not valid_identifier(claims["sid"]))):
                raise ValueError("Invalid identity token type")
            subject = claims.get("sub")
            nonce = claims.get("nonce")
            if (type(subject) is not str or not subject or subject != subject.strip()
                    or len(subject) > 255 or type(nonce) is not str
                    or not compare_digest(nonce, expected_nonce)
                    or ("azp" in claims and claims["azp"] != self._audience)
                    or any(type(claims[key]) is not int for key in ("iat", "exp", "auth_time"))
                    or claims["auth_time"] > time() or claims["auth_time"] > claims["iat"]
                    or time() - claims["auth_time"] > self._max_authentication_age_seconds):
                raise ValueError("Invalid identity claims")
            acr = claims.get("acr")
            return VerifiedIdentity(self._issuer, subject,
                                    type(acr) is str and acr in self._mfa_acr_values,
                                    claims.get("sid"), claims["auth_time"])
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise InvalidIdentity("Invalid identity token") from None
