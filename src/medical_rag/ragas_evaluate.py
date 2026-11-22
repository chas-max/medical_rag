"""Post-hoc Ragas scoring of an existing local evaluation report.

The local report already contains the retrieved evidence and the generated
answer.  This CLI replays only the Ragas metrics, so changing the metric set or
the judge does not require re-running Milvus retrieval or generation.

Ragas is imported lazily so ``--help`` and argument validation work without the
optional evaluation dependency installed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from .config import Settings
from .evaluation_dataset import load_evaluation_dataset
from .ragas_adapter import RagasConfig, merge_ragas_report, normalize_metric_names

_REFERENCE_FIELDS = (
    "reference_answer",
    "key_points",
    "answerability",
    "category",
    "question_type",
    "difficulty",
    "source_refs",
    "relevant_chunk_ids",
    "chunk_grades",
    "context_chunk_ids",
)


def _write_report(path: Path, report: dict) -> None:
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _load_report(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("报告读取失败") from exc
    if not isinstance(value, dict) or not isinstance(value.get("cases"), list):
        raise ValueError("报告格式无效")
    return value


def _merge_dataset_fields(report: dict, dataset: list[dict]) -> None:
    rows = {row["id"]: row for row in dataset}
    seen: set[str] = set()
    for case in report["cases"]:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("报告题目格式无效")
        identifier = case["id"].strip()
        if not identifier or identifier in seen:
            raise ValueError("报告题目 ID 重复或为空")
        seen.add(identifier)
        source = rows.get(identifier)
        if source is None:
            raise ValueError("报告题目不在题集中")
        for field in _REFERENCE_FIELDS:
            if field not in case or case[field] is None:
                if field in source:
                    case[field] = source[field]


def main(
    argv: list[str] | None = None,
    runner: object | None = None,
    settings_factory: object | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="对已有 RAG 评测报告运行 Ragas 指标")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metrics", default=None,
                        help="Ragas 指标，逗号分隔或 all；MRR/NDCG 等由本地指标计算")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--strictness", type=int, default=3)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--raw-output", type=Path, default=None)
    parser.add_argument("--allow-paid-model", action="store_true",
                        help="确认会产生模型费用后才允许运行")
    parser.add_argument("--no-chinese-prompts", action="store_true",
                        help="指标回退到上游英文 prompt（默认使用中文对齐 prompt）")
    parser.add_argument("--repeats", type=int, default=3,
                        help="LLM 判分指标的重复采样次数，取中位数；1 关闭重复")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if not args.allow_paid_model:
        print("Ragas 评分需要显式 --allow-paid-model", file=sys.stderr)
        return 2
    if args.output.resolve() in {args.dataset.resolve(), args.report.resolve()}:
        print("输出文件不能覆盖题集或输入报告", file=sys.stderr)
        return 2
    if args.raw_output is not None and args.raw_output.resolve() in {
        args.dataset.resolve(), args.report.resolve(), args.output.resolve()
    }:
        print("Ragas 原始输出不能覆盖题集、输入报告或最终报告", file=sys.stderr)
        return 2
    try:
        config = RagasConfig(
            metrics=normalize_metric_names(args.metrics),
            concurrency=args.concurrency,
            strictness=args.strictness,
            max_retries=args.max_retries,
            limit=args.limit,
            chinese_prompts=not args.no_chinese_prompts,
            repeats=args.repeats,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if runner is None and importlib.util.find_spec("ragas") is None:
        print("未安装 ragas；请安装 .[ragas] 或使用独立 Ragas 环境", file=sys.stderr)
        return 2

    try:
        dataset = load_evaluation_dataset(args.dataset)
        report = _load_report(args.report)
        if not report["cases"]:
            raise ValueError("报告没有题目")
        _merge_dataset_fields(report, dataset)
    except (OSError, ValueError):
        print("题集或报告校验失败：请检查 JSONL/JSON 格式和题目 ID", file=sys.stderr)
        return 2

    try:
        settings = None
        if runner is None:
            settings = (settings_factory or Settings)()
            from .ragas_evaluation import run_ragas_evaluation

            def runner(current_report: dict, current_config: RagasConfig) -> dict:
                return run_ragas_evaluation(
                    current_report, current_config, settings=settings)

        block = runner(report, config)
        merge_ragas_report(report, block, overwrite_judge_fields=False)
        _write_report(args.output, report)
    except Exception:  # noqa: BLE001 -- sanitize dependency/model/report failures
        report["ragas"] = {"enabled": True, "error": "RAGAS_FAILED"}
        report.setdefault("config", {})["ragas"] = {"error": "RAGAS_FAILED"}
        try:
            _write_report(args.output, report)
        except Exception:  # noqa: BLE001 -- keep the safe exit code
            print("报告写入失败", file=sys.stderr)
            return 1
        print("Ragas 评分失败", file=sys.stderr)
        return 1
    if args.raw_output is not None:
        try:
            _write_report(args.raw_output, report["ragas"])
        except Exception:  # noqa: BLE001 -- merged output is already safe
            print("Ragas 原始输出写入失败", file=sys.stderr)
            return 1

    failures = block.get("failure_counts")
    if block.get("scored_count", 0) == 0 and isinstance(failures, dict) and any(
        int(value) for value in failures.values() if isinstance(value, int)
    ):
        print("Ragas 评分完成，但没有任何指标成功", file=sys.stderr)
        return 1
    print(
        f"Ragas 评估完成：{block.get('scored_count', 0)}/{block.get('sample_count', 0)} 题，"
        f"报告保存至 {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
