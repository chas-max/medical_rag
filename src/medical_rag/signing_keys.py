"""Immutable, bounded, operator-approved RSA trust; no network discovery."""

import json
import re
from collections.abc import Mapping
from types import MappingProxyType

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

_KID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CONFIG_ERROR = "Invalid trusted OIDC configuration"


def validate_key_names(values: object) -> None:
    """Validate the complete bounded namespace before any operator file reads."""
    if (not isinstance(values, Mapping) or not 1 <= len(values) <= 8
            or any(type(name) is not str or not _KID.fullmatch(name) for name in values)):
        raise ValueError(_CONFIG_ERROR)


def _unique_object(pairs: list) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError()
        result[name] = value
    return result


class SigningKeys:
    """Select exactly one pinned public key without fallback or negotiation."""

    __slots__ = ("_public_key", "_verification_keys")

    def __init__(self, public_key: RSAPublicKey | None = None,
                 verification_keys: Mapping[str, RSAPublicKey] | None = None) -> None:
        if (public_key is None) == (verification_keys is None):
            raise ValueError(_CONFIG_ERROR)
        ring = None
        if verification_keys is not None:
            validate_key_names(verification_keys)
            ring = dict(verification_keys)
        keys = list(ring.values()) if ring is not None else [public_key]
        if any(not isinstance(key, RSAPublicKey) or key.key_size < 2048 for key in keys):
            raise ValueError(_CONFIG_ERROR)
        numbers = [key.public_numbers() for key in keys]
        if len(set(numbers)) != len(numbers):
            raise ValueError(_CONFIG_ERROR)
        self._public_key = public_key
        self._verification_keys = MappingProxyType(ring) if ring is not None else None

    @property
    def verification_keys(self) -> Mapping[str, RSAPublicKey] | None:
        """Frozen trusted mapping for configuration handoff, never diagnostics."""
        return self._verification_keys

    def select(self, token: str) -> RSAPublicKey:
        """Reject ambiguous/remote headers, then perform one exact local lookup."""
        if type(token) is not str or not 0 < len(token) <= 16384:
            raise ValueError()
        # PyJWT remains responsible for cryptographic validation. Parse the
        # bounded protected header with duplicate detection before selecting.
        header = jwt.get_unverified_header(token)
        json.loads(jwt.utils.base64url_decode(token.split(".", 1)[0].encode()),
                   object_pairs_hook=_unique_object)
        if (header.get("alg") != "RS256"
                or any(name in header for name in ("crit", "jku", "jwk", "x5u", "x5c"))
                or ("kid" in header and (type(header["kid"]) is not str
                                         or not _KID.fullmatch(header["kid"])))):
            raise ValueError()
        if self._verification_keys is None:
            return self._public_key
        if "kid" not in header or header["kid"] not in self._verification_keys:
            raise ValueError()
        return self._verification_keys[header["kid"]]
