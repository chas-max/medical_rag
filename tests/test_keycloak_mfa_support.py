"""Pure reference-policy and bounded native OTP-driver contracts."""

import inspect
import json
from pathlib import Path

import httpx
import keycloak_forms
import keycloak_runtime
import pytest

POLICY = Path(__file__).resolve().parents[1] / "deploy/identity/keycloak-mfa-policy.json"


def test_runtime_has_explicit_opt_in_policy_composition():
    available = "realm_transform" in inspect.signature(keycloak_runtime.identity_runtime).parameters
    assert available, "Actual MFA provisioning must opt in without changing default fixture behavior"


def test_fixture_composer_uses_same_public_policy_and_memory_only_credentials():
    helper = Path(__file__).with_name("keycloak_mfa.py")
    present = helper.exists()
    assert present, "Focused MFA composer must load the shipped policy"
    from keycloak_mfa import compose_mfa

    imported, users = {"roles": {"realm": [{"name": "admin"}]} , "users": []}, {}
    compose_mfa(imported, users)
    public = json.loads(POLICY.read_text(encoding="utf-8"))
    assert imported["authenticationFlows"] == public["authenticationFlows"]
    assert imported["attributes"] == public["attributes"]
    expected_users = set(users) == {"staff", "staff_norole", "staff_unenrolled"}
    assert expected_users, "Composer must provision exactly the fictional staff scenarios"
    enrollment_matches = "otp_seed" in users["staff"] and "otp_seed" not in users["staff_unenrolled"]
    assert enrollment_matches, "Only enrolled fictional staff may have a fixture OTP seed"
    assert all(user["realmRoles"] == ["medical-staff-mfa"] for user in imported["users"])
    otp_models = [json.loads(credential["credentialData"]) for user in imported["users"]
                  for credential in user["credentials"] if credential["type"] == "otp"]
    encoding_matches = all(model.get("secretEncoding") == "BASE32" for model in otp_models)
    assert encoding_matches, "Keycloak credential model must explicitly decode the Base32 fixture secret"


@pytest.mark.parametrize("now,waiting", [(60.0, False), (89.0, True)])
def test_code_generation_uses_cryptography_and_bounded_rollover(monkeypatch, now, waiting):
    import keycloak_mfa
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.twofactor.totp import TOTP

    available = hasattr(keycloak_mfa, "otp_code")
    assert available, "Mature OTP generation with bounded rollover handling required"
    slept = []
    clock = iter([now, 90.05])
    monkeypatch.setattr(keycloak_mfa.time, "time", lambda: next(clock))
    monkeypatch.setattr(keycloak_mfa.time, "sleep", slept.append)
    seed = b"fictional-unit-test-only"
    code = keycloak_mfa.otp_code({"otp_seed": seed})
    valid = code == TOTP(seed, 6, hashes.SHA1(), 30).generate(90.05 if waiting else now).decode()
    assert valid, "Generated code must match mature TOTP utility"
    assert bool(slept) == waiting
    assert all(0 < duration < 3.1 for duration in slept)


def test_code_generation_never_reuses_the_last_issued_timestep(monkeypatch):
    import keycloak_mfa

    clock = iter([65.0, 90.05])
    slept = []
    monkeypatch.setattr(keycloak_mfa.time, "time", lambda: next(clock))
    monkeypatch.setattr(keycloak_mfa.time, "sleep", slept.append)
    user = {"otp_seed": b"fictional-unit-test-only", "_last_otp_step": 2}
    keycloak_mfa.otp_code(user)
    fresh = user["_last_otp_step"] == 3 and len(slept) == 1 and 0 < slept[0] <= 30.05
    assert fresh, "Keep provider single-use OTP policy; wait boundedly for a fresh timestep"


