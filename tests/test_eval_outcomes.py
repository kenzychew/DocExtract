"""Tests for error-cause reporting (``summarize_errors`` and the outcomes block).

A document that never reached the model produced no extraction. It lowers
recall exactly as a genuine miss does -- gold present, nothing predicted -- while
leaving precision untouched. Summing the two silently would let an
infrastructure outage read as extraction quality, so the report has to state the
split explicitly. These tests pin that it does, and that an unfamiliar failure
is still surfaced rather than bucketed away.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eval.cache import write_entry
from eval.metrics import classify_error, summarize_errors
from eval.score import build_report, format_report

from tests.test_eval_revalidate import _clean_document, _real_entry

_QUOTA = (
    "Gemini extraction failed after 3 attempts: 429 RESOURCE_EXHAUSTED. "
    "{'error': {'code': 429, 'message': 'Your project has exceeded its monthly "
    "spending cap.'}}"
)


def _errored(example_id: str, message: str) -> dict[str, Any]:
    """An entry shaped like one core._review_on_error would have produced."""
    from doc_agent.schema.models import Document

    entry = _real_entry(example_id, Document(), gold={"total": "10.00"})
    entry["error"] = message
    entry["confidence"] = 0.0
    return entry


# ---------------------------------------------------------------------------
# Cause classification
# ---------------------------------------------------------------------------


def test_quota_exhaustion_is_labelled_infrastructure() -> None:
    label = classify_error(_QUOTA)
    assert "quota" in label
    assert "infrastructure" in label


def test_known_causes_are_distinguished() -> None:
    assert "unsupported file type" in classify_error("UnsupportedModalityError: bad.xyz")
    assert "not implemented" in classify_error("NotImplementedError: ocr_then_text")
    assert "schema validation" in classify_error("ValidationError: 3 validation errors")


def test_unknown_cause_is_surfaced_not_bucketed() -> None:
    """An unfamiliar failure keeps its message instead of becoming 'other'."""
    label = classify_error("KaboomError: the flux capacitor desynchronised")
    assert "flux capacitor" in label


def test_long_unknown_cause_is_truncated_and_marked() -> None:
    label = classify_error("KaboomError: " + "x" * 200)
    assert label.endswith("...")
    assert len(label) <= 73


def test_summarize_groups_and_orders_by_frequency() -> None:
    entries = [
        _errored("a", _QUOTA),
        _errored("b", _QUOTA),
        _errored("c", "UnsupportedModalityError: bad.xyz"),
        _real_entry("ok", _clean_document()),
    ]
    summary = summarize_errors(entries)

    assert summary[0][1] == 2
    assert "quota" in summary[0][0]
    assert summary[1][1] == 1
    assert sum(count for _, count in summary) == 3  # the clean entry is not counted


def test_summarize_returns_nothing_when_all_reached_the_model() -> None:
    assert summarize_errors([_real_entry("ok", _clean_document())]) == []


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def test_report_states_reached_and_unreached_counts(tmp_path: Path) -> None:
    """The outcomes block separates model failures from documents never seen."""
    for name in ("ok1", "ok2", "ok3"):
        write_entry(tmp_path, "d", _real_entry(name, _clean_document(), gold={"total": "11.00"}))
    write_entry(tmp_path, "d", _errored("dead", _QUOTA))

    report = build_report("d", cache_base=tmp_path, revalidate=True)
    text = format_report(report)

    assert report.n == 4
    assert report.n_error == 1
    assert report.n_reached_model == 3
    assert "reached the model      :    3 of 4" in text
    assert "never reached the model:    1 of 4 (25.0%)" in text
    assert "quota" in text
    # The reader is told which metric the outage distorts, and which it does not.
    assert "counted as a miss in recall" in text
    assert "not in" in text


def test_report_confirms_when_every_document_reached_the_model(tmp_path: Path) -> None:
    write_entry(tmp_path, "d", _real_entry("ok", _clean_document(), gold={"total": "11.00"}))
    text = format_report(build_report("d", cache_base=tmp_path))

    assert "all 1 documents reached the model." in text
    assert "never reached the model" not in text
