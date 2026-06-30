"""Mocked unit tests for the Gemini extraction backend (T2).

No real API calls are made -- the google.genai client is bypassed by
constructing ``GeminiBackend`` via ``object.__new__`` and injecting mock
attributes directly. This keeps the tests deterministic, free of network/quota,
and fast.

Covers the acceptance criteria for T2:
- schema-valid JSON response is parsed into a ``BackendResult`` whose data
  validates into a ``Document``,
- a transient failure on attempt 1 is retried and succeeds on attempt 2,
- all ``_MAX_RETRIES`` attempts failing raises ``RuntimeError`` (which the
  core catches and routes to review).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, call, patch

import pytest

from doc_agent.backends.base import DocumentPayload
from doc_agent.backends.gemini import GeminiBackend, _ExtractionSchema, _MAX_RETRIES
from doc_agent.schema.models import Document


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend() -> tuple[GeminiBackend, MagicMock]:
    """Build a GeminiBackend bypassing __init__ with a mock client.

    Returns:
        A tuple of (backend, mock_client) where mock_client is the object
        wired to ``backend._client``.
    """
    mock_types = MagicMock()
    mock_client = MagicMock()

    backend = object.__new__(GeminiBackend)
    backend._model = "gemini-test"
    backend._types = mock_types
    backend._client = mock_client
    return backend, mock_client


def _json_response(data: dict) -> MagicMock:
    """Build a mock Gemini response whose ``.text`` is a valid JSON string.

    Args:
        data: Fields to include in the ``_ExtractionSchema`` response.

    Returns:
        A MagicMock whose ``.text`` attribute is a JSON-serialised
        ``_ExtractionSchema`` populated from ``data``.
    """
    schema_fields = _ExtractionSchema.model_fields.keys()
    filtered = {k: v for k, v in data.items() if k in schema_fields}
    text = _ExtractionSchema(**filtered).model_dump_json()
    mock_resp = MagicMock()
    mock_resp.text = text
    return mock_resp


def _image_payload() -> DocumentPayload:
    """A minimal vision-direct image payload."""
    return DocumentPayload(
        modality="image",
        image_bytes=b"fake-image-bytes",
        image_mime="image/jpeg",
    )


def _text_payload() -> DocumentPayload:
    """A minimal text payload (native-PDF / OCR path)."""
    return DocumentPayload(
        modality="native_pdf",
        text="Invoice #001  Total: $42.50",
    )


# ---------------------------------------------------------------------------
# Schema-valid parsing
# ---------------------------------------------------------------------------


def test_extract_image_returns_schema_valid_data() -> None:
    """A valid Gemini JSON response parses into Document-compatible data (AC)."""
    backend, mock_client = _make_backend()
    mock_client.models.generate_content.return_value = _json_response(
        {
            "doc_type": "receipt",
            "vendor_name": "Test Cafe",
            "invoice_number": "R-001",
            "document_date": "2024-03-15",
            "currency": "USD",
            "subtotal": 10.00,
            "tax": 0.80,
            "total": 10.80,
        }
    )

    result = backend.extract(_image_payload(), Document)

    assert result.data["doc_type"] == "receipt"
    assert result.data["vendor_name"] == "Test Cafe"
    assert result.data["total"] == pytest.approx(10.80)
    # The dict must validate cleanly into the Document schema.
    doc = Document.model_validate(result.data)
    assert doc.vendor_name == "Test Cafe"
    assert doc.total == pytest.approx(10.80)


def test_extract_text_returns_schema_valid_data() -> None:
    """A text payload (native-PDF / OCR) is accepted and parsed correctly."""
    backend, mock_client = _make_backend()
    mock_client.models.generate_content.return_value = _json_response(
        {"doc_type": "invoice", "invoice_number": "INV-42", "total": 99.99}
    )

    result = backend.extract(_text_payload(), Document)

    assert result.data["doc_type"] == "invoice"
    assert result.data["total"] == pytest.approx(99.99)
    Document.model_validate(result.data)  # must not raise


def test_extract_null_fields_become_none() -> None:
    """Absent fields in the JSON response survive as None through the Document."""
    backend, mock_client = _make_backend()
    mock_client.models.generate_content.return_value = _json_response(
        {"doc_type": "other", "total": None, "vendor_name": None}
    )

    result = backend.extract(_image_payload(), Document)
    doc = Document.model_validate(result.data)

    assert doc.total is None
    assert doc.vendor_name is None


def test_field_confidence_is_none() -> None:
    """GeminiBackend returns None field_confidence (no per-field signal)."""
    backend, mock_client = _make_backend()
    mock_client.models.generate_content.return_value = _json_response({"total": 5.00})

    result = backend.extract(_image_payload(), Document)

    assert result.field_confidence is None


def test_payload_without_image_or_text_raises() -> None:
    """A payload with neither image_bytes nor text raises ValueError."""
    backend, _ = _make_backend()
    bad_payload = DocumentPayload(modality="image")

    with pytest.raises(ValueError, match="image_bytes"):
        backend.extract(bad_payload, Document)


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


def test_retry_succeeds_on_second_attempt() -> None:
    """A transient error on attempt 1 is retried; attempt 2 returns data."""
    backend, mock_client = _make_backend()
    good_response = _json_response({"doc_type": "receipt", "total": 7.77})
    mock_client.models.generate_content.side_effect = [
        RuntimeError("transient network error"),
        good_response,
    ]

    with patch("doc_agent.backends.gemini.time.sleep") as mock_sleep:
        result = backend.extract(_image_payload(), Document)

    assert result.data["total"] == pytest.approx(7.77)
    assert mock_client.models.generate_content.call_count == 2
    mock_sleep.assert_called_once()  # one backoff sleep between attempts


def test_retry_all_attempts_fail_raises_runtime_error() -> None:
    """All _MAX_RETRIES attempts failing raises RuntimeError (core catches it)."""
    backend, mock_client = _make_backend()
    mock_client.models.generate_content.side_effect = TimeoutError("request timed out")

    with patch("doc_agent.backends.gemini.time.sleep"):
        with pytest.raises(RuntimeError, match=f"failed after {_MAX_RETRIES}"):
            backend.extract(_image_payload(), Document)

    assert mock_client.models.generate_content.call_count == _MAX_RETRIES


def test_retry_backoff_grows_exponentially() -> None:
    """Each retry sleeps longer than the previous one (exponential backoff)."""
    backend, mock_client = _make_backend()
    mock_client.models.generate_content.side_effect = OSError("network")

    sleep_calls: list[float] = []
    with patch("doc_agent.backends.gemini.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        with pytest.raises(RuntimeError):
            backend.extract(_image_payload(), Document)

    # _MAX_RETRIES attempts -> (_MAX_RETRIES - 1) sleeps.
    assert len(sleep_calls) == _MAX_RETRIES - 1
    for i in range(1, len(sleep_calls)):
        assert sleep_calls[i] > sleep_calls[i - 1]


def test_no_sleep_on_first_attempt() -> None:
    """The first attempt is made immediately without any sleep."""
    backend, mock_client = _make_backend()
    mock_client.models.generate_content.return_value = _json_response({"total": 1.00})

    with patch("doc_agent.backends.gemini.time.sleep") as mock_sleep:
        backend.extract(_image_payload(), Document)

    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Factory integration
# ---------------------------------------------------------------------------


def test_factory_builds_gemini_backend() -> None:
    """create_backend resolves 'gemini' and returns a GeminiBackend."""
    from doc_agent.backends.base import create_backend
    from doc_agent.config import load_config

    settings = load_config(
        extraction_backend="gemini",
        gemini_api_key="test-key",
        image_strategy="vision_direct",
    )
    # Patch the google.genai.Client so no real HTTP client is constructed.
    with patch("google.genai.Client"):
        backend = create_backend(settings)

    assert isinstance(backend, GeminiBackend)
    assert backend.name == "gemini"
