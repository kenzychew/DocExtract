"""Tests for the revalidation path (``eval.revalidate``) and its reporting.

The cache freezes a ``confidence`` scalar and a ``validation`` report at predict
time. These tests pin the two properties that make recomputing them trustworthy:

1. **Fidelity.** Recomputation over an up-to-date cache reproduces the cached
   values exactly -- otherwise "revalidated" numbers would differ from
   production for reasons unrelated to any rule change.
2. **Visibility.** When the cache and the current rules disagree, the report
   says so in both modes, and always names which rule set produced its numbers.

Everything here is offline and synthetic; no dataset and no model are involved.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from doc_agent.core import aggregate_model_signal
from doc_agent.routing.score import score
from doc_agent.schema.models import Document
from doc_agent.validation.rules import validate

from eval.cache import write_entry
from eval.revalidate import detect_drift, revalidate_entries, revalidate_entry
from eval.score import build_report, format_report


def _real_entry(
    example_id: str,
    document: Document,
    *,
    gold: dict[str, Any] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Build a cache entry exactly as the predict phase would, from a Document.

    Mirrors ``eval.predict._build_entry``: the cached confidence and validation
    are the real ones produced by the pipeline's own pure functions, so a
    faithful revalidation must reproduce them exactly.
    """
    report = validate(document, today=today)
    signal = aggregate_model_signal(document.field_confidence)
    confidence = score(document, report, signal)
    return {
        "id": example_id,
        "dataset": "synthetic",
        "gold": gold or {},
        "labeled_fields": ["total"],
        "predicted": document.model_dump(mode="json"),
        "confidence": confidence,
        "decision": "review",
        "modality": "image",
        "backend": "stub",
        "validation": report.to_dict(),
        "error": None,
    }


def _clean_document() -> Document:
    """A document that passes every hard rule and every soft rule."""
    return Document(
        doc_type="receipt",
        vendor_name="Acme",
        currency="SGD",
        document_date="2019-01-15",
        line_items=[{"description": "widget", "quantity": 2, "unit_price": 5.0, "amount": 10.0}],
        subtotal=10.0,
        tax=1.0,
        total=11.0,
    )


# ---------------------------------------------------------------------------
# Fidelity of the recomputation
# ---------------------------------------------------------------------------


def test_revalidate_reproduces_an_up_to_date_cache() -> None:
    """An entry already consistent with current rules recomputes identically."""
    entry = _real_entry("clean", _clean_document())
    current = revalidate_entry(entry)

    assert current["confidence"] == pytest.approx(entry["confidence"])
    assert current["validation"] == entry["validation"]
    assert detect_drift(entry, current) is None


def test_revalidate_recomputes_a_stale_confidence() -> None:
    """A tampered cached scalar is replaced by the recomputed one, and flagged."""
    entry = _real_entry("stale", _clean_document())
    true_confidence = entry["confidence"]
    entry["confidence"] = 0.99  # pretend an older rule set scored it differently

    current = revalidate_entry(entry)
    assert current["confidence"] == pytest.approx(true_confidence)

    drift = detect_drift(entry, current)
    assert drift is not None
    assert drift.confidence_changed
    assert drift.cached_confidence == pytest.approx(0.99)
    assert drift.current_confidence == pytest.approx(true_confidence)


def test_revalidate_recovers_model_signal_from_field_confidence() -> None:
    """The model signal is rebuilt from the document, not flattened to neutral.

    A backend that exposed per-field confidence must be scored against that
    signal on revalidation, exactly as the core did at predict time. Averaging
    0.9 and 0.7 gives 0.8, above the 0.5 neutral prior -- so a revalidation that
    ignored ``field_confidence`` would score lower and silently disagree with
    production.
    """
    document = _clean_document().model_copy(
        update={"field_confidence": {"total": 0.9, "tax": 0.7}}
    )
    entry = _real_entry("signal", document)

    current = revalidate_entry(entry)

    assert current["confidence"] == pytest.approx(0.8)
    assert detect_drift(entry, current) is None


