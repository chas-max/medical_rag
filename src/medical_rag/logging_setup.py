"""Process-wide Loguru setup: rotation, retention, compression and redaction.

Only entry points may call :func:`configure_logging`; importing this module never
touches the filesystem, so tests that import the application cannot create logs.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps import order cheap
    from .config import Settings
    from .security_config import PatientLoggingConfig

DEFAULT_LEVEL = "INFO"
DEFAULT_ROTATION = "20 MB"
DEFAULT_RETENTION = "14 days"
DEFAULT_COMPRESSION = "zip"
DEFAULT_DIRECTORY = "outputs/logs"

TEXT_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"
)

_LEVELS = frozenset({"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"})
_UNITS = "B|KB|MB|GB|TB"
_TIME_UNITS = "second|minute|hour|day|week|month|year"
_ROTATION_PATTERN = re.compile(
    rf"^(?:\d+(?:\.\d+)?\s?(?:{_UNITS})"
    rf"|\d{{1,2}}:\d{{2}}(?::\d{{2}})?"
    rf"|\d+\s(?:{_TIME_UNITS})s?"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday)$",
    re.IGNORECASE,
)
_RETENTION_PATTERN = re.compile(
    rf"^(?:\d+(?:\.\d+)?\s?(?:{_UNITS})"
    rf"|\d{{1,2}}:\d{{2}}(?::\d{{2}})?"
    rf"|\d+\s(?:{_TIME_UNITS})s?)$",
    re.IGNORECASE,
)
_COMPRESSIONS = frozenset(
    {"gz", "bz2", "xz", "zip", "tar", "tar.gz", "tar.bz2", "tar.xz"}
)

# Credentials must never reach a log file. Patterns are applied to the rendered
# message only, so structured fields keep their original meaning.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{6,}"), r"\1 ***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "***"),
    (
        re.compile(
            r"(?i)\b(password|passwd|api[_-]?key|secret|token)\b(\s*[=:]\s*)(\S+)"
        ),
        r"\1\2***",
    ),
    (re.compile(r"([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@"), r"\1***:***@"),
)

# Third-party libraries keep using stdlib logging; their records are forwarded
# into the same Loguru sinks instead of being written twice.
_THIRD_PARTY_LOGGERS = (
    "",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "httpx",
    "httpcore",
    "openai",
    "urllib3",
    "pymilvus",
    "aio_pika",
    "sentence_transformers",
    "transformers",
    "psycopg",
)


@dataclass(frozen=True)
class LoggingState:
    """What the last ``configure_logging`` call actually installed."""

    role: str
    file_path: Path | None
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.file_path is not None

    def signature(self) -> tuple[Any, ...]:
        return (
            self.role,
            str(self.file_path) if self.file_path else "",
            tuple(sorted(self.options.items())),
        )


_INTERCEPT_FILES = (logging.__file__, __file__)

_state: LoggingState | None = None
_saved_loggers: dict[str, tuple[list[logging.Handler], bool, int]] = {}


def is_valid_level(value: object) -> bool:
    return isinstance(value, str) and value.strip().upper() in _LEVELS


def is_valid_rotation(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and bool(_ROTATION_PATTERN.match(value.strip()))
    )


def is_valid_retention(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and bool(_RETENTION_PATTERN.match(value.strip()))
    )


def is_valid_compression(value: object) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.strip().lower()
    return candidate in _COMPRESSIONS or candidate in {"", "none", "off"}


def normalize_level(value: object) -> str:
    if is_valid_level(value):
        return value.strip().upper()  # type: ignore[union-attr]
    return DEFAULT_LEVEL


def normalize_rotation(value: object) -> str:
    if is_valid_rotation(value):
        return value.strip()  # type: ignore[union-attr]
    return DEFAULT_ROTATION


def normalize_retention(value: object) -> str:
    if is_valid_retention(value):
        return value.strip()  # type: ignore[union-attr]
    return DEFAULT_RETENTION


def normalize_compression(value: object) -> str:
    """``""`` means "keep rotated files uncompressed"."""
    if value is None:
        return ""
    if is_valid_compression(value):
        candidate = value.strip().lower()  # type: ignore[union-attr]
        return "" if candidate in {"", "none", "off"} else candidate
    return DEFAULT_COMPRESSION


def redact(message: str) -> str:
    """Mask credentials in one already-rendered message."""
    cleaned = message
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _patch_record(record: dict[str, Any]) -> None:
    record["message"] = redact(record["message"])
    # Intercepted stdlib records keep their own logger name (e.g. ``httpx``)
    # instead of the module that hosts the handler.
    stdlib_name = record["extra"].pop("stdlib_name", None)
    if stdlib_name:
        record["name"] = stdlib_name


def sink_options(
    *,
    level: str,
    json_output: bool,
    rotation: str = DEFAULT_ROTATION,
    retention: str = DEFAULT_RETENTION,
    compression: str = DEFAULT_COMPRESSION,
) -> dict[str, Any]:
    """File sink options. ``diagnose`` would print local variables, so it is off."""
    return {
        "level": normalize_level(level),
        "format": TEXT_FORMAT,
        "serialize": bool(json_output),
        "rotation": rotation or DEFAULT_ROTATION,
        "retention": retention or DEFAULT_RETENTION,
        "compression": compression or None,
        "enqueue": True,
        "catch": True,
        "backtrace": False,
        "diagnose": False,
        "encoding": "utf-8",
        "colorize": False,
    }


def console_options(*, level: str, json_output: bool) -> dict[str, Any]:
    options = sink_options(level=level, json_output=json_output)
    for key in ("rotation", "retention", "compression", "encoding", "colorize"):
        options.pop(key)
    return options


class InterceptHandler(logging.Handler):
    """Forward a stdlib record to Loguru without duplicating the sink."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename in _INTERCEPT_FILES:
            frame = frame.f_back
            depth += 1
        logger.bind(stdlib_name=record.name).opt(
            depth=depth, exception=record.exc_info
        ).log(level, record.getMessage())


