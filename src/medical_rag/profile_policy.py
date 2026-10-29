"""Pure, conservative validation of untrusted profile mutation candidates."""

import re

from .memory import PROFILE_KEYS, SELF_REPORT_PATTERNS, sensitive_text
from .profile_models import (
    ActionStatus,
    Candidate,
    IntentDecision,
    Mutation,
    ProfileAction,
    ProfilePlan,
    ProfileSnapshot,
)

MULTI_FACT_KEYS = frozenset({"interests", "goals", "health"})
FIELD_NAMES = {
    "display_name": r"姓名|名字|称呼|display_name|name",
    "occupation": r"职业|工作|身份|occupation",
    "language": r"语言|语种|language",
    "response_style": r"回答风格|回复风格|回答方式|回复方式|response_style",
    "interests": r"兴趣|爱好|关注领域|interests",
    "goals": r"目标|计划|goals",
    "health": r"健康|病史|疾病|用药|过敏|health",
}
PREFERENCE_MAP = {
    "language": {"英语": {"英文", "English"}, "英文": {"英语", "English"},
                 "中文": {"Chinese"}},
    "response_style": {"简洁": {"简短"}, "简短": {"简洁"}, "分点": {"列表"}},
}
_HYPOTHETICAL = re.compile(r"如果|假如|假设|假使|倘若|要是|假定|\b(?:if|suppose|imagine)\b", re.IGNORECASE)
_QUOTED = re.compile(r"[\"'`“”‘’「」『』]|(?:朋友|他|她|别人|助手|系统|文章|原文).{0,8}(?:说|写|提到)|引用|\b(?:said|quote)\b", re.IGNORECASE)
_UNCERTAIN = re.compile(r"可能|也许|或许|大概|不确定|好像|似乎|\b(?:maybe|perhaps|might)\b", re.IGNORECASE)
_NEGATIVE = re.compile(r"不要|不必|无需|不需要|不是|并非|没有|未曾|未患|不患|不再|别(?:记|保存|忘)|\b(?:not|never|don't|do not)\b", re.IGNORECASE)
_OTHERS = re.compile(r"我(?:的)?(?:父亲|母亲|爸爸|妈妈|家人|朋友|孩子|妻子|丈夫|同事|患者)|\bmy (?:father|mother|friend|child|wife|husband|patient)\b", re.IGNORECASE)
_FORGET = re.compile(r"忘记|忘掉|遗忘|删除|移除|清除|清空|\b(?:forget|delete|remove|clear)\b", re.IGNORECASE)
_CONTACT = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|\d[\d -]{6,}\d")
_TARGET_END = r"(?=\s*(?:[，,。.!！?？;；\n]|$))"
NEGATIVE_SELF_PATTERNS = {
    "display_name": r"我(?:不再叫|不叫)",
    "occupation": r"我(?:已经)?(?:不再是|不是|不再担任|不再从事)(?:一名|一个)?",
    "language": r"我(?:不再偏好|不再喜欢|不喜欢|不再使用)",
    "response_style": r"我(?:不再偏好|不再喜欢|不喜欢|不再希望)",
    "interests": r"我(?:不再喜欢|不再关注|不喜欢|不再对)",
    "goals": r"我(?:的目标不再是|不再希望|不再计划)",
    "health": r"我(?:不再|没有|并非|不是)(?:患有|有|确诊为|被确诊为|服用|正在服用)?",
}


def edit_fact_text(current: str, value: str, operation: str) -> str | None:
    """Edit only exact semicolon-delimited facts, never rewrite free text."""
    facts = [part.strip() for part in current.split("；") if part.strip()]
    if operation == "assert":
        if value not in facts:
            facts.append(value)
        return "；".join(facts)
    if operation == "retract" and value in facts:
        return "；".join(part for part in facts if part != value)
    return None


def _support_context(question: str, support: str) -> str | None:
    """Require an unambiguous literal occurrence and inspect its local sentence."""
    start = question.find(support)
    if start < 0 or question.find(support, start + 1) >= 0:
        return None
    left = max(question.rfind(mark, 0, start) for mark in "。！？；\n") + 1
    end = start + len(support)
    right = len(question)
    for mark in "。！？；\n":
        found = question.find(mark, end)
        if found >= 0:
            right = min(right, found + 1)
    # Other clauses may mention family or a disease; only uncertainty/quotation
    # scope extends before the supporting clause, never a whole-message veto.
    local_left = max(question.rfind(mark, left, start) for mark in "，,") + 1
    local_right = right
    for mark in "，,":
        found = question.find(mark, end)
        if found >= 0:
            local_right = min(local_right, found)
    scoped = question[left:local_right]
    if _HYPOTHETICAL.search(scoped) or _QUOTED.search(scoped):
        return None
    return question[max(left, local_left):local_right]


