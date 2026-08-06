"""Unit tests for the evaluation harness (build-plan phase 5).

Fully offline: no model calls, no dataset downloads. The comparison function is
tested directly, and the score-phase computation is tested on hand-built cached
entries with known, hand-computed metrics. The threshold sweep is tested for the
hard-failure override and the precision/recall trade-off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eval.cache import read_entries, report_from_dict, write_entry
from eval.metrics import (
    THRESHOLDS,
    compute_field_metrics,
    confidence_histogram,
    smallest_threshold_meeting,
    sweep_thresholds,
)
from eval.normalize import is_present, normalize, values_match
from eval.score import build_report


# ---------------------------------------------------------------------------
# Comparison function (normalize / values_match)
# ---------------------------------------------------------------------------


def test_money_matches_cent_exact() -> None:
    """Monetary values match only when equal at cent precision."""
    assert values_match("total", 193.0, "193.00")
    assert values_match("total", "1,234.56", "1234.56")
    assert values_match("total", 100.004, 100.0)  # sub-cent noise rounds away
    assert not values_match("total", 100.0, 100.01)  # a genuine 1-cent difference
    assert not values_match("total", 100.0, 105.0)


def test_money_rejects_relative_tolerance_error() -> None:
    """Regression: a materially-wrong total within 0.5% is NOT scored correct.

    The eval comparator must not adopt the reconciliation allowance, or a $2
    error on a $500 total (and $10 on $2000) would inflate critical precision.
    These gaps were inside the old 0.5% relative band; that band has since been
    removed from validation as well (FC-2), but the comparator must stay
    cent-exact whatever the validation allowance is, so this test stays.
    """
    assert not values_match("total", 502.0, "500.00")   # inside the old 0.5% band
    assert not values_match("total", 2010.0, 2000.0)     # +/-$10 window at $2000
    assert not values_match("tax", 9.05, 9.0)            # 5-cent tax error


def test_money_handles_currency_symbols_and_separators() -> None:
    """Currency symbols and thousands separators normalize away."""
    assert normalize("total", "$1,000.00") == pytest.approx(1000.0)
    assert values_match("total", "RM 193.00", "193.0")


def test_date_matches_day_first_format() -> None:
    """SROIE-style day-first dates match the ISO-cached prediction."""
    # gold "15/01/2019" (D/M/Y) vs predicted cached ISO "2019-01-15".
    assert values_match("document_date", "2019-01-15", "15/01/2019")
    assert not values_match("document_date", "2019-01-16", "15/01/2019")


def test_text_matches_case_and_whitespace_insensitive() -> None:
    """Text matches after lower-casing and whitespace collapsing."""
    assert values_match("vendor_name", "OJC  Marketing   SDN BHD", "ojc marketing sdn bhd")
    assert not values_match("vendor_name", "Acme Corp", "Beta LLC")


def test_absent_values_never_match() -> None:
    """A present prediction against absent gold (or vice versa) is not a match."""
    assert not values_match("total", 100.0, None)
    assert not values_match("total", None, 100.0)
    assert not values_match("vendor_name", "", "acme")
    assert not is_present("vendor_name", "   ")
    assert not is_present("total", "N/A")


def test_unparseable_money_is_absent() -> None:
    """An unparseable monetary string normalizes to None (absent), not a crash."""
    assert normalize("total", "not a number") is None
    assert not is_present("total", "abc")


# ---------------------------------------------------------------------------
# Synthetic cached entries with known metrics
# ---------------------------------------------------------------------------


def _validation(hard_failed: bool, *, hard_codes: list[str] | None = None) -> dict[str, Any]:
    """Build a minimal validation dict as ValidationReport.to_dict would."""
    results = []
    for code in hard_codes or []:
        results.append(
            {"code": code, "severity": "hard", "status": "fail", "message": "synthetic"}
        )
    return {
        "hard_failed": hard_failed,
        "results": results,
        "hard_failures": list(hard_codes or []),
        "soft_failures": [],
    }


def _entry(
    example_id: str,
    *,
    predicted: dict[str, Any],
    gold: dict[str, Any],
    confidence: float,
    hard_failed: bool = False,
    hard_codes: list[str] | None = None,
    labeled: tuple[str, ...] = ("vendor_name", "vendor_address", "document_date", "total"),
) -> dict[str, Any]:
    return {
        "id": example_id,
        "dataset": "synthetic",
        "gold": gold,
        "labeled_fields": list(labeled),
        "predicted": predicted,
        "confidence": confidence,
        "decision": "review",
        "modality": "image",
        "backend": "stub",
        "validation": _validation(hard_failed, hard_codes=hard_codes),
        "error": None,
    }


@pytest.fixture
def synthetic_entries() -> list[dict[str, Any]]:
    """Four entries with hand-computable per-field metrics.

    total field: preds present on all 4; golds present on all 4;
      - e1 correct, e2 correct, e3 wrong value, e4 correct  -> 3/4 match.
    vendor_name: preds present on 3 (e4 missing); golds present on 4;
      - e1 correct, e2 correct, e3 correct                  -> 3 match.
      => precision 3/3 = 1.0, recall 3/4 = 0.75.
    """
    return [
        _entry(
            "e1",
            predicted={"vendor_name": "Acme", "total": 100.0},
            gold={"vendor_name": "acme", "total": "100.00", "vendor_address": None,
                  "document_date": None},
            confidence=0.90,
        ),
        _entry(
            "e2",
            predicted={"vendor_name": "Beta", "total": 50.0},
            gold={"vendor_name": "beta", "total": "50.00", "vendor_address": None,
                  "document_date": None},
            confidence=0.80,
        ),
        _entry(
            "e3",
            predicted={"vendor_name": "Gamma", "total": 999.0},  # total wrong
            gold={"vendor_name": "gamma", "total": "10.00", "vendor_address": None,
                  "document_date": None},
            confidence=0.70,
        ),
        _entry(
            "e4",
            predicted={"vendor_name": None, "total": 25.0},  # vendor missing
            gold={"vendor_name": "delta", "total": "25.00", "vendor_address": None,
                  "document_date": None},
            confidence=0.60,
        ),
    ]


def test_field_metrics_match_hand_computed(synthetic_entries: list[dict[str, Any]]) -> None:
    """Per-field precision/recall/F1 equal the hand-computed values."""
    metrics = {m.field: m for m in compute_field_metrics(
        synthetic_entries, ("vendor_name", "total"))}

    total = metrics["total"]
    assert (total.n_pred, total.n_gold, total.n_match) == (4, 4, 3)
    assert total.precision == pytest.approx(0.75)
    assert total.recall == pytest.approx(0.75)
    assert total.f1 == pytest.approx(0.75)

    vendor = metrics["vendor_name"]
    assert (vendor.n_pred, vendor.n_gold, vendor.n_match) == (3, 4, 3)
    assert vendor.precision == pytest.approx(1.0)
    assert vendor.recall == pytest.approx(0.75)
    assert vendor.f1 == pytest.approx(2 * 1.0 * 0.75 / (1.0 + 0.75))


def test_field_precision_none_when_no_prediction() -> None:
    """Precision is None (undefined) when nothing was predicted for a field."""
    entries = [
        _entry("e1", predicted={"total": None}, gold={"total": "5.00"}, confidence=0.9),
    ]
    (metric,) = compute_field_metrics(entries, ("total",))
    assert metric.n_pred == 0
    assert metric.precision is None
    assert metric.recall == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Threshold sweep
# ---------------------------------------------------------------------------


def test_sweep_accept_count_falls_as_threshold_rises(
    synthetic_entries: list[dict[str, Any]],
) -> None:
    """Higher thresholds auto-accept no more documents than lower ones."""
    rows = sweep_thresholds(synthetic_entries, ("total",), THRESHOLDS)
    accept_counts = [row.n_accepted for row in rows]
    assert accept_counts == sorted(accept_counts, reverse=True)
    # At 0.50 every clean doc (confidence >= 0.50) is accepted; all 4 here.
    assert rows[0].threshold == 0.50
    assert rows[0].n_accepted == 4


def test_sweep_hard_failure_never_accepted() -> None:
    """A hard-failed document is forced to review at every threshold."""
    entries = [
        _entry(
            "hard",
            predicted={"total": 100.0},
            gold={"total": "100.00"},
            confidence=0.99,  # very confident...
            hard_failed=True,
            hard_codes=["H2"],  # ...but a hard rule failed.
        ),
    ]
    rows = sweep_thresholds(entries, ("total",), THRESHOLDS)
    assert all(row.n_accepted == 0 for row in rows)
    # And the reconstructed report reports the hard failure.
    report = report_from_dict(entries[0]["validation"])
    assert report.hard_failed is True


def test_sweep_critical_precision_and_recall(
    synthetic_entries: list[dict[str, Any]],
) -> None:
    """At threshold 0.50 the critical (total) precision/recall match hand calc.

    All 4 accepted; total correct on 3/4 => precision 0.75; gold present on 4 =>
    recall 3/4 = 0.75.
    """
    rows = sweep_thresholds(synthetic_entries, ("total",), THRESHOLDS)
    row_050 = rows[0]
    assert row_050.crit_pred == 4
    assert row_050.crit_match == 3
    assert row_050.crit_precision == pytest.approx(0.75)
    assert row_050.crit_recall == pytest.approx(0.75)


def test_smallest_threshold_meeting_target() -> None:
    """The lowest qualifying threshold is found when a clean high-conf doc exists."""
    entries = [
        _entry("ok", predicted={"total": 10.0}, gold={"total": "10.00"}, confidence=0.95),
    ]
    rows = sweep_thresholds(entries, ("total",), THRESHOLDS)
    target = smallest_threshold_meeting(rows, 0.98)
    assert target is not None
    # confidence 0.95 accepts for thresholds <= 0.95; precision is 1.0 (perfect).
    assert target.threshold == 0.50
    assert target.crit_precision == pytest.approx(1.0)


def test_smallest_threshold_meeting_none_when_unreachable() -> None:
    """Returns None when no threshold reaches the target precision."""
    entries = [
        _entry("bad", predicted={"total": 999.0}, gold={"total": "10.00"}, confidence=0.95),
    ]
    rows = sweep_thresholds(entries, ("total",), THRESHOLDS)
    assert smallest_threshold_meeting(rows, 0.98) is None


def test_confidence_histogram_counts() -> None:
    """The histogram buckets rounded confidences."""
    entries = [
        _entry("a", predicted={}, gold={}, confidence=0.50),
        _entry("b", predicted={}, gold={}, confidence=0.50),
        _entry("c", predicted={}, gold={}, confidence=0.40),
    ]
    hist = confidence_histogram(entries)
    assert hist == {0.40: 1, 0.50: 2}


# ---------------------------------------------------------------------------
# End-to-end score phase over a written cache (still offline)
# ---------------------------------------------------------------------------


def test_build_report_from_written_cache(
    tmp_path: Path, synthetic_entries: list[dict[str, Any]]
) -> None:
    """Writing entries then building a report round-trips and computes metrics."""
    for entry in synthetic_entries:
        write_entry(tmp_path, "synthetic", entry)

    assert len(read_entries(tmp_path, "synthetic")) == 4

    report = build_report("synthetic", cache_base=tmp_path)
    assert report.n == 4
    assert "total" in report.labeled_fields
    # SROIE-like labeling: total is the only critical field labeled here.
    assert report.critical_labeled == ("total",)
    total = next(m for m in report.field_metrics if m.field == "total")
    assert total.n_match == 3


def test_build_report_raises_without_cache(tmp_path: Path) -> None:
    """Scoring a dataset with no cache raises a clear error."""
    with pytest.raises(FileNotFoundError):
        build_report("missing", cache_base=tmp_path)
