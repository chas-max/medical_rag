"""Focused deterministic timing and rewritten-diagnostic regressions only."""

import ast
import time
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace

import pytest
from _pytest.assertion.rewrite import rewrite_asserts

SOURCE = Path(__file__).with_name("test_provider_logout.py")
SCENARIO = "test_wrong_signature_algorithm_missing_claims_and_time_bounds"


def scenario():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == SCENARIO)


def test_independent_claims_sample_can_shorten_301_seconds_to_300(monkeypatch):
    import test_provider_logout

    epoch = 1_800_000_000
    # Replace only this test module's imported clock, never global time.time.
    monkeypatch.setattr(test_provider_logout, "time", SimpleNamespace(time=lambda: epoch + 1))
    private_payload = test_provider_logout.claims(exp=epoch + 301)
    mismatch_reproduced = private_payload["exp"] - private_payload["iat"] == 300
    assert mismatch_reproduced, "Independent payload clock must reproduce the inherited one-second lifetime mismatch"


def test_amended_logout_test_keeps_credential_fixture_out_of_arguments():
    arguments_private = "system" not in {argument.arg for argument in scenario().args.args}
    assert arguments_private, "Inherited credential fixture must be retrieved privately through request"


def test_amended_logout_assertions_are_boolean_locals_with_static_messages():
    assertions = [node for node in ast.walk(scenario()) if isinstance(node, ast.Assert)]
    private = bool(assertions) and all(
        isinstance(node.test, ast.Name) and isinstance(node.msg, ast.Constant) and isinstance(node.msg.value, str)
        for node in assertions
    )
    assert private, "Amended logout assertions must not rewrite tokens, payloads or connection tuples"


def test_rewritten_actual_logout_failures_hide_signed_token_canary():
    assertions = [node for node in ast.walk(scenario()) if isinstance(node, ast.Assert)]
    # Replay each amended assertion itself, not a look-alike assertion helper.
    for assertion in assertions:
        source = ast.unparse(ast.Module(body=[assertion], type_ignores=[])).encode()
        rewritten = ast.parse(source)
        rewrite_asserts(rewritten, source)
        private_token = "SIGNED_LOGOUT_TOKEN_CANARY"
        namespace = {
            "client": object(), "private_token": private_token,
            "private_connection": {"password": "PRIVATE_LOGOUT_DB_CANARY"},
            "rsa": SimpleNamespace(generate_private_key=lambda **kwargs: object()),
            "signed": lambda *args, token=private_token, **kwargs: token,
            "post": lambda *args: SimpleNamespace(status_code=200),
        }
        if isinstance(assertion.test, ast.Name):
            namespace[assertion.test.id] = False
        try:
            exec(compile(rewritten, "<synthetic-logout-check>", "exec"), namespace)  # noqa: S102
        except AssertionError:
            diagnostic = str(pytest.ExceptionInfo.from_current().getrepr(style="short", showlocals=False))
        else:
            pytest.fail("Synthetic actual logout check must fail")
        private = not any(canary in diagnostic for canary in (private_token, "PRIVATE_LOGOUT_DB_CANARY"))
        assert private, "Rewritten actual logout assertion exposed synthetic sensitive state"
        static = (isinstance(assertion.msg, ast.Constant) and assertion.msg.value in diagnostic
                  and "assert False" in diagnostic)
        assert static, "Rewritten logout failure must retain its static case label and boolean proof"


def test_fixed_clock_is_shared_scoped_and_restored(monkeypatch):
    import jwt.api_jwt
    from provider_logout_clock import freeze_logout_clock

    from medical_rag import provider_logout

    original_global = time.time
    original_provider = provider_logout.time
    original_jwt = jwt.api_jwt.datetime
    epoch = 1_800_000_000
    with monkeypatch.context() as scoped:
        freeze_logout_clock(scoped, epoch)
        same_epoch = provider_logout.time() == epoch and jwt.api_jwt.datetime.now(UTC).timestamp() == epoch
        assert same_epoch, "Provider verifier and PyJWT UTC must share the exact fixed epoch"
        untouched = time.time is original_global
        assert untouched, "Global time must remain untouched for database, asyncio and unrelated threads"
    restored = provider_logout.time is original_provider and jwt.api_jwt.datetime is original_jwt
    assert restored, "Both verifier clock references must be restored after the test scope"


@pytest.mark.parametrize("iat_offset,exp_offset,expected", [
    (0, 120, True), (0, 300, True), (0, 301, False), (1, 120, False),
    (-120, 120, True), (-121, 120, False), (0, 0, False), (0, -1, False), (-1, -1, False),
], ids=["current", "lifetime-300", "lifetime-301", "future-one", "age-120", "age-121",
        "expiration-now", "expiration-past", "nonpositive-lifetime"])
def test_fixed_epoch_exact_verifier_boundaries(monkeypatch, iat_offset, exp_offset, expected):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from provider_logout_clock import freeze_logout_clock

    from medical_rag.provider_logout import (
        InvalidProviderLogout,
        ProviderLogoutVerifier,
    )

    epoch = 1_800_000_000
    freeze_logout_clock(monkeypatch, epoch)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = ProviderLogoutVerifier(issuer="https://synthetic.example", audience="synthetic", public_key=private_key.public_key())
    private_payload = {"iss": "https://synthetic.example", "aud": "synthetic", "iat": epoch + iat_offset,
                       "exp": epoch + exp_offset, "jti": "synthetic", "sub": "synthetic",
                       "events": {"http://schemas.openid.net/event/backchannel-logout": {}}}
    private_token = jwt.encode(private_payload, private_key, algorithm="RS256", headers={"typ": "logout+jwt"})
    try:
        verifier.verify(private_token)
    except InvalidProviderLogout:
        accepted = False
    else:
        accepted = True
    boundary_matches = accepted is expected
    assert boundary_matches, "Exact fixed-epoch boundary must retain the existing verifier policy"
