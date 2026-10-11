"""Separate untrusted model drafting from citation validation."""

from ...generation import ANALYSIS_EVENT
from ..contracts import CAPABILITY_WEB_SEARCH, ExecutionContext, supports


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

        # 逐条推送已通过逐字校验的结论，最后再推一次分析段；未校验通过的内容不会离开生成器。
        # 联网能力必须原样传进去：漏传会让 draft() 退回 web_search=False，
        # 于是弱证据不补充检索、检索落空也不再交给生成阶段兜底（状态帧却还写着 true）。
        # draft() 只有本仓库的生成器提供；缺失时退回旧的 generate_stream 契约，后者只推结论。
        streamer = getattr(generator, "draft", None) or generator.generate_stream
        result = streamer(inputs["question"], inputs["evidence"], provider,
                          web_search=web_search, on_claim=publish)
    else:
        result = generator.generate(inputs["question"], inputs["evidence"], provider,
                                    web_search=web_search)
    # Web hits are evidence only for this node, so citation checking must see the same set.
    return {"draft": result["draft"], "evidence": result["evidence"]}


def validate(inputs: dict, config: dict, context: ExecutionContext, generator: object) -> dict:
    return {"answer": generator.validate(inputs["draft"], inputs["evidence"])}
