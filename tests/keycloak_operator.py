"""Disposable realm-only native code/PKCE operator; not application trust code."""

# ruff: noqa: BLE001 -- token-bearing library/parser/transport failures stay static

import json
import re
import secrets
import sys
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx2 as httpx
from authlib.integrations.httpx_client import OAuth2Client
from jwt.algorithms import RSAAlgorithm
from keycloak_forms import password_redirect

from medical_rag.provider_tls import provider_ssl_context

_ERROR = "Disposable provider operator failed"
_TYPE = "org.keycloak.keys.KeyProvider"
_CALLBACK = "https://fixture-operator.invalid/callback"


class _Stream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        size = 0
        for chunk in self.stream:
            size += len(chunk)
            if size > 65536:
                raise RuntimeError(_ERROR)
            yield chunk

    def close(self):
        self.stream.close()


class _Transport(httpx.BaseTransport):
    def __init__(self, provider):
        self.inner = httpx.HTTPTransport(verify=provider_ssl_context(provider.ca_file), trust_env=False)

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        response.stream = _Stream(response.stream)
        return response

    def close(self):
        self.inner.close()


def _tokens(provider, username, password, client_id, secret, callback):
    """Real native form and S256 code exchange, no password/service grant."""
    nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    try:
        with (provider.browser() as browser,
              OAuth2Client(client_id, secret, redirect_uri=callback, scope="openid",
                           code_challenge_method="S256", transport=_Transport(provider),
                           trust_env=False, timeout=5, follow_redirects=False) as oauth):
            url, state = oauth.create_authorization_url(
                provider.issuer + "/protocol/openid-connect/auth", nonce=nonce, code_verifier=verifier)
            redirect = password_redirect(browser, url, username, password)
            if not redirect or urlsplit(redirect)[:3] != urlsplit(callback)[:3]:
                raise TypeError()
            parameters = parse_qs(urlsplit(redirect).query)
            if parameters.get("state") != [state] or len(parameters.get("code", [])) != 1:
                raise RuntimeError()
            result = oauth.fetch_token(provider.issuer + "/protocol/openid-connect/token",
                                       code=parameters["code"][0], code_verifier=verifier)
            if not isinstance(result.get("access_token"), str) or not isinstance(result.get("id_token"), str):
                raise TypeError()
            return result, nonce
    except Exception:
        raise RuntimeError(_ERROR) from None


def upstream_token(provider, name):
    """Capture actual patient token in memory for the retirement-only check."""
    result, nonce = _tokens(provider, name, provider.users[name]["password"],
                            "medical-rag", provider.client_secret,
                            "https://medical.example.org/auth/callback")
    return result["id_token"], nonce


class Operator:
    """Only this unique fictional realm; bearer retained in fixture memory."""

    def __init__(self, provider):
        parsed = urlsplit(provider.issuer)
        if (parsed.scheme != "https" or parsed.hostname != "localhost" or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or not re.fullmatch(r"/realms/fictional-[a-f0-9]{12}", parsed.path)):
            raise RuntimeError(_ERROR)
        self.provider = provider
        self.base = parsed.scheme + "://" + parsed.netloc + "/admin" + parsed.path
        self.realm = parsed.path.rsplit("/", 1)[1]
        self.browser = None
        self._bearer = None
        self._created = {}

    def __enter__(self):
        values = self.provider.operator
        token, _ = _tokens(self.provider, values["username"], values["password"],
                           "fixture-operator", values["client_secret"], _CALLBACK)
        self._bearer = token["access_token"]
        self.browser = self.provider.browser()
        return self

    def __exit__(self, *exc):
        self._bearer = None
        self.browser.close()

    def _request(self, method, suffix, *, expected=200, **kwargs):
        try:
            with self.browser.stream(method, self.base + suffix,
                                     headers={"Authorization": "Bearer " + self._bearer}, **kwargs) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > 65536:
                        raise RuntimeError()
                    body.extend(chunk)
                if response.status_code != expected:
                    raise RuntimeError()
                return json.loads(body) if body else None
        except Exception:
            raise RuntimeError(_ERROR) from None

    def _owned(self, component):
        try:
            identifier = str(UUID(component["id"]))
            if identifier != component["id"] or self._created.get(identifier) != component["name"]:
                raise ValueError()
            actual = self._request("GET", "/components/" + identifier)
            if (actual.get("id") != identifier or actual.get("parentId") != self.realm
                    or actual.get("providerType") != _TYPE or actual.get("providerId") != "rsa-generated"
                    or actual.get("name") != component["name"]
                    or not re.fullmatch(r"fixture-rotation-[a-f0-9]{32}", actual["name"])):
                raise ValueError()
            return actual
        except Exception:
            raise RuntimeError(_ERROR) from None

    @contextmanager
    def second_signing_key(self):
        """Enroll passive provider first, delete only exact verified component."""
        name = "fixture-rotation-" + uuid4().hex
        self._request("POST", "/components", expected=201, json={
            "name": name, "parentId": self.realm, "providerType": _TYPE,
            "providerId": "rsa-generated", "config": {
                "priority": ["200"], "enabled": ["true"], "active": ["false"],
                "algorithm": ["RS256"], "keySize": ["2048"]}})
        matches = self._request("GET", "/components", params={"name": name, "type": _TYPE})
        if type(matches) is not list or len(matches) != 1:
            raise RuntimeError(_ERROR)
        if matches[0].get("name") != name:
            raise RuntimeError(_ERROR)
        self._created[matches[0]["id"]] = name
        component = self._owned(matches[0])
        ownership = {"id": component["id"], "name": name}
        try:
            yield component
        finally:
            original = sys.exception()
            try:
                actual = self._owned(ownership)
                self._request("DELETE", "/components/" + actual["id"], expected=204)
                del self._created[actual["id"]]
            except Exception:
                if original is not None:
                    original.add_note("Disposable signing component cleanup failed")
                else:
                    raise RuntimeError("Disposable signing component cleanup failed") from None

    def activate(self, component):
        actual = self._owned(component)
        actual["config"]["active"] = ["true"]
        self._request("PUT", "/components/" + actual["id"], expected=204, json=actual)

    def public_keys(self):
        """Fixture trust enrollment only, RSA/RS256/sig entries over verified TLS."""
        try:
            from keycloak_runtime import _ready

            jwks = json.loads(_ready(self.browser, self.provider.issuer + "/protocol/openid-connect/certs", 5))
            return {item["kid"]: RSAAlgorithm.from_jwk(item) for item in jwks["keys"]
                    if item.get("kty") == "RSA" and item.get("alg") == "RS256" and item.get("use") == "sig"}
        except Exception:
            raise RuntimeError(_ERROR) from None
