"""Incremental parsing of the streamed claim document.

The generation prompt fixes one shape: a single JSON object whose only key holds
the claim array. Streaming needs each claim the moment it is complete, so this
parser tracks quoting and container depth instead of trying to decode a
truncated document. It only ever reports finished array elements, and a fragment
that is not valid JSON is skipped rather than raised: the final answer is still
produced by the one authoritative validation step over the full draft.
"""

import json
from typing import Any

CLAIMS_KEY = "claims"


class ClaimStreamParser:
    """Collect complete claim objects from JSON that arrives in fragments."""

    def __init__(self) -> None:
        self.buffer = ""
        self._cursor = 0
        self._depth = 0
        self._in_string = False
        self._escaped = False
        self._string_start = 0
        self._root_key: str | None = None
        self._value_pending = False
        self._array_level: int | None = None
        self._claim_start: int | None = None
        self._closed = False

    def feed(self, delta: str) -> list[dict[str, Any]]:
        """Append a fragment and return every claim that finished inside it."""
        if not delta:
            return []
        self.buffer += delta
        claims: list[dict[str, Any]] = []
        self._scan(claims)
        return claims

    @property
    def claims_complete(self) -> bool:
        """Report whether the claim array has been closed by a complete ``]``."""
        return self._closed

    def _scan(self, claims: list[dict[str, Any]]) -> None:
        buffer = self.buffer
        while self._cursor < len(buffer) and not self._closed:
            index = self._cursor
            character = buffer[index]
            self._cursor = index + 1
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif character == "\\":
                    self._escaped = True
                elif character == '"':
                    self._in_string = False
                    self._string_ended(buffer[self._string_start:index])
                continue
            if character.isspace():
                continue
            if character == '"':
                # A string that opens where a root value is expected is a value,
                # not the key that names the claim array.
                if self._depth == 1 and self._value_pending:
                    self._value_pending = False
                self._in_string = True
                self._string_start = index + 1
            elif character == ":":
                if self._depth == 1:
                    self._value_pending = True
            elif character in "{[":
                pending = self._value_pending
                self._value_pending = False
                self._depth += 1
                if pending and character == "[" and self._root_key == CLAIMS_KEY:
                    self._array_level = self._depth
                elif (character == "{" and self._claim_start is None
                      and self._array_level is not None and self._depth == self._array_level + 1):
                    self._claim_start = index
            elif character in "}]":
                closing_depth = self._depth
                self._depth -= 1
                if (self._array_level is not None and self._claim_start is not None
                        and character == "}" and closing_depth == self._array_level + 1):
                    self._finish_claim(index, claims)
                elif self._array_level is not None and character == "]" and closing_depth == self._array_level:
                    self._closed = True
                elif self._array_level is None and character == "}" and closing_depth == 1:
                    # The root object closed without a claim array to watch.
                    self._closed = True
            elif self._value_pending:
                self._value_pending = False

    def _string_ended(self, token: str) -> None:
        """Remember the most recent root-level key, ignoring nested strings."""
        if self._depth == 1:
            self._root_key = token

    def _finish_claim(self, index: int, claims: list[dict[str, Any]]) -> None:
        start = self._claim_start
        self._claim_start = None
        if start is None:
            return
        try:
            claim = json.loads(self.buffer[start:index + 1])
        except ValueError:
            return
        if isinstance(claim, dict):
            claims.append(claim)
