"""Unit tests for the backend interface, factory, and offline stub.

Covers the acceptance criteria for build-plan task 2.4 (+ the stub):

- the factory returns the configured backend (resolved from settings or an
  explicit override),
- an unknown backend name raises a clear, recoverable error,
- the stub returns schema-valid ``Document`` data with no network.

Also exercises the stub's determinism, its deep-copy isolation between calls,
protocol conformance, and that the default stub document passes validation
cleanly (the happy path the core smoke test relies on).
"""

from __future__ import annotations

import pytest

from docfield.backends.base import (
    BackendResult,
    DocumentPayload,
    ExtractionBackend,
    available_backends,
    create_backend,
)
from docfield.backends.stub import (
    DEFAULT_STUB_DOCUMENT,
    StubBackend,
)
from docfield.config import ConfigError, Settings, load_config
from docfield.schema.models import Document
from docfield.validation.rules import validate


def _gemini_settings() -> Settings:
    """A valid Settings instance configured for the Gemini backend."""
    return load_config(extraction_backend="gemini", gemini_api_key="test-key")


def _ollama_settings() -> Settings:
    """A valid Settings instance configured for the (text-only) Ollama backend."""
    return load_config(extraction_backend="ollama", image_strategy="ocr_then_text")


def _payload() -> DocumentPayload:
    """A minimal text payload for driving ``extract`` (ignored by the stub)."""
    return DocumentPayload(modality="image", text="any text the stub ignores")


# --- factory: returns the configured backend ----------------------------------


def test_factory_returns_backend_for_explicit_name() -> None:
    """An explicit name override resolves to the registered backend (AC)."""
    backend = create_backend(_gemini_settings(), name="stub")
    assert isinstance(backend, StubBackend)
    assert backend.name == "stub"


def test_factory_override_takes_precedence_over_config() -> None:
    """The explicit ``name`` wins over ``settings.extraction_backend``."""
    # Settings say "gemini", but the override forces the stub -- proving the
    # factory honors the override (and that selection is the one resolution
    # point the rest of the system depends on).
    backend = create_backend(_ollama_settings(), name="stub")
    assert isinstance(backend, StubBackend)


def test_factory_reads_backend_name_from_settings() -> None:
    """With no override the factory resolves the name from settings.

    Gemini is now registered (T2), so _gemini_settings() succeeds.
    Ollama is still unregistered, so _ollama_settings() still raises a
    ConfigError naming the configured backend.
    """
    from unittest.mock import patch

    from docfield.backends.gemini import GeminiBackend

    with patch("google.genai.Client"):
        backend = create_backend(_gemini_settings())
    assert isinstance(backend, GeminiBackend)

    with pytest.raises(ConfigError, match="ollama"):
        create_backend(_ollama_settings())


def test_factory_returns_protocol_conforming_object() -> None:
    """The built backend satisfies the ``ExtractionBackend`` protocol."""
    backend = create_backend(_gemini_settings(), name="stub")
    assert isinstance(backend, ExtractionBackend)


# --- factory: unknown backend -> clear error ----------------------------------


def test_unknown_backend_raises_config_error() -> None:
    """An unrecognized backend name raises a recoverable ConfigError (AC)."""
    with pytest.raises(ConfigError) as excinfo:
        create_backend(_gemini_settings(), name="does-not-exist")
    message = str(excinfo.value)
    assert "does-not-exist" in message
    # The message is actionable: it lists what *is* available.
    assert "stub" in message


def test_available_backends_lists_registered_backends() -> None:
    """The registry exposes gemini and stub (sorted); ollama is not yet registered."""
    names = available_backends()
    assert "stub" in names
    assert "gemini" in names
    assert "ollama" not in names
    assert list(names) == sorted(names)


# --- stub: returns schema-valid data ------------------------------------------


def test_stub_returns_schema_valid_document_data() -> None:
    """The stub's data validates against the ``Document`` schema (AC)."""
    result = StubBackend().extract(_payload(), Document)
    assert isinstance(result, BackendResult)

    document = Document.model_validate(result.data)
    assert document.total == pytest.approx(11.77)
    assert document.invoice_number == "STUB-0001"
    assert len(document.line_items) == 2


def test_stub_default_document_passes_validation_cleanly() -> None:
    """The default stub document trips no hard or soft rule (the happy path)."""
    document = Document.model_validate(StubBackend().extract(_payload(), Document).data)
    report = validate(document)
    assert not report.hard_failed
    assert not report.soft_failures


def test_stub_reports_field_confidence() -> None:
    """The stub exposes a per-field confidence signal in [0, 1]."""
    result = StubBackend().extract(_payload(), Document)
    assert result.field_confidence is not None
    assert "total" in result.field_confidence
    assert all(0.0 <= v <= 1.0 for v in result.field_confidence.values())


# --- stub: determinism and isolation ------------------------------------------


def test_stub_is_deterministic_across_calls() -> None:
    """Two calls (with different payloads) return identical data."""
    backend = StubBackend()
    first = backend.extract(DocumentPayload(modality="native_pdf", text="A"), Document)
    second = backend.extract(DocumentPayload(modality="image", text="B"), Document)
    assert first.data == second.data


def test_stub_extract_returns_an_independent_copy() -> None:
    """Mutating a returned result cannot corrupt later calls or the template."""
    backend = StubBackend()
    result = backend.extract(_payload(), Document)
    result.data["total"] = 999.0
    result.data["line_items"].clear()

    fresh = backend.extract(_payload(), Document)
    assert fresh.data["total"] == pytest.approx(11.77)
    assert len(fresh.data["line_items"]) == 2
    # The module-level template is likewise untouched.
    assert DEFAULT_STUB_DOCUMENT["total"] == pytest.approx(11.77)


# --- stub: injected data ------------------------------------------------------


def test_stub_returns_injected_data() -> None:
    """A custom ``data`` mapping is returned verbatim, enabling targeted tests."""
    custom = {"doc_type": "invoice", "total": "42.00", "vendor_name": "Custom Co"}
    backend = StubBackend(data=custom, field_confidence={"total": 0.5})
    result = backend.extract(_payload(), Document)
    assert result.data == custom
    assert result.field_confidence == {"total": 0.5}
