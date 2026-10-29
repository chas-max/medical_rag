"""Recover the JSON payload from model output that is not strictly clean JSON.

OpenAI-compatible endpoints do not all honour ``response_format``: a model may wrap
the object in a Markdown fence, say one sentence before it, leave a dangling comma,
or be cut off by the endpoint's ``max_tokens``. Decoding such an output straight
with ``json.loads`` turned a generation fault into a fake "insufficient evidence"
abstention, so the payload is extracted here before the citation validator decides
whether the answer is usable.

Recovery is deterministic and local (no extra dependency): a candidate is accepted
only when it decodes to a JSON object. The repair is reported so the caller can
record why the text needed help, and an output that is simply unusable still
returns ``None`` instead of inventing content.
"""

import json
import re
from dataclasses import dataclass

# Why a candidate needed repair. These labels travel into the run trace, so keep
# them stable: they are the difference between "the model was truncated" and
# "the model answered in prose".
REPAIR_FENCE = "fence"
REPAIR_PROSE = "prose"
REPAIR_TRAILING_COMMA = "trailing_comma"
REPAIR_TRUNCATED = "truncated"
REPAIR_CLAIMS_LIST = "claims_list"

# The claim array the prompt asks for. Chinese endpoints occasionally emit a
# full-width colon, which is just as scannable as the ASCII one here.
_CLAIMS_ARRAY = re.compile(r'"claims"\s*[:：]\s*\[')

_CLOSERS = {"{": "}", "[": "]"}


@dataclass(frozen=True)
class ModelJSON:
    """A decoded object plus the repair it needed, if any."""

    payload: dict | None
    repair: str | None = None

    @property
    def recovered(self) -> bool:
        return self.payload is not None


def parse_model_json(text: object) -> ModelJSON:
    """Decode the first usable JSON object in ``text``.

    A clean object wins. Otherwise fences are tried first (they mark the JSON
    explicitly), then the surrounding text (first balanced object), and finally a
    truncated document is salvaged by keeping only the claims that were written in
    full.
    """
    if not isinstance(text, str):
        return ModelJSON(None)
    raw = text.strip().lstrip("\ufeff")
    if not raw:
        return ModelJSON(None)
    payload = _loads_object(raw)
    if payload is not None:
        return ModelJSON(payload)
    for body in _fenced_blocks(raw):
        recovered = _recover_body(body, REPAIR_FENCE, force_label=True)
        if recovered is not None:
            return recovered
    recovered = _recover_body(raw, REPAIR_PROSE, force_label=False)
    return recovered if recovered is not None else ModelJSON(None)


def _recover_body(body: str, default_repair: str, *, force_label: bool) -> ModelJSON | None:
    """Try the body as given, then only the balanced object inside it.

    Whole-object decoding runs across both candidates before truncation salvage:
    a fenced or explained document that is actually complete must be reported as
    ``fence``/``prose``, not as the heavier ``truncated`` repair.
    """
    candidates = [(body, False)]
    start = body.find("{")
    if start >= 0:
        end = _balanced_slice(body, start)
        candidates.append((body[start:end] if end is not None else body[start:], True))
    for candidate, trimmed in candidates:
        payload, repair = _decode_object(candidate)
        if payload is not None:
            return ModelJSON(payload, repair or (default_repair if force_label or trimmed else None))
    for candidate, _trimmed in candidates:
        claims = _recover_claims(candidate)
        if claims:
            return ModelJSON({"claims": claims}, REPAIR_TRUNCATED)
    return None


def _decode_object(text: str) -> tuple[dict | None, str | None]:
    """Decode a whole object, reporting which surface repair made it valid."""
    value = _loads_object(text)
    if value is not None:
        return value, None
    without_commas = _drop_trailing_commas(text)
    if without_commas != text:
        value = _loads_object(without_commas)
        if value is not None:
            return value, REPAIR_TRAILING_COMMA
    claims = _loads_claims_list(text)
    if claims is not None:
        return {"claims": claims}, REPAIR_CLAIMS_LIST
    return None, None


def _fenced_blocks(text: str) -> list[str]:
    """Every Markdown fence body, including a fence the model never closed."""
    blocks = []
    index = 0
    while True:
        opening = text.find("```", index)
        if opening < 0:
            break
        body_start = opening + len("```")
        # Skip the optional language tag ("json") and the rest of the info line.
        match = re.match(r"[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n?", text[body_start:])
        if match is not None:
            body_start += match.end()
        closing = text.find("```", body_start)
        if closing < 0:
            blocks.append(text[body_start:])
            break
        blocks.append(text[body_start:closing])
        index = closing + len("```")
    return [block for block in blocks if block.strip()]


def _balanced_slice(text: str, start: int) -> int | None:
    """End index (exclusive) of the container opening at ``start``, or ``None``."""
    opener = text[start]
    if opener not in _CLOSERS:
        return None
    stack: list[str] = []
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in _CLOSERS:
            stack.append(_CLOSERS[character])
        elif character in "}]":
            if not stack or stack.pop() != character:
                return None
            if not stack:
                return index + 1
    return None


def _recover_claims(text: str) -> list[dict]:
    """Keep every claim object the model finished writing before it was cut off.

    Truncation almost always happens inside the last claim (or later, inside the
    analysis section). The claims that were completed before that point are still
    anchored and valid, so they are returned instead of discarding the whole answer.
    """
    match = _CLAIMS_ARRAY.search(text)
    if match is None:
        return []
    return _claim_objects(text, match.end() - 1)


def _claim_objects(text: str, array_start: int) -> list[dict]:
    claims: list[dict] = []
    index = array_start + 1
    length = len(text)
    while index < length:
        character = text[index]
        if character.isspace() or character == ",":
            index += 1
            continue
        if character == "]":
            break
        if character != "{":
            break
        end = _balanced_slice(text, index)
        if end is None:
            break
        claim = _loads(text[index:end])
        if not isinstance(claim, dict):
            break
        claims.append(claim)
        index = end
    return claims


def _loads_claims_list(text: str) -> list[dict] | None:
    """Accept a bare claim array, the other shape models sometimes return."""
    value = _loads(_drop_trailing_commas(text))
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    return None


def _loads_object(text: str) -> dict | None:
    value = _loads(text)
    return value if isinstance(value, dict) else None


def _loads(text: str) -> object:
    """Decode leniently: a raw newline inside a Java/JSON string is a model typo.

    ``strict=False`` only relaxes control characters inside strings; quoting,
    commas and structure are still enforced, so this cannot turn prose into a
    payload. Models routinely emit a real newline in a claim's ``text``.
    """
    try:
        return json.loads(text, strict=False)
    except (TypeError, ValueError):
        return None


def _drop_trailing_commas(text: str) -> str:
    """Remove commas before ``}``/``]`` without touching commas inside strings."""
    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if character == ",":
            look = index + 1
            while look < length and text[look].isspace():
                look += 1
            if look < length and text[look] in "}]":
                index += 1
                continue
        output.append(character)
        index += 1
    return "".join(output)