def _literal_values(item: Candidate) -> list[str]:
    """Return literal evidence strings for a value or a finite preference alias."""
    values = [item.value] if item.value and item.value in item.support else []
    for original, equivalents in PREFERENCE_MAP.get(item.key, {}).items():
        if item.value in equivalents and original in item.support:
            values.append(original)
    return values


def _complete_target(pattern: str, text: str, key: str) -> bool:
    """Require a whole fact, retaining qualifiers rather than asserting a prefix.

    Only finite preference grammar may follow the literal target. Health and
    other facts must include meaningful suffixes such as family history in the
    value itself; they cannot silently become an affirmative disease diagnosis.
    """
    suffix = {"language": r"(?:回答|回复|交流)?",
              "response_style": r"(?:(?:中文|英文|英语)?(?:回答|回复))?"}.get(key, "")
    pattern += suffix + _TARGET_END
    return bool(re.search(pattern, text, re.IGNORECASE))


def _positive_self_report(item: Candidate, context: str) -> bool:
    """Bind the exact value to a field-specific self-report or preference cue."""
    for literal in _literal_values(item):
        pattern = SELF_REPORT_PATTERNS[item.key] + r"\s*" + re.escape(literal)
        if (_complete_target(pattern, item.support, item.key) and
                _complete_target(pattern, context, item.key)):
            return True
        if item.key == "interests":
            inverted = r"我对\s*" + re.escape(literal) + r"感兴趣"
            if (_complete_target(inverted, item.support, item.key) and
                    _complete_target(inverted, context, item.key)):
                return True
    return False


def _correction_target(item: Candidate, context: str) -> bool:
    """Require the proposed value to follow the original field correction cue."""
    field = "(?:" + FIELD_NAMES[item.key] + ")"
    prefix = r"(?:更正|纠正|改正|更新|修改|替换)(?:我(?:的)?|本人的)?" + field
    infix = r"(?:把)?(?:我的|本人的)" + field + r"(?:改为|改成|更新为|修改为|替换为)"
    for literal in _literal_values(item):
        ending = r"\s*" + re.escape(literal)
        for pattern in (prefix + r"(?:信息|记录|画像)?\s*(?:为|是|成|:|：|到)" + ending, infix + ending):
            if (_complete_target(pattern, item.support, item.key) and
                    _complete_target(pattern, context, item.key)):
                return True
    return False


def _scoped_retraction_pattern(item: Candidate) -> str:
    return (r"(?:撤回|撤销|取消)(?:我(?:的)?|本人的)?(?:" + FIELD_NAMES[item.key] +
            r")(?:信息|记录|事实|中|里|的|是|为|：|:)*\s*" + re.escape(item.value))


def _retraction_target(item: Candidate, context: str) -> bool:
    """A cancellation unrelated to the exact profile assertion is not a retract."""
    if not item.value or item.value not in item.support:
        return False
    literal = re.escape(item.value)
    direct = NEGATIVE_SELF_PATTERNS[item.key] + r"\s*" + literal
    return any(_complete_target(pattern, item.support, item.key) and
               _complete_target(pattern, context, item.key)
               for pattern in (direct, _scoped_retraction_pattern(item)))


def _explicit_retraction(item: Candidate) -> bool:
    """Bare denial conflicts with a manual value; a scoped withdrawal does not."""
    direct = NEGATIVE_SELF_PATTERNS[item.key] + r"\s*" + re.escape(item.value)
    match = re.search(direct, item.support, re.IGNORECASE)
    return bool((match and "不再" in match.group()) or
                re.search(_scoped_retraction_pattern(item), item.support, re.IGNORECASE))


def _forget_target(item: Candidate, context: str) -> bool:
    """Bind a deletion command to the user's field or an explicit profile target."""
    field = "(?:" + FIELD_NAMES[item.key] + ")"
    verb = "(?:" + _FORGET.pattern + ")"
    targets = (r"(?:我的|本人的)" + field + r"(?:画像|信息|记录|偏好)?",
               field + r"(?:画像|信息|记录|偏好)")
    return any(re.search(verb + r"\s*" + target + _TARGET_END, item.support, re.IGNORECASE) and
               re.search(verb + r"\s*" + target + _TARGET_END, context, re.IGNORECASE)
               for target in targets)


def _supported(item: Candidate, context: str) -> bool:
    if item.subject != "user" or item.sensitivity == "unknown":
        return False
    if _UNCERTAIN.search(context) or _OTHERS.search(context):
        return False
    if re.search(r"[?？]|(?:吗|么|呢)[。！!\s]*$", context):
        return False
    if item.operation == "retract":
        return item.modality in {"negative", "positive"} and _retraction_target(item, context)
    if item.modality != "positive" or _NEGATIVE.search(context):
        return False
    if item.operation == "forget_all":
        return bool(_FORGET.search(item.support) and re.search(
            r"(?:所有|全部|一切).{0,5}(?:画像|记忆|信息)|(?:画像|记忆|信息).{0,5}(?:所有|全部)|all (?:my )?(?:profile|memor)",
            item.support, re.IGNORECASE))
    if item.operation == "forget":
        return _forget_target(item, context)
    if not _literal_values(item):
        return False
    if item.operation == "replace":
        return _correction_target(item, context)
    return _positive_self_report(item, context)


