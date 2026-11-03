"""Strict pinned-key institutional backchannel profile; no key discovery.

The immutable validated values are used only by SessionStore, never returned or
logged. Revocation and the durable pending-login fence commit before audit.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import time
from urllib.parse import parse_qsl

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import Request

from .signing_keys import SigningKeys

EVENT = "http://schemas.openid.net/event/backchannel-logout"
_BAD_PERCENT = re.compile(r"%(?![A-Fa-f0-9]{2})")
_FORM_TYPE = re.compile(r'application/x-www-form-urlencoded(?:[ \t]*;[ \t]*charset[ \t]*=[ \t]*(?:utf-8|"utf-8"))?\Z',
                        re.IGNORECASE)


class InvalidProviderLogout(Exception):
    """Static invalid protocol request, token or conflicting replay."""


def valid_identifier(value: object) -> bool:
    """Bound provider identifiers without interpreting them as paths or URLs."""
    return (type(value) is str and 0 < len(value) <= 255 and value == value.strip()
            and not any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                        for char in value))


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedProviderLogout:
    issuer: str = field(repr=False)
    jti: str = field(repr=False)
    subject: str | None = field(repr=False)
    sid: str | None = field(repr=False)
    issued_at: int = field(repr=False)
    expires_at: int = field(repr=False)


class ProviderLogoutVerifier:
    """Shares precisely the trusted issuer/client/key of the ID verifier."""

    def __init__(self, *, issuer: str, audience: str, public_key: RSAPublicKey | None = None,
                 verification_keys: Mapping[str, RSAPublicKey] | None = None) -> None:
        self._issuer, self._audience = issuer, audience
        self._keys = SigningKeys(public_key, verification_keys)

    def verify(self, token: str) -> VerifiedProviderLogout:
        try:
            if type(token) is not str or not 0 < len(token) <= 16384:
                raise ValueError()
            header = jwt.get_unverified_header(token)
            if header.get("typ") != "logout+jwt":
                raise ValueError()
            c = jwt.decode(token, self._keys.select(token), algorithms=["RS256"], issuer=self._issuer, audience=self._audience,
                           options={"require": ["iss", "aud", "iat", "exp", "jti", "events"],
                                    "strict_aud": True})
            now = time()
            if (any(type(c[key]) is not int for key in ("iat", "exp"))
                    or ("nbf" in c and type(c["nbf"]) is not int)
                    or not 0 <= c["iat"] <= now or c["iat"] < now - 120
                    or not now < c["exp"] <= c["iat"] + 300 or c["exp"] <= c["iat"]
                    or "nonce" in c or not valid_identifier(c["jti"])
                    or type(c["events"]) is not dict or EVENT not in c["events"]
                    or type(c["events"][EVENT]) is not dict
                    or not any(key in c for key in ("sub", "sid"))
                    or any(key in c and not valid_identifier(c[key]) for key in ("sub", "sid"))):
                raise ValueError()
            return VerifiedProviderLogout(self._issuer, c["jti"], c.get("sub"), c.get("sid"), c["iat"], c["exp"])
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise InvalidProviderLogout() from None


async def read_logout_token(request: Request) -> str:
    """Read bounded strict UTF-8 URL-encoded form, ignoring unknown keys."""
    try:
        types = request.headers.getlist("content-type")
        if (request.scope.get("query_string") or len(types) != 1
                or not _FORM_TYPE.fullmatch(types[0].strip())
                or request.headers.getlist("content-encoding") not in ([], ["identity"])):
            raise ValueError()
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 32768:
                raise ValueError()
            body.extend(chunk)
        value = body.decode("utf-8", errors="strict")
        if _BAD_PERCENT.search(value):
            raise ValueError()
        # The byte limit bounds field count as well; standard bare unknown
        # flags are harmless empty-valued fields, not authority.
        pairs = parse_qsl(value, keep_blank_values=True, encoding="utf-8", errors="strict", max_num_fields=32769)
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError()
        token = dict(pairs).get("logout_token")
        if type(token) is not str or not 0 < len(token) <= 16384:
            raise ValueError()
        return token
    except (ValueError, UnicodeError):
        raise InvalidProviderLogout() from None
