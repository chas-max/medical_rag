"""Bounded native form driver over real verified HTTPS, not browser E2E."""

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx


class NativeForm(HTMLParser):
    """Accept one provider POST form and bounded successful control fields."""

    def __init__(self, html: str, form_id: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.action = None
        self.fields = {}
        self._inside = False
        self._matched = False
        self._form_id = form_id
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs)
        if tag == "form" and (
            self._form_id is None or attrs.get("id") == self._form_id
        ):
            if self._matched or attrs.get("method", "get").lower() != "post":
                raise RuntimeError("Unexpected provider form")
            self._matched = True
            self.action, self._inside = attrs.get("action"), True
        if tag == "input" and self._inside and attrs.get("name"):
            value = attrs.get("value") or ""
            if (
                len(self.fields) >= 32
                or attrs["name"] in self.fields
                or len(attrs["name"]) > 256
                or len(value) > 4096
            ):
                raise RuntimeError("Provider form limit")
            self.fields[attrs["name"]] = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._inside = False


def _request(
    browser: httpx.Client, method: str, url: str, **kwargs
) -> tuple[httpx.Response, bytes]:
    """Bound bytes while reading, before HTML decoding or parsing."""
    with browser.stream(method, url, **kwargs) as response:
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > 131072:
                raise RuntimeError("Provider page limit")
            body.extend(chunk)
        return response, bytes(body)


def _page(
    response: httpx.Response, body: bytes, form_id: str | None = "kc-form-login"
) -> NativeForm:
    """Parse only a bounded successful native HTML page."""
    if response.status_code != 200:
        raise RuntimeError("Unexpected provider page")
    return NativeForm(body.decode("utf-8", errors="strict"), form_id)


def _action(response: httpx.Response, form: NativeForm) -> str:
    """Resolve one fixed provider-origin action, never follow external forms."""
    if not form.action:
        raise RuntimeError("Provider form missing")
    action = urljoin(str(response.url), form.action)
    parsed = urlsplit(action)
    if (
        len(action) > 4096
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed[:2] != urlsplit(str(response.url))[:2]
    ):
        raise RuntimeError("Provider form origin mismatch")
    return action


def password_redirect(
    browser: httpx.Client, authorization_url: str, username: str, password: str
) -> str | None:
    """Return the real provider redirect only after native password form POST."""
    response, body = _request(browser, "GET", authorization_url)
    form = _page(response, body)
    response, body = _request(
        browser,
        "POST",
        _action(response, form),
        data=form.fields | {"username": username, "password": password},
    )
    if response.status_code == 200:
        _page(response, body)  # ordinary failed login; no code/session
        return None
    if response.status_code != 302 or len(response.headers.get("location", "")) > 4096:
        raise RuntimeError("Unexpected provider authentication result")
    return response.headers["location"]


@dataclass(repr=False)
class OTPChallenge:
    """Ephemeral native action and hidden fields, never useful in diagnostics."""

    action: str
    fields: dict[str, str]


def _authentication_result(response: httpx.Response, body: bytes) -> tuple[OTPChallenge | None, str | None]:
    if response.status_code == 200:
        form = _page(response, body, "kc-otp-login-form")
        if form.action:
            return OTPChallenge(_action(response, form), form.fields), None
        return None, None
    location = response.headers.get("location", "")
    if response.status_code != 302 or not location or len(location) > 4096:
        raise RuntimeError("Unexpected provider authentication result")
    return None, location


def password_challenge(browser: httpx.Client, authorization_url: str,
                       username: str, password: str) -> tuple[OTPChallenge | None, str | None]:
    """Require native password acceptance before exposing a pending OTP form.

    A later SSO request can start directly at OTP; no password is sent then.
    A non-OTP page (failed password or enrollment) grants no redirect/session.
    """
    response, body = _request(browser, "GET", authorization_url)
    login = _page(response, body)
    if not login.action:
        return _authentication_result(response, body)
    response, body = _request(browser, "POST", _action(response, login),
                              data=login.fields | {"username": username, "password": password})
    return _authentication_result(response, body)


def submit_otp(browser: httpx.Client, challenge: OTPChallenge,
               code: str) -> tuple[OTPChallenge | None, str | None]:
    """Submit to the provider, including bad values; never verify locally."""
    response, body = _request(browser, "POST", challenge.action,
                              data=challenge.fields | {"otp": code})
    return _authentication_result(response, body)


def provider_logout(
    browser: httpx.Client, issuer: str, client_id: str, post_logout: str
) -> None:
    response, body = _request(
        browser,
        "GET",
        issuer + "/protocol/openid-connect/logout",
        params={"client_id": client_id, "post_logout_redirect_uri": post_logout},
    )
    form = _page(response, body, form_id=None)
    response, _ = _request(
        browser,
        "POST",
        _action(response, form),
        data=form.fields | {"confirmLogout": ""},
    )
    if response.status_code not in (200, 302):
        raise RuntimeError("Provider logout did not complete")
