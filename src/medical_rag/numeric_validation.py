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
_CHINESE = "零〇一二两三四五六七八九十百千万亿半"
_CHINESE_NUM = rf"[{_CHINESE}]+(?:点[{_CHINESE}]+)?"
_ARABIC = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?(?:[百万]|千(?!克|卡|帕|焦|米|瓦|伏|安))?"
_NUMBER = rf"[+-]?(?:{_ARABIC}|{_CHINESE_NUM})"
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
    "千卡": "kcal", "kcal": "kcal", "大卡": "kcal",
}
_TIME_UNITS = {"day", "week", "month", "year", "hour", "minute", "second"}
_UNIT = "(?:" + "|".join(
    re.escape(unit).replace("/", r"\s*/\s*")
    for unit in sorted(_UNITS, key=len, reverse=True)) + r")(?![a-zμ])"
_DENOMINATORS = {
    **{key: value for key, value in _UNITS.items()
       if value in _TIME_UNITS | {"kg", "l", "ml", "g", "mg", "times"}},
    "d": "day",
    "m2": "m2",
    "㎡": "m2",
    "平方米": "m2",
    "米2": "m2",
    "dl": "dl",
    "分": "minute",
}
_DENOMINATOR = "(?:" + "|".join(
    re.escape(unit) for unit in sorted(_DENOMINATORS, key=len, reverse=True)) + r")(?![a-zμ])"
_SCALED_DENOMINATOR = (
    r"(?:(?:100|1000)\s*(?:kg|dl|ml|g|l)|(?:24|12)\s*(?:小时|h)|1\.73\s*(?:平方米|米2|m2|㎡))(?![a-zμ])"
)
_UNPAREN_DENOM = rf"(?:{_SCALED_DENOMINATOR}|{_DENOMINATOR})"
# 支持医学文献常见的括号复合分母（如 /(kg·d)、/(kg.次)），按分母拆开校验，避免整段证据被误判失真。
_PAREN_DENOMINATOR = rf"\(\s*(?:\d+(?:\.\d+)?\s*)?{_DENOMINATOR}(?:\s*[·.⋅*,]\s*(?:\d+(?:\.\d+)?\s*)?{_DENOMINATOR})*\s*\)"
_DENOM_GROUP = rf"(?:\s*/\s*(?:{_UNPAREN_DENOM}|{_PAREN_DENOMINATOR}))*"
_COMPARISONS = {
    "不低于": "ge", "不少于": "ge", "不小于": "ge", "至少": "ge",
    "大于等于": "ge", "大于或等于": "ge", ">=": "ge", "≥": "ge",
    "不高于": "le", "不超过": "le", "不大于": "le", "至多": "le", "最多": "le",
    "小于等于": "le", "小于或等于": "le", "<=": "le", "≤": "le",
    "高于": "gt", "大于": "gt", "超过": "gt", ">": "gt",
    "低于": "lt", "小于": "lt", "少于": "lt", "不足": "lt", "未满": "lt", "<": "lt",
    "等于": "eq", "=": "eq",
}
for _negation in ("不得", "不能", "不可", "不应", "不宜", "不允许", "未"):
    for _word in ("超过", "高于", "大于"):
        _COMPARISONS[_negation + _word] = "le"
    for _word in ("低于", "小于", "少于"):
        _COMPARISONS[_negation + _word] = "ge"
