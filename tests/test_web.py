"""Tests for the Gradio demo's rendering layer (``doc_agent.web.app``).

The demo's job is to answer "why did it decide that" without making the reader
learn rule codes. Two properties carry that, and both are easy to break silently:

1. **Rule codes never reach the user-facing surfaces.** The verdict and the
   checks list must be plain language; codes belong only in the details tab.
   A rule added without copy would otherwise leak "H5" into the UI at the exact
   moment nobody is looking, so the drift guards below fail the build instead.
2. **The stated reason is the real one.** Cause-of-review precedence
   (processing failure > hard rule > low confidence) and the threshold
   comparison are pinned, because a wrong reason is worse than no reason.

Results are built by running the real pipeline with ``StubBackend`` and an
injected acquirer, following ``tests/test_core_smoke.py``. Using genuine
``validate()`` output rather than hand-built reports is what makes the drift
guard meaningful. No network, no API key, no Gradio server.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import gradio as gr
import pytest

from doc_agent.backends.base import DocumentPayload
from doc_agent.backends.stub import DEFAULT_STUB_DOCUMENT, StubBackend
from doc_agent.config import ConfigError, Settings, load_config
from doc_agent.core import ExtractionResult, process_document
from doc_agent.parsing.detect import Modality
from doc_agent.schema.models import Document
from doc_agent.validation.rules import validate
from doc_agent.web import app as webapp

TODAY = date(2024, 6, 1)

# Matches a bare rule code (H1..H4, S1..S4) as a whole word.
_CODE_RE = re.compile(r"\b[HS][0-9]\b")


def _offline_settings(**overrides: Any) -> Settings:
    """Settings needing no API key or network (Ollama + OCR is the offline combo)."""
    return load_config(extraction_backend="ollama", image_strategy="ocr_then_text", **overrides)


def _acquire(path: Path, modality: Modality) -> DocumentPayload:
    return DocumentPayload(modality=modality, source_path=path, text="ignored by stub")


def _run(data: dict[str, Any] | None = None, **backend_kwargs: Any) -> ExtractionResult:
    """Run the real pipeline over a stub backend and return the result."""
    backend = StubBackend(data=data or dict(DEFAULT_STUB_DOCUMENT), **backend_kwargs)
    return process_document(
        "receipt.png",
        settings=_offline_settings(),
        backend=backend,
        acquire=_acquire,
        today=TODAY,
    )


class _ExplodingBackend:
    """A backend that always raises, to drive the pipeline-error path."""

    name = "exploding"

    def extract(self, payload: DocumentPayload, schema: type[Document]) -> Any:
        raise RuntimeError("backend unavailable")


def _accepted() -> ExtractionResult:
    return _run()


def _hard_failed() -> ExtractionResult:
    """Totals that do not reconcile -> H2 fails."""
    return _run({**DEFAULT_STUB_DOCUMENT, "total": 999.00})


def _two_hard_failures() -> ExtractionResult:
    """Both the H2 sum and the H3 line-item sum disagree with the stated figures."""
    return _run({**DEFAULT_STUB_DOCUMENT, "subtotal": 500.00, "total": 999.00})


def _low_confidence(threshold: float = 0.85) -> ExtractionResult:
    """Clean document with no model signal, decided against a raised threshold.

    The threshold must be applied at decision time, not only at render time: the
    renderers read ``result.decision`` rather than recomputing it, so handing a
    renderer a threshold the pipeline never saw would describe a decision that
    was never made.
    """
    backend = StubBackend(data=dict(DEFAULT_STUB_DOCUMENT), field_confidence={})
    return process_document(
        "receipt.png",
        settings=_offline_settings(confidence_threshold=threshold),
        backend=backend,
        acquire=_acquire,
        today=TODAY,
    )


def _errored() -> ExtractionResult:
    return process_document(
        "receipt.png",
        settings=_offline_settings(),
        backend=_ExplodingBackend(),
        acquire=_acquire,
        today=TODAY,
    )


ALL_RESULTS = {
    "accepted": _accepted,
    "hard_failed": _hard_failed,
    "low_confidence": _low_confidence,
    "errored": _errored,
}


# ---------------------------------------------------------------------------
# Drift guards -- the maps must track the rules
# ---------------------------------------------------------------------------


def _emitted_codes() -> set[str]:
    """Every rule code a real validate() call can emit."""
    codes: set[str] = set()
    for doc in (Document(), Document.model_validate(DEFAULT_STUB_DOCUMENT)):
        codes.update(r.code for r in validate(doc, today=TODAY).results)
    return codes


def test_every_emitted_rule_code_has_plain_language_copy() -> None:
    """A rule with no copy would render as a bare code in the UI."""
    missing = _emitted_codes() - set(webapp._RULE_COPY)
    assert not missing, f"validation rules with no plain-language copy: {sorted(missing)}"


def test_no_stale_rule_copy() -> None:
    """Copy for a rule that no longer exists is a documentation lie."""
    stale = set(webapp._RULE_COPY) - _emitted_codes()
    assert not stale, f"copy for rules that no longer exist: {sorted(stale)}"


def test_rule_copy_is_itself_plain_language() -> None:
    """No copy string may contain a rule code, and none may be empty."""
    for code, copy in webapp._RULE_COPY.items():
        for field, text in copy._asdict().items():
            assert text.strip(), f"{code}.{field} is empty"
            assert not _CODE_RE.search(text), f"{code}.{field} contains a rule code: {text!r}"


# ---------------------------------------------------------------------------
# Rule codes must not leak into the user-facing surfaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ALL_RESULTS))
def test_checks_block_contains_no_rule_codes(name: str) -> None:
    rendered = webapp._render_checks(ALL_RESULTS[name]())
    assert not _CODE_RE.search(rendered), rendered


@pytest.mark.parametrize("name", sorted(ALL_RESULTS))
def test_verdict_block_contains_no_rule_codes(name: str) -> None:
    rendered = webapp._render_verdict(ALL_RESULTS[name](), threshold=0.50)
    assert not _CODE_RE.search(rendered), rendered


def test_details_block_does_contain_rule_codes() -> None:
    """The split is intentional, so assert it from both sides."""
    rendered = webapp._render_details(_hard_failed(), threshold=0.50)
    assert "H2" in rendered
    assert "S1" in rendered


# ---------------------------------------------------------------------------
# Verdict and cause-of-review precedence
# ---------------------------------------------------------------------------


def test_accept_verdict_states_acceptance_plainly() -> None:
    rendered = webapp._render_verdict(_accepted(), threshold=0.50)
    assert "Accepted automatically" in rendered
    assert "Sent to review" not in rendered


def test_accept_verdict_omits_the_numeric_confidence() -> None:
    """Deliberate: the backend reports no per-field signal, so the number would
    read as a coin flip on the happy path. It stays in the details tab."""
    result = _accepted()
    rendered = webapp._render_verdict(result, threshold=0.50)
    assert f"{result.confidence:.0%}" not in rendered


def test_hard_failure_verdict_names_the_specific_check_and_its_evidence() -> None:
    result = _hard_failed()
    rendered = webapp._render_verdict(result, threshold=0.50)
    assert "Sent to review" in rendered
    assert webapp._RULE_COPY["H2"].bad in rendered
    assert "999.0" in rendered  # the raw arithmetic, as evidence


def test_multiple_hard_failures_are_all_listed() -> None:
    result = _two_hard_failures()
    assert len(result.report.hard_failures) > 1
    rendered = webapp._render_verdict(result, threshold=0.50)
    for rule in result.report.hard_failures:
        assert webapp._RULE_COPY[rule.code].bad in rendered


@pytest.mark.parametrize("threshold", [0.85, 0.95])
def test_low_confidence_verdict_names_both_numbers(threshold: float) -> None:
    """Proves the threshold reached the renderer instead of being hardcoded."""
    result = _low_confidence(threshold)
    assert result.decision == "review"
    assert not result.report.hard_failed  # low confidence is the only cause
    rendered = webapp._render_verdict(result, threshold=threshold)
    assert f"{result.confidence:.0%}" in rendered
    assert f"{threshold:.0%}" in rendered


def test_pipeline_error_outranks_hard_failure() -> None:
    """The error path validates an empty document, so H4 also fails there.

    The reason must name the processing failure, not the downstream symptom.
    """
    result = _errored()
    assert result.report.hard_failed  # H4: no total on the empty document
    rendered = webapp._render_verdict(result, threshold=0.50)
    assert "Something went wrong" in rendered
    assert "backend unavailable" in rendered
    assert webapp._RULE_COPY["H4"].bad not in rendered


def test_accepted_result_has_no_review_reason() -> None:
    assert webapp._review_reason(_accepted(), threshold=0.50) is None


def test_review_reason_never_falls_through_at_the_threshold_boundary() -> None:
    """``confidence < threshold`` must be the exact negation of route()'s ``>=``.

    A document scoring exactly the threshold is accepted, so no reason is owed.
    One step below, a reason must be found -- if the comparison here were ``<=``
    or a rounded form, a boundary review would reach the useless catch-all.
    """
    at_boundary = _low_confidence(0.50)
    assert at_boundary.decision == "accept"
    assert webapp._review_reason(at_boundary, threshold=0.50) is None

    just_under = _low_confidence(0.51)
    assert just_under.decision == "review"
    reason = webapp._review_reason(just_under, threshold=0.51)
    assert reason is not None
    assert "held back for a person to confirm" not in reason


# ---------------------------------------------------------------------------
# Checks block content
# ---------------------------------------------------------------------------


def test_checks_lists_both_severity_groups() -> None:
    rendered = webapp._render_checks(_accepted())
    assert "Must pass to auto-accept" in rendered
    assert "Quality signals" in rendered


def test_skip_renders_as_not_applicable() -> None:
    """A skipped rule is not a failure and must not read like one."""
    result = _run({"total": "10.00", "vendor_name": "Acme"})  # no currency, no line items
    statuses = {r.code: r.status for r in result.report.results}
    assert statuses["S2"] == "skip"  # absent currency skips since the S2 change

    rendered = webapp._render_checks(result)
    assert "Not applicable" in rendered
    assert "SKIP" not in rendered


def test_failing_check_shows_evidence_and_passing_check_does_not() -> None:
    """Evidence is actionable on failure and noise on success; details has both."""
    result = _hard_failed()
    checks = webapp._render_checks(result)
    details = webapp._render_details(result, threshold=0.50)

    h2 = result.report.by_code("H2")
    h4 = result.report.by_code("H4")
    assert h2 is not None and h2.status == "fail"
    assert h4 is not None and h4.status == "pass"

    assert h2.message in checks       # failure evidence is shown
    assert h4.message not in checks   # passing evidence is not
    assert h4.message in details      # but it is one tab away


def test_error_result_notes_the_checks_ran_on_an_empty_document() -> None:
    rendered = webapp._render_checks(_errored())
    assert "ran against an empty document" in rendered


# ---------------------------------------------------------------------------
# Details block
# ---------------------------------------------------------------------------


def test_details_carries_the_technical_view() -> None:
    result = _accepted()
    rendered = webapp._render_details(result, threshold=0.50)
    assert "stub" in rendered
    assert f"{result.confidence:.0%}" in rendered
    assert "50%" in rendered
    assert str(result.modality) in rendered


def test_details_shows_the_pipeline_error_when_present() -> None:
    assert "backend unavailable" in webapp._render_details(_errored(), threshold=0.50)


# ---------------------------------------------------------------------------
# Callback wiring and empty state
# ---------------------------------------------------------------------------


def test_process_with_no_file_returns_the_empty_state() -> None:
    assert webapp._process(None) == (
        webapp._NO_FILE_VERDICT,
        webapp._EMPTY_FIELDS,
        webapp._EMPTY_CHECKS,
        webapp._EMPTY_DETAILS,
    )


def test_process_returns_four_values() -> None:
    """Catches an outputs-arity mismatch that otherwise only shows in a browser."""
    assert len(webapp._process(None)) == 4


def test_startup_error_is_rendered_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing API key is the likeliest failure on a fresh Space deploy.

    It raises before any ExtractionResult exists, so it cannot be expressed as a
    document outcome and must not surface as a traceback.
    """
    doc = tmp_path / "receipt.png"
    doc.write_bytes(b"not really a png")  # copied before load_config is reached

    def _boom() -> Settings:
        raise ConfigError("GEMINI_API_KEY is not set")

    monkeypatch.setattr(webapp, "load_config", _boom)

    verdict, fields, checks, details = webapp._process(str(doc))
    assert "Could not run" in verdict
    assert "setup problem with the demo" in verdict
    assert "GEMINI_API_KEY is not set" in verdict
    assert fields == webapp._EMPTY_FIELDS
    assert checks == webapp._EMPTY_CHECKS


def test_build_demo_constructs_without_configuration() -> None:
    """Also proves the module reads no config at import or build time, which
    would otherwise crash the Space whenever a secret is absent."""
    assert isinstance(webapp.build_demo(), gr.Blocks)


def test_privacy_notice_carries_the_training_warning() -> None:
    """NFR-2: the hosted demo must warn that free-tier inputs may be trained on."""
    assert "SYNTHETIC / PUBLIC DOCUMENTS ONLY" in webapp._PRIVACY_NOTICE
    assert "may train on your inputs" in webapp._PRIVACY_NOTICE
