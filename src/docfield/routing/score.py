"""Confidence scoring and the auto-accept/review routing decision (pure, no I/O).

These two pure functions are the final stage of the core pipeline (architecture
section 8). They turn a parsed ``Document`` plus its ``ValidationReport`` plus
whatever confidence the backend exposed into a single document-level score, and
then into a ``"accept"`` / ``"review"`` decision.

The posture is precision-first (see CLAUDE.md "Precision posture"). Two ideas
encode it:

- **A hard failure short-circuits to review.** ``route`` checks
  ``report.hard_failed`` *before* the threshold, so a failed arithmetic
  cross-check forces review no matter how confident the model was. The model is
  treated as fallible by design.
- **No model signal is not high confidence.** When the backend exposes no
  confidence (``model_signal is None``) the blend starts from a neutral 0.5, so
  a value can only clear a meaningfully-high threshold when something positive
  (a real model signal) backs it. Missing values are cheap -- they are caught by
  review -- so erring toward review is the safe direction.

Confidence blends three inputs (data spec section 4):

1. **Model signal** -- the backend's document-level confidence in ``[0, 1]``;
   neutral ``0.5`` when unavailable.
2. **Validation** -- start from the model signal and subtract a fixed penalty
   for each *soft* rule failure (hard failures are handled by ``route``, not by
   the score).
3. **Completeness** -- subtract a penalty proportional to the fraction of the
   always-attempt required fields that are missing.

The weights here are deliberate, documented defaults; the single operative
threshold lives in config and is tuned empirically by the evaluation harness
(data spec section 6). ``route`` takes the threshold as an argument so the core
can pass ``Settings.confidence_threshold`` while this module stays a pure,
I/O-free, fully unit-tested leaf (CLAUDE.md architectural rule 7).
"""

from __future__ import annotations

from docfield.schema.models import Decision, Document
from docfield.validation.rules import ValidationReport

# --- Scoring policy -------------------------------------------------------------

# Confidence assigned to the model signal when the backend exposes none. Neutral
# rather than optimistic: absent evidence must not manufacture confidence.
NEUTRAL_MODEL_SIGNAL: float = 0.5

# Score subtracted per soft-rule failure (S1-S4). Four possible soft failures
# cap the total soft penalty at 0.4.
SOFT_FAILURE_PENALTY: float = 0.1

# Maximum score subtracted for missing required fields, scaled by the fraction
# of required fields that are absent (0.0 when all present, this value when all
# are missing).
COMPLETENESS_PENALTY: float = 0.2

# The always-attempt fields whose absence signals an incomplete extraction (data
# spec section 2, "Field requirements"). ``doc_type`` is intentionally excluded:
# it has a non-null default ("other"), so it can never be absent and would carry
# no completeness information.
REQUIRED_FIELDS: tuple[str, ...] = ("vendor_name", "document_date", "total")

# Default auto-accept threshold. The operative value is read from config
# (``Settings.confidence_threshold``) and passed into ``route`` by the core; this
# constant only keeps the pure function self-contained and matches the config
# default (0.50, set from the SROIE evaluation) so direct callers behave
# consistently.
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.50


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Clamp ``value`` into the closed interval ``[low, high]``.

    Args:
        value: The value to clamp.
        low: Lower bound. Defaults to 0.0.
        high: Upper bound. Defaults to 1.0.

    Returns:
        ``value`` constrained to ``[low, high]``.
    """
    return max(low, min(high, value))


def _missing_required_fraction(document: Document) -> float:
    """Fraction of the required fields that are absent (``None``).

    Args:
        document: The parsed document to inspect.

    Returns:
        A value in ``[0, 1]``: ``0.0`` when every required field is present,
        ``1.0`` when all are missing.
    """
    missing = sum(1 for name in REQUIRED_FIELDS if getattr(document, name) is None)
    return missing / len(REQUIRED_FIELDS)


def score(
    data: Document,
    report: ValidationReport,
    model_signal: float | None = None,
) -> float:
    """Blend model signal, soft-validation penalties, and completeness into [0, 1].

    Pure: no I/O, and neither ``data`` nor ``report`` is mutated. Hard failures
    are intentionally *not* reflected here -- they are an absolute routing
    override applied by ``route`` -- so this score reflects only graded
    confidence in the extracted values.

    Args:
        data: The parsed, schema-validated document.
        report: The validation report produced by ``validation.rules.validate``.
        model_signal: The backend's document-level confidence in ``[0, 1]``;
            ``None`` (the default) when the backend exposes none, in which case a
            neutral ``0.5`` is used. Out-of-range values are clamped.

    Returns:
        A document-level confidence score in ``[0, 1]``.
    """
    base = NEUTRAL_MODEL_SIGNAL if model_signal is None else _clamp(model_signal)
    soft_penalty = SOFT_FAILURE_PENALTY * len(report.soft_failures)
    completeness_penalty = COMPLETENESS_PENALTY * _missing_required_fraction(data)
    return _clamp(base - soft_penalty - completeness_penalty)


def route(
    confidence: float,
    report: ValidationReport,
    *,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> Decision:
    """Decide whether to auto-accept a document or route it to review.

    The hard-failure short-circuit is checked first and unconditionally: any
    failed hard rule forces ``"review"`` regardless of ``confidence`` (data spec
    section 5). Only a clean report with a confidence at or above the threshold
    is auto-accepted.

    Args:
        confidence: The document-level score from ``score``.
        report: The validation report; ``report.hard_failed`` is the override.
        threshold: Auto-accept cutoff in ``[0, 1]``. Defaults to
            ``DEFAULT_CONFIDENCE_THRESHOLD``; the core passes
            ``Settings.confidence_threshold``.

    Returns:
        ``"accept"`` if no hard rule failed and ``confidence >= threshold``,
        otherwise ``"review"``.
    """
    if report.hard_failed:
        return "review"
    if confidence >= threshold:
        return "accept"
    return "review"