def test_wrong_code_is_outside_default_provider_time_window(monkeypatch):
    import keycloak_mfa
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.twofactor.totp import TOTP

    available = hasattr(keycloak_mfa, "wrong_otp_code")
    assert available, "Actual bad-OTP test must deterministically avoid valid adjacent timestep codes"
    monkeypatch.setattr(keycloak_mfa.time, "time", lambda: 300.0)
    user = {"otp_seed": b"fictional-unit-test-only"}
    code = keycloak_mfa.wrong_otp_code(user)
    candidates = {TOTP(user["otp_seed"], 6, hashes.SHA1(), 30).generate(300 + offset * 30).decode() for offset in range(-2, 3)}
    invalid = code not in candidates and len(code) == 6 and code.isdecimal()
    assert invalid, "Bad code must be a native six-digit input outside the provider default acceptance window"


def test_public_policy_native_role_and_level_required_without_deployment_identity():
    present = POLICY.exists()
    assert present, "Reusable public MFA policy fragment is required"
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    assert set(policy) == {"authenticationFlows", "authenticatorConfig", "browserFlow", "attributes", "roles"}
    assert policy["attributes"]["acr.loa.map"] == '{"urn:medical-rag:reference:otp":2}'
    flows = {flow["alias"]: flow for flow in policy["authenticationFlows"]}
    level2 = flows["medical-reference-staff-level2"]["authenticationExecutions"]
    assert [item["authenticator"] for item in level2] == [
        "conditional-user-role", "conditional-level-of-authentication", "auth-otp-form"]
    assert all(item["requirement"] == "REQUIRED" for item in level2)
    configs = {item["alias"]: item["config"] for item in policy["authenticatorConfig"]}
    assert configs["medical-reference-level2"] == {"loa-condition-level": "2", "loa-max-age": "0"}
    assert configs["medical-reference-staff"] == {"condUserRole": "medical-staff-mfa", "negate": "false"}
    assert policy["roles"] == {"realm": [{"name": "medical-staff-mfa"}]}
    assert not any("mapper" in key.lower() for key in policy)


def test_password_challenge_driver_exists():
    available = callable(getattr(keycloak_forms, "password_challenge", None))
    assert available, "Native password driver must return a bounded pending OTP challenge"


def test_otp_submit_driver_exists():
    available = callable(getattr(keycloak_forms, "submit_otp", None))
    assert available, "Native OTP driver must submit to actual provider"


def test_pending_challenge_repr_is_private():
    available = hasattr(keycloak_forms, "OTPChallenge")
    assert available, "Pending challenge must have a private representation"
    challenge = keycloak_forms.OTPChallenge("PRIVATE-CANARY", {"otp": "PRIVATE-CANARY"})
    assert "PRIVATE-CANARY" not in repr(challenge)


@pytest.mark.parametrize("result", ["otp", "rejected", "redirect", "invalid", "external", "large"])
def test_driver_requires_native_password_then_bounded_same_origin_otp(monkeypatch, result):
    assert hasattr(keycloak_forms, "password_challenge"), "Native challenge driver required"
    calls = []
    login = b'<form id="kc-form-login" method="post" action="/password"><input name="credentialId" value="fictional"></form>'
    otp = b'<form id="kc-otp-login-form" method="post" action="/otp"><input name="otp"></form>'
    if result == "external":
        otp = otp.replace(b'/otp', b'https://external.invalid/otp')
    response = httpx.Response(200, request=httpx.Request("GET", "https://provider.invalid/auth"))

    def request(browser, method, url, **kwargs):
        calls.append((method, url, kwargs))
        if len(calls) == 1:
            return response, login
        if result == "redirect":
            return httpx.Response(302, headers={"location": "https://app.invalid/callback"}, request=response.request), b""
        if result == "invalid":
            return httpx.Response(500, request=response.request), b""
        return response, login if result == "rejected" else otp

    monkeypatch.setattr(keycloak_forms, "_request", request)
    if result in {"invalid", "external"}:
        with pytest.raises(RuntimeError):
            keycloak_forms.password_challenge(None, "https://provider.invalid/auth", "fictional", "private")
        return
    challenge, redirect = keycloak_forms.password_challenge(None, "https://provider.invalid/auth", "fictional", "private")
    assert calls[1][0:2] == ("POST", "https://provider.invalid/password")
    assert (challenge is not None) == (result in {"otp", "large"})
    assert bool(redirect) == (result == "redirect")


