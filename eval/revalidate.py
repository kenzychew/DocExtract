"""Recompute validation and scoring from cached predictions (offline, no model).

The predict phase freezes two things into every cache entry: the ``confidence``
scalar and the ``validation`` report, both computed by whichever rule set was in
force at predict time. The score phase replays only ``route`` over those frozen
values, so a change to a soft rule in ``validation/rules.py`` -- or to the
weights in ``routing/score.py`` -- does **not** show up in a re-scored report.
The cache silently goes stale against the codebase.

This module closes that gap. The cache also stores the full predicted document
(``predict._build_entry`` writes ``Document.model_dump(mode="json")``), and
validation and scoring are pure functions of that document. So both can be
recomputed exactly, offline, for zero API calls:

    cached predicted dict -> Document -> validate() -> score() -> route()

Two uses, and the distinction matters:

- **Revalidation** (``revalidate_entries``) returns entries whose ``confidence``
  and ``validation`` reflect the *current* rules. Scoring these answers "what
  would this slice do today?"
- **Drift detection** (``detect_drift``) compares the recomputed values against
  the frozen ones without substituting them. Scoring is left alone; the
  disagreement is simply reported, so a stale cache announces itself instead of
  quietly producing numbers that describe a rule set no longer in the code.

Because the model output is held fixed and only the rules vary, a before/after
comparison built this way isolates the rule change. Re-running inference would
confound it with model nondeterminism.

**Caveat on ``today``.** The ``S1`` future-date rule needs a reference date, and
the cache does not record when the predict run happened. The default
``today=None`` checks date *presence* only (see ``validation.rules.validate``),
which is deterministic and reproducible but can differ from predict time for a
future-dated document -- such a document would have failed ``S1`` then and pass
now, and be reported as drift. Pass an explicit ``today`` to reproduce a
historical run exactly.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from doc_agent.core import aggregate_model_signal
from doc_agent.routing.score import score
from doc_agent.schema.models import Document
from doc_agent.validation.rules import validate

logger = logging.getLogger(__name__)

# Confidence scores closer than this are treated as equal (float round-trip
# through JSON is exact for these values, so the tolerance only guards against
# accumulated arithmetic noise, not genuine rule differences).
SCORE_EPSILON: float = 1e-9


@dataclass(frozen=True)
class Drift:
    """A disagreement between a cached entry and its recomputation.

    Attributes:
        id: The example id.
        cached_confidence: The confidence frozen into the cache at predict time.
        current_confidence: The confidence recomputed under current rules.
        cached_hard_failed: Whether the cached report recorded a hard failure.
        current_hard_failed: Whether current rules record a hard failure.
        cached_soft: Soft-rule codes that failed at predict time.
        current_soft: Soft-rule codes that fail under current rules.
    """

    id: str
    cached_confidence: float
    current_confidence: float
    cached_hard_failed: bool
    current_hard_failed: bool
    cached_soft: tuple[str, ...]
    current_soft: tuple[str, ...]

    @property
    def confidence_changed(self) -> bool:
        """Whether the two confidence scores differ beyond ``SCORE_EPSILON``."""
        return abs(self.cached_confidence - self.current_confidence) > SCORE_EPSILON

    @property
    def routing_changed(self) -> bool:
        """Whether the hard-failure verdict changed (a routing-relevant drift)."""
        return self.cached_hard_failed != self.current_hard_failed

    def describe(self) -> str:
        """Render a one-line human-readable summary of the disagreement."""
        soft_from = ",".join(self.cached_soft) or "-"
        soft_to = ",".join(self.current_soft) or "-"
        parts = [
            f"{self.id}: conf {self.cached_confidence:.2f} -> {self.current_confidence:.2f}",
            f"soft [{soft_from}] -> [{soft_to}]",
        ]
        if self.routing_changed:
            parts.append(
                f"hard_failed {self.cached_hard_failed} -> {self.current_hard_failed}"
            )
        return "  ".join(parts)


def revalidate_entry(entry: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """Recompute one entry's ``confidence`` and ``validation`` under current rules.

    Reconstitutes the cached predicted document and re-runs the real ``validate``
    and ``score`` functions over it. The model signal is recovered from the
    document's own ``field_confidence`` using the same reduction the core applies
    (``aggregate_model_signal``), so a backend that exposes per-field confidence
    is handled identically to production rather than being flattened to neutral.

    The returned entry is a shallow copy. The nested ``predicted.validation`` and
    ``predicted.decision`` are left as recorded at predict time: they are
    informational, and ``decision`` in particular is threshold-dependent, so
    there is no single correct value to rewrite it to. Only the top-level
    ``confidence`` and ``validation`` -- the two fields scoring actually reads --
    are refreshed.

    Args:
        entry: A cached entry containing a ``predicted`` document dict.
        today: Reference date for the ``S1`` future-date check; ``None`` checks
            date presence only (see the module docstring).

    Returns:
        A new entry dict with ``confidence`` and ``validation`` recomputed.

    Raises:
        pydantic.ValidationError: If ``predicted`` cannot be reconstituted into a
            ``Document`` -- which would mean the cache predates a breaking schema
            change and must be regenerated.
    """
    document = Document.model_validate(entry.get("predicted", {}))
    report = validate(document, today=today)
    model_signal = aggregate_model_signal(document.field_confidence)
    confidence = score(document, report, model_signal)

    updated = dict(entry)
    updated["confidence"] = confidence
    updated["validation"] = report.to_dict()
    return updated


def detect_drift(
    cached: dict[str, Any],
    current: dict[str, Any],
) -> Drift | None:
    """Compare a cached entry against its recomputation, if they disagree.

    Args:
        cached: The entry as read from disk.
        current: The same entry after :func:`revalidate_entry`.

    Returns:
        A :class:`Drift` describing the disagreement, or ``None`` when the cached
        values already match current rules.
    """
    cached_validation = cached.get("validation", {})
    current_validation = current.get("validation", {})
    record = Drift(
        id=str(cached.get("id", "?")),
        cached_confidence=float(cached.get("confidence", 0.0)),
        current_confidence=float(current.get("confidence", 0.0)),
        cached_hard_failed=bool(cached_validation.get("hard_failed", False)),
        current_hard_failed=bool(current_validation.get("hard_failed", False)),
        cached_soft=tuple(cached_validation.get("soft_failures", [])),
        current_soft=tuple(current_validation.get("soft_failures", [])),
    )
    if record.confidence_changed or record.routing_changed or record.cached_soft != record.current_soft:
        return record
    return None


def revalidate_entries(
    entries: Sequence[dict[str, Any]],
    *,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], list[Drift]]:
    """Recompute a whole slice and report every disagreement with the cache.

    Args:
        entries: Cached entries as read by ``eval.cache.read_entries``.
        today: Reference date passed through to validation.

    Returns:
        A two-tuple of (recomputed entries, drift records). The entry list is in
        input order and always the same length as ``entries``; the drift list
        contains only the entries whose recomputation disagreed with the cache.
    """
    recomputed: list[dict[str, Any]] = []
    drift: list[Drift] = []
    for entry in entries:
        current = revalidate_entry(entry, today=today)
        recomputed.append(current)
        record = detect_drift(entry, current)
        if record is not None:
            drift.append(record)

    if drift:
        logger.warning(
            "cache drift: %d of %d cached scores disagree with current rules",
            len(drift),
            len(entries),
        )
    return recomputed, drift
