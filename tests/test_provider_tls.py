"""Operator-only bounded provider trust, independent of environment trust."""
# ruff: noqa: F401,F811 -- imported pytest fixtures

import importlib.util
import ssl
from dataclasses import replace

import pytest
from test_oidc_login import configuration, signing_key
from test_security_server import config_file

from medical_rag.security_config import StartupError, load_configuration


def test_provider_tls_helper_exists():
    assert importlib.util.find_spec("medical_rag.provider_tls") is not None


def test_optional_relative_ca_is_private_and_validated(config_file):
    original = config_file.read_text(encoding="utf-8")
    config_file.write_text(
        original.replace("[oidc]", '[oidc]\nprovider_ca_file = "tls.pem"'),
        encoding="utf-8",
    )
    loaded = load_configuration(config_file)
    assert loaded.login.provider_ca_file == config_file.parent / "tls.pem"
    assert str(loaded.login.provider_ca_file) not in repr(loaded.login)


def test_old_configuration_retains_public_trust(config_file):
    assert load_configuration(config_file).login.provider_ca_file is None


@pytest.mark.parametrize(
    "content",
    [b"", b"not-a-certificate", b"x" * 65537, b"\xff"],
    ids=["empty", "malformed", "oversized", "nonascii"],
)
def test_invalid_ca_has_static_startup_error(config_file, content):
    config_file.write_text(
        config_file.read_text(encoding="utf-8").replace(
            "[oidc]", '[oidc]\nprovider_ca_file = "private-ca.pem"'
        ),
        encoding="utf-8",
    )
    (config_file.parent / "private-ca.pem").write_bytes(content)
    with pytest.raises(StartupError, match="^患者服务启动失败，请联系机构运维。$"):
        load_configuration(config_file)


@pytest.mark.parametrize("value", ['"absent.pem"', "true", '""'])
def test_missing_or_wrong_type_ca_denied(config_file, value):
    config_file.write_text(
        config_file.read_text(encoding="utf-8").replace(
            "[oidc]", "[oidc]\nprovider_ca_file = " + value
        ),
        encoding="utf-8",
    )
    with pytest.raises(StartupError):
        load_configuration(config_file)


def test_fresh_verified_context_and_no_environment_override(config_file, monkeypatch):
    from medical_rag.provider_tls import provider_ssl_context

    monkeypatch.setenv("SSL_CERT_FILE", "nonexistent-private-path")
    monkeypatch.setenv("SSL_CERT_DIR", "nonexistent-private-path")
    path = config_file.parent / "tls.pem"
    first, second = provider_ssl_context(path), provider_ssl_context(path)
    assert first is not second
    for context in (first, second, provider_ssl_context(None)):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert first.cert_store_stats()["x509"] == 1
    monkeypatch.setenv("SSL_CERT_FILE", str(path))
    assert provider_ssl_context(None).cert_store_stats()["x509"] > 1


def test_later_invalid_or_unreadable_ca_fails_closed(config_file, signing_key):
    from medical_rag.oidc_login import OIDCLogin

    path = config_file.parent / "tls.pem"
    login = OIDCLogin(
        replace(configuration(signing_key), provider_ca_file=path),
        lambda: None,
        tenant_id="00000000-0000-0000-0000-000000000101",
        transaction_encryption_key=b"x" * 32,
    )
    path.write_bytes(b"invalid-private-ca-CANARY")
    with pytest.raises(
        ValueError, match="^Invalid trusted provider TLS configuration$"
    ):
        login._client()
    path.unlink()
    with pytest.raises(ValueError):
        login._client()


def test_pem_bundle_rejects_extra_non_certificate_content(config_file):
    from medical_rag.provider_tls import provider_ssl_context

    path = config_file.parent / "tls.pem"
    path.write_bytes(path.read_bytes() + b"\nPRIVATE-CANARY")
    with pytest.raises(ValueError):
        provider_ssl_context(path)


def test_bundle_and_inclusive_byte_bound(config_file):
    from medical_rag.provider_tls import provider_ssl_context

    path = config_file.parent / "tls.pem"
    pem = path.read_bytes()
    path.write_bytes(pem + pem)
    assert provider_ssl_context(path).cert_store_stats()["x509"] == 1
    path.write_bytes(pem + b" " * (65536 - len(pem)))
    assert provider_ssl_context(path).cert_store_stats()["x509"] == 1


def test_wrong_direct_ca_type_is_rejected():
    from medical_rag.provider_tls import provider_ssl_context

    with pytest.raises(
        ValueError, match="^Invalid trusted provider TLS configuration$"
    ):
        provider_ssl_context("private-path")