@pytest.mark.parametrize("status,challenge_page", [(302, False), (200, True), (400, False)])
def test_otp_submit_accepts_redirect_only_and_preserves_rejected_challenge(monkeypatch, status, challenge_page):
    assert hasattr(keycloak_forms, "submit_otp"), "OTP submission driver required"
    challenge = keycloak_forms.OTPChallenge("https://provider.invalid/otp", {})
    body = b'<form id="kc-otp-login-form" method="post" action="/otp"><input name="otp"></form>'
    response = httpx.Response(status, headers={"location": "https://app.invalid/callback"}, request=httpx.Request("POST", challenge.action))
    monkeypatch.setattr(keycloak_forms, "_request", lambda *args, **kwargs: (response, body))
    if status == 400:
        with pytest.raises(RuntimeError):
            keycloak_forms.submit_otp(None, challenge, "123456")
    else:
        pending, redirect = keycloak_forms.submit_otp(None, challenge, "123456")
        assert (pending is not None) == challenge_page
        assert bool(redirect) == (status == 302)


@pytest.mark.parametrize("oversized", [False, True])
def test_native_stream_is_bounded_before_decoding(oversized):
    # Synthetic driver-only test; actual-provider suites do not use MockTransport.
    payload = b"x" * (131073 if oversized else 8)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))) as browser:
        if oversized:
            with pytest.raises(RuntimeError, match="^Provider page limit$"):
                keycloak_forms._request(browser, "GET", "https://provider.invalid/form")
        else:
            response, body = keycloak_forms._request(browser, "GET", "https://provider.invalid/form")
            assert response.status_code == 200 and body == payload


@pytest.mark.parametrize("html", [
    '<form id="f" method="get">',
    '<form id="f" method="post"></form><form id="f" method="post">',
    '<form id="f" method="post"><input name="x"><input name="x">',
    '<form id="f" method="post"><input name="' + 'x' * 257 + '">',
])
def test_native_form_rejects_ambiguous_or_unbounded_controls(html):
    with pytest.raises(RuntimeError):
        keycloak_forms.NativeForm(html, "f")


@pytest.mark.parametrize("action", [None, "https://external.invalid/otp", "https://user@provider.invalid/otp", "/otp#fragment"])
def test_native_otp_action_remains_same_origin_without_userinfo_or_fragments(action):
    response = httpx.Response(200, request=httpx.Request("GET", "https://provider.invalid/form"))
    form = keycloak_forms.NativeForm('<form id="f" method="post">', "f")
    form.action = action
    with pytest.raises(RuntimeError):
        keycloak_forms._action(response, form)


def test_native_sso_get_can_return_otp_without_resending_password(monkeypatch):
    calls = []
    response = httpx.Response(200, request=httpx.Request("GET", "https://provider.invalid/form"))
    body = b'<form id="kc-otp-login-form" method="post" action="/otp"><input name="otp"></form>'

    def request(*args, **kwargs):
        calls.append(args)
        return response, body

    monkeypatch.setattr(keycloak_forms, "_request", request)
    pending, redirect = keycloak_forms.password_challenge(None, "https://provider.invalid/auth", "fictional", "private")
    assert pending is not None and redirect is None and len(calls) == 1


def test_otp_clock_rollback_fails_statically(monkeypatch):
    import keycloak_mfa

    monkeypatch.setattr(keycloak_mfa.time, "time", lambda: 60.0)
    monkeypatch.setattr(keycloak_mfa.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="^Fixture OTP clock did not advance$"):
        keycloak_mfa.otp_code({"otp_seed": b"fictional-unit-test-only", "_last_otp_step": 2})
