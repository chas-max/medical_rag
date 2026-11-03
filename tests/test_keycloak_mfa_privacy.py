"""Replay actual MFA assertion AST with synthetic sensitive canaries only."""

import ast
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from _pytest.assertion import util as assertion_util
from _pytest.assertion.rewrite import rewrite_asserts

SOURCE = Path(__file__).with_name("test_keycloak_mfa.py")


def test_new_acr_red_diagnostics_do_not_repr_credential_bearing_fixture_argument():
    tree = ast.parse(SOURCE.with_name("test_oidc_login.py").read_text(encoding="utf-8"))
    scenario = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "test_login_requests_only_sorted_trusted_nonessential_acr")
    private = "system" not in {argument.arg for argument in scenario.args.args}
    assert private, "New ACR test must not expose the legacy credential-bearing fixture tuple in pytest argument diagnostics"


def checks(source=SOURCE, scenario=None):
    tree = ast.parse(source.read_text(encoding="utf-8"))
    if scenario is not None:
        tree = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == scenario)
    result = {}
    for parent in ast.walk(tree):
        for value in vars(parent).values():
            if not isinstance(value, list):
                continue
            for index, node in enumerate(value):
                if isinstance(node, ast.Assert):
                    statements = [node]
                    if isinstance(node.test, ast.Name) and index and isinstance(value[index - 1], ast.Assign):
                        statements.insert(0, value[index - 1])
                    result[getattr(node.test, "id", str(node.lineno))] = statements
    return result


def composer_checks():
    assertions = checks(SOURCE.with_name("test_keycloak_mfa_support.py"),
                        "test_fixture_composer_uses_same_public_policy_and_memory_only_credentials")
    return [items for items in assertions.values()
            if any(isinstance(node, ast.Name) and node.id == "users"
                   for statement in items for node in ast.walk(statement))]


def test_composer_sensitive_checks_are_boolean_locals_and_static_messages():
    assertions = composer_checks()
    private = len(assertions) == 2 and all(
        len(items) == 2 and isinstance(items[-1].test, ast.Name)
        and isinstance(items[-1].msg, ast.Constant) and isinstance(items[-1].msg.value, str)
        for items in assertions)
    assert private, "Credential-bearing composer checks must use boolean locals and static messages"


@pytest.mark.parametrize("failure", ["missing_user", "unexpected_unenrolled_seed"])
def test_rewritten_actual_composer_failures_never_dump_credentials(failure, monkeypatch):
    # Exercise default rewritten diagnostics too; set-diff hooks can hide call arguments.
    monkeypatch.setattr(assertion_util, "_reprcompare", None)
    password, seed = "PRIVATE_PASSWORD_CANARY", b"PRIVATE_SEED_CANARY"
    users = {name: {"password": password, "otp_seed": seed}
             for name in ("staff", "staff_norole", "staff_unenrolled")}
    if failure == "missing_user":
        del users["staff_norole"]
        del users["staff_unenrolled"]["otp_seed"]
    statements = composer_checks()[0 if failure == "missing_user" else 1]
    source = ast.unparse(ast.Module(body=statements, type_ignores=[])).encode()
    rewritten = ast.parse(source)
    rewrite_asserts(rewritten, source)
    try:
        exec(compile(rewritten, "<synthetic-composer-check>", "exec"), {"users": users})  # noqa: S102
    except AssertionError:
        diagnostic = str(pytest.ExceptionInfo.from_current().getrepr(style="short", showlocals=False))
    else:
        pytest.fail("Synthetic actual composer check must fail")
    safe = not any(value in diagnostic for value in (
        password, seed.decode(), seed.hex(), base64.b32encode(seed).decode().rstrip("=")))
    assert safe, "Rewritten actual composer assertion exposed synthetic sensitive values"
    fixed = isinstance(statements[-1].msg, ast.Constant) and statements[-1].msg.value in diagnostic and "assert False" in diagnostic
    assert fixed, "Synthetic composer diagnostic must retain the actual static assertion message"


def test_consumed_callback_replay_supplies_original_pending_browser_proof():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    scenario = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "test_actual_sso_reauthentication_requires_otp_again_and_stale_challenge_denied")
    calls = [node for node in ast.walk(scenario) if isinstance(node, ast.Call)]
    replay = next((node for node in calls if isinstance(node.func, ast.Attribute)
                   and node.func.attr == "get" and node.args and isinstance(node.args[0], ast.Name)
                   and node.args[0].id == "first_callback"), None)
    supplied = replay is not None and any(
        keyword.arg == "headers" and any(isinstance(node, ast.Name) and node.id == "original_pending_cookie"
                                         for node in ast.walk(keyword.value))
        for keyword in replay.keywords)
    assert supplied, "Consumed first callback replay must supply original pending browser proof explicitly"


def test_actual_mfa_checks_are_boolean_locals_and_static_messages():
    assertions = checks().values()
    private = all(len(items) == 2 and isinstance(items[-1].test, ast.Name)
                  and isinstance(items[-1].msg, ast.Constant) and isinstance(items[-1].msg.value, str)
                  for items in assertions)
    assert private, "All actual MFA checks must use boolean locals and static messages"


@pytest.mark.parametrize("name", ["credentials_private", "seed_base32_private", "tokens_private", "otp_private"])
def test_rewritten_privacy_assertions_never_dump_sensitive_values(name):
    all_checks = checks()
    present = name in all_checks
    assert present, "Actual MFA privacy checks must cover encoded seed, raw JWT and the used OTP"
    password, cookie, raw_logout, code = ("PRIVATE_PASSWORD_CANARY", "PRIVATE_COOKIE_CANARY", "PRIVATE_JWT_CANARY", "654321")
    seed = b"PRIVATE_SEED_CANARY"
    encoded = base64.b32encode(seed).decode().rstrip("=")
    exposed = {"credentials_private": password, "seed_base32_private": encoded,
               "tokens_private": raw_logout, "otp_private": code}[name]
    namespace = {"base64": base64, "provider": SimpleNamespace(users={"staff": {"password": password, "otp_seed": seed}}),
                 "exposed": exposed, "cookie": cookie, "logout_token": raw_logout,
                 "body": ("logout_token=" + raw_logout).encode(), "used_code": code}
    statements = all_checks[name]
    source = ast.unparse(ast.Module(body=statements, type_ignores=[])).encode()
    rewritten = ast.parse(source)
    rewrite_asserts(rewritten, source)
    try:
        exec(compile(rewritten, "<synthetic-mfa-check>", "exec"), namespace)  # noqa: S102
    except AssertionError:
        diagnostic = str(pytest.ExceptionInfo.from_current().getrepr(style="short", showlocals=False))
    else:
        pytest.fail("Synthetic actual MFA privacy check must fail")
    safe = not any(value in diagnostic for value in (password, cookie, raw_logout, code, seed.hex(), encoded))
    assert safe, "Rewritten actual MFA assertion exposed synthetic sensitive values"
    fixed = statements[-1].msg.value in diagnostic and "assert False" in diagnostic
    assert fixed, "Synthetic diagnostic must retain the actual static assertion message"
