"""Separate untrusted model drafting from citation validation."""

import inspect

from ...dialogue_models import MAX_REFERENCES
from ...generation import ANALYSIS_EVENT
from ..contracts import (
    CAPABILITY_WEB_SEARCH,
    MODEL_OUTPUT_TRUNCATED_VARIABLE,
    RERANK_DEGRADED_VARIABLE,
    ExecutionContext,
    supports,
)

# 诊断字段只进 trace，不改变 answer/citations/abstained 的既有语义。
DIAGNOSTIC_KEYS = ("abstain_reason", "output_repaired", "truncated", "degraded",
                   "degraded_reason", "numeric_rejections", "numeric_repaired")


def _accepts_kwarg(fn: object, name: str) -> bool:
    """Return True if callable ``fn`` accepts keyword argument ``name``."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        param.kind == inspect.Parameter.VAR_KEYWORD or param.name == name
        for param in params
    )


def _resolve_repair_mode(config: dict, inputs: dict) -> str:
    raw = str(config.get("numeric_repair_mode", "off"))
    if raw == "by_route":
        route = str(inputs.get("route") or "")
        return "all_claims" if route == "numeric_exact" else "rescue_only"
    return raw if raw in ("off", "rescue_only", "all_claims") else "off"


def _stream_events(payload: dict) -> list[dict]:
    """Translate one generator payload into the SSE events it stands for.

    The generator reports a claim as ``{text, citations}`` and the interpretive
    section as ``{event: "analysis", analysis: {...}}``. Both must leave here with
    their own event name and the data the client reads for that name: folding the
    section into the ``claim`` channel would send a payload without
    ``text``/``citations`` to a client that reads exactly those keys, which drops
    the whole answer instead of rendering the analysis.
    """
    if payload.get("event") == ANALYSIS_EVENT:
        return [{"event": ANALYSIS_EVENT, "data": payload.get("analysis")}]
    return [{"event": "claim", "data": payload}]


def draft(inputs: dict, config: dict, context: ExecutionContext, generator: object,
          default_provider: str) -> dict:
    web_search = supports(context.capabilities, CAPABILITY_WEB_SEARCH)
    provider = inputs.get("provider") or default_provider
    # 回答规划是可选连线：没接规划节点时 plan 为空，生成提示词保持原样。
    plan = inputs.get("plan", "")
    context_options = {"memory_context": context.memory_context} if context.memory_context else {}
    short_term = getattr(context, "short_term", None)
    if short_term:
        context_options["short_term"] = short_term
    # Recalled conversation blocks arrive only when rule 2 fired; they stay a
    # separate field so citation validation keeps seeing retrieved evidence only.
    history = inputs.get("history")
    if isinstance(history, list) and history:
        context_options["history"] = [item for item in history if isinstance(item, dict)]
    # The raw user words travel beside the resolved question for intent checking
    # only; every node still executes the complete semantic question.
    dialogue = getattr(context, "dialogue", None)
    resolution = getattr(dialogue, "resolution", None) if dialogue is not None else None
    if resolution is not None and getattr(resolution, "original_question", ""):
        context_options["original_question"] = resolution.original_question
    # 代词消解结果同样只做上下文：帮生成理解“它/这个”的指向与措辞分寸，
    # 以独立键传递，绝不并进 evidence，引用校验继续只看检索块。
    references = getattr(resolution, "references", ()) if resolution is not None else ()
    resolved_references = [
        {"expression": item.expression, "entity": item.entity,
         "source_role": item.source_role or "unknown"}
        for item in references
    ][:MAX_REFERENCES]
    if resolved_references:
        context_options["resolved_references"] = resolved_references
    route = str(inputs.get("route") or "")
    emit = context.emit
    if emit is not None:
        # 生成一段带证据的回答要十几到几十秒，先把检索结果数量推给前端，
        # 否则界面在整个生成期间只会停在“正在检索并验证证据”，看起来像卡住了。
        evidence = inputs.get("evidence")
        emit({"event": "status", "data": {"stage": "generating",
                                          "evidence": len(evidence) if isinstance(evidence, list) else 0,
                                          "web_search": web_search}})
    if emit is not None and hasattr(generator, "generate_stream"):
        def publish(payload: dict) -> None:
            for event in _stream_events(payload):
                emit(event)

        # 逐条推送已通过引用与数值校验的结论，最后再推一次分析段。
        # 联网能力必须原样传进去：漏传会让 draft() 退回 web_search=False，
        # 于是弱证据不补充检索、检索落空也不再交给生成阶段兜底（状态帧却还写着 true）。
        # draft() 只有本仓库的生成器提供；缺失时退回旧的 generate_stream 契约，后者只推结论。
        streamer = getattr(generator, "draft", None) or generator.generate_stream
        draft_options = dict(context_options)
        if route and _accepts_kwarg(streamer, "route"):
            draft_options["route"] = route
        result = streamer(inputs["question"], inputs["evidence"], provider,
                          web_search=web_search, on_claim=publish, plan=plan, **draft_options)
    else:
        gen_options = dict(context_options)
        if route and _accepts_kwarg(generator.generate, "route"):
            gen_options["route"] = route
        result = generator.generate(inputs["question"], inputs["evidence"], provider,
                                    web_search=web_search, plan=plan, **gen_options)
    # 截断是生成侧的运行状态，不是端口数据：写进 run 变量，供引用校验节点区分
    # “模型被截断”与“资料不足”，同时不改变 draft/evidence 的既有接线契约。
    if result.get("truncated"):
        context.variables[MODEL_OUTPUT_TRUNCATED_VARIABLE] = True
    # Web hits are evidence only for this node, so citation checking must see the same set.
    return {"draft": result["draft"], "evidence": result["evidence"]}


def validate(inputs: dict, config: dict, context: ExecutionContext, generator: object,
             default_provider: str = "qwen") -> dict:
    truncated = bool(context.variables.get(MODEL_OUTPUT_TRUNCATED_VARIABLE))
    repair_mode = _resolve_repair_mode(config, inputs)
    provider = str(inputs.get("provider") or default_provider)
    kwargs: dict = {"truncated": truncated}
    if repair_mode != "off" and _accepts_kwarg(generator.validate, "repair_mode"):
        kwargs["repair_mode"] = repair_mode
        if _accepts_kwarg(generator.validate, "provider"):
            kwargs["provider"] = provider
        emit = context.emit
        if emit is not None and _accepts_kwarg(generator.validate, "on_repaired_claim"):
            def publish_repaired(payload: dict) -> None:
                for event in _stream_events(payload):
                    emit(event)

            kwargs["on_repaired_claim"] = publish_repaired
    answer = generator.validate(inputs["draft"], inputs["evidence"], **kwargs)
    for key in DIAGNOSTIC_KEYS:
        if key in answer:
            context.node_metadata[key] = answer[key]
    degraded = context.variables.get(RERANK_DEGRADED_VARIABLE)
    if degraded:
        reason = (degraded.get("reason", "reranker_unavailable")
                  if isinstance(degraded, dict) else "reranker_unavailable")
        # 公开结果里明确标出“这条答案是在检索降级状态下生成的”，而不是让调用方
        # 从分数或日志里猜。字段是附加的，不改变既有 answer/citations/abstained。
        answer = {**answer, "degraded": True, "degraded_reason": reason}
    return {"answer": answer}