_PREFIX = "|".join(re.escape(word) for word in sorted(_COMPARISONS, key=len, reverse=True))
_SUFFIXES = {"及以上": "ge", "以上": "ge", "及以下": "le", "以下": "le", "以内": "le"}
_SEPARATOR = r"(?:至|到|[-~～–—/])"
_UNIT_SEPARATOR = rf"(?:至|到|[-~～–—]|/(?!\s*{_SCALED_DENOMINATOR}))"
_QUANTITY = re.compile(
    rf"(?<![\d.+\-/{_CHINESE}])(?:(?P<prefix>{_PREFIX})\s*)?"
    rf"(?P<first>{_NUMBER})\s*(?:个\s*)?"
    rf"(?:(?P<left_unit>{_UNIT})(?P<left_denominators>{_DENOM_GROUP})"
    rf"\s*(?P<unit_separator>{_UNIT_SEPARATOR})\s*"
    rf"(?P<unit_second>{_NUMBER})\s*(?:个\s*)?|"
    rf"(?P<separator>{_SEPARATOR})\s*(?P<second>{_NUMBER})\s*(?:个\s*)?)?"
    rf"(?P<unit>{_UNIT})(?P<denominators>{_DENOM_GROUP})\s*"
    r"(?P<half>半)?\s*(?P<suffix>及以上|及以下|以上|以下|以内)?"
)
_CADENCE = re.compile(
    rf"每\s*(?:(?P<count>{_NUMBER})(?:\s*[-~～–—至到]\s*(?P<count_end>{_NUMBER}))?\s*)?(?:个\s*)?"
    r"(?P<unit>小时|分钟|星期|秒钟|日|天|周|月|年|次|时|秒)"
)
_CLAUSE_END = re.compile(r"[。！？!?；;\n]")
_BROKEN_CJK_UNIT = re.compile(
    r"(?:(?<=[\d\s/])千\s+克|"
    r"毫\s+克|微\s+克|毫\s+升|分\s+钟|小\s+时|星\s+期|秒\s+钟|分\s+升|"
    r"平\s*方\s*米|毫\s*米\s*汞\s*柱|毫\s*摩\s*尔|国\s*际\s*单\s*位)"
)
_TRAILING_CMP = {"<=": "ge", "≤": "ge", "<": "gt", ">=": "le", "≥": "le", ">": "lt"}
# 年龄与生理解剖/检验阈值属于状态或控制目标，不从同句前置“每……监测/随访”继承给药频次。
_NON_CADENCE_UNITS = {"age", "kg/m2", "mmhg", "kpa", "mmol/l", "mg/dl"}
_TIME_RANK = {
    "second": 0,
    "minute": 1,
    "hour": 2,
    "day": 3,
    "week": 4,
    "month": 5,
    "year": 6,
}


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


def _is_compound_slash_separator(after_text: str, current_unit: str) -> bool:
    """Distinguish compound drug strengths (50mg/氢氯噻嗪12.5mg, 50mg/12.5mg) from denominators."""
    slash = re.match(r"^\s*/\s*(.+)$", after_text, re.DOTALL)
    if not slash:
        return False
    rhs = slash.group(1)
    if re.match(rf"^[\u4e00-\u9fff]{{1,14}}\s*(?:{_NUMBER})\s*(?:{_UNIT})", rhs):
        if not re.match(rf"^(?:{_DENOMINATOR}|{_UNIT})", rhs):
            return True
    paired = re.match(rf"^(?:{_NUMBER})\s*(?P<u2>{_UNIT})", rhs)
    if paired:
        u2_key = re.sub(r"\s+", "", paired.group("u2"))
        if _UNITS.get(u2_key) == current_unit:
            return True
    return False


_COMPOUND_MASS_UNITS = {"mg", "g", "μg", "ug", "mcg", "iu", "u"}


def _append_compound_pair_facts(text: str, facts: list[NumericFact]) -> None:
    """Extract pair facts and unwind mass pairs (e.g. 100/12.5 mg -> 100 mg + 12.5 mg)."""
    for existing in list(facts):
        if existing.relation == "pair" and len(existing.values) == 2 and existing.unit in _COMPOUND_MASS_UNITS:
            for val in existing.values:
                facts.append(NumericFact((val,), existing.unit, "eq", existing.cadence))
    pattern = re.compile(
        rf"(?<![\d.+\-/])(?P<v1>{_ARABIC})\s*(?P<u1>{_UNIT})"
        rf"(?!\s*[·⋅^]|\s*/\s*(?:{_UNPAREN_DENOM}))\s*/\s*"
        rf"(?:[\u4e00-\u9fff]{{1,14}}\s*)?(?P<v2>{_ARABIC})\s*(?P<u2>{_UNIT})"
    )
    for match in pattern.finditer(text):
        u1 = _UNITS.get(re.sub(r"\s+", "", match.group("u1")))
        u2 = _UNITS.get(re.sub(r"\s+", "", match.group("u2")))
        if not u1 or u1 != u2:
            continue
        try:
            facts.append(NumericFact((_decimal(match.group("v1")), _decimal(match.group("v2"))), u1, "pair"))
        except (DecimalException, ValueError):
            continue


