"""Unit tests for confidence scoring and the routing decision.

Covers the acceptance criteria for build-plan task 1.3:

- a hard failure forces ``review`` regardless of how high the score is,
- the threshold boundary behaves (``>=`` accepts, just-below reviews),
- missing required fields lower the score.

Also exercises the model-signal neutral default, soft-failure penalties, score
clamping, and the purity of both functions.
"""

from __future__ import annotations

from datetime import date

import pytest

from docfield.routing.score import (
    COMPLETENESS_PENALTY,
    DEFAULT_CONFIDENCE_THRESHOLD,
    NEUTRAL_MODEL_SIGNAL,
    SOFT_FAILURE_PENALTY,
    route,
    score,
)
from docfield.schema.models import Document
from docfield.validation.rules import ValidationReport, validate

# A fixed reference date so the S1 future-date check is deterministic.
TODAY = date(2024, 6, 1)


def _doc(**fields: object) -> Document:
    """Build a ``Document`` from a field mapping."""
    return Document.model_validate(fields)


def _perfect_doc() -> Document:
    """A complete, fully-reconciling document with no soft failures.

    All required fields present, currency known, date present and not future,
    and totals reconcile -- so every hard rule passes and no soft rule fails.
    """
    return _doc(
        vendor_name="Acme Corp",
        document_date="2024-01-15",
        currency="SGD",
        subtotal="100.00",
        tax="7.00",
        total="107.00",
    )


def _clean_report() -> ValidationReport:
    """A report over a complete, valid document (no hard or soft failures)."""
    report = validate(_perfect_doc(), today=TODAY)
    assert not report.hard_failed
    assert not report.soft_failures
    return report


# --- score: model signal and its neutral default ------------------------------


def test_perfect_document_with_full_model_signal_scores_one() -> None:
    """A flawless document with a maximal model signal scores 1.0."""
    document = _perfect_doc()
    report = validate(document, today=TODAY)
    assert score(document, report, model_signal=1.0) == pytest.approx(1.0)


def test_absent_model_signal_uses_neutral_baseline() -> None:
    """With no model signal, a flawless document tops out at the neutral 0.5.

    This is the precision-safe posture: absent backend confidence cannot, by
    itself, clear a high threshold.
    """
    document = _perfect_doc()
    report = validate(document, today=TODAY)
    assert score(document, report, model_signal=None) == pytest.approx(NEUTRAL_MODEL_SIGNAL)


def test_model_signal_is_clamped_into_unit_interval() -> None:
    """Out-of-range model signals clamp to [0, 1] before blending."""
    document = _perfect_doc()
    report = validate(document, today=TODAY)
    assert score(document, report, model_signal=5.0) == pytest.approx(1.0)
    assert score(document, report, model_signal=-2.0) == pytest.approx(0.0)


# --- score: completeness penalty (missing required fields lower score) --------


def test_missing_required_fields_lower_score() -> None:
    """A document missing required fields scores below a complete one (AC)."""
    full = _perfect_doc()
    sparse = _doc(total="107.00")  # missing vendor_name and document_date

    full_score = score(full, validate(full, today=TODAY), model_signal=1.0)
    sparse_score = score(sparse, validate(sparse, today=TODAY), model_signal=1.0)
    assert sparse_score < full_score


def test_score_formula_composition_is_exact() -> None:
    """The score is base minus soft and completeness penalties, clamped.

    Document: total present but vendor_name, document_date, and currency absent.
    That is 2 of 3 required fields missing (completeness) and soft failures S1
    (no date) and S3 (no vendor). S2 skips -- an absent currency is not a soft
    failure -- and so does S4 (no full line items), and a skip carries no
    penalty.
    """
    document = _doc(total="107.00")
    report = validate(document, today=TODAY)

    assert {r.code for r in report.soft_failures} == {"S1", "S3"}
    expected = 1.0 - 2 * SOFT_FAILURE_PENALTY - COMPLETENESS_PENALTY * (2 / 3)
    assert score(document, report, model_signal=1.0) == pytest.approx(expected)