def build_profile_plan(question: str, decision: IntentDecision,
                       snapshot: ProfileSnapshot, consent: bool) -> ProfilePlan:
    """Validate candidates and plan writes without mutating caller-owned data.

    Args:
        question: Unchanged current user message.
        decision: Strictly parsed but untrusted model proposals.
        snapshot: Local profile and privacy version boundary.
        consent: Explicit permission for this message's sensitive additions.

    Returns:
        A version-bound plan with safe public feedback and deferred conflicts.
    """
    mutations: dict[str, Mutation] = {}
    confirmations: dict[str, Mutation] = {}
    actions: list[ProfileAction] = []
    profiles = {key: dict(value) for key, value in snapshot.profiles.items()}

    def feedback(key: str, status: ActionStatus, message: str) -> None:
        actions.append(ProfileAction(key=key, status=status, message=message))

    for item in decision.candidates:
        key = item.key
        if not snapshot.enabled:
            feedback(key, "blocked", "长期记忆已关闭，未处理画像变更")
            continue
        if (key not in PROFILE_KEYS and not (key == "*" and item.operation == "forget_all")) or (
                item.operation == "forget_all" and key != "*"):
            feedback(key, "unsupported", "不支持此画像字段或操作范围")
            continue
        if item.operation in {"assert", "replace"} and _CONTACT.search(item.value):
            feedback(key, "unsupported", "联系方式不属于当前支持的画像信息，未保存")
            continue
        context = _support_context(question, item.support)
        if context is None or not _supported(item, context):
            feedback(key, "blocked", "原文不足以明确确认此项本人画像操作，请澄清或到设置页编辑")
            continue
        if item.operation == "forget_all":
            for stored_key, existing in profiles.items():
                if stored_key in PROFILE_KEYS:
                    confirmations[stored_key] = Mutation(
                        stored_key, "delete", "", bool(existing.get("sensitive") or stored_key == "health"),
                        "explicit", item.support)
            feedback(key, "needs_confirmation" if confirmations else "unchanged",
                     "请确认删除全部画像；原始聊天和情景不会因此删除" if confirmations else "当前没有可删除的画像")
            continue
        if key in confirmations:
            feedback(key, "blocked", "此画像字段已有待确认变更，请先确认或取消")
            continue
        existing = profiles.get(key)
        current = existing.get("value", "") if existing else ""
        sensitive = bool(key == "health" or item.sensitivity == "sensitive" or
                         (existing and existing.get("sensitive")) or
                         sensitive_text(item.support) or sensitive_text(item.value))
        removing = item.operation in {"forget", "retract"}
        if sensitive and not removing and not consent:
            feedback(key, "blocked", "此项包含敏感信息，默认不保存；如需记录请到设置页「长期记忆」手动添加")
            continue
        value = item.value.strip()
        if item.operation == "forget":
            value = ""
        elif item.operation == "retract":
            edited = edit_fact_text(current, value, "retract") if key in MULTI_FACT_KEYS else (
                "" if value == current else None)
            if edited is None:
                feedback(key, "blocked", "无法精确定位需撤回的已存事实，请澄清或到设置页编辑")
                continue
            value = edited
        elif item.operation == "assert" and key in MULTI_FACT_KEYS and current:
            value = edit_fact_text(current, value, "assert")
        if len(value) > 500 or (not value and not removing):
            feedback(key, "blocked", "画像值为空或超过500字，未保存且未截断")
            continue
        if value == current:
            feedback(key, "unchanged", "此项画像无需变更")
            continue
        mutation = Mutation(key, "set" if value else "delete", value, sensitive,
                            "auto" if item.operation == "assert" else "explicit", item.support)
        if existing and existing.get("source") == "manual" and (
                item.operation == "assert" or
                (item.operation == "retract" and not _explicit_retraction(item))):
            confirmations[key] = mutation
            feedback(key, "needs_confirmation", "此项与人工画像不同，请确认画像变更")
            continue
        mutations[key] = mutation
        if value:
            profiles[key] = {"value": value, "source": mutation.source, "sensitive": sensitive}
        else:
            profiles.pop(key, None)
        feedback(key, "deleted" if not value else "updated" if current else "added",
                 "已计划删除此项画像" if not value else "已计划更新此项画像" if current else "已计划新增此项画像")
    return ProfilePlan(snapshot.profile_revision, snapshot.privacy_revision,
                       tuple(mutations.values()), tuple(confirmations.values()), tuple(actions))
