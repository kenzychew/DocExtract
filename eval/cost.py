"""Per-call cost/latency/token instrumentation for the eval harness.

This is the only place the three-way backend comparison (gemini vs anthropic
vs anthropic-agentic) gets its cost and latency numbers from -- everything
here is additive to the eval harness and touches no backend source file
(CLAUDE.md: "Do not touch the Gemini backend ... this is additive only").

Two different capture strategies, because the backends expose usage
differently:

- **Anthropic / Anthropic-agentic** (``docfield/backends/anthropic*.py``, both
  written for this task) already put ``input_tokens``/``output_tokens`` from
  the real Claude ``response.usage`` into ``BackendResult.raw``, so
  :class:`InstrumentedBackend` reads it straight from there.
- **Gemini** (``docfield/backends/gemini.py``) does not surface token usage in
  its ``BackendResult`` today, and that file is explicitly out of scope to
  edit. Instead, :class:`InstrumentedBackend` wraps the *already-constructed*
  ``genai`` client's bound ``generate_content`` method at the instance level
  (a runtime monkeypatch of one object this module owns, not a source edit)
  and reads the real ``response.usage_metadata.{prompt_token_count,
  candidates_token_count}`` -- the actual field names on
  ``google.genai.types.GenerateContentResponseUsageMetadata`` in the
  ``google-genai`` SDK version this project pins.

Wall-clock latency is measured uniformly for every backend by timing the
``extract()`` call itself from here, so no backend-specific code is needed for
that part.

Pricing (per 1M tokens, USD; cited so the number can be checked/updated):

- Gemini 2.5 Flash: $0.30 input / $2.50 output. Source:
  ai.google.dev/gemini-api/docs/pricing, retrieved 2026-09-01. This is the
  model this project's README documents actually running
  (``GEMINI_MODEL=gemini-2.5-flash``); the ``gemini-flash-latest`` alias in
  ``.env.example`` may resolve to a different generation at eval time -- check
  the live pricing page if the alias has moved since.
- Claude Haiku 4.5: $1.00 input / $5.00 output. Source:
  platform.claude.com/docs/en/pricing (Claude API skill's cached model table,
  cache dated 2026-06-24), retrieved 2026-09-01. Haiku 4.5 is the model this
  project's ``anthropic``/``anthropic-agentic`` backends default to
  (``ANTHROPIC_MODEL=claude-haiku-4-5``) -- chosen as the cost-comparable tier
  to Gemini Flash for this comparison, not Anthropic's most capable model.
"""

from __future__ import annotations

import time
from typing import Any

# (input $/1M tokens, output $/1M tokens). See module docstring for citations.
_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Which pricing row applies to each registered backend name (backends/base.py).
_BACKEND_PRICING_KEY: dict[str, str] = {
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-haiku-4-5",
    "anthropic-agentic": "claude-haiku-4-5",
}


def compute_cost(backend_name: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    """Compute USD cost for one call from token counts and the pricing table.

    Args:
        backend_name: A registered backend name (``ExtractionBackend.name``).
        input_tokens: Input/prompt token count, or ``None`` if unavailable.
        output_tokens: Output/candidate token count, or ``None`` if unavailable.

    Returns:
        The cost in USD, or ``None`` if the backend has no pricing entry or
        either token count is unavailable.
    """
    if input_tokens is None or output_tokens is None:
        return None
    key = _BACKEND_PRICING_KEY.get(backend_name)
    if key is None:
        return None
    in_rate, out_rate = _PRICING_PER_MTOK[key]
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


class InstrumentedBackend:
    """Wraps an ``ExtractionBackend`` to record latency/tokens/cost per call.

    Satisfies the same protocol it wraps (``name`` + ``extract``), so it drops
    straight into ``process_document(..., backend=...)`` exactly like the real
    backend. After each ``extract()`` call, the four ``last_*`` attributes hold
    that call's stats for the caller to read (the eval predict loop is
    single-threaded and sequential, so there is no concurrent-call hazard).

    Attributes:
        name: The wrapped backend's identifier.
        last_latency_s: Wall-clock seconds for the most recent ``extract()``
            call (including any internal retries the backend performed).
        last_input_tokens: Input token count for the most recent call, or
            ``None`` if the backend exposed none.
        last_output_tokens: Output token count for the most recent call, or
            ``None``.
        last_cost_usd: Computed USD cost for the most recent call, or ``None``.
        last_raw: The wrapped backend's ``BackendResult.raw`` from the most
            recent call, for extra diagnostics (e.g. agentic round count).
    """

    def __init__(self, backend: Any) -> None:
        """Wrap a backend instance, installing the Gemini usage capture if needed.

        Args:
            backend: A constructed object satisfying ``ExtractionBackend``
                (e.g. the return of ``create_backend``).
        """
        self._backend = backend
        self.name: str = backend.name
        self.last_latency_s: float = 0.0
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None
        self.last_cost_usd: float | None = None
        self.last_raw: dict[str, Any] | None = None
        self._gemini_usage: Any = None
        if self.name == "gemini":
            self._install_gemini_usage_capture(backend)

    def _install_gemini_usage_capture(self, backend: Any) -> None:
        """Monkeypatch this Gemini backend instance's client to capture usage.

        Wraps the bound ``generate_content`` method on the already-constructed
        ``genai`` client so every call's ``response.usage_metadata`` is stashed
        on this wrapper -- without editing ``docfield/backends/gemini.py``.

        Args:
            backend: The ``GeminiBackend`` instance to instrument.
        """
        client = backend._client
        original = client.models.generate_content

        def _capturing_generate_content(*args: Any, **kwargs: Any) -> Any:
            response = original(*args, **kwargs)
            self._gemini_usage = getattr(response, "usage_metadata", None)
            return response

        client.models.generate_content = _capturing_generate_content

    def extract(self, payload: Any, schema: Any) -> Any:
        """Run the wrapped backend's ``extract()``, recording stats as a side effect.

        Args:
            payload: Forwarded to the wrapped backend.
            schema: Forwarded to the wrapped backend.

        Returns:
            Whatever the wrapped backend returns (a ``BackendResult``), unmodified.
        """
        self._gemini_usage = None
        start = time.perf_counter()
        result = self._backend.extract(payload, schema)
        self.last_latency_s = time.perf_counter() - start

        if self.name == "gemini":
            usage = self._gemini_usage
            input_tokens = getattr(usage, "prompt_token_count", None) if usage else None
            output_tokens = getattr(usage, "candidates_token_count", None) if usage else None
            raw = result.raw
        else:
            raw = result.raw or {}
            input_tokens = raw.get("input_tokens")
            output_tokens = raw.get("output_tokens")

        self.last_input_tokens = input_tokens
        self.last_output_tokens = output_tokens
        self.last_cost_usd = compute_cost(self.name, input_tokens, output_tokens)
        self.last_raw = raw if isinstance(raw, dict) else None
        return result
