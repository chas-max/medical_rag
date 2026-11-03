"""Failure diagnostics from the actual rotation checks, with synthetic inputs only."""

import ast
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from _pytest.assertion.rewrite import rewrite_asserts

ROTATION_SOURCE = Path(__file__).with_name("test_keycloak_rotation.py")
OLD_KID = "SYNTHETIC_OLD_HEADER_KID_CANARY"
NEW_KID = "SYNTHETIC_NEW_HEADER_KID_CANARY"
PUBLIC_KEY = "SYNTHETIC_RING_PUBLIC_KEY_CANARY"
NONCE = "SYNTHETIC_NONCE_CANARY"
TOKEN_MARKER = "SYNTHETIC_TOKEN_CANARY"
TOKEN = jwt.encode(
    {"sub": TOKEN_MARKER},
    "synthetic-diagnostic-only-not-an-identity-key",
    algorithm="HS256",
    headers={"kid": NEW_KID},
)
CANARIES = (OLD_KID, NEW_KID, PUBLIC_KEY, NONCE, TOKEN_MARKER, TOKEN)


def rotation_checks():
    """Read, never import/run, the actual-provider scenario's assertion source."""
    tree = ast.parse(ROTATION_SOURCE.read_text(encoding="utf-8"))
    scenario = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_actual_operator_rotation_overlap_and_retired_token"
    )
    checks = []
    for parent in ast.walk(scenario):
        for value in vars(parent).values():
            if not isinstance(value, list):
                continue
            for index, node in enumerate(value):
                if isinstance(node, ast.Assert):
                    # Include the actual boolean computation after the fix, not
                    # a separate demonstration that could drift from this test.
                    statements = [node]
                    if isinstance(node.test, ast.Name) and index:
                        previous = value[index - 1]
                        if (
                            isinstance(previous, ast.Assign)
                            and len(previous.targets) == 1
                            and isinstance(previous.targets[0], ast.Name)
                            and previous.targets[0].id == node.test.id
                        ):
                            statements.insert(0, previous)
                    checks.append((node.lineno, statements))
    return [statements for _, statements in sorted(checks)]


def test_actual_provider_assertions_use_only_boolean_locals_and_static_messages():
    checks = rotation_checks()
    contract_is_private = len(checks) == 16 and all(
        len(statements) == 2
        and isinstance(statements[-1].test, ast.Name)
        and isinstance(statements[-1].msg, ast.Constant)
        and isinstance(statements[-1].msg.value, str)
        for statements in checks
    )
    assert contract_is_private, "Actual rotation assertions must use boolean locals and fixed messages"


@pytest.mark.parametrize(
    "check_index",
    [1, 2, 4, 9, 12, 15],
    ids=[
        "initial-verifier-false-hardening",
        "overlap-count",
        "old-logout-header",
        "new-logout-membership",
        "overlap-verifier-false-hardening",
        "original-key-set",
    ],
)
def test_actual_rotation_rewritten_failures_exclude_synthetic_canaries(check_index):
    # False-returning verifiers test defensive diagnostics only: the actual
    # verifier returns a truthy identity or raises static InvalidIdentity.
    verifier = SimpleNamespace(verify=lambda *args, **kwargs: False)
    namespace = {
        "jwt": jwt,
        "login": SimpleNamespace(_verifier=verifier),
        "ring_login": SimpleNamespace(_verifier=verifier),
        "old_token": TOKEN,
        "old_logout": TOKEN,
        "nonce": NONCE,
        "old_kid": OLD_KID,
        "new_kid": NEW_KID,
        "overlap": {OLD_KID: PUBLIC_KEY},
        "old_keys": {NEW_KID: PUBLIC_KEY},
    }
    statements = rotation_checks()[check_index]
    source = ast.unparse(ast.Module(body=statements, type_ignores=[])).encode()
    rewritten = ast.parse(source)
    rewrite_asserts(rewritten, source)
    try:
        # Only the checked-in assertion/check AST executes, with synthetic data.
        exec(compile(rewritten, "<synthetic-actual-rotation-check>", "exec"), namespace)  # noqa: S102
    except AssertionError:
        diagnostic = str(
            pytest.ExceptionInfo.from_current().getrepr(style="short", showlocals=False)
        )
    else:
        pytest.fail("Synthetic actual rotation check must fail")
    diagnostic_is_private = not any(canary in diagnostic for canary in CANARIES)
    assert diagnostic_is_private, "Rewritten actual rotation failure exposed a synthetic canary"
    fixed_diagnostic_present = (
        statements[-1].msg.value in diagnostic and "assert False" in diagnostic
    )
    assert fixed_diagnostic_present, "Synthetic failure must retain the actual fixed assertion message"
