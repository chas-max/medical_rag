"""Opt-in asynchronous evaluation of one pinned published answer workflow."""

from time import perf_counter

from .evaluation_report import build_report, new_case, score_answer, score_evidence
from .workflow.bootstrap import build_registry
from .workflow.validator import validate_workflow


def resolve_workflow_version(repository: object, workflow_id: str,
                             workflow_version: int | None = None) -> int:
    """Resolve highest published, never highest draft, and refuse iteration runs."""
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ValueError("工作流 ID 不能为空")
    if workflow_version is not None and (
        type(workflow_version) is not int or workflow_version < 1
    ):
        raise ValueError("工作流版本必须为正整数")
    try:
        definition = (repository.get_published(workflow_id) if workflow_version is None
                      else repository.get(workflow_id, workflow_version))
    except (KeyError, ValueError) as exc:
        raise ValueError("工作流不存在或未发布") from exc
    if definition.status != "published" or definition.kind != "workflow":
        raise ValueError("仅可评估已发布的普通工作流，不能直接运行迭代子图")
    # Catalog/graph validation is pure: no model, store or embedder is initialized.
    registry = build_registry(None, None, None, None, repository=repository)
    if validate_workflow(definition, registry):
        raise ValueError("工作流校验未通过")
    return definition.version


async def evaluate_workflow_cases(cases: list[dict], service: object, judge: object,
                                  top_k: int, answer_provider: str, judge_provider: str,
                                  workflow_id: str, workflow_version: int | None = None) -> dict:
    """Run the real graph and judge its answer-producing final evidence.

    Callers in an event loop should await this function. Only the CLI creates
    an event loop; the evaluator never calls ``asyncio.run``.
    """
    if type(top_k) is not int or not 1 <= top_k <= 20 or not cases:
        raise ValueError("top_k 必须在 1 到 20 之间，题集不能为空")
    if not isinstance(answer_provider, str) or not answer_provider.strip():
        raise ValueError("provider 必须为非空字符串")
    if any(not isinstance(case.get("question"), str) or not case["question"].strip()
           for case in cases):
        raise ValueError("问题不能为空")
    version = resolve_workflow_version(service.repository, workflow_id, workflow_version)
    results = []
    for case in cases:
        result = new_case(case)
        result.update({"workflow_id": workflow_id, "workflow_version": version,
                       "run_id": None, "run_status": None, "latency_ms": None,
                       "agent_model_calls": None, "retrieval_rounds": None, "agentic": []})
        results.append(result)
        started = perf_counter()
        try:
            run = await service.run_workflow(
                workflow_id, {"question": case["question"], "top_k": top_k,
                              "provider": answer_provider}, version=version)
        except Exception:  # noqa: BLE001 -- isolate cases and never persist provider responses
            result["error"] = "WORKFLOW_FAILED"
            continue
        finally:
            # Graph wall time excludes the separate judge calls.
            result["latency_ms"] = (perf_counter() - started) * 1000
        result.update({"run_id": run.run_id, "run_status": run.status})
        reports = [entry.metadata["agentic"] for entry in run.trace
                   if isinstance(entry.metadata.get("agentic"), dict)]
        result["agentic"] = reports
        if reports:
            result["agent_model_calls"] = sum(item["model_calls"] for item in reports)
            result["retrieval_rounds"] = sum(item["retrieval_rounds"] for item in reports)
        if run.status == "failed":
            if run.evidence:
                score_evidence(result, run.evidence, top_k)
            result["error"] = "WORKFLOW_FAILED"
            continue
        score_evidence(result, run.evidence, top_k)
        if not isinstance(run.output.get("answer"), str) or type(run.output.get("abstained")) is not bool:
            result["error"] = "ANSWER_FAILED"
            continue
        score_answer(result, run.output, run.evidence, judge)
    report = build_report(results, judge, top_k, answer_provider, judge_provider)
    report["config"].update({"workflow_id": workflow_id, "workflow_version": version})
    return report
