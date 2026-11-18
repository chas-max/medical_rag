"""CLI wrapper for secret-free platform-neutral judge export (v2)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from medical_rag.evaluation_tools import export_judge_cases  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export v2 evaluation cases for an external judge")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        count = export_judge_cases(args.report, args.dataset, args.output)
    except (OSError, ValueError) as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 2
    print(f"exported {count} judge cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
