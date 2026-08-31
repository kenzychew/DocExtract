"""Mocked unit tests for the plain Anthropic extraction backend.

No real API calls are made -- the anthropic client is bypassed by constructing
``AnthropicBackend`` via ``object.__new__`` and injecting a mock client
directly, mirroring ``tests/test_gemini.py``. Covers: schema-valid parsing of a
forced tool call, retry-then-succeed, all-retries-exhausted, a response with no
tool-use block (treated as a failed attempt), and factory integration.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from docfield.backends.anthropic import (
    AnthropicBackend,
    EXTRACT_TOOL_NAME,
    _MAX_RETRIES,
)
from docfield.backends.base import DocumentPayload
from docfield.schema.models import Document


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend() -> tuple[AnthropicBackend, MagicMock]:
    """Build an AnthropicBackend bypassing __init__ with a mock client."""
    mock_client = MagicMock()
    backend = object.__new__(AnthropicBackend)
    backend._model = "claude-test"
    backend._client = mock_client
    return backend, mock_client


def _tool_use_block(name: str, input_data: dict) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = input_data
    block.id = "toolu_test"
    return block


def _tool_response(data: dict, *, input_tokens: int = 100, output_tokens: int = 50) -> MagicMock:
    """Build a mock Claude response carrying one extract_document tool call."""
    response = MagicMock()
    response.content = [_tool_use_block(EXTRACT_TOOL_NAME, data)]
    response.stop_reason = "tool_use"
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    return response


def _image_payload() -> DocumentPayload:
    return DocumentPayload(modality="image", image_bytes=b"fake-image-bytes", image_mime="image/jpeg")


def _text_payload() -> DocumentPayload:
    return DocumentPayload(modality="native_pdf", text="Invoice #001  Total: $42.50")


# ---------------------------------------------------------------------------
# Schema-valid parsing
# ---------------------------------------------------------------------------


def test_extract_image_returns_schema_valid_data() -> None:
    """A forced tool-call response parses into Document-compatible data (AC)."""
    backend, mock_client = _make_backend()
    mock_client.messages.create.return_value = _tool_response(
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
    doc = Document.model_validate(result.data)
    assert doc.vendor_name == "Test Cafe"
    assert doc.total == pytest.approx(10.80)


def test_extract_text_returns_schema_valid_data() -> None:
    """A text payload (native-PDF / OCR) is accepted and parsed correctly."""
    backend, mock_client = _make_backend()
    mock_client.messages.create.return_value = _tool_response(
        {"doc_type": "invoice", "invoice_number": "INV-42", "total": 99.99}
    )

    result = backend.extract(_text_payload(), Document)

    assert result.data["doc_type"] == "invoice"
    assert result.data["total"] == pytest.approx(99.99)
    Document.model_validate(result.data)  # must not raise


def test_extract_null_fields_become_none() -> None:
    """Absent fields in the tool-call input survive as None through the Document."""
    backend, mock_client = _make_backend()
    mock_client.messages.create.return_value = _tool_response(
        {"doc_type": "other", "total": None, "vendor_name": None}
    )

    result = backend.extract(_image_payload(), Document)
    doc = Document.model_validate(result.data)

    assert doc.total is None
    assert doc.vendor_name is None


def test_field_confidence_is_none() -> None:
    """AnthropicBackend returns None field_confidence (no per-field signal)."""
    backend, mock_client = _make_backend()
    mock_client.messages.create.return_value = _tool_response({"total": 5.00})

    result = backend.extract(_image_payload(), Document)

    assert result.field_confidence is None


def test_raw_carries_model_and_token_usage() -> None:
    """The raw result carries the model name and per-call token usage for cost accounting."""
    backend, mock_client = _make_backend()
    mock_client.messages.create.return_value = _tool_response(
        {"total": 5.00}, input_tokens=321, output_tokens=64
    )

    result = backend.extract(_image_payload(), Document)

    assert result.raw["model"] == "claude-test"
    assert result.raw["input_tokens"] == 321
    assert result.raw["output_tokens"] == 64
    assert result.raw["rounds"] == 1


def test_payload_without_image_or_text_raises() -> None:
    """A payload with neither image_bytes nor text raises ValueError."""
    backend, _ = _make_backend()
    bad_payload = DocumentPayload(modality="image")

    with pytest.raises(ValueError, match="image_bytes"):
        backend.extract(bad_payload, Document)


# ---------------------------------------------------------------------------
# No tool call in the response (e.g. a refusal)
# ---------------------------------------------------------------------------


def test_response_without_tool_use_is_treated_as_failed_attempt() -> None:
    """A response with no extract_document tool call exhausts retries and raises."""
    backend, mock_client = _make_backend()
    text_only = MagicMock()
    text_only.content = [MagicMock(type="text", text="I can't help with that.")]
    text_only.stop_reason = "end_turn"
    mock_client.messages.create.return_value = text_only

    with patch("docfield.backends.anthropic.time.sleep"):
        with pytest.raises(RuntimeError, match=f"failed after {_MAX_RETRIES}"):
            backend.extract(_image_payload(), Document)

    assert mock_client.messages.create.call_count == _MAX_RETRIES


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


def test_retry_succeeds_on_second_attempt() -> None:
    """A transient error on attempt 1 is retried; attempt 2 returns data."""
    backend, mock_client = _make_backend()
    good_response = _tool_response({"doc_type": "receipt", "total": 7.77})
    mock_client.messages.create.side_effect = [
        RuntimeError("transient network error"),
        good_response,
    ]

    with patch("docfield.backends.anthropic.time.sleep") as mock_sleep:
        result = backend.extract(_image_payload(), Document)

    assert result.data["total"] == pytest.approx(7.77)
    assert mock_client.messages.create.call_count == 2
    mock_sleep.assert_called_once()


def test_retry_all_attempts_fail_raises_runtime_error() -> None:
    """All _MAX_RETRIES attempts failing raises RuntimeError (core catches it)."""
    backend, mock_client = _make_backend()
    mock_client.messages.create.side_effect = TimeoutError("request timed out")

    with patch("docfield.backends.anthropic.time.sleep"):
        with pytest.raises(RuntimeError, match=f"failed after {_MAX_RETRIES}"):
            backend.extract(_image_payload(), Document)

    assert mock_client.messages.create.call_count == _MAX_RETRIES


def test_no_sleep_on_first_attempt() -> None:
    """The first attempt is made immediately without any sleep."""
    backend, mock_client = _make_backend()
    mock_client.messages.create.return_value = _tool_response({"total": 1.00})

    with patch("docfield.backends.anthropic.time.sleep") as mock_sleep:
        backend.extract(_image_payload(), Document)

    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Factory integration
# ---------------------------------------------------------------------------


def test_factory_builds_anthropic_backend() -> None:
    """create_backend resolves 'anthropic' and returns an AnthropicBackend."""
    from docfield.backends.base import create_backend
    from docfield.config import load_config

    settings = load_config(
        extraction_backend="anthropic",
        anthropic_api_key="test-key",
        image_strategy="vision_direct",
    )
    with patch("anthropic.Anthropic"):
        backend = create_backend(settings)

    assert isinstance(backend, AnthropicBackend)
    assert backend.name == "anthropic"


def test_available_backends_lists_anthropic_variants() -> None:
    """The factory registry exposes both 'anthropic' and 'anthropic-agentic'."""
    from docfield.backends.base import available_backends

    names = available_backends()
    assert "anthropic" in names
    assert "anthropic-agentic" in names
