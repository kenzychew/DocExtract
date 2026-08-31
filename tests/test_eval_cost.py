"""Unit tests for eval/cost.py: pricing math and the InstrumentedBackend wrapper.

Fully offline -- no real model calls. Backends are hand-built fakes (a
Gemini-shaped one whose ``extract()`` drives a mockable ``_client.models.
generate_content``, and an Anthropic-shaped one that returns usage in
``BackendResult.raw``) so both of ``InstrumentedBackend``'s capture strategies
are exercised without touching any real backend or SDK.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from docfield.backends.base import BackendResult
from eval.cost import InstrumentedBackend, compute_cost


# ---------------------------------------------------------------------------
# compute_cost
# ---------------------------------------------------------------------------


def test_compute_cost_gemini() -> None:
    cost = compute_cost("gemini", 1_000_000, 1_000_000)
    assert cost == pytest.approx(0.30 + 2.50)


def test_compute_cost_anthropic() -> None:
    cost = compute_cost("anthropic", 1_000_000, 1_000_000)
    assert cost == pytest.approx(1.00 + 5.00)


def test_compute_cost_anthropic_agentic_uses_same_rate_as_anthropic() -> None:
    """The agentic backend uses the same model/rate; only volume differs."""
    assert compute_cost("anthropic-agentic", 500_000, 200_000) == compute_cost(
        "anthropic", 500_000, 200_000
    )


def test_compute_cost_unknown_backend_is_none() -> None:
    assert compute_cost("stub", 100, 100) is None


def test_compute_cost_missing_tokens_is_none() -> None:
    assert compute_cost("gemini", None, 100) is None
    assert compute_cost("gemini", 100, None) is None


# ---------------------------------------------------------------------------
# InstrumentedBackend -- Anthropic-shaped (usage already in BackendResult.raw)
# ---------------------------------------------------------------------------


class _FakeAnthropicBackend:
    name = "anthropic"

    def extract(self, payload: object, schema: object) -> BackendResult:
        return BackendResult(
            data={"total": 1.0},
            field_confidence=None,
            raw={"model": "claude-haiku-4-5", "input_tokens": 200, "output_tokens": 80, "rounds": 1},
        )


def test_instrumented_backend_reads_anthropic_usage_from_raw() -> None:
    wrapped = InstrumentedBackend(_FakeAnthropicBackend())

    result = wrapped.extract(None, None)

    assert result.data == {"total": 1.0}
    assert wrapped.last_input_tokens == 200
    assert wrapped.last_output_tokens == 80
    assert wrapped.last_cost_usd == pytest.approx(compute_cost("anthropic", 200, 80))
    assert wrapped.last_latency_s >= 0.0
    assert wrapped.last_raw["rounds"] == 1


def test_instrumented_backend_exposes_wrapped_name() -> None:
    wrapped = InstrumentedBackend(_FakeAnthropicBackend())
    assert wrapped.name == "anthropic"


# ---------------------------------------------------------------------------
# InstrumentedBackend -- Gemini-shaped (usage captured via client monkeypatch)
# ---------------------------------------------------------------------------


class _FakeGeminiClient:
    def __init__(self) -> None:
        self.models = MagicMock()


class _FakeGeminiBackend:
    name = "gemini"

    def __init__(self) -> None:
        self._client = _FakeGeminiClient()

    def extract(self, payload: object, schema: object) -> BackendResult:
        # Mirrors GeminiBackend._call_api: calls generate_content, ignores the
        # response's usage here (that's exactly what InstrumentedBackend exists
        # to capture without editing gemini.py).
        self._client.models.generate_content(model="gemini-2.5-flash", contents=[])
        return BackendResult(data={"total": 2.0}, field_confidence=None, raw={"model": "gemini-2.5-flash"})


def test_instrumented_backend_captures_gemini_usage_via_client_wrap() -> None:
    fake = _FakeGeminiBackend()
    usage = SimpleNamespace(prompt_token_count=120, candidates_token_count=45)
    fake._client.models.generate_content.return_value = SimpleNamespace(usage_metadata=usage)

    wrapped = InstrumentedBackend(fake)
    result = wrapped.extract(None, None)

    assert result.data == {"total": 2.0}
    assert wrapped.last_input_tokens == 120
    assert wrapped.last_output_tokens == 45
    assert wrapped.last_cost_usd == pytest.approx(compute_cost("gemini", 120, 45))


def test_instrumented_backend_gemini_missing_usage_metadata_is_none() -> None:
    """A Gemini response with no usage_metadata leaves cost/tokens as None, not an error."""
    fake = _FakeGeminiBackend()
    fake._client.models.generate_content.return_value = SimpleNamespace(usage_metadata=None)

    wrapped = InstrumentedBackend(fake)
    wrapped.extract(None, None)

    assert wrapped.last_input_tokens is None
    assert wrapped.last_output_tokens is None
    assert wrapped.last_cost_usd is None


def test_instrumented_backend_does_not_leak_usage_across_calls() -> None:
    """A second call's usage must not be contaminated by the first's captured state.

    ``InstrumentedBackend`` replaces ``client.models.generate_content`` with its
    own closure at wrap time, so this test keeps a handle to the *original*
    mock (what the closure actually calls) to vary its return value per call --
    exactly like a real Gemini client returning a different response each time.
    """
    fake = _FakeGeminiBackend()
    original_mock = fake._client.models.generate_content
    first_usage = SimpleNamespace(prompt_token_count=10, candidates_token_count=5)
    original_mock.return_value = SimpleNamespace(usage_metadata=first_usage)
    wrapped = InstrumentedBackend(fake)
    wrapped.extract(None, None)
    assert wrapped.last_input_tokens == 10

    second_usage = SimpleNamespace(prompt_token_count=999, candidates_token_count=111)
    original_mock.return_value = SimpleNamespace(usage_metadata=second_usage)
    wrapped.extract(None, None)

    assert wrapped.last_input_tokens == 999
    assert wrapped.last_output_tokens == 111
