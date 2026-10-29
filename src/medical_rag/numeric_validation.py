"""Conservative numeric grounding, not clinical or semantic entailment.

Only complete, equal numeric facts match. We canonicalize spelling, never infer
a dose, convert units, weaken a bound, or assemble a range across sources.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext

_DIGITS = dict(zip("零〇一二两三四五六七八九", "001223456789", strict=True))
_CHINESE = "零〇一二两三四五六七八九十百千万亿点半"
_ARABIC = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?[百千万]?"
_NUMBER = rf"[+-]?(?:{_ARABIC}|[{_CHINESE}]+)"
# Representation/resource limits, not medical plausibility thresholds.
_MAX_NUMBER_CHARS = 256
_MAX_ABS_EXPONENT = 1000

_UNITS = {
    "毫克": "mg", "mg": "mg", "克": "g", "g": "g",
    "微克": "ug", "μg": "ug", "ug": "ug", "mcg": "ug",
    "千克": "kg", "公斤": "kg", "kg": "kg",
    "毫升": "ml", "ml": "ml", "升": "l", "l": "l",
    "毫米汞柱": "mmhg", "mmhg": "mmhg", "千帕": "kpa", "kpa": "kpa",
    "毫摩尔/升": "mmol/l", "mmol/l": "mmol/l",
    "毫克/分升": "mg/dl", "mg/dl": "mg/dl",
    "%": "%", "次": "times", "天": "day", "日": "day",
    "周": "week", "星期": "week", "月": "month", "年": "year",
    "小时": "hour", "时": "hour", "h": "hour",
    "分钟": "minute", "min": "minute", "秒钟": "second", "秒": "second",
    "岁": "age", "片": "tablet", "粒": "capsule", "滴": "drop",
    "国际单位": "iu", "iu": "iu", "u": "u",
}
_TIME_UNITS = {"day", "week", "month", "year", "hour", "minute", "second"}
_UNIT = "(?:" + "|".join(
    re.escape(unit).replace("/", r"\s*/\s*")
    for unit in sorted(_UNITS, key=len, reverse=True)) + r")(?![a-zμ])"
_DENOMINATORS = {
    **{key: value for key, value in _UNITS.items()
       if value in _TIME_UNITS | {"kg", "times"}},
    "d": "day",
}
_DENOMINATOR = "(?:" + "|".join(
    re.escape(unit) for unit in sorted(_DENOMINATORS, key=len, reverse=True)) + r")(?![a-zμ])"
_COMPARISONS = {
    "不低于": "ge", "不少于": "ge", "不小于": "ge", "至少": "ge",
    "大于等于": "ge", "大于或等于": "ge", ">=": "ge", "≥": "ge",
    "不高于": "le", "不超过": "le", "不大于": "le", "至多": "le", "最多": "le",
    "小于等于": "le", "小于或等于": "le", "<=": "le", "≤": "le",
    "高于": "gt", "大于": "gt", "超过": "gt", ">": "gt",
    "低于": "lt", "小于": "lt", "少于": "lt", "不足": "lt", "未满": "lt", "<": "lt",
    "等于": "eq", "=": "eq",
}
for _negation in ("不得", "不能", "不可", "不应", "不允许", "未"):
    for _word in ("超过", "高于", "大于"):
        _COMPARISONS[_negation + _word] = "le"
    for _word in ("低于", "小于", "少于"):
        _COMPARISONS[_negation + _word] = "ge"
_PREFIX = "|".join(re.escape(word) for word in sorted(_COMPARISONS, key=len, reverse=True))
_SUFFIXES = {"及以上": "ge", "以上": "ge", "及以下": "le", "以下": "le", "以内": "le"}
_SEPARATOR = r"(?:至|到|[-~～–—/])"
_QUANTITY = re.compile(
    rf"(?<![\d.+\-/{_CHINESE}])(?:(?P<prefix>{_PREFIX})\s*)?"
    rf"(?P<first>{_NUMBER})\s*(?:个\s*)?"
    rf"(?:(?P<left_unit>{_UNIT})(?P<left_denominators>(?:\s*/\s*{_DENOMINATOR})*)"
    rf"\s*(?P<unit_separator>{_SEPARATOR})\s*"
    rf"(?P<unit_second>{_NUMBER})\s*(?:个\s*)?|"
    rf"(?P<separator>{_SEPARATOR})\s*(?P<second>{_NUMBER})\s*(?:个\s*)?)?"
    rf"(?P<unit>{_UNIT})(?P<denominators>(?:\s*/\s*{_DENOMINATOR})*)\s*"
    r"(?P<half>半)?\s*(?P<suffix>及以上|及以下|以上|以下|以内)?"
)
_CADENCE = re.compile(
    rf"每\s*(?P<count>{_NUMBER})?\s*(?:个\s*)?"
    r"(?P<unit>小时|分钟|星期|秒钟|日|天|周|月|年|次|时|秒)"
)
_CLAUSE_END = re.compile(r"[。！？!?；;\n]")


@dataclass(frozen=True)
class NumericFact:
    """One indivisible quantity, bound/range, unit and explicit dosing cadence."""

    values: tuple[Decimal, ...]
    unit: str
    relation: str = "eq"
    cadence: tuple[tuple[Decimal, str], ...] = ()


def _integer(text: str) -> int:
    """Read Chinese place values without substituting digits inside ordinary prose."""
    for marker, scale in (("亿", 100000000), ("万", 10000)):
        if marker in text:
            left, right = text.split(marker, 1)
            return (_integer(left) if left else 1) * scale + (_integer(right) if right else 0)
    if all(character in _DIGITS for character in text):
        return int("".join(_DIGITS[character] for character in text))
    total, current = 0, 0
    for character in text:
        if character in _DIGITS:
            current = int(_DIGITS[character])
        else:
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[character]
            current = 0
    return total + current


def _decimal(text: str) -> Decimal:
    """Canonicalize decimal spelling only; no unit or medical arithmetic."""
    if len(text) > _MAX_NUMBER_CHARS:
        raise ValueError("numeric representation is too long")
    sign = "-" if text.startswith("-") else ""
    text = text.lstrip("+-")
    if text == "半":
        return Decimal(sign + "0.5")
    if text[0].isdigit() or text[0] == ".":
        power = {"百": 2, "千": 3, "万": 4}.get(text[-1], 0)
        number = Decimal(sign + (text[:-1] if power else text).replace(",", ""))
        # Decimal arithmetic uses the process precision (usually 28 digits).
        # Constructing the tuple instead keeps every digit and cannot overflow
        # merely because an exponent is outside that arithmetic context.
        parts = number.as_tuple()
        if abs(parts.exponent + power) > _MAX_ABS_EXPONENT:
            raise ValueError("numeric exponent exceeds the representation budget")
        return Decimal((parts.sign, parts.digits, parts.exponent + power))
    whole, point, fraction = text.partition("点")
    number = str(_integer(whole)) if whole else "0"
    if point:
        number += "." + "".join(_DIGITS[character] for character in fraction)
    return Decimal(sign + number)


def _unit_parts(raw: str, denominators: str) -> tuple[str, tuple[tuple[Decimal, str], ...]]:
    """Keep every denominator, separating time/per-dose bases from the dimension."""
    unit = _UNITS[re.sub(r"\s+", "", raw)]
    bases = []
    for raw_denominator in re.findall(rf"/\s*({_DENOMINATOR})", denominators):
        denominator = _DENOMINATORS[raw_denominator]
        if denominator in _TIME_UNITS | {"times"}:
            bases.append((Decimal(1), denominator))
        else:
            unit += "/" + denominator
    return unit, tuple(sorted(set(bases)))


def numeric_facts(text: str) -> tuple[NumericFact, ...] | None:
    """Extract supported quantities; return None for malformed recognized facts.

    Unrecognized dimensions and bare numbers are outside this deterministic
    gate. None, in contrast to an empty tuple, always fails closed.
    """
    text = unicodedata.normalize("NFKC", text).lower().replace("−", "-")
    text = re.sub(rf"负\s*(?=[\d.{_CHINESE}])", "-", text)
    text = re.sub(rf"百分之\s*({_NUMBER})", r"\1%", text)
    facts = []
    cadences = list(_CADENCE.finditer(text))
    try:
        for marker in cadences:
            facts.append(NumericFact((_decimal(marker["count"] or "1"),),
                                     _UNITS[marker["unit"]], "every"))
    except (DecimalException, ValueError, KeyError):
        return None
    for match in _QUANTITY.finditer(text):
        try:
            if (match.start() >= 2 and text[match.start() - 1] == ","
                    and text[match.start() - 2].isdigit()):
                return None  # malformed thousands separator, not a list delimiter
            if re.match(r"\s*(?:/|[·⋅^])", text[match.end():]):
                return None  # never silently truncate an unrecognized denominator
            values = (_decimal(match["first"]),)
            second = match["second"] or match["unit_second"]
            if second:
                values += (_decimal(second),)
            unit, bases = _unit_parts(match["unit"], match["denominators"])
            if match["half"]:
                if len(values) != 1 or unit not in _TIME_UNITS:
                    return None
                with localcontext() as context:
                    context.prec = max(28, len(values[0].as_tuple().digits)
                                       + abs(values[0].as_tuple().exponent) + 2)
                    values = (values[0] + (Decimal("-0.5") if values[0] < 0 else Decimal("0.5")),)
            relation = _COMPARISONS.get(match["prefix"], "eq")
            suffix = _SUFFIXES.get(match["suffix"])
            if suffix:
                if relation not in {"eq", suffix}:
                    return None
                relation = suffix
            if second:
                separator = match["separator"] or match["unit_separator"]
                shape = "pair" if separator == "/" else "range"
                relation = shape if relation == "eq" else f"{relation}:{shape}"
                left_unit = match["left_unit"]
                if left_unit:
                    left, left_bases = _unit_parts(left_unit, match["left_denominators"])
                    if (left, left_bases) != (unit, bases):
                        unit = f"{left}{left_bases!r}..{unit}{bases!r}"
                if shape == "range" and values[0] > values[1]:
                    return None
            cadence = []
            for marker in cadences:
                if marker.start() <= match.start() and match.end("unit") <= marker.end():
                    relation = "every"
            if unit not in _TIME_UNITS:
                # Only a preceding explicit cadence in this clause can qualify a
                # quantity. Do not infer per-dose vs daily total from a later phrase.
                start = max((stop.end() for stop in _CLAUSE_END.finditer(text, 0, match.start())),
                            default=0)
                cadence = [(_decimal(marker["count"] or "1"), _UNITS[marker["unit"]])
                           for marker in cadences if start <= marker.start() < match.start()]
            for basis in bases:
                if (basis[1] in _TIME_UNITS and any(
                        old[1] in _TIME_UNITS and old != basis for old in cadence)):
                    return None
                cadence.append(basis)
                facts.append(NumericFact((basis[0],), basis[1], "every"))
            facts.append(NumericFact(values, unit, relation, tuple(sorted(set(cadence)))))
        except (DecimalException, ValueError, KeyError):
            return None
    return tuple(facts)


def numbers_supported(text: str, sources: Sequence[str]) -> bool:
    """Require each complete fact to occur in at least one individual source.

    Args:
        text: Untrusted statement to check before publication.
        sources: Only the contexts actually cited by this statement.

    Returns:
        Whether all recognized numeric facts are identically supported.
    """
    required = numeric_facts(text)
    if required is None:
        return False
    if not required:
        return True
    supported = set()
    for source in sources:
        facts = numeric_facts(source)
        if facts is not None:
            supported.update(facts)
    return all(fact in supported for fact in required)
