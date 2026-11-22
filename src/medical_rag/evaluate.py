"""Opt-in command-line entry point for offline RAG evaluation."""

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from .config import Settings
from .evaluation import evaluate_cases, evaluate_workflow_cases, load_dataset
from .evaluation_anchors import source_sha256
from .evaluation_dataset import validate_quotas
from .evaluation_judge import DisabledJudge
from .evaluation_tools import validate_release
from .evaluation_workflow import resolve_workflow_version
from .ragas_adapter import RagasConfig, merge_ragas_report, normalize_metric_names
from .service import DEFAULT_WORKFLOWS
from .workflow.repository import WorkflowRepository


_DERIVED_RUNTIME_FIELDS = {"relevant_chunk_ids", "chunk_grades", "context_chunk_ids"}


def _write_report(path: Path, report: dict) -> None:
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _live_dependencies(settings: Settings, answer_provider: str, judge_provider: str) -> tuple:
    if not settings.qwen_api_key:
        raise ValueError("模型 API Key 未配置")
    from pymilvus import MilvusClient

    from .embeddings import BgeEmbedder, BgeReranker
    from .evaluation_judge import EvaluationJudge
    from .generation import OpenAITransport, build_generator
    from .service import RagService
    from .store import MilvusStore

    client = MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or None)
    store = MilvusStore(client, settings.collection_name)
    store.ensure_schema()
    service = RagService(settings, store, BgeEmbedder(), BgeReranker(), build_generator(settings))
    judge = EvaluationJudge(OpenAITransport(settings), judge_provider, settings.qwen_model)
    return service, judge