def install_interception(level: str) -> None:
    """Route third-party stdlib loggers into Loguru exactly once per process."""
    global _saved_loggers
    if _saved_loggers:
        return
    numeric = logging.getLevelName(normalize_level(level))
    handler = InterceptHandler()
    for name in _THIRD_PARTY_LOGGERS:
        target = logging.getLogger(name)
        _saved_loggers[name] = (list(target.handlers), target.propagate, target.level)
        target.handlers = [handler] if name == "" else []
        target.propagate = True
        target.setLevel(numeric if name == "" else logging.NOTSET)


def restore_interception() -> None:
    global _saved_loggers
    for name, (handlers, propagate, level) in _saved_loggers.items():
        target = logging.getLogger(name)
        target.handlers = handlers
        target.propagate = propagate
        target.setLevel(level)
    _saved_loggers = {}


def active_sink_ids() -> tuple[int, ...]:
    """Ids of the sinks currently registered on the global Loguru logger."""
    try:
        return tuple(sorted(logger._core.handlers))  # noqa: SLF001 - read-only probe
    except AttributeError:  # pragma: no cover - defensive for future loguru versions
        return ()


def silence_logging() -> None:
    """Drop every sink; used by the patient portal and by tests."""
    logger.remove()


def flush_logging() -> None:
    """Wait for queued records so a crashing process still leaves its tail."""
    logger.complete()


def reset_logging() -> None:
    """Restore a pristine Loguru/stdlib state; intended for tests only."""
    global _state
    logger.remove()
    restore_interception()
    _state = None


def _file_path(role: str, directory: str | Path) -> Path:
    path = Path(directory).expanduser() / role / f"{role}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def configure_logging(
    role: str,
    *,
    settings: "Settings | None" = None,
    patient: "PatientLoggingConfig | None" = None,
) -> LoggingState:
    """Idempotently install the sinks for one process role.

    Roles: ``api``, ``relay``, ``ingest``, ``memory`` and ``patient``. A disabled
    patient configuration installs nothing, which keeps the portal silent.
    """
    global _state
    if patient is not None:
        return _configure_patient(role, patient)
    from .config import Settings

    active = settings or Settings()
    level = normalize_level(active.log_level)
    options = sink_options(
        level=level,
        json_output=active.log_json,
        rotation=normalize_rotation(active.log_rotation),
        retention=normalize_retention(active.log_retention),
        compression=normalize_compression(active.log_compression),
    )
    path = _file_path(role, active.log_dir) if active.log_enabled else None
    candidate = LoggingState(role=role, file_path=path, options=options)
    if _state is not None and _state.signature() == candidate.signature():
        return _state

    silence_logging()
    restore_interception()
    logger.configure(patcher=_patch_record)
    install_interception(level)
    logger.add(sys.stderr, **console_options(level=level, json_output=active.log_json))
    if path is not None:
        logger.add(path, **options)
    _state = candidate
    return _state


def _configure_patient(role: str, patient: "PatientLoggingConfig") -> LoggingState:
    global _state
    if not patient.enabled or patient.directory is None:
        if _state is None:
            _state = LoggingState(role=role, file_path=None)
        return _state
    level = normalize_level(patient.level)
    options = sink_options(
        level=level,
        json_output=patient.json_output,
        rotation=normalize_rotation(patient.rotation),
        retention=normalize_retention(patient.retention),
        compression=normalize_compression(patient.compression),
    )
    path = _file_path(role, patient.directory)
    candidate = LoggingState(role=role, file_path=path, options=options)
    if _state is not None and _state.signature() == candidate.signature():
        return _state

    silence_logging()
    restore_interception()
    logger.configure(patcher=_patch_record)
    # No console sink and no stdlib interception: the portal stays silent on
    # stderr and keeps its request-level data out of logs.
    logger.add(path, **options)
    _state = candidate
    return _state