def test_revalidate_leaves_the_predicted_document_untouched() -> None:
    """Recomputation rewrites scores, never the model's recorded output."""
    entry = _real_entry("keep", _clean_document())
    current = revalidate_entry(entry)

    assert current["predicted"] == entry["predicted"]
    assert current["gold"] == entry["gold"]
    assert current["backend"] == entry["backend"]


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def test_drift_detects_a_changed_soft_rule_outcome() -> None:
    """A soft-failure set that no longer matches current rules is reported."""
    entry = _real_entry("soft", _clean_document())
    entry["validation"] = {
        **entry["validation"],
        "soft_failures": ["S2"],  # as if S2 had failed under an older rule set
    }
    entry["confidence"] = entry["confidence"] - 0.1

    drift = detect_drift(entry, revalidate_entry(entry))
    assert drift is not None
    assert drift.cached_soft == ("S2",)
    assert drift.current_soft == ()
    assert "S2" in drift.describe()


def test_drift_detects_a_changed_hard_failure_verdict() -> None:
    """A routing-relevant drift (hard-failure flip) is marked as such."""
    entry = _real_entry("hard", _clean_document())
    entry["validation"] = {**entry["validation"], "hard_failed": True}

    drift = detect_drift(entry, revalidate_entry(entry))
    assert drift is not None
    assert drift.routing_changed
    assert "hard_failed" in drift.describe()


def test_revalidate_entries_preserves_order_and_length() -> None:
    """Every entry is returned, in order; only disagreements appear in drift."""
    good = _real_entry("good", _clean_document())
    bad = _real_entry("bad", _clean_document())
    bad["confidence"] = 0.11

    recomputed, drift = revalidate_entries([good, bad])

    assert [e["id"] for e in recomputed] == ["good", "bad"]
    assert len(recomputed) == 2
    assert [d.id for d in drift] == ["bad"]


# ---------------------------------------------------------------------------
# Reporting: the numbers must be attributable to a rule set
# ---------------------------------------------------------------------------


def test_build_report_revalidate_uses_current_scores(tmp_path: Path) -> None:
    """--revalidate scores the recomputed confidences; the default does not."""
    entry = _real_entry("e1", _clean_document(), gold={"total": "11.00"})
    true_confidence = entry["confidence"]
    entry["confidence"] = 0.05  # stale: below every threshold in the sweep
    write_entry(tmp_path, "synthetic", entry)

    cached = build_report("synthetic", cache_base=tmp_path)
    current = build_report("synthetic", cache_base=tmp_path, revalidate=True)

    assert cached.score_source == "cached"
    assert not cached.revalidated
    assert cached.confidence_hist == {0.05: 1}
    assert cached.sweep[0].n_accepted == 0  # stale score accepts nothing

    assert current.revalidated
    assert current.confidence_hist == {round(true_confidence, 2): 1}
    assert current.sweep[0].n_accepted == 1  # recomputed score clears 0.50


def test_build_report_reports_drift_in_both_modes(tmp_path: Path) -> None:
    """Drift is detected whether or not the recomputed scores are used."""
    entry = _real_entry("e1", _clean_document(), gold={"total": "11.00"})
    entry["confidence"] = 0.05
    write_entry(tmp_path, "synthetic", entry)

    for revalidate in (False, True):
        report = build_report("synthetic", cache_base=tmp_path, revalidate=revalidate)
        assert len(report.drift) == 1, revalidate
        assert report.drift[0].id == "e1"


def test_report_text_names_which_rules_produced_the_numbers(tmp_path: Path) -> None:
    """The rendered report always states the score source unambiguously."""
    entry = _real_entry("e1", _clean_document(), gold={"total": "11.00"})
    entry["confidence"] = 0.05
    write_entry(tmp_path, "synthetic", entry)

    cached_text = format_report(build_report("synthetic", cache_base=tmp_path))
    current_text = format_report(
        build_report("synthetic", cache_base=tmp_path, revalidate=True)
    )

    assert "AS CACHED at predict time" in cached_text
    assert "CURRENT RULES" not in cached_text
    assert "--revalidate" in cached_text  # tells the reader how to get current numbers

    assert "CURRENT RULES" in current_text
    assert "AS CACHED at predict time" not in current_text

    # Both must surface the drift warning rather than hiding it.
    for text in (cached_text, current_text):
        assert "WARNING" in text
        assert "disagree with current rules" in text


def test_report_text_confirms_a_clean_cache(tmp_path: Path) -> None:
    """A cache consistent with current rules reports an explicit all-clear."""
    write_entry(
        tmp_path, "synthetic", _real_entry("e1", _clean_document(), gold={"total": "11.00"})
    )
    text = format_report(build_report("synthetic", cache_base=tmp_path))

    assert "OK -- all 1 cached scores agree with current rules" in text
    assert "WARNING" not in text
