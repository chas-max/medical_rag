"""Aggregate ChatGPT, Claude Code, and Qoder rubric results without model calls."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from medical_rag.evaluation_judge_aggregation import aggregate_judges  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="三平台独立裁判结果汇总")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--judge-input", type=Path, required=True)
    parser.add_argument("--chatgpt", type=Path, required=True)
    parser.add_argument("--claude", type=Path, required=True)
    parser.add_argument("--qoder", type=Path, required=True)
    parser.add_argument("--rubric-version", default="medical-health-v1.0")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = aggregate_judges(args.dataset, [args.chatgpt, args.claude, args.qoder],
                                  args.output, rubric_version=args.rubric_version,
                                  judge_input_path=args.judge_input)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"aggregation failed: {exc}", file=sys.stderr)
        return 2
    print(f"judged {report['valid_answer_count']} answerable cases; "
          f"incomplete={report['incomplete_judging_count']}; "
          f"score={report['answer_score_100']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
