"""Operator-process feature switch for the patient dialogue path.

The patient security modules deliberately never import the legacy ``Settings`` or
``dotenv``: their configuration must come from the operator's process environment,
not from a local demo ``.env`` file that the patient service may not even read.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

DIALOGUE_FLAG = "DIALOGUE_STATE_ENABLED"
_TRUE = frozenset({"true", "1"})
_FALSE = frozenset({"false", "0"})


def dialogue_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Read the dialogue feature switch from the operator environment only.

    Args:
        environ: Explicit environment mapping, defaulting to ``os.environ``.

    Returns:
        ``True`` only for the explicit enabling spellings.

    Raises:
        ValueError: For any other value; a typo must stop startup rather than
            silently run with the feature in an unexpected state. The error never
            repeats the configured value.
    """
    source = os.environ if environ is None else environ
    value = source.get(DIALOGUE_FLAG, "false").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError("Invalid dialogue feature setting")


__all__ = ["DIALOGUE_FLAG", "dialogue_enabled"]