def main(argv: list[str] | None = None, factory: object | None = None,
         ragas_factory: object | None = None) -> int:
    """Validate inputs before constructing paid or networked dependencies."""
    parser = argparse.ArgumentParser(description="运行离线 RAG 检索与答案评估")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, help="版本化题库的发布清单；默认读取数据集同目录的 manifest.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    # Labels recorded in the report; one endpoint is configured, so any vendor name is allowed.
    parser.add_argument("--answer-provider", default="qwen")
    parser.add_argument("--judge-provider", default="qwen")
    parser.add_argument("--workflow-id", help="显式选择普通已发布工作流；未传时保留旧评估模式")
    parser.add_argument("--workflow-version", type=int, help="固定已发布版本；必须同时传 workflow-id")
    parser.add_argument("--ragas", action="store_true", help="同时运行 Ragas 指标评测")
    parser.add_argument("--ragas-metrics", default=None,
                        help="Ragas 指标，逗号分隔或 all；MRR/NDCG 等由本地指标计算")
    parser.add_argument("--ragas-limit", type=int, default=None, help="只对前 N 题做 Ragas 评分")
    parser.add_argument("--ragas-concurrency", type=int, default=None, help="Ragas LLM 并发数，默认 4")
    parser.add_argument("--ragas-strictness", type=int, default=None,
                        help="answer_relevancy 生成问题数，默认 3")
    parser.add_argument("--ragas-max-retries", type=int, default=None,
                        help="ContextRelevance 内部重试次数，默认 1")
    parser.add_argument("--ragas-raw-output", type=Path, default=None,
                        help="只写出 ragas block 的路径")
    parser.add_argument("--keep-local-judge", action="store_true",
                        help="Ragas 模式仍运行本地 LLM judge，用于同口径对比")
    parser.add_argument("--no-chinese-prompts", action="store_true",
                        help="Ragas 指标回退到上游英文 prompt（默认使用中文对齐 prompt）")
    parser.add_argument("--ragas-repeats", type=int, default=None,
                        help="LLM 判分指标的重复采样次数，取中位数；默认 3，1 关闭重复")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if not 1 <= args.top_k <= 20:
        print("top-k 必须在 1 到 20 之间", file=sys.stderr)
        return 2
    if args.dataset.resolve() == args.output.resolve():
        print("题集和结果不能使用相同路径", file=sys.stderr)
        return 2
    if args.workflow_version is not None and not args.workflow_id:
        print("workflow-version 必须同时指定 workflow-id", file=sys.stderr)
        return 2

    ragas_flags = (
        args.ragas_metrics is not None
        or args.ragas_limit is not None
        or args.ragas_concurrency is not None
        or args.ragas_strictness is not None
        or args.ragas_max_retries is not None
        or args.ragas_raw_output is not None
        or args.keep_local_judge
        or args.no_chinese_prompts
        or args.ragas_repeats is not None
    )
    ragas_config: RagasConfig | None = None
    if not args.ragas:
        if ragas_flags:
            print("Ragas 参数必须与 --ragas 一起使用", file=sys.stderr)
            return 2
    else:
        if ragas_factory is None and importlib.util.find_spec("ragas") is None:
            print("未安装 ragas；请安装 .[ragas] 或使用独立 Ragas 环境", file=sys.stderr)
            return 2
        try:
            ragas_config = RagasConfig(
                metrics=normalize_metric_names(args.ragas_metrics),
                concurrency=(args.ragas_concurrency
                             if args.ragas_concurrency is not None else 4),
                strictness=(args.ragas_strictness
                            if args.ragas_strictness is not None else 3),
                max_retries=args.ragas_max_retries if args.ragas_max_retries is not None else 1,
                limit=args.ragas_limit,
                chinese_prompts=not args.no_chinese_prompts,
                repeats=args.ragas_repeats if args.ragas_repeats is not None else 3,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.ragas_raw_output is not None and args.ragas_raw_output.resolve() in {
            args.dataset.resolve(), args.output.resolve()
        }:
            print("Ragas 原始输出不能覆盖题集或最终报告", file=sys.stderr)
            return 2
    try:
        workflow_version = None
        if args.workflow_id is not None:
            if not args.answer_provider.strip():
                raise ValueError("provider 不能为空")
            workflow_version = resolve_workflow_version(
                WorkflowRepository(DEFAULT_WORKFLOWS), args.workflow_id, args.workflow_version)
    except ValueError:
        print("工作流选择无效：请选择普通已发布工作流与正整数版本", file=sys.stderr)
        return 2
    try:
        cases = load_dataset(args.dataset)
        manifest_path = args.manifest
        if manifest_path is None and (args.dataset.parent / "manifest.json").is_file():
            manifest_path = args.dataset.parent / "manifest.json"
        manifest = None
        if manifest_path is not None:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest 格式无效")
            validate_quotas(cases, manifest)
            release_path = manifest_path.parent / "questions.jsonl"
            registry_path = manifest_path.parent / "source-registry.json"
            validate_release(release_path, manifest_path, registry_path)
            release_cases = load_dataset(release_path)
            if len(cases) != len(release_cases) or any(
                {key: value for key, value in runtime.items() if key not in _DERIVED_RUNTIME_FIELDS}
                != {key: value for key, value in original.items() if key not in _DERIVED_RUNTIME_FIELDS}
                for runtime, original in zip(cases, release_cases)
            ):
                raise ValueError("运行时题集与经校验的发布题库内容不一致")
            if any(case.get("answerability") == "answerable" and not case["relevant_chunk_ids"]
                   for case in cases):
                raise ValueError("可回答题须先映射到相关 chunk")
    except (OSError, UnicodeError, ValueError):
        print("题集读取或校验失败：请检查 JSONL 格式和字段", file=sys.stderr)
        return 2
    try:
        settings = Settings()
        service, judge = (factory or _live_dependencies)(settings, args.answer_provider, args.judge_provider)
        if args.ragas and not args.keep_local_judge:
            judge = DisabledJudge()
    except Exception:  # noqa: BLE001 -- CLI boundary must not expose credentials or responses
        print("评估依赖初始化失败：请检查 Milvus 和模型配置", file=sys.stderr)
        return 1
    try:
        if args.workflow_id is None:
            report = evaluate_cases(cases, service, judge, args.top_k, args.answer_provider, args.judge_provider)
        else:
            report = asyncio.run(evaluate_workflow_cases(
                cases, service, judge, args.top_k, args.answer_provider,
                args.judge_provider, args.workflow_id, workflow_version))
        if manifest is not None:
            report["release_dataset_sha256"] = source_sha256(release_path)
            report["runtime_dataset_sha256"] = source_sha256(args.dataset)
            report["source_registry_sha256"] = source_sha256(registry_path)
            report["dataset_version"] = manifest.get("dataset_version")
            report["rubric_version"] = manifest.get("rubric_version")
            report["config"]["dataset_version"] = manifest.get("dataset_version")
            report["config"]["rubric_version"] = manifest.get("rubric_version")
            counts = {}
            for case in cases:
                answerability = case["answerability"]
                counts[answerability] = counts.get(answerability, 0) + 1
            report["summary"]["answerability_counts"] = counts
    except Exception:  # noqa: BLE001 -- safe report/runner boundary preserves legacy exit codes
        print("评估运行失败", file=sys.stderr)
        return 1
    try:
        # Write the expensive local result first; a later Ragas failure must not
        # discard hours of retrieval/generation work.
        _write_report(args.output, report)
    except Exception:  # noqa: BLE001 -- CLI boundary must not expose paths or responses
        print("报告写入失败", file=sys.stderr)
        return 1

    if args.ragas and ragas_config is not None:
        try:
            runner = ragas_factory
            if runner is None:
                from .ragas_evaluation import run_ragas_evaluation

                def runner(current_report: dict, current_config: RagasConfig) -> dict:
                    return run_ragas_evaluation(
                        current_report, current_config, settings=settings)
            block = runner(report, ragas_config)
            merge_ragas_report(
                report,
                block,
                overwrite_judge_fields=not args.keep_local_judge,
                model=settings.qwen_model,
            )
            _write_report(args.output, report)
        except Exception:  # noqa: BLE001 -- sanitize the Ragas boundary
            report["ragas"] = {"enabled": True, "error": "RAGAS_FAILED"}
            report.setdefault("config", {})["ragas"] = {"error": "RAGAS_FAILED"}
            try:
                _write_report(args.output, report)
            except Exception:  # noqa: BLE001 -- report already existed; keep exit code safe
                print("报告写入失败", file=sys.stderr)
                return 1
            print("Ragas 评估失败", file=sys.stderr)
            return 1
        if args.ragas_raw_output is not None:
            try:
                _write_report(args.ragas_raw_output, report["ragas"])
            except Exception:  # noqa: BLE001 -- the merged report is already safe
                print("Ragas 原始输出写入失败", file=sys.stderr)
                return 1

    print(f"评估完成：{len(cases)} 题，报告保存至 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
