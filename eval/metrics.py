"""Metric aggregation and the threshold sweep (pure functions over cache entries).

Everything here operates on already-cached entries (see ``eval.cache``) and never
calls a model, so it is fully unit-testable on synthetic data and re-runnable for
free. Two computations implement the evaluation methodology (data spec section 6):

- :func:`compute_field_metrics` -- per-field precision / recall / F1 over the
  whole slice, restricted to the fields a dataset labels.
- :func:`sweep_thresholds` -- for each candidate threshold, replay the real
  ``route`` over the cached ``(confidence, validation)`` pairs (honoring the
  hard-failure override) and report auto-accept volume and the precision/recall
  of the auto-accepted critical fields -- the precision/recall trade-off curve.

Definitions (field level, against ground truth):

- A predicted value is *present* if it normalizes to a non-absent value; a match
  requires the gold to be present too and the normalized values to agree.
- *precision* = matches / predicted-present. *recall* = matches / gold-present.
- *F1* = harmonic mean.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from doc_agent.routing.score import route

from eval.cache import report_from_dict
from eval.normalize import is_present, values_match

# Critical, precision-prioritised fields (data spec section 2 / CLAUDE.md).
CRITICAL_FIELDS: tuple[str, ...] = ("total", "tax", "invoice_number")

# Threshold grid for the sweep: 0.50 -> 0.99 inclusive at 0.01 steps.
THRESHOLDS: tuple[float, ...] = tuple(round(0.50 + 0.01 * i, 2) for i in range(50))


def _f1(precision: float | None, recall: float | None) -> float | None:
    """Harmonic mean of precision and recall (``None`` if both are undefined)."""
    if precision is None and recall is None:
        return None
    if not precision or not recall:  # covers None or 0.0 on either side
        return 0.0
    return 2 * precision * recall / (precision + recall)


@dataclass(frozen=True)
class FieldMetric:
    """Precision / recall / F1 for one field over a slice.

    Attributes:
        field: The ``Document`` field name.
        n_pred: Number of examples where the pipeline produced a value.
        n_gold: Number of examples where the gold labels a value.
        n_match: Number of examples where prediction and gold agree.
    """

    field: str
    n_pred: int
    n_gold: int
    n_match: int

    @property
    def precision(self) -> float | None:
        """matches / predicted-present, or ``None`` if nothing was predicted."""
        return self.n_match / self.n_pred if self.n_pred else None

    @property
    def recall(self) -> float | None:
        """matches / gold-present, or ``None`` if there is no gold."""
        return self.n_match / self.n_gold if self.n_gold else None

    @property
    def f1(self) -> float | None:
        """Harmonic mean of precision and recall."""
        return _f1(self.precision, self.recall)


@dataclass(frozen=True)
class SweepRow:
    """One threshold's auto-accept volume and critical-field trade-off.

    Attributes:
        threshold: The candidate auto-accept threshold.
        n_total: Total examples in the slice.
        n_accepted: How many examples ``route`` auto-accepts at this threshold.
        crit_pred: Predicted-present critical values among accepted examples
            (the denominator of auto-accept precision).
        crit_match: Correct critical values among accepted examples.
        crit_gold_total: Gold-present critical values across the whole slice
            (the denominator of critical recall).
    """

    threshold: float
    n_total: int
    n_accepted: int
    crit_pred: int
    crit_match: int
    crit_gold_total: int

    @property
    def accept_rate(self) -> float:
        """Fraction of the slice auto-accepted at this threshold."""
        return self.n_accepted / self.n_total if self.n_total else 0.0

    @property
    def crit_precision(self) -> float | None:
        """Precision on critical fields over the auto-accepted subset.

        This is the metric the operating point targets (>= 0.98). ``None`` when
        no critical value was auto-accepted (precision undefined).
        """
        return self.crit_match / self.crit_pred if self.crit_pred else None

    @property
    def crit_recall(self) -> float | None:
        """Correctly auto-accepted critical values / all gold critical values.

        The recall "kept" at this threshold; the rest is review-queue volume.
        """
        return self.crit_match / self.crit_gold_total if self.crit_gold_total else None


def compute_field_metrics(
    entries: Sequence[dict[str, Any]],
    fields: Sequence[str],
) -> list[FieldMetric]:
    """Compute per-field precision/recall/F1 over the slice.

    Args:
        entries: Cached prediction entries (each with ``predicted`` and ``gold``).
        fields: The ``Document`` field names to score (a dataset's labeled set).

    Returns:
        One :class:`FieldMetric` per field, in the order of ``fields``.
    """
    metrics: list[FieldMetric] = []
    for field in fields:
        n_pred = n_gold = n_match = 0
        for entry in entries:
            predicted = entry.get("predicted", {}).get(field)
            gold = entry.get("gold", {}).get(field)
            if is_present(field, predicted):
                n_pred += 1
            if is_present(field, gold):
                n_gold += 1
            if values_match(field, predicted, gold):
                n_match += 1
        metrics.append(FieldMetric(field, n_pred, n_gold, n_match))
    return metrics


def sweep_thresholds(
    entries: Sequence[dict[str, Any]],
    critical_fields: Sequence[str],
    thresholds: Sequence[float] = THRESHOLDS,
) -> list[SweepRow]:
    """Replay ``route`` across thresholds and measure the critical-field trade-off.

    For each threshold the real ``route`` is applied to every entry's cached
    ``(confidence, validation)`` pair -- so a hard-failure entry is forced to
    review at *every* threshold, exactly as in production -- and the auto-accepted
    subset's critical-field precision and recall are computed. No inference runs.

    Args:
        entries: Cached prediction entries.
        critical_fields: The critical fields the dataset labels (the subset of
            ``total``/``tax``/``invoice_number`` with gold present).
        thresholds: The candidate thresholds to sweep. Defaults to
            :data:`THRESHOLDS` (0.50->0.99).

    Returns:
        One :class:`SweepRow` per threshold, in ``thresholds`` order.
    """
    reports = {entry["id"]: report_from_dict(entry.get("validation", {})) for entry in entries}
    n_total = len(entries)

    # Denominator for critical recall: gold-present critical values across all.
    crit_gold_total = sum(
        1
        for entry in entries
        for field in critical_fields
        if is_present(field, entry.get("gold", {}).get(field))
    )

    rows: list[SweepRow] = []
    for threshold in thresholds:
        crit_pred = crit_match = n_accepted = 0
        for entry in entries:
            report = reports[entry["id"]]
            decision = route(entry.get("confidence", 0.0), report, threshold=threshold)
            if decision != "accept":
                continue
            n_accepted += 1
            for field in critical_fields:
                predicted = entry.get("predicted", {}).get(field)
                gold = entry.get("gold", {}).get(field)
                if is_present(field, predicted):
                    crit_pred += 1
                    if values_match(field, predicted, gold):
                        crit_match += 1
        rows.append(
            SweepRow(
                threshold=threshold,
                n_total=n_total,
                n_accepted=n_accepted,
                crit_pred=crit_pred,
                crit_match=crit_match,
                crit_gold_total=crit_gold_total,
            )
        )
    return rows


def confidence_histogram(
    entries: Sequence[dict[str, Any]],
    ndigits: int = 2,
) -> dict[float, int]:
    """Count cached confidence scores, rounded, for distribution reporting.

    Surfaces why the sweep looks the way it does: when a backend exposes no
    per-field confidence the scorer starts from a neutral 0.5, capping scores at
    0.5, so almost nothing clears a threshold above 0.5.

    Args:
        entries: Cached prediction entries.
        ndigits: Rounding precision for bucketing confidences.

    Returns:
        A dict mapping rounded confidence to count, ascending by confidence.
    """
    counts: dict[float, int] = {}
    for entry in entries:
        bucket = round(float(entry.get("confidence", 0.0)), ndigits)
        counts[bucket] = counts.get(bucket, 0) + 1
    return dict(sorted(counts.items()))


def smallest_threshold_meeting(
    rows: Sequence[SweepRow],
    target_precision: float,
) -> SweepRow | None:
    """Return the lowest-threshold row whose critical precision meets a target.

    Reported as analysis only -- the operator chooses the actual threshold.

    Args:
        rows: Sweep rows (assumed ascending by threshold).
        target_precision: The critical auto-accept precision to meet (e.g. 0.98).

    Returns:
        The first row (lowest threshold) with a defined critical precision at or
        above ``target_precision`` and at least one auto-accepted example, or
        ``None`` if no threshold achieves it.
    """
    for row in rows:
        if (
            row.n_accepted > 0
            and row.crit_precision is not None
            and row.crit_precision >= target_precision
        ):
            return row
    return None