_HEADER_HINT = re.compile(r"(?:半衰期|剂量|每份质量|质量|含量|体重|身高|年龄|血压|血糖|服药|次数|表\s*\d|续表)")
_HEADER_UNIT_PAREN = re.compile(rf"\(\s*(?P<u>{_UNIT})(?P<d>{_DENOM_GROUP})\s*\)")
_START_FULL_HEADER = re.compile(r"起始[^)）]{0,8}[~～\-]\s*足量")
_DAILY_COUNT_HEADER = re.compile(r"每\s*[天日]\s*(?:服药\s*)?次数")
_TABLE_CELL_NUMBER = re.compile(
    r"(?<![\d.\[/\-a-z])(?P<a>\d+(?:\.\d+)?)(?:\s*(?P<sep>[~～\-–—/])\s*(?P<b>\d+(?:\.\d+)?))?(?![\d.\]/\-a-z%])"
)
_INLINE_UNIT_FOLLOW = re.compile(rf"^\s*(?:个\s*)?(?:{_UNIT}|[年月日周岁次片粒滴卷期页条项位组例])")
_PROSE_END = re.compile(r"[。！？!?]\s*$")


def _append_table_header_facts(text: str, facts: list[NumericFact]) -> None:
    """Bind parenthesized table-column header units to bare numbers in subsequent rows."""
    active_units: list[tuple[str, tuple[tuple[Decimal, str], ...], bool]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        found_headers: list[tuple[str, tuple[tuple[Decimal, str], ...], bool]] = []
        for match in _HEADER_UNIT_PAREN.finditer(stripped):
            if match.start() > 0 and stripped[match.start() - 1].isdigit():
                continue
            try:
                unit, bases = _unit_parts(match.group("u"), match.group("d"))
                prefix_window = stripped[max(0, match.start() - 24):match.start()]
                allow_endpoints = bool(_START_FULL_HEADER.search(prefix_window))
                entry = (unit, bases, allow_endpoints)
                if entry not in found_headers:
                    found_headers.append(entry)
            except (DecimalException, ValueError, KeyError):
                continue
        if _DAILY_COUNT_HEADER.search(stripped):
            count_entry = ("times", ((Decimal(1), "day"),), False)
            if count_entry not in found_headers:
                found_headers.append(count_entry)
        if found_headers and (_HEADER_HINT.search(stripped) or len(stripped.split()) >= 2):
            active_units = found_headers
            continue
        if not active_units:
            continue
        if _PROSE_END.search(stripped) and len(stripped) > 40:
            active_units = []
            continue
        if re.search(r"vol\.?\s*\d|第\s*\d+\s*[卷期页]", stripped):
            continue
        for match in _TABLE_CELL_NUMBER.finditer(stripped):
            if _INLINE_UNIT_FOLLOW.match(stripped[match.end():]):
                continue
            sep = match.group("sep")
            try:
                v_a = _decimal(match.group("a"))
                if match.group("b") is not None:
                    v_b = _decimal(match.group("b"))
                    if sep != "/" and v_a > v_b:
                        continue
                    values = (v_a, v_b)
                    relation = "pair" if sep == "/" else "range"
                else:
                    values = (v_a,)
                    relation = "eq"
            except (DecimalException, ValueError):
                continue
            for h_unit, h_bases, allow_endpoints in active_units:
                cadence = tuple(sorted(set(h_bases)))
                facts.append(NumericFact(values, h_unit, relation, cadence))
                if relation == "range" and allow_endpoints and len(values) == 2:
                    facts.append(NumericFact((values[0],), h_unit, "eq", cadence))
                    facts.append(NumericFact((values[1],), h_unit, "eq", cadence))
                elif relation == "pair" and h_unit in _TIME_UNITS and len(values) == 2:
                    facts.append(NumericFact((values[0],), h_unit, "eq", cadence))
                    facts.append(NumericFact((values[1],), h_unit, "eq", cadence))


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
    for raw_match in re.finditer(rf"(?:(?P<scale>\d+(?:\.\d+)?)\s*)?(?P<denom>{_DENOMINATOR})", denominators):
        denominator = _DENOMINATORS[raw_match.group("denom")]
        scale_raw = raw_match.group("scale")
        scale = _decimal(scale_raw) if scale_raw else Decimal(1)
        if denominator in _TIME_UNITS | {"times"}:
            bases.append((scale, denominator))
        elif scale_raw and scale != Decimal(1):
            unit += f"/{scale}{denominator}"
        else:
            unit += "/" + denominator
    return unit, tuple(sorted(set(bases)))


def _clause_cadences(
    text: str,
    start: int,
    end: int,
    cadences: list[re.Match[str]],
) -> list[tuple[Decimal, str]]:
    """Collect active cadence markers in a clause, resetting prior comma cadences on new cadence."""
    breaks = [start] + [start + m.end() for m in re.finditer(r"(?<!\d),(?!\d)", text[start:end])] + [end]
    active: list[tuple[Decimal, str]] = []
    for seg_start, seg_end in zip(breaks, breaks[1:]):
        seg_markers = [
            (_decimal(marker["count"] or "1"), _UNITS[marker["unit"]])
            for marker in cadences
            if seg_start <= marker.start() < seg_end
        ]
        if not seg_markers:
            continue
        if any(m_unit in _TIME_UNITS for _, m_unit in seg_markers):
            active = list(seg_markers)
        else:
            active = [item for item in active if item[1] != "times"] + seg_markers
    return active


def numeric_facts(text: str, *, strict: bool = True) -> tuple[NumericFact, ...] | None:
    """Extract supported quantities; fail closed in strict mode or skip isolated malformed tokens.

    strict=True 校验模型声明（单条不合法即整体拒收）；strict=False 用于检索父块证据，
    防止表格或复方斜杠记号里某一项未知格式导致整块其它合法数值被连坐抹除。
    """
    # 折叠 PDF 抽取造成的汉字间水平空格与跨行断开的多字中文单位（如“千\n克”“分 钟”），保留其余断句；
    # 全角中文逗号后补空格再 NFKC，避免“kg/m²，80岁”被误判为畸形千分位。
    text = re.sub(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])", "", text)
    if not strict:
        text = re.sub(r"(?<=[\u4e00-\u9fff（(])\n(?=[+\-]?\d|[≤≥<>])", "", text)
    text = _BROKEN_CJK_UNIT.sub(lambda m: re.sub(r"\s+", "", m.group(0)), text)
    text = text.replace("，", "， ")
    text = unicodedata.normalize("NFKC", text).lower().replace("−", "-")
    text = re.sub(rf"负\s*(?=[\d.{_CHINESE}])", "-", text)
    text = re.sub(rf"百分之\s*({_NUMBER})", r"\1%", text)
    table_text = text
    if not strict:
        # 表格解析需要保留原始换行；正文则在快照后折叠 PDF 中文断行及单位内部空格，
        # 避免“每日……不超\n过 5g”丢掉频次、“1.20～39岁”被当成小数或“kg/m 2”漏识别。
        text = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]*\n[ \t]*(?=[\u4e00-\u9fff])", "", text)
        text = re.sub(
            r"(?:(?<=^)|(?<=[。；！？：\s]))([1-9])\.(?=\d{2,}\s*(?:[~～\-至到]\s*\d+\s*)?岁)",
            r"\1. ",
            text,
        )
        text = re.sub(r"(?<=/)\s*m\s+2(?![\d])", "m2", text)
        text = re.sub(r"(?<=/)\s*米\s+2(?![\d])", "米2", text)
    facts = []
    cadences = list(_CADENCE.finditer(text))
    for marker in cadences:
        try:
            count_values = (_decimal(marker["count"] or "1"),)
            if marker.group("count_end"):
                count_values += (_decimal(marker["count_end"]),)
            facts.append(NumericFact(count_values, _UNITS[marker["unit"]], "every"))
        except (DecimalException, ValueError, KeyError):
            if strict:
                return None
    prev_non_cadence_qty_end = 0
    for match in _QUANTITY.finditer(text):
        try:
            if (match.start() >= 2 and text[match.start() - 1] == ","
                    and text[match.start() - 2].isdigit()
                    and not re.search(r"(?:^|[^0-9])(?:m|米)2$", text[:match.start() - 1])):
                if strict:
                    return None  # malformed thousands separator, not a list delimiter
                continue
            if re.match(r"\s*(?:/|[·⋅^])", text[match.end():]):
                current_canonical = _UNITS.get(re.sub(r"\s+", "", match["unit"]), "")
                if not _is_compound_slash_separator(text[match.end():], current_canonical):
                    if strict:
                        return None  # never silently truncate an unrecognized denominator
                    continue
            values = (_decimal(match["first"]),)
            second = match["second"] or match["unit_second"]
            if second:
                values += (_decimal(second),)
            unit, bases = _unit_parts(match["unit"], match["denominators"])
            if match["half"]:
                if len(values) != 1 or unit not in _TIME_UNITS:
                    if strict:
                        return None
                    continue
                with localcontext() as context:
                    context.prec = max(28, len(values[0].as_tuple().digits)
                                       + abs(values[0].as_tuple().exponent) + 2)
                    values = (values[0] + (Decimal("-0.5") if values[0] < 0 else Decimal("0.5")),)
            relation = _COMPARISONS.get(match["prefix"], "eq")
            suffix = _SUFFIXES.get(match["suffix"])
            if suffix:
                if relation not in {"eq", suffix}:
                    if strict:
                        return None
                    continue
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
                    if strict:
                        return None
                    continue
            elif relation == "eq" and not match["prefix"] and not match["suffix"]:
                # 处理“5.1 mmol/L≤空腹血糖<7.0 mmol/L”类左端链式不等式：量词在比较符左侧，实际语义为 ≥5.1 mmol/L。
                trailing = re.match(
                    rf"\s*(<=|>=|≤|≥|<|>)(?!\s*(?:[\d.]|[{_CHINESE}]))",
                    text[match.end():],
                )
                if trailing:
                    relation = _TRAILING_CMP[trailing.group(1)]
            cadence = []
            is_cadence_span = False
            for marker in cadences:
                if marker.start() <= match.start() and match.end("unit") <= marker.end():
                    relation = "every"
                    is_cadence_span = True
            if unit not in _TIME_UNITS and unit not in _NON_CADENCE_UNITS:
                # Only a preceding explicit cadence in this clause can qualify a
                # quantity. Do not infer per-dose vs daily total from a later phrase.
                start = max((stop.end() for stop in _CLAUSE_END.finditer(text, 0, match.start())),
                            default=0)
                inherited = _clause_cadences(text, start, match.start(), cadences)
                if unit == "times":
                    inherited = [item for item in inherited if item[1] != "times"]
                cadence.extend(inherited)
            elif unit == "minute" and relation != "every":
                # 让“每周至少250分钟”与“至少250分钟/周”映射到同一规范化周期，仅限同无逗号短语内紧邻的更大时间粒度。
                seg_start = max(
                    [stop.end() for stop in _CLAUSE_END.finditer(text, 0, match.start())]
                    + [c.end() for c in re.finditer(r"(?<!\d),(?!\d)", text[:match.start()])],
                    default=0,
                )
                seg_start = max(seg_start, prev_non_cadence_qty_end)
                for marker in cadences:
                    if not (seg_start <= marker.start() and marker.end() <= match.start()):
                        continue
                    m_unit = _UNITS.get(marker["unit"], "")
                    if m_unit in _TIME_UNITS and _TIME_RANK[m_unit] > _TIME_RANK[unit]:
                        cadence.append((_decimal(marker["count"] or "1"), m_unit))
            if (
                unit == "times"
                and values == (Decimal(2),)
                and relation == "eq"
                and not bases
                and not cadence
                and re.match(r"[\u4e00-\u9fff]{0,6}间隔", text[match.end():])
            ):
                if not is_cadence_span:
                    prev_non_cadence_qty_end = match.end()
                continue
            conflict = False
            for basis in bases:
                if (basis[1] in _TIME_UNITS and any(
                        old[1] in _TIME_UNITS and old != basis for old in cadence)):
                    conflict = True
                    break
                cadence.append(basis)
                facts.append(NumericFact((basis[0],), basis[1], "every"))
            if conflict:
                if strict:
                    return None
                continue
            facts.append(NumericFact(values, unit, relation, tuple(sorted(set(cadence)))))
            if not is_cadence_span:
                prev_non_cadence_qty_end = match.end()
        except (DecimalException, ValueError, KeyError):
            if strict:
                return None
            continue
    if not strict:
        _append_compound_pair_facts(text, facts)
        _append_table_header_facts(table_text, facts)
    return tuple(facts)


def numbers_supported(text: str, sources: Sequence[str]) -> bool:
    """Require each complete fact to occur in at least one individual source."""
    required = numeric_facts(text, strict=True)
    if required is None:
        return False
    if not required:
        return True
    supported = set()
    for source in sources:
        facts = numeric_facts(source, strict=False)
        if facts is not None:
            supported.update(facts)
    return all(fact in supported for fact in required)
