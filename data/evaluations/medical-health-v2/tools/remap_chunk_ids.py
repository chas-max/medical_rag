"""CLI wrapper for deterministic source-anchor remapping (v2)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from medical_rag.evaluation_tools import remap_release  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve v2 evaluation anchors to runtime chunks")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--chunks", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = remap_release(args.dataset, args.chunks, args.registry, args.output)
    except (OSError, ValueError) as exc:
        print(f"mapping failed: {exc}", file=sys.stderr)
        return 2
    print(f"remapped {result['question_count']} questions; mappings={result['mapping_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
