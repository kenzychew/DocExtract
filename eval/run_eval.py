"""Command-line entry point for the two-phase evaluation harness.

Usage::

    # Phase 1 -- runs the model, spends quota; start with a small slice.
    uv run python -m eval.run_eval predict --dataset sroie --limit 20

    # Phase 2 -- offline; recompute metrics and sweep as often as you like.
    uv run python -m eval.run_eval score --dataset sroie

The predict phase caches results under ``eval/cache/<dataset>/`` and is
idempotent (already-cached ids are skipped unless ``--overwrite``). The score
phase reads that cache and prints the metrics tables and threshold sweep; it
never re-runs inference.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from eval.cache import DEFAULT_CACHE_BASE
from eval.predict import run_predict
from eval.score import build_report, format_report


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", default="sroie", help="Dataset adapter name (default: sroie).")
    parser.add_argument(
        "--cache-base",
        type=Path,
        default=DEFAULT_CACHE_BASE,
        help="Root cache directory (default: eval/cache).",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval.run_eval", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    predict = subparsers.add_parser("predict", help="Run the model over a slice and cache results.")
    _add_common(predict)
    predict.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of examples to process (the held-out slice size; default: 20).",
    )
    predict.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-process and overwrite already-cached examples.",
    )

    score = subparsers.add_parser("score", help="Compute metrics + sweep from the cache (offline).")
    _add_common(score)
    score.add_argument(
        "--revalidate",
        action="store_true",
        help=(
            "Score under CURRENT validation/scoring rules by recomputing them from the "
            "cached predicted documents, instead of the scalars frozen in at predict "
            "time. Still offline (no API calls). Drift between the two is reported "
            "either way."
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 on success).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_parser().parse_args(argv)

    if args.command == "predict":
        stats = run_predict(
            args.dataset,
            args.limit,
            cache_base=args.cache_base,
            overwrite=args.overwrite,
        )
        print(
            f"\nPredict complete for {stats.dataset}: "
            f"processed={stats.processed} skipped={stats.skipped} "
            f"accepted={stats.accepted} review={stats.review} errors={stats.errors} "
            f"failed={stats.failed}\n"
            f"Now run: uv run python -m eval.run_eval score --dataset {stats.dataset}"
        )
        return 0

    if args.command == "score":
        report = build_report(
            args.dataset, cache_base=args.cache_base, revalidate=args.revalidate
        )
        print(format_report(report))
        return 0

    return 1  # pragma: no cover -- argparse enforces a valid subcommand.


if __name__ == "__main__":
    sys.exit(main())
