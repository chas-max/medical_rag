"""Bounded trusted configuration; never imports legacy Settings or dotenv."""

import ipaddress
import ssl
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .oidc_login import LoginConfiguration
from .patient_service import HealthNotice
from .signing_keys import validate_key_names


class StartupError(Exception):
    """Only this static message may cross the startup boundary."""

    def __str__(self) -> str:
        return "患者服务启动失败，请联系机构运维。"


@dataclass(frozen=True, slots=True)
class PatientConfiguration:
    tenant_id: str
    database_dsn: str = field(repr=False)
    transaction_key: bytes = field(repr=False)
    frontend_dist: Path
    listen_host: str
    listen_port: int
    tls_cert_file: Path = field(repr=False)
    tls_key_file: Path = field(repr=False)
    login: LoginConfiguration = field(repr=False)
    notice: HealthNotice = field(repr=False)


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if not data or len(data) > limit:
        raise StartupError()
    return data


def _keys(value: object, expected: str, optional: str = "") -> dict:
    required = set(expected.split())
    if type(value) is not dict or not required <= set(value) or set(value) - required - set(optional.split()):
        raise StartupError()
    return value


def _string(value: object) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > 65536
        or "\x00" in value
    ):
        raise StartupError()
    return value


def load_configuration(path: Path) -> PatientConfiguration:
    """Resolve all file paths relative to this operator-owned TOML file."""
    try:
        path = Path(path).resolve(strict=True)
        data = _keys(
            tomllib.loads(_read(path, 65536).decode("utf-8")),
            "tenant_id database_dsn_file transaction_key_file frontend_dist listen_host listen_port "
            "tls_cert_file tls_key_file oidc health_notice",
        )
        oidc = _keys(
            data["oidc"],
            "issuer authorization_endpoint token_endpoint callback_uri client_id "
            "client_secret_file mfa_acr_values",
            "provider_ca_file public_key_file verification_key_files",
        )
        if ("public_key_file" in oidc) == ("verification_key_files" in oidc):
            raise StartupError()
        if "verification_key_files" in oidc:
            validate_key_names(oidc["verification_key_files"])
        notice = _keys(data["health_notice"], "version text_file validity_seconds")

        def file(value: object) -> Path:
            return (path.parent / _string(value)).resolve(strict=True)

        def text(value: object, limit: int) -> str:
            return _string(_read(file(value), limit).decode("utf-8").strip())

        tenant = str(UUID(_string(data["tenant_id"])))
        host = str(ipaddress.ip_address(_string(data["listen_host"])))
        if ipaddress.ip_address(host).is_unspecified:
            raise StartupError()
        port = data["listen_port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise StartupError()
        acr = oidc["mfa_acr_values"]
        if (
            type(acr) is not list
            or not 1 <= len(acr) <= 32
            or len(set(map(_string, acr))) != len(acr)
        ):
            raise StartupError()
        login = LoginConfiguration(
            **{
                key: _string(oidc[key])
                for key in (
                    "issuer",
                    "authorization_endpoint",
                    "token_endpoint",
                    "callback_uri",
                    "client_id",
                )
            },
            client_secret=text(oidc["client_secret_file"], 16384),
            public_key=(load_pem_public_key(_read(file(oidc["public_key_file"]), 65536))
                        if "public_key_file" in oidc else None),
            verification_keys=({name: load_pem_public_key(_read(file(value), 65536))
                                for name, value in oidc["verification_key_files"].items()}
                               if "verification_key_files" in oidc else None),
            mfa_acr_values=frozenset(acr),
            provider_ca_file=file(oidc["provider_ca_file"]) if "provider_ca_file" in oidc else None,
        )
        encryption = _read(file(data["transaction_key_file"]), 32)
        if len(encryption) != 32:
            raise StartupError()
        cert, key = file(data["tls_cert_file"]), file(data["tls_key_file"])
        # Validate eagerly, before Uvicorn can print credential-path errors.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key, password=lambda: b"")
        return PatientConfiguration(
            tenant,
            text(data["database_dsn_file"], 16384),
            encryption,
            file(data["frontend_dist"]),
            host,
            port,
            cert,
            key,
            login,
            HealthNotice(
                _string(notice["version"]),
                text(notice["text_file"], 65536),
                notice["validity_seconds"],
            ),
        )
    except Exception:  # noqa: BLE001 -- secret-bearing parser/crypto errors stay private
        raise StartupError() from None