def test_score_never_goes_below_zero() -> None:
    """Penalties cannot drive the score negative; it clamps at 0.0."""
    document = _doc(total="107.00")
    report = validate(document, today=TODAY)
    assert score(document, report, model_signal=0.0) == pytest.approx(0.0)


# --- score: soft-failure penalty ----------------------------------------------


def test_soft_failure_lowers_score() -> None:
    """A single isolated soft failure (unknown currency) reduces the score."""
    known = _doc(vendor_name="A", document_date="2024-01-15", currency="SGD", total="10.00")
    unknown = _doc(vendor_name="A", document_date="2024-01-15", currency="ZZZ", total="10.00")

    known_score = score(known, validate(known, today=TODAY), model_signal=1.0)
    unknown_score = score(unknown, validate(unknown, today=TODAY), model_signal=1.0)
    assert unknown_score == pytest.approx(known_score - SOFT_FAILURE_PENALTY)


def test_score_is_always_in_unit_interval() -> None:
    """Across a range of inputs the score stays within [0, 1]."""
    documents = [_perfect_doc(), _doc(total="1.00"), _doc(vendor_name="X", total="5.00")]
    for document in documents:
        report = validate(document, today=TODAY)
        for signal in (None, 0.0, 0.5, 1.0):
            value = score(document, report, model_signal=signal)
            assert 0.0 <= value <= 1.0


# --- route: hard-failure short-circuit ----------------------------------------


def test_hard_failure_forces_review_regardless_of_score() -> None:
    """A hard rule failure routes to review even with a maximal score (AC)."""
    # Totals do not reconcile (H2 fails), but everything else is pristine and the
    # model is maximally confident -- the score is high yet routing must review.
    document = _doc(
        vendor_name="Acme Corp",
        document_date="2024-01-15",
        currency="SGD",
        subtotal="100.00",
        tax="7.00",
        total="120.00",
    )
    report = validate(document, today=TODAY)
    assert report.hard_failed

    confidence = score(document, report, model_signal=1.0)
    assert confidence >= DEFAULT_CONFIDENCE_THRESHOLD  # genuinely high
    assert route(confidence, report) == "review"


def test_hard_failure_reviews_even_at_perfect_confidence() -> None:
    """An explicit confidence of 1.0 is still overridden by a hard failure."""
    document = _doc(total="-5.00")  # H4 fails: negative total
    report = validate(document, today=TODAY)
    assert report.hard_failed
    assert route(1.0, report, threshold=0.0) == "review"


# --- route: threshold boundary ------------------------------------------------


def test_threshold_boundary_is_inclusive() -> None:
    """A clean report accepts at exactly the threshold and reviews just below."""
    report = _clean_report()
    assert route(0.85, report, threshold=0.85) == "accept"
    assert route(0.8499, report, threshold=0.85) == "review"
    assert route(0.95, report, threshold=0.85) == "accept"


def test_route_uses_default_threshold_when_unspecified() -> None:
    """Omitting the threshold falls back to the documented default (0.50)."""
    report = _clean_report()
    assert route(DEFAULT_CONFIDENCE_THRESHOLD, report) == "accept"
    assert route(DEFAULT_CONFIDENCE_THRESHOLD - 0.01, report) == "review"


def test_route_respects_custom_threshold() -> None:
    """A caller-supplied threshold governs the accept boundary."""
    report = _clean_report()
    assert route(0.6, report, threshold=0.5) == "accept"
    assert route(0.6, report, threshold=0.7) == "review"


def test_clean_high_confidence_document_is_accepted() -> None:
    """The happy path: no hard failure and confidence over threshold -> accept."""
    document = _perfect_doc()
    report = validate(document, today=TODAY)
    confidence = score(document, report, model_signal=1.0)
    assert route(confidence, report) == "accept"


# --- purity -------------------------------------------------------------------


def test_score_and_route_do_not_mutate_inputs() -> None:
    """Scoring and routing leave the document's pipeline fields untouched."""
    document = _perfect_doc()
    report = validate(document, today=TODAY)

    confidence = score(document, report, model_signal=0.9)
    route(confidence, report)

    assert document.field_confidence == {}
    assert document.validation == {}
    assert document.decision is None
