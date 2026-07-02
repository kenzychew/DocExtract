"""End-to-end smoke tests for the core pipeline (build-plan task 3.1).

These exercise ``process_document`` from path to decision with the offline
``StubBackend`` and an injected acquirer -- no network, no API key, no Ollama
server, no real parser. They cover the acceptance criterion ("runs end-to-end on
a couple of sample files with a stub backend; returns a populated result with a
decision") and the precision/robustness postures the core must honor:

- the clean default document auto-accepts and its pipeline fields are populated,
- a hard-rule failure (totals that do not reconcile) forces review regardless of
  the model's confidence,
- a backend that raises is caught and routed to review (rule 6), not propagated,
- detect -> acquire wiring passes the right modality through,
- the core writes nothing (no side effects).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from doc_agent.backends.base import DocumentPayload
from doc_agent.backends.stub import DEFAULT_STUB_DOCUMENT, StubBackend
from doc_agent.config import Settings, load_config
from doc_agent.core import ExtractionResult, process_document
from doc_agent.parsing.detect import Modality
from doc_agent.schema.models import Document

# A fixed reference date well after the stub document's 2024-01-15, so the S1
# future-date check is deterministic regardless of the real clock.
TODAY = date(2024, 6, 1)


def _offline_settings() -> Settings:
    """A valid Settings instance that needs no API key or network.

    Ollama + ocr_then_text is the one fully-offline valid combination; it only
    supplies ``confidence_threshold`` here, since every test injects the backend.
    """
    return load_config(extraction_backend="ollama", image_strategy="ocr_then_text")


def _acquire_capturing(seen: dict[str, Modality]) -> object:
    """Build an acquirer that records the modality it was handed.

    Args:
        seen: A dict the acquirer writes ``{"modality": modality}`` into, so a
            test can assert detect -> acquire passed the right parse path.

    Returns:
        An ``Acquire`` callable returning a minimal text payload.
    """

    def _acquire(path: Path, modality: Modality) -> DocumentPayload:
        seen["modality"] = modality
        return DocumentPayload(modality=modality, source_path=path, text="ignored by stub")

    return _acquire


def _acquire_fixed(path: Path, modality: Modality) -> DocumentPayload:
    """A trivial acquirer returning a fixed payload (the stub ignores it)."""
    return DocumentPayload(modality=modality, source_path=path, text="ignored by stub")


def test_process_document_accepts_clean_document() -> None:
    """The clean default stub document runs end-to-end and auto-accepts (AC)."""
    result = process_document(
        "invoice.pdf",
        settings=_offline_settings(),
        backend=StubBackend(),
        acquire=_acquire_fixed,
        today=TODAY,
    )

    assert isinstance(result, ExtractionResult)
    assert result.decision == "accept"
    assert result.accepted
    assert result.error is None


def test_accepted_result_is_fully_populated() -> None:
    """The returned document carries the pipeline-populated fields and values."""
    result = process_document(
        "receipt.png",
        settings=_offline_settings(),
        backend=StubBackend(),
        acquire=_acquire_fixed,
        today=TODAY,
    )

    document = result.document
    # Extracted values survived validation into the populated document.
    assert document.total == pytest.approx(11.77)
    assert document.invoice_number == "STUB-0001"
    assert len(document.line_items) == 2
    # Pipeline-populated fields are set (not the schema defaults).
    assert document.decision == "accept"
    assert document.field_confidence  # non-empty: the stub exposed a signal
    assert document.validation["hard_failed"] is False
    # The result surfaces the routing rationale.
    assert 0.0 <= result.confidence <= 1.0
    assert result.model_signal is not None
    assert result.backend_name == "stub"
    assert result.source_path == Path("receipt.png")
    assert not result.report.hard_failed


def test_detect_then_acquire_passes_modality_through() -> None:
    """Detection feeds the acquirer the modality inferred from the extension."""
    seen: dict[str, Modality] = {}
    pdf_result = process_document(
        "scan.pdf",
        settings=_offline_settings(),
        backend=StubBackend(),
        acquire=_acquire_capturing(seen),
        today=TODAY,
    )
    assert seen["modality"] == "native_pdf"
    assert pdf_result.modality == "native_pdf"

    img_result = process_document(
        "photo.jpg",
        settings=_offline_settings(),
        backend=StubBackend(),
        acquire=_acquire_capturing(seen),
        today=TODAY,
    )
    assert seen["modality"] == "image"
    assert img_result.modality == "image"


def test_hard_failure_forces_review_despite_high_confidence() -> None:
    """A non-reconciling total hard-fails (H2) and routes to review (rule 5)."""
    broken = dict(DEFAULT_STUB_DOCUMENT)
    broken["total"] = 999.00  # subtotal 11.00 + tax 0.77 != 999.00
    # Confidence is uniformly high, proving the hard failure -- not the score --
    # is what forces review.
    backend = StubBackend(data=broken, field_confidence={"total": 0.99})

    result = process_document(
        "tampered.pdf",
        settings=_offline_settings(),
        backend=backend,
        acquire=_acquire_fixed,
        today=TODAY,
    )

    assert result.decision == "review"
    assert result.report.hard_failed
    assert "H2" in [r.code for r in result.report.hard_failures]


def test_low_confidence_clean_document_routes_to_review() -> None:
    """A clean doc below threshold routes to review (threshold from settings)."""
    # No model signal -> neutral 0.5. Pin an explicit threshold above that neutral
    # prior so the test exercises "below threshold -> review" independent of the
    # empirically-set default (the eval lowered it to 0.50).
    backend = StubBackend(field_confidence={})
    settings = _offline_settings().model_copy(update={"confidence_threshold": 0.85})

    result = process_document(
        "uncertain.pdf",
        settings=settings,
        backend=backend,
        acquire=_acquire_fixed,
        today=TODAY,
    )

    assert not result.report.hard_failed
    assert result.model_signal is None
    assert result.confidence < 0.85
    assert result.decision == "review"


def test_backend_failure_routes_to_review_not_crash() -> None:
    """A backend that raises is caught and routed to review (rule 6)."""

    class ExplodingBackend:
        name = "exploding"

        def extract(self, payload: DocumentPayload, schema: type[Document]) -> object:
            raise RuntimeError("simulated transient backend failure")

    result = process_document(
        "boom.pdf",
        settings=_offline_settings(),
        backend=ExplodingBackend(),
        acquire=_acquire_fixed,
        today=TODAY,
    )

    assert result.decision == "review"
    assert result.error is not None
    assert "simulated transient backend failure" in result.error
    # The shape is still consistent: an empty document whose validation is recorded.
    assert result.document.decision == "review"
    assert result.document.validation["error"] == result.error


def test_malformed_backend_data_routes_to_review() -> None:
    """Unparseable extracted data fails schema validation and routes to review."""
    # A non-numeric total cannot be coerced -> Document validation raises ->
    # caught and routed to review rather than recording a wrong number.
    backend = StubBackend(data={"total": "not-a-number"})

    result = process_document(
        "garbled.png",
        settings=_offline_settings(),
        backend=backend,
        acquire=_acquire_fixed,
        today=TODAY,
    )

    assert result.decision == "review"
    assert result.error is not None


def test_processes_a_batch_of_mixed_files() -> None:
    """A couple of sample files of different modalities all yield a decision (AC)."""
    paths = ["a.pdf", "b.png", "c.jpeg", "d.tiff"]
    results = [
        process_document(
            p,
            settings=_offline_settings(),
            backend=StubBackend(),
            acquire=_acquire_fixed,
            today=TODAY,
        )
        for p in paths
    ]

    assert len(results) == len(paths)
    assert all(r.decision in ("accept", "review") for r in results)
    assert all(r.error is None for r in results)


def test_core_writes_nothing(tmp_path: Path) -> None:
    """The core performs no side effects: the working directory is untouched."""
    before = set(tmp_path.iterdir())
    process_document(
        tmp_path / "nothing-written.pdf",
        settings=_offline_settings(),
        backend=StubBackend(),
        acquire=_acquire_fixed,
        today=TODAY,
    )
    assert set(tmp_path.iterdir()) == before
