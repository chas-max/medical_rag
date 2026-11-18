"""Bounded trusted configuration; never imports legacy Settings or dotenv."""

import ipaddress
import ssl
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .dialogue_flags import dialogue_enabled
from .logging_setup import (
    DEFAULT_COMPRESSION,
    DEFAULT_LEVEL,
    DEFAULT_RETENTION,
    DEFAULT_ROTATION,
    is_valid_compression,
    is_valid_level,
    is_valid_retention,
    is_valid_rotation,
)
from .oidc_login import LoginConfiguration
from .patient_chat import ChatClient
from .patient_service import HealthNotice
from .signing_keys import validate_key_names


class StartupError(Exception):
    """Only this static message may cross the startup boundary."""

    def __str__(self) -> str:
        return "患者服务启动失败，请联系机构运维。"


@dataclass(frozen=True, slots=True)
class PatientLoggingConfig:
    """Operator-owned portal log switches; disabled unless the TOML says otherwise."""

    enabled: bool = False
    directory: Path | None = None
    level: str = DEFAULT_LEVEL
    rotation: str = DEFAULT_ROTATION
    retention: str = DEFAULT_RETENTION
    compression: str = DEFAULT_COMPRESSION
    json_output: bool = False


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
    chat: ChatClient | None = field(default=None, repr=False)
    # Read from the operator process environment, never from TOML or the legacy
    # demo .env file this service deliberately does not load.
    dialogue_state_enabled: bool = False
    # Off by default: an installed portal keeps the synchronous route until an
    # operator has verified streaming end to end.
    chat_streaming: bool = False
    # Portal file logging stays opt-in: the default keeps stderr and disk silent
    # because request-level evidence belongs in PostgreSQL audit rows, not logs.
    logging_config: PatientLoggingConfig = field(default_factory=PatientLoggingConfig)


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


def _bounded_text(value: object, limit: int = 32) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit or "\x00" in value:
        raise StartupError()
    return value.strip()


def _logging_options(value: object, base: Path) -> PatientLoggingConfig:
    """Reject anything that is not an explicit, bounded logging request."""
    keys = _keys(value, "enabled", "directory level rotation retention compression json")
    if type(keys["enabled"]) is not bool:
        raise StartupError()
    if not keys["enabled"]:
        return PatientLoggingConfig()
    if "directory" not in keys:
        raise StartupError()
    directory = (base / _bounded_text(keys["directory"], 4096)).resolve()
    if directory.exists() and not directory.is_dir():
        raise StartupError()
    level = _bounded_text(keys.get("level", DEFAULT_LEVEL))
    rotation = _bounded_text(keys.get("rotation", DEFAULT_ROTATION))
    retention = _bounded_text(keys.get("retention", DEFAULT_RETENTION))
    compression = keys.get("compression", DEFAULT_COMPRESSION)
    if type(compression) is not str or len(compression) > 16:
        raise StartupError()
    if not (
        is_valid_level(level)
        and is_valid_rotation(rotation)
        and is_valid_retention(retention)
        and is_valid_compression(compression)
    ):
        raise StartupError()
    json_output = keys.get("json", False)
    if type(json_output) is not bool:
        raise StartupError()
    normalized_compression = compression.strip().lower()
    return PatientLoggingConfig(
        enabled=True,
        directory=directory,
        level=level.upper(),
        rotation=rotation,
        retention=retention,
        compression="" if normalized_compression in {"", "none", "off"} else normalized_compression,
        json_output=json_output,
    )


def load_configuration(path: Path) -> PatientConfiguration:
    """Resolve all file paths relative to this operator-owned TOML file."""
    try:
        path = Path(path).resolve(strict=True)
        data = _keys(
            tomllib.loads(_read(path, 65536).decode("utf-8")),
            "tenant_id database_dsn_file transaction_key_file frontend_dist listen_host listen_port "
            "tls_cert_file tls_key_file oidc health_notice",
            "chat logging",
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
        chat, streaming = None, False
        if "chat" in data:
            settings = _keys(data["chat"], "base_url timeout_seconds top_k", "streaming")
            timeout, top_k = settings["timeout_seconds"], settings["top_k"]
            streaming = settings.get("streaming", False)
            if (type(timeout) is not int or not 1 <= timeout <= 300
                    or type(top_k) is not int or not 1 <= top_k <= 20
                    or type(streaming) is not bool):
                raise StartupError()
            try:
                chat = ChatClient(_string(settings["base_url"]), timeout, top_k)
            except ValueError:
                raise StartupError() from None

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
            chat,
            dialogue_enabled(),
            streaming,
            _logging_options(data["logging"], path.parent)
            if "logging" in data
            else PatientLoggingConfig(),
        )
    except Exception:  # noqa: BLE001 -- secret-bearing parser/crypto errors stay private
        raise StartupError() from None
