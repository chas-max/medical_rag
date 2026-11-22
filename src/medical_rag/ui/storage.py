"""Shared SQLite plumbing for the local Streamlit application.

The application keeps three kinds of state in one local database (``UI_STATE_FILE``):
viewer/tenant session state, tenant accounts and Q&A metrics. Every store below
opens short-lived connections so several browser tabs and the metrics writer can
share the file without sharing connection state. This is local demonstration
storage, not a production multi-tenant database.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class UiStoreError(RuntimeError):
    """Raised when the local UI database cannot be read or written."""


def project_path(value: str | Path) -> Path:
    """Resolve a configured path against the project root, not the CWD."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


class UiSqliteStore:
    """Base class: lazy schema creation, busy timeout and readable errors."""

    SCHEMA = ""
    ERROR_MESSAGE = "本地界面数据库无法读写，请检查 UI_STATE_FILE 的路径、权限和磁盘空间"

    def __init__(self, path: str | Path) -> None:
        self.path = project_path(path)
        self._ready = False

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=15)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 5000")
                if not self._ready and self.SCHEMA:
                    connection.executescript(self.SCHEMA)
                    self._ready = True
                yield connection
        except (sqlite3.Error, OSError) as error:
            # A schema failure must be retried on the next call, not hidden forever.
            self._ready = False
            raise UiStoreError(self.ERROR_MESSAGE) from error
