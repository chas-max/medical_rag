"""Diagnostic privacy of test infrastructure, not an upstream provider fake."""

import subprocess
from types import SimpleNamespace

import keycloak_runtime
import pytest
from keycloak_runtime import docker, identity_runtime


@pytest.mark.parametrize("failure", ["timeout", "oserror", "exit"])
def test_infrastructure_errors_do_not_disclose_command_or_output(monkeypatch, failure):
    def fail(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(
                ["docker", "PRIVATE-CANARY"], 30, output="PRIVATE-CANARY"
            )
        if failure == "oserror":
            raise OSError("PRIVATE-CANARY")
        return SimpleNamespace(
            returncode=1, stdout="PRIVATE-CANARY", stderr="PRIVATE-CANARY"
        )

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError) as error:
        docker("PRIVATE-CANARY")
    assert str(error.value) == "Disposable identity infrastructure command failed"
    assert error.value.__suppress_context__ or failure == "exit"


def test_explicit_provider_selection_without_docker_is_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("RUN_KEYCLOAK_INTEGRATION", "1")
    monkeypatch.setattr("keycloak_runtime.shutil.which", lambda _: None)
    with (
        pytest.raises(
            pytest.fail.Exception,
            match="Actual Keycloak suite selected but Docker unavailable",
        ),
        identity_runtime(tmp_path),
    ):
        pytest.fail("Missing infrastructure unexpectedly yielded")


@pytest.mark.parametrize("failure", ["inspect", "remove", "ownership", "none"])
@pytest.mark.parametrize("original_failure", [True, False])
@pytest.mark.parametrize("failing_target", ["first", "network"])
def test_cleanup_attempts_each_owned_target_and_preserves_original(monkeypatch, failure, original_failure, failing_target):
    calls = []

    def command(*args):
        calls.append(args)
        if failing_target in args:
            if "inspect" in args and failure == "inspect":
                raise RuntimeError("PRIVATE-CANARY")
            if "rm" in args and failure == "remove":
                raise RuntimeError("PRIVATE-CANARY")
            if failure == "ownership":
                return "unowned"
        return "owner"

    monkeypatch.setattr(keycloak_runtime, "docker", command)
    original = ValueError("original") if original_failure else None
    if failure != "none" and original is None:
        with pytest.raises(RuntimeError, match="^Disposable identity cleanup failed$"):
            keycloak_runtime.cleanup_owned(["last", "first"], "network", "owner", original)
    else:
        keycloak_runtime.cleanup_owned(["last", "first"], "network", "owner", original)
    assert ("rm", "-f", "last") in calls
    if failing_target == "first" or failure not in ("inspect", "ownership"):
        assert ("network", "rm", "network") in calls
    if failure in ("inspect", "ownership"):
        assert (("rm", "-f", "first") if failing_target == "first" else ("network", "rm", "network")) not in calls
    if original is not None:
        assert getattr(original, "__notes__", []) == (["Disposable identity cleanup failed"] if failure != "none" else [])


@pytest.mark.parametrize("failure", ["timeout", "exit", "bound", "none"])
def test_realm_import_is_bounded_stdin_not_host_artifact(monkeypatch, tmp_path, failure):
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 30, output=b"PRIVATE-CANARY")
        return SimpleNamespace(returncode=int(failure == "exit"), stdout=b"PRIVATE-CANARY", stderr=b"PRIVATE-CANARY")

    monkeypatch.setattr(subprocess, "run", command)
    body = b"x" * 65537 if failure == "bound" else b'{"credential":"PRIVATE-CANARY"}'
    if failure != "none":
        with pytest.raises(RuntimeError, match="^Disposable identity import failed$") as caught:
            keycloak_runtime.import_realm("fixture-id", body)
        assert "PRIVATE-CANARY" not in str(caught.value)
    else:
        keycloak_runtime.import_realm("fixture-id", body)
    assert not list(tmp_path.iterdir())
    if failure == "bound":
        assert calls == []
    else:
        args, kwargs = calls[0]
        assert "PRIVATE-CANARY" not in repr(args)
        assert kwargs["input"] == body + b"\n" and kwargs["capture_output"] is True
