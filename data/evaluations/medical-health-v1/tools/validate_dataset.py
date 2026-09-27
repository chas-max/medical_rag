"""CLI wrapper for medical release validation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from medical_rag.evaluation_tools import validate_release  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a medical evaluation release")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = validate_release(args.dataset, args.manifest, args.registry)
    except (OSError, ValueError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        return 2
    print(f"validated {result['question_count']} questions; sources={len(result['source_ids'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
