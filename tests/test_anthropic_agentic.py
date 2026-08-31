"""Mocked unit tests for the self-correcting Anthropic (agentic) backend.

No real API calls are made -- ``AnthropicAgenticBackend`` is constructed via
``object.__new__`` with a mock client, and ``messages.create`` is driven with a
scripted sequence of responses to exercise the bounded tool-call loop: the
model calling ``validate_arithmetic``, seeing a mismatch, and revising via a
second ``extract_document`` call; the loop stopping when the round budget is
exhausted without a revision; and the pure ``_validate_arithmetic`` arithmetic
itself.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from docfield.backends.anthropic import EXTRACT_TOOL_NAME, _MAX_RETRIES
from docfield.backends.anthropic_agentic import (
    VALIDATE_TOOL_NAME,
    AnthropicAgenticBackend,
    _MAX_TOOL_ROUNDS,
    _validate_arithmetic,
)
from docfield.backends.base import DocumentPayload
from docfield.schema.models import Document


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend() -> tuple[AnthropicAgenticBackend, MagicMock]:
    mock_client = MagicMock()
    backend = object.__new__(AnthropicAgenticBackend)
    backend._model = "claude-test"
    backend._client = mock_client
    return backend, mock_client


def _tool_use_block(name: str, input_data: dict, *, call_id: str = "toolu_test") -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = input_data
    block.id = call_id
    return block


def _response(blocks: list, *, input_tokens: int = 100, output_tokens: int = 50) -> MagicMock:
    response = MagicMock()
    response.content = blocks
    response.stop_reason = "tool_use"
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    return response


def _image_payload() -> DocumentPayload:
    return DocumentPayload(modality="image", image_bytes=b"fake-image-bytes", image_mime="image/jpeg")


# ---------------------------------------------------------------------------
# _validate_arithmetic (pure, client-side)
# ---------------------------------------------------------------------------


def test_validate_arithmetic_detects_reconciling_sum() -> None:
    verdict = _validate_arithmetic({"line_item_amounts": [7.00, 4.00], "subtotal": 11.00})
    assert verdict["reconciles"] is True
    assert verdict["computed_sum"] == pytest.approx(11.00)


def test_validate_arithmetic_detects_mismatch() -> None:
    verdict = _validate_arithmetic({"line_item_amounts": [7.00, 4.00], "subtotal": 20.00})
    assert verdict["reconciles"] is False
    assert verdict["residual"] == pytest.approx(9.00)


def test_validate_arithmetic_handles_missing_subtotal() -> None:
    verdict = _validate_arithmetic({"line_item_amounts": [1.0, 2.0], "subtotal": None})
    assert verdict["reconciles"] is None


# ---------------------------------------------------------------------------
# Self-correction loop
# ---------------------------------------------------------------------------


def test_model_self_corrects_after_arithmetic_mismatch() -> None:
    """validate_arithmetic flags a mismatch, then extract_document revises it (the AC)."""
    backend, mock_client = _make_backend()

    initial = _response(
        [_tool_use_block(EXTRACT_TOOL_NAME, {"total": 20.00, "subtotal": 20.00, "tax": 0.0})]
    )
    validate_round = _response(
        [_tool_use_block(VALIDATE_TOOL_NAME, {"line_item_amounts": [11.00], "subtotal": 20.00})]
    )
    revised = _response(
        [_tool_use_block(EXTRACT_TOOL_NAME, {"total": 11.00, "subtotal": 11.00, "tax": 0.0})]
    )
    mock_client.messages.create.side_effect = [initial, validate_round, revised]

    result = backend.extract(_image_payload(), Document)

    assert result.data["total"] == pytest.approx(11.00)
    assert result.raw["revised"] is True
    assert result.raw["validate_calls"] == 1
    assert mock_client.messages.create.call_count == 3


def test_loop_stops_at_round_budget_keeping_last_accepted_extraction() -> None:
    """If the model never finalizes, the bounded loop stops and uses the last extraction."""
    backend, mock_client = _make_backend()

    initial = _response(
        [_tool_use_block(EXTRACT_TOOL_NAME, {"total": 20.00, "subtotal": 20.00})]
    )
    # The model keeps re-checking without ever calling extract_document again.
    keeps_validating = _response(
        [_tool_use_block(VALIDATE_TOOL_NAME, {"line_item_amounts": [11.00], "subtotal": 20.00})]
    )
    mock_client.messages.create.side_effect = [initial] + [keeps_validating] * _MAX_TOOL_ROUNDS

    result = backend.extract(_image_payload(), Document)

    # Never revised -- the initial (unreconciled) extraction is still what's returned.
    assert result.data["total"] == pytest.approx(20.00)
    assert result.raw["revised"] is False
    assert result.raw["validate_calls"] == _MAX_TOOL_ROUNDS
    # 1 initial + _MAX_TOOL_ROUNDS follow-up calls.
    assert mock_client.messages.create.call_count == 1 + _MAX_TOOL_ROUNDS


def test_clean_extraction_needs_no_revision() -> None:
    """A model that validates once, finds no mismatch, and re-confirms still finalizes."""
    backend, mock_client = _make_backend()

    initial = _response(
        [_tool_use_block(EXTRACT_TOOL_NAME, {"total": 11.00, "subtotal": 11.00})]
    )
    validate_round = _response(
        [_tool_use_block(VALIDATE_TOOL_NAME, {"line_item_amounts": [11.00], "subtotal": 11.00})]
    )
    finalize = _response(
        [_tool_use_block(EXTRACT_TOOL_NAME, {"total": 11.00, "subtotal": 11.00})]
    )
    mock_client.messages.create.side_effect = [initial, validate_round, finalize]

    result = backend.extract(_image_payload(), Document)

    assert result.data["total"] == pytest.approx(11.00)
    assert result.raw["rounds"] == 3


def test_token_usage_sums_across_all_rounds() -> None:
    """raw usage is the sum across every API call in the loop, not just the last one."""
    backend, mock_client = _make_backend()

    initial = _response(
        [_tool_use_block(EXTRACT_TOOL_NAME, {"total": 11.00, "subtotal": 11.00})],
        input_tokens=100,
        output_tokens=20,
    )
    validate_round = _response(
        [_tool_use_block(VALIDATE_TOOL_NAME, {"line_item_amounts": [11.00], "subtotal": 11.00})],
        input_tokens=150,
        output_tokens=10,
    )
    finalize = _response(
        [_tool_use_block(EXTRACT_TOOL_NAME, {"total": 11.00, "subtotal": 11.00})],
        input_tokens=180,
        output_tokens=15,
    )
    mock_client.messages.create.side_effect = [initial, validate_round, finalize]

    result = backend.extract(_image_payload(), Document)

    assert result.raw["input_tokens"] == 100 + 150 + 180
    assert result.raw["output_tokens"] == 20 + 10 + 15


# ---------------------------------------------------------------------------
# No initial tool call (refusal)
# ---------------------------------------------------------------------------


def test_no_initial_tool_call_is_treated_as_failed_attempt() -> None:
    backend, mock_client = _make_backend()
    text_only = MagicMock()
    text_only.content = [MagicMock(type="text", text="I can't help with that.")]
    text_only.stop_reason = "end_turn"
    mock_client.messages.create.return_value = text_only

    with patch("docfield.backends.anthropic_agentic.time.sleep"):
        with pytest.raises(RuntimeError, match=f"failed after {_MAX_RETRIES}"):
            backend.extract(_image_payload(), Document)

    assert mock_client.messages.create.call_count == _MAX_RETRIES


# ---------------------------------------------------------------------------
# Retry logic (whole-loop retry on transient failure)
# ---------------------------------------------------------------------------


def test_retry_reruns_whole_loop_on_transient_failure() -> None:
    backend, mock_client = _make_backend()
    good = _response([_tool_use_block(EXTRACT_TOOL_NAME, {"total": 5.00})])
    # After the successful initial extraction, the self-correction round runs
    # once more; an empty-content response ends phase 2 immediately.
    no_more_calls = _response([])
    mock_client.messages.create.side_effect = [RuntimeError("transient"), good, no_more_calls]

    with patch("docfield.backends.anthropic_agentic.time.sleep") as mock_sleep:
        result = backend.extract(_image_payload(), Document)

    assert result.data["total"] == pytest.approx(5.00)
    mock_sleep.assert_called_once()


# ---------------------------------------------------------------------------
# Factory integration
# ---------------------------------------------------------------------------


def test_factory_builds_anthropic_agentic_backend() -> None:
    from docfield.backends.base import create_backend
    from docfield.config import load_config

    settings = load_config(
        extraction_backend="anthropic-agentic",
        anthropic_api_key="test-key",
        image_strategy="vision_direct",
    )
    with patch("anthropic.Anthropic"):
        backend = create_backend(settings)

    assert isinstance(backend, AnthropicAgenticBackend)
    assert backend.name == "anthropic-agentic"
