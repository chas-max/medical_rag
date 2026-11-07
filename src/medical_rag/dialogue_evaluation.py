"""Synthetic multi-turn evaluation for the dialogue resolver.

The evaluator advances a real :class:`QuestionResolver` through fixed synthetic
conversations. It measures structural outcomes only (topic, intent, constraint
retention, clarification) and never treats string equality as semantic truth.

Offline replay uses the case file's ``replay`` fixture, adapted into a valid model
reply by binding source turn ids from the snapshot the resolver sends. Live mode is
opt-in twice: ``--mode live`` plus ``--allow-paid-model``.

No metric records a synthetic question, an entity name or a model response body.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from time import perf_counter

from .dialogue_models import DialogueSnapshot, RecentTurn
from .dialogue_state import advance_state
from .question_resolver import QuestionResolver

CASE_COUNT = 40
MIN_TURNS, MAX_TURNS = 3, 6
CLARIFY_GROUPS = 10
REQUIRED_KEYS = ("question", "topic", "intent", "must_clarify", "required_terms",
                 "intent_terms", "forbidden_terms")
MAX_CASE_BYTES = 2_000_000


class CaseError(ValueError):
    """A static validation failure; never repeats case content."""


def load_cases(path: Path) -> list[dict]:
    """Read and validate the fixed synthetic case file."""
    data = path.read_bytes()
    if len(data) > MAX_CASE_BYTES:
        raise CaseError("Case file is too large")
    try:
        document = json.loads(data)
    except ValueError as error:
        raise CaseError("Case file is not valid JSON") from error
    cases = document.get("cases") if isinstance(document, dict) else None
    validate_cases(cases)
    return cases


def validate_cases(cases: object) -> None:
    """Enforce the fixed shape: 40 cases, 3-6 turns, exactly 10 ambiguity groups.

    Args:
        cases: The parsed ``cases`` array.

    Raises:
        CaseError: On any structural violation. The message never includes the
            offending question text.
    """
    if not isinstance(cases, list) or len(cases) != CASE_COUNT:
        raise CaseError(f"Expected {CASE_COUNT} cases")
    identifiers: set[str] = set()
    clarify_groups = 0
    for case in cases:
        if not isinstance(case, dict):
            raise CaseError("Invalid case")
        identifier = case.get("id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 64:
            raise CaseError("Invalid case id")
        if identifier in identifiers:
            raise CaseError("Duplicate case id")
        identifiers.add(identifier)
        turns = case.get("turns")
        if not isinstance(turns, list) or not MIN_TURNS <= len(turns) <= MAX_TURNS:
            raise CaseError("Invalid turn count")
        ambiguous = False
        for turn in turns:
            _validate_turn(turn)
            ambiguous = ambiguous or turn["must_clarify"] is True
        clarify_groups += ambiguous
    if clarify_groups != CLARIFY_GROUPS:
        raise CaseError(f"Expected {CLARIFY_GROUPS} clarification groups")


def _validate_turn(turn: object) -> None:
    if not isinstance(turn, dict) or any(key not in turn for key in REQUIRED_KEYS):
        raise CaseError("Missing turn field")
    if not isinstance(turn["question"], str) or not turn["question"].strip():
        raise CaseError("Invalid question")
    if turn["topic"] is not None and not isinstance(turn["topic"], str):
        raise CaseError("Invalid topic")
    if not isinstance(turn["intent"], str) or not turn["intent"]:
        raise CaseError("Invalid intent")
    if type(turn["must_clarify"]) is not bool:
        raise CaseError("Invalid must_clarify")
    for key in ("required_terms", "intent_terms", "forbidden_terms"):
        value = turn[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise CaseError("Invalid term list")
    if not isinstance(turn.get("replay"), dict):
        raise CaseError("Missing replay fixture")


def score_turn(expected: dict, actual: dict) -> dict[str, bool]:
    """Score one turn against its expectation without judging medical truth.

    ``required_terms`` must all appear, at least one ``intent_terms`` must appear,
    and no ``forbidden_terms`` may appear. This is a deterministic screen; it does
    not replace human review of a live run.
    """
    question = actual.get("question") or ""
    must_clarify = expected["must_clarify"]
    is_clarification = actual.get("status") == "clarification"
    intent_terms = expected.get("intent_terms", [])
    question_ok = (
        not is_clarification
        and all(term in question for term in expected.get("required_terms", []))
        and (not intent_terms or any(term in question for term in intent_terms))
        and not any(term in question for term in expected.get("forbidden_terms", []))
    )
    return {
        "topic_ok": actual.get("topic") == expected["topic"],
        "intent_ok": actual.get("intent") == expected["intent"],
        "question_ok": bool(question_ok),
        "clarification_ok": is_clarification if must_clarify else not is_clarification,
    }


def evaluate_cases(cases: list[dict], resolver_factory: object) -> dict[str, object]:
    """Run every case with an independent snapshot and summarize the outcome.

    Args:
        cases: Already validated synthetic cases.
        resolver_factory: Callable taking one case and returning a resolver whose
            ``transport`` may expose a ``calls`` counter.

    Returns:
        De-identified metrics: case ids, per-turn flags, latency percentiles and a
        model-call count. No question, entity or answer text is included.
    """
    results: list[dict] = []
    durations: list[float] = []
    calls = 0
    topic_total = topic_ok = intent_total = intent_ok = question_total = question_ok = 0
    clarify_total = clarify_ok = 0
    for case in cases:
        resolver = resolver_factory(case)
        snapshot = DialogueSnapshot()
        for index, turn in enumerate(case["turns"], 1):
            started = perf_counter()
            resolution = resolver.resolve(turn["question"], snapshot)
            duration = (perf_counter() - started) * 1000
            durations.append(duration)
            actual = {
                "status": resolution.status,
                "topic": resolution.topic.name if resolution.topic is not None else None,
                "intent": resolution.intent,
                "question": resolution.resolved_question,
            }
            scores = score_turn(turn, actual)
            if not turn["must_clarify"] and resolution.status == "clarification":
                # An unrequested clarification is a failure to resolve, never a
                # free pass for a topic-less turn: keep it in the denominator as
                # wrong instead of silently dropping it.
                scores.update(topic_ok=False, intent_ok=False, question_ok=False)
            if turn["must_clarify"]:
                clarify_total += 1
                clarify_ok += int(scores["clarification_ok"])
            else:
                topic_total += 1
                topic_ok += int(scores["topic_ok"])
                intent_total += 1
                intent_ok += int(scores["intent_ok"])
                question_total += 1
                question_ok += int(scores["question_ok"])
            results.append({
                "case": case["id"], "turn": index, "status": resolution.status,
                "relation": resolution.relation, "duration_ms": round(duration, 3),
                **scores,
            })
            state = advance_state(snapshot.state, resolution, turn_id=str(index))
            recent = (snapshot.recent + (RecentTurn(turn_id=str(index),
                                                    user=turn["question"][:200],
                                                    assistant="合成回答"),))[-3:]
            snapshot = DialogueSnapshot(state=state, recent=recent,
                                        context_version=index)
        counter = getattr(getattr(resolver, "transport", None), "calls", None)
        if isinstance(counter, int):
            calls += counter
    return {
        "cases": len(cases),
        "turns": len(results),
        "results": results,
        "topic_accuracy": _ratio(topic_ok, topic_total),
        "intent_accuracy": _ratio(intent_ok, intent_total),
        "question_accuracy": _ratio(question_ok, question_total),
        "clarification_accuracy": _ratio(clarify_ok, clarify_total),
        "clarification_turns": clarify_total,
        "latency_ms_p50": round(_percentile(durations, 0.50), 3) if durations else 0.0,
        "latency_ms_p95": round(_percentile(durations, 0.95), 3) if durations else 0.0,
        "model_calls": calls,
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return statistics.median([ordered[index]])


def source_for(name: str, question: str, context: dict) -> str:
    """Bind a replayed candidate to a real source turn in the snapshot."""
    if name and name in question:
        return "current"
    for turn in context.get("recent") or []:
        if isinstance(turn, dict) and name and name in str(turn.get("user", "")):
            return str(turn.get("turn_id"))
    state = context.get("state") or {}
    pending = state.get("pending_clarification") or {}
    for candidate in pending.get("candidates") or []:
        if isinstance(candidate, dict) and candidate.get("name") == name:
            return str(candidate.get("source_turn_id"))
    active = state.get("active_topic") or {}
    if active.get("name") == name:
        return str(active.get("source_turn_id"))
    return "current"


class ReplayTransport:
    """Turn each replay fixture into a source-bound model reply."""

    def __init__(self, case: dict) -> None:
        self.replies = {turn["question"]: turn["replay"] for turn in case["turns"]}
        self.calls = 0

    def complete_with_timeout(self, provider, system, user, timeout):
        payload = json.loads(user)
        question, context = payload["question"], payload["context"]
        replay = self.replies.get(question)
        if replay is None:
            raise ValueError("No replay fixture")
        self.calls += 1
        return json.dumps(self._reply(replay, question, context), ensure_ascii=False)

    @staticmethod
    def _reply(replay: dict, question: str, context: dict) -> dict:
        if "clarify" in replay:
            candidates = [{"name": name, "type": replay.get("type", "disease"),
                           "source_turn_id": source_for(name, question, context),
                           "source_role": "user"} for name in replay["clarify"]]
            return {
                "status": "clarification", "relation": replay.get("relation", "follow_up"),
                "resolved_question": None, "topic": None,
                "intent": replay.get("intent", "other"),
                "intent_text": replay.get("intent_text", ""), "references": [],
                "clarification": {
                    "candidates": candidates, "intent": replay.get("intent", "other"),
                    "intent_text": replay.get("intent_text", ""),
                    "original_question": question, "source_turn_id": "current",
                },
            }
        topic = replay.get("topic")
        return {
            "status": "resolved", "relation": replay.get("relation", "standalone"),
            "resolved_question": replay["resolved"], "topic": (
                {"name": topic, "type": replay.get("type", "disease"),
                 "source_turn_id": source_for(topic, question, context),
                 "source_role": "user"} if topic else None),
            "intent": replay["intent"], "intent_text": replay.get("intent_text", ""),
            "references": [], "clarification": None,
        }


def replay_factory(transport: type = ReplayTransport) -> object:
    """A resolver factory that never calls a paid model."""
    def build(case: dict) -> QuestionResolver:
        return QuestionResolver(transport(case))
    return build


def live_factory() -> object:
    """A resolver factory over the configured production transport."""
    from .config import Settings
    from .generation import OpenAITransport

    settings = Settings()

    def build(case: dict) -> QuestionResolver:
        return QuestionResolver(OpenAITransport(settings), settings.provider)

    return build


def main(argv: list[str] | None = None) -> int:
    """Run the fixed case file in replay or explicitly paid live mode."""
    parser = argparse.ArgumentParser(description="结构化对话解析评测")
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--mode", choices=("replay", "live"), default="replay")
    parser.add_argument("--allow-paid-model", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        cases = load_cases(arguments.cases)
    except (CaseError, OSError):
        print("用例文件不可用", file=sys.stderr)
        return 2
    if arguments.mode == "live":
        # Refuse before constructing any model client, so this flag alone can never
        # spend money: the operator has to opt in twice.
        if not arguments.allow_paid_model:
            print("live 模式需要显式 --allow-paid-model", file=sys.stderr)
            return 2
        factory = live_factory()
    else:
        factory = replay_factory()
    summary = evaluate_cases(cases, factory)
    printable = {key: value for key, value in summary.items() if key != "results"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
