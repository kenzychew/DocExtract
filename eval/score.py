"""Score phase: compute all metrics and the threshold sweep from the cache.

Purely offline -- it loads cached predictions (``eval.cache``), runs the pure
aggregations (``eval.metrics``), and formats human-readable tables. Re-running
with a different threshold grid or comparison never re-runs inference, which is
the whole point of the two-phase split.

The formatted output covers the four things the methodology asks for (data spec
section 6): per-field precision/recall/F1, per-critical-field metrics, document
routing stats, and the threshold sweep trade-off curve. It also prints the
confidence distribution (so a flat sweep is explained by the backend exposing no
per-field confidence) and, as analysis only, the lowest threshold that reaches a
target auto-accept precision -- the operator still chooses the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval.cache import DEFAULT_CACHE_BASE, read_entries
from eval.metrics import (
    CRITICAL_FIELDS,
    THRESHOLDS,
    FieldMetric,
    SweepRow,
    compute_field_metrics,
    confidence_histogram,
    smallest_threshold_meeting,
    sweep_thresholds,
)
from eval.normalize import is_present

# Auto-accept precision target on critical fields (data spec section 6).
TARGET_CRITICAL_PRECISION: float = 0.98


@dataclass(frozen=True)
class ScoreReport:
    """Everything the score phase computed for one dataset slice."""

    dataset: str
    n: int
    labeled_fields: tuple[str, ...]
    critical_labeled: tuple[str, ...]
    field_metrics: list[FieldMetric]
    sweep: list[SweepRow]
    confidence_hist: dict[float, int]
    n_error: int


def _labeled_fields(entries: list[dict[str, Any]]) -> tuple[str, ...]:
    """Union of the ``labeled_fields`` recorded across cached entries."""
    seen: list[str] = []
    for entry in entries:
        for field in entry.get("labeled_fields", []):
            if field not in seen:
                seen.append(field)
    return tuple(seen)


def _critical_labeled(labeled: tuple[str, ...], entries: list[dict[str, Any]]) -> tuple[str, ...]:
    """Critical fields the dataset actually labels *and* has gold present for.

    A critical field with no gold anywhere in the slice cannot be scored, so it
    is excluded from the critical-precision denominators.
    """
    result: list[str] = []
    for field in CRITICAL_FIELDS:
        if field not in labeled:
            continue
        if any(is_present(field, entry.get("gold", {}).get(field)) for entry in entries):
            result.append(field)
    return tuple(result)


def build_report(
    dataset: str,
    *,
    cache_base: Path = DEFAULT_CACHE_BASE,
    thresholds: tuple[float, ...] = THRESHOLDS,
) -> ScoreReport:
    """Load the cache for a dataset and compute the full score report.

    Args:
        dataset: Dataset name whose cache to score.
        cache_base: Root cache directory. Defaults to ``eval/cache``.
        thresholds: The threshold grid to sweep.

    Returns:
        A :class:`ScoreReport`.

    Raises:
        FileNotFoundError: If no cached entries exist for the dataset.
    """
    entries = read_entries(cache_base, dataset)
    if not entries:
        raise FileNotFoundError(
            f"No cached predictions for dataset {dataset!r} under {cache_base}. "
            "Run the predict phase first."
        )

    labeled = _labeled_fields(entries)
    critical_labeled = _critical_labeled(labeled, entries)
    field_metrics = compute_field_metrics(entries, labeled)
    sweep = sweep_thresholds(entries, critical_labeled, thresholds)
    hist = confidence_histogram(entries)
    n_error = sum(1 for entry in entries if entry.get("error"))

    return ScoreReport(
        dataset=dataset,
        n=len(entries),
        labeled_fields=labeled,
        critical_labeled=critical_labeled,
        field_metrics=field_metrics,
        sweep=sweep,
        confidence_hist=hist,
        n_error=n_error,
    )


# --- Formatting ----------------------------------------------------------------


def _pct(value: float | None) -> str:
    """Format an optional ratio as a percentage, or 'n/a' when undefined."""
    return "  n/a" if value is None else f"{value * 100:5.1f}%"


def _format_field_table(report: ScoreReport) -> list[str]:
    lines = [
        "Per-field metrics (whole slice):",
        f"  {'field':<16} {'P':>7} {'R':>7} {'F1':>7}   {'pred':>4} {'gold':>4} {'ok':>4}",
    ]
    for metric in report.field_metrics:
        marker = " *" if metric.field in report.critical_labeled else "  "
        lines.append(
            f"{marker}{metric.field:<16} {_pct(metric.precision)} {_pct(metric.recall)} "
            f"{_pct(metric.f1)}   {metric.n_pred:>4} {metric.n_gold:>4} {metric.n_match:>4}"
        )
    lines.append("  (* = critical field)")
    return lines


def _format_sweep_table(report: ScoreReport, coarse_step: int = 5) -> list[str]:
    lines = [
        "Threshold sweep (critical fields on the auto-accepted subset):",
        f"  {'thr':>5} {'accept':>7} {'accept%':>8} {'crit P':>8} {'crit R':>8}",
    ]
    for index, row in enumerate(report.sweep):
        # Print a coarse grid plus the final threshold to keep it readable.
        is_grid = index % coarse_step == 0 or index == len(report.sweep) - 1
        if not is_grid:
            continue
        lines.append(
            f"  {row.threshold:>5.2f} {row.n_accepted:>7} {row.accept_rate * 100:>7.1f}% "
            f"{_pct(row.crit_precision)} {_pct(row.crit_recall)}"
        )
    return lines


def _format_confidence(report: ScoreReport) -> list[str]:
    lines = ["Confidence distribution (cached scores):"]
    for value, count in report.confidence_hist.items():
        bar = "#" * count
        lines.append(f"  {value:>5.2f}  {count:>3}  {bar}")
    return lines


def _format_routing(report: ScoreReport) -> list[str]:
    target = smallest_threshold_meeting(report.sweep, TARGET_CRITICAL_PRECISION)
    lines = [
        "Operating point analysis (you choose the threshold):",
        f"  Target: auto-accept precision on critical fields "
        f"{report.critical_labeled or '(none labeled)'} >= "
        f"{TARGET_CRITICAL_PRECISION:.2f}",
    ]
    if not report.critical_labeled:
        lines.append(
            "  This dataset labels none of total/tax/invoice_number, so critical "
            "auto-accept precision cannot be measured here."
        )
    elif target is None:
        lines.append(
            "  No threshold in the sweep reaches the target with any "
            "auto-accepted document (see the confidence distribution above)."
        )
    else:
        lines.append(
            f"  Lowest qualifying threshold: {target.threshold:.2f} "
            f"(accept {target.n_accepted}/{target.n_total} = "
            f"{target.accept_rate * 100:.1f}%, crit P {_pct(target.crit_precision)}, "
            f"crit R {_pct(target.crit_recall)})."
        )
    return lines


def format_report(report: ScoreReport) -> str:
    """Render a :class:`ScoreReport` as a plain-text report.

    Args:
        report: The computed score report.

    Returns:
        A multi-line string ready to print.
    """
    header = [
        "=" * 68,
        f"Evaluation: {report.dataset}  (n={report.n}, errors={report.n_error})",
        f"Labeled fields: {', '.join(report.labeled_fields) or '(none)'}",
        "=" * 68,
    ]
    sections = [
        header,
        _format_field_table(report),
        _format_confidence(report),
        _format_sweep_table(report),
        _format_routing(report),
    ]
    return "\n\n".join("\n".join(section) for section in sections)
