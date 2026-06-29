"""Unit tests for the hard/soft validation rules.

Covers the acceptance criterion for build-plan task 1.2: reconciling totals
pass H2/H3, mismatches fail, and soft failures are recorded without forcing
review. Also exercises the monetary epsilon, the skip semantics for absent
inputs, and the report's serialization.
"""

from __future__ import annotations

from datetime import date

import pytest

from doc_agent.schema.models import Document
from doc_agent.validation.rules import (
    MONETARY_ABS_EPSILON,
    ValidationReport,
    money_close,
    validate,
)


def _status(report: ValidationReport, code: str) -> str:
    """Return the status string for a rule code (fails the test if absent)."""
    result = report.by_code(code)
    assert result is not None, f"missing rule {code}"
    return result.status


# --- money_close ---------------------------------------------------------------


def test_money_close_within_absolute_epsilon() -> None:
    """Differences at or under the absolute floor compare equal."""
    assert money_close(10.00, 10.00 + MONETARY_ABS_EPSILON)
    assert money_close(10.00, 10.00 - MONETARY_ABS_EPSILON)


def test_money_close_tolerates_cents_rejects_larger_gaps() -> None:
    """Cent-level gaps reconcile; a half-unit gap on a small amount does not."""
    assert money_close(10.00, 10.02)
    assert not money_close(10.00, 10.50)


def test_money_close_tiny_amounts_use_absolute_floor() -> None:
    """Below the floor crossover the absolute epsilon governs the tolerance."""
    # At amount ~1 the relative term (0.005) is under the absolute floor (0.02).
    assert money_close(1.00, 1.01)
    assert not money_close(1.00, 1.05)


def test_money_close_large_amounts_use_relative_tolerance() -> None:
    """For large amounts the relative term widens the tolerance."""
    # 0.5% of 10000 == 50, so a 40-unit gap is within tolerance but 100 is not.
    assert money_close(10000.0, 10040.0)
    assert not money_close(10000.0, 10100.0)


# --- H2: subtotal + tax == total ----------------------------------------------


def test_h2_reconciling_totals_pass() -> None:
    """Reconciling subtotal + tax == total passes H2 (acceptance criterion)."""
    document = Document.model_validate({"subtotal": "100.00", "tax": "7.00", "total": "107.00"})
    report = validate(document)
    assert _status(report, "H2") == "pass"
    assert not report.hard_failed


def test_h2_mismatch_fails_and_forces_review() -> None:
    """A totals mismatch fails H2 and marks the report hard-failed."""
    document = Document.model_validate({"subtotal": "100.00", "tax": "7.00", "total": "120.00"})
    report = validate(document)
    assert _status(report, "H2") == "fail"
    assert report.hard_failed
    assert "H2" in [r.code for r in report.hard_failures]


def test_h2_within_epsilon_passes() -> None:
    """A sub-cent rounding gap still reconciles under the epsilon."""
    document = Document.model_validate({"subtotal": "100.00", "tax": "7.00", "total": "107.01"})
    assert _status(validate(document), "H2") == "pass"


def test_h2_skipped_when_inputs_absent() -> None:
    """H2 is skipped (not failed) when an input is missing."""
    document = Document.model_validate({"subtotal": "100.00", "total": "107.00"})  # no tax
    report = validate(document)
    assert _status(report, "H2") == "skip"
    assert not report.hard_failed


# --- H3: line items reconcile --------------------------------------------------


def test_h3_line_items_reconcile_to_subtotal() -> None:
    """Summed line amounts matching the subtotal passes H3 (acceptance)."""
    document = Document.model_validate(
        {
            "line_items": [
                {"description": "A", "amount": "40.00"},
                {"description": "B", "amount": "60.00"},
            ],
            "subtotal": "100.00",
            "tax": "7.00",
            "total": "107.00",
        }
    )
    report = validate(document)
    assert _status(report, "H3") == "pass"
    assert _status(report, "H2") == "pass"
    assert not report.hard_failed


def test_h3_reconciles_to_total_when_no_subtotal() -> None:
    """With no subtotal, H3 reconciles the line sum against the total."""
    document = Document.model_validate(
        {
            "line_items": [{"amount": "10.00"}, {"amount": "15.00"}],
            "total": "25.00",
        }
    )
    assert _status(validate(document), "H3") == "pass"


def test_h3_mismatch_fails() -> None:
    """Line amounts that do not sum to the subtotal fail H3."""
    document = Document.model_validate(
        {
            "line_items": [{"amount": "40.00"}, {"amount": "60.00"}],
            "subtotal": "150.00",
        }
    )
    report = validate(document)
    assert _status(report, "H3") == "fail"
    assert report.hard_failed


def test_h3_skipped_without_line_items() -> None:
    """No line items means H3 cannot run and is skipped."""
    document = Document.model_validate({"subtotal": "100.00", "total": "100.00"})
    assert _status(validate(document), "H3") == "skip"


def test_h3_skipped_when_an_amount_missing() -> None:
    """A single missing line amount makes the sum incomplete: skip, not fail."""
    document = Document.model_validate(
        {
            "line_items": [{"amount": "40.00"}, {"description": "no amount"}],
            "subtotal": "40.00",
        }
    )
    assert _status(validate(document), "H3") == "skip"


# --- H1 / H4: critical-field guards -------------------------------------------


def test_h1_passes_for_well_typed_document() -> None:
    """A normally-parsed document satisfies the H1 type guard."""
    document = Document.model_validate({"total": "10.00", "tax": "1.00", "invoice_number": "X1"})
    assert _status(validate(document), "H1") == "pass"


