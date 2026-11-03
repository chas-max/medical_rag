"""Strict operator file trust configuration, independent of provider requests."""

# ruff: noqa: F401,F811 -- imported pytest fixtures
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from test_security_server import config_file, signing_key
from test_signing_keys import keys

from medical_rag.security_config import StartupError, load_configuration


def ring_config(config_file, setting='verification_key_files = {old = "public.pem", new = "new.pem"}'):
    text = config_file.read_text(encoding="utf-8")
    config_file.write_text(text.replace('public_key_file = "public.pem"', setting), encoding="utf-8")


def test_operator_ring_and_login_config_freeze_mapping(config_file, keys):
    (config_file.parent / "new.pem").write_bytes(keys[1].public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    ring_config(config_file)
    login = load_configuration(config_file).login
    assert login.public_key is None and set(login.verification_keys) == {"old", "new"}
    original = dict(login.verification_keys)
    changed = replace(login, verification_keys=original)
    original.clear()
    assert set(changed.verification_keys) == {"old", "new"}
    with pytest.raises(TypeError):
        changed.verification_keys["other"] = keys[0].public_key()
    assert "public.pem" not in repr(changed) and "verification_keys" not in repr(changed)


@pytest.mark.parametrize("setting", [
    "", 'public_key_file = "public.pem"\nverification_key_files = {old = "public.pem"}',
    'verification_key_files = {}', 'verification_key_files = "public.pem"',
    'verification_key_files = {"../CANARY" = "absent.pem"}',
    'verification_key_files = {old = "public.pem", new = "public.pem"}',
    'verification_key_files = {old = "absent-CANARY.pem"}',
    'verification_key_files = {old = 3}',
    'verification_key_files = {old = "public.pem", old = "public.pem"}',
    'verification_key_files = {' + ','.join(f'k{i} = "absent.pem"' for i in range(9)) + '}',
])
def test_bad_modes_are_static(config_file, setting):
    ring_config(config_file, setting)
    with pytest.raises(StartupError) as caught:
        load_configuration(config_file)
    assert str(caught.value) == "患者服务启动失败，请联系机构运维。"
    assert "CANARY" not in repr(caught.value)


def test_names_checked_before_any_key_file_is_opened(config_file, monkeypatch):
    ring_config(config_file, 'verification_key_files = {"../CANARY" = "absent.pem"}')
    original = Path.open
    opened = []

    def observed(path, *args, **kwargs):
        opened.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed)
    with pytest.raises(StartupError):
        load_configuration(config_file)
    assert opened == [config_file.name]


@pytest.mark.parametrize("data", [b"x" * 65537, b"PRIVATE-CANARY", b""], ids=["bound", "parser", "empty"])
def test_ring_file_bounds_and_parser_privacy(config_file, data):
    (config_file.parent / "public.pem").write_bytes(data)
    ring_config(config_file, 'verification_key_files = {old = "public.pem"}')
    with pytest.raises(StartupError) as caught:
        load_configuration(config_file)
    assert str(caught.value) == "患者服务启动失败，请联系机构运维。"
