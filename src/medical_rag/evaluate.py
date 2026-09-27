"""Opt-in command-line entry point for offline RAG evaluation."""

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .evaluation import evaluate_cases, load_dataset
from .evaluation_dataset import validate_quotas
from .evaluation_anchors import source_sha256
from .evaluation_tools import validate_release


def _live_dependencies(settings: Settings, answer_provider: str, judge_provider: str) -> tuple:
    if not settings.qwen_api_key:
        raise ValueError("Qwen API Key 未配置")
    from pymilvus import MilvusClient

    from .embeddings import BgeEmbedder, BgeReranker
    from .evaluation_judge import EvaluationJudge
    from .generation import GroundedGenerator, OpenAITransport
    from .service import RagService
    from .store import MilvusStore

    client = MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or None)
    store = MilvusStore(client, settings.collection_name)
    store.ensure_schema()
    service = RagService(settings, store, BgeEmbedder(), BgeReranker(),
                         GroundedGenerator(OpenAITransport(settings)))
    judge = EvaluationJudge(OpenAITransport(settings), judge_provider, settings.qwen_model)
    return service, judge


def main(argv: list[str] | None = None, factory: object | None = None) -> int:
    """Validate inputs before constructing paid or networked dependencies."""
    parser = argparse.ArgumentParser(description="运行离线 RAG 检索与答案评估")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, help="版本化题库的发布清单；默认读取数据集同目录的 manifest.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--answer-provider", choices=("qwen",), default="qwen")
    parser.add_argument("--judge-provider", choices=("qwen",), default="qwen")
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
                {key: value for key, value in runtime.items() if key != "relevant_chunk_ids"}
                != {key: value for key, value in original.items() if key != "relevant_chunk_ids"}
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
    except Exception:
        print("评估依赖初始化失败：请检查 Milvus 和模型配置", file=sys.stderr)
        return 1
    try:
        report = evaluate_cases(cases, service, judge, args.top_k, args.answer_provider, args.judge_provider)
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
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                               encoding="utf-8")
    except Exception:
        print("评估运行或报告写入失败", file=sys.stderr)
        return 1
    print(f"评估完成：{len(cases)} 题，报告保存至 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