def test_h4_passes_when_total_present_and_nonnegative() -> None:
    """A present, non-negative total passes H4."""
    assert _status(validate(Document.model_validate({"total": "0.00"})), "H4") == "pass"


def test_h4_fails_when_total_missing() -> None:
    """A missing total is a hard failure (never safe to auto-accept)."""
    report = validate(Document.model_validate({"vendor_name": "Acme"}))
    assert _status(report, "H4") == "fail"
    assert report.hard_failed


def test_h4_fails_when_total_negative() -> None:
    """A negative total is a hard failure."""
    report = validate(Document.model_validate({"total": "-5.00"}))
    assert _status(report, "H4") == "fail"
    assert report.hard_failed


# --- Soft rules: recorded without forcing review ------------------------------


def test_soft_failures_do_not_force_review() -> None:
    """Soft failures are recorded but never set hard_failed (acceptance).

    This document reconciles arithmetically (hard rules pass) but is missing the
    vendor name, currency, and date and has no checkable line items -- so every
    soft rule fails. The decision path must stay open: hard_failed is False.
    """
    document = Document.model_validate({"subtotal": "100.00", "tax": "7.00", "total": "107.00"})
    report = validate(document)

    assert not report.hard_failed
    failed_codes = {r.code for r in report.soft_failures}
    assert {"S1", "S2", "S3"} <= failed_codes


def test_s1_present_date_is_plausible_without_reference() -> None:
    """With no ``today`` reference, a present date passes S1 (presence only)."""
    document = Document.model_validate({"document_date": "2024-01-15", "total": "1.00"})
    assert _status(validate(document), "S1") == "pass"


def test_s1_future_date_fails_against_reference() -> None:
    """A date past today + grace fails S1 when a reference is supplied."""
    document = Document.model_validate({"document_date": "2030-01-01", "total": "1.00"})
    report = validate(document, today=date(2024, 1, 15))
    assert _status(report, "S1") == "fail"
    assert not report.hard_failed  # still soft


def test_s1_missing_date_fails_soft() -> None:
    """A missing document_date is a soft failure."""
    assert _status(validate(Document.model_validate({"total": "1.00"})), "S1") == "fail"


def test_s2_known_currency_passes_unknown_fails() -> None:
    """S2 passes a known ISO code and fails an unknown one."""
    good = Document.model_validate({"currency": "sgd", "total": "1.00"})
    bad = Document.model_validate({"currency": "ZZZ", "total": "1.00"})
    assert _status(validate(good), "S2") == "pass"
    assert _status(validate(bad), "S2") == "fail"


def test_s3_vendor_present_passes() -> None:
    """A present vendor name passes S3."""
    document = Document.model_validate({"vendor_name": "Acme Corp", "total": "1.00"})
    assert _status(validate(document), "S3") == "pass"


def test_s4_per_line_arithmetic() -> None:
    """S4 passes consistent lines and fails when a line does not reconcile."""
    good = Document.model_validate(
        {"line_items": [{"quantity": "2", "unit_price": "5.00", "amount": "10.00"}], "total": "10.00"}
    )
    bad = Document.model_validate(
        {"line_items": [{"quantity": "2", "unit_price": "5.00", "amount": "11.00"}], "total": "11.00"}
    )
    assert _status(validate(good), "S4") == "pass"
    report = validate(bad)
    assert _status(report, "S4") == "fail"
    assert not report.hard_failed  # S4 is soft


def test_s4_skipped_without_full_line_fields() -> None:
    """S4 is skipped when no line item carries quantity, unit_price, and amount."""
    document = Document.model_validate(
        {"line_items": [{"description": "X", "amount": "10.00"}], "total": "10.00"}
    )
    assert _status(validate(document), "S4") == "skip"


# --- Report shape --------------------------------------------------------------


def test_report_has_one_result_per_rule() -> None:
    """Every rule reports exactly once, in a stable set of codes."""
    report = validate(Document.model_validate({"total": "1.00"}))
    codes = [r.code for r in report.results]
    assert codes == ["H1", "H2", "H3", "H4", "S1", "S2", "S3", "S4"]


def test_report_to_dict_is_serializable() -> None:
    """The report serializes to a plain dict suitable for Document.validation."""
    document = Document.model_validate({"subtotal": "100.00", "tax": "7.00", "total": "120.00"})
    payload = validate(document).to_dict()

    assert payload["hard_failed"] is True
    assert "H2" in payload["hard_failures"]
    assert isinstance(payload["results"], list)
    assert len(payload["results"]) == 8
    first = payload["results"][0]
    assert set(first) == {"code", "severity", "status", "message"}

    # Round-trips through JSON cleanly (no non-serializable objects).
    import json

    assert json.loads(json.dumps(payload)) == payload


def test_validate_does_not_mutate_document() -> None:
    """Validation is pure: it leaves the input document untouched."""
    document = Document.model_validate({"subtotal": "100.00", "tax": "7.00", "total": "120.00"})
    validate(document)
    assert document.validation == {}
    assert document.decision is None


@pytest.mark.parametrize(
    ("subtotal", "tax", "total", "expect_pass"),
    [
        ("19.99", "1.60", "21.59", True),
        ("19.99", "1.60", "21.60", True),  # 0.01 rounding, within epsilon
        ("19.99", "1.60", "25.00", False),
    ],
)
def test_h2_epsilon_boundary(subtotal: str, tax: str, total: str, expect_pass: bool) -> None:
    """H2 tolerates cent-level rounding but rejects real mismatches."""
    document = Document.model_validate({"subtotal": subtotal, "tax": tax, "total": total})
    status = _status(validate(document), "H2")
    assert (status == "pass") is expect_pass
