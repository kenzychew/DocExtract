"""Self-correcting Anthropic (Claude) backend with a client-side arithmetic tool.

Answers the concrete question this backend exists to test: today, an arithmetic
error (line items that don't sum to the stated total) is caught *after*
extraction by the hard-rule check in ``routing/score.py``, which forces the
document to manual review regardless of how confident the model was. This
backend gives the model a ``validate_arithmetic`` tool it can call *during*
extraction -- the sum is recomputed client-side (no LLM involved in the check
itself, exactly like the H2/H3 rules it mirrors) -- so the model can see a
mismatch and revise its own output before finalizing, within a small bounded
number of tool-call rounds.

Same schema-constrained ``extract_document`` tool as :mod:`docfield.backends.anthropic`
(reused from there, not duplicated), plus this one additional callable tool.
No other tools, no framework: a plain bounded loop over ``messages.create``,
matching the Gemini/Anthropic backends' existing bounded-retry shape (CLAUDE.md
rule 2's "adding a backend" contract -- implement the interface, register it,
nothing else changes).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel

from docfield.backends.anthropic import (
    _EXTRACT_PROMPT,
    _MAX_RETRIES,
    EXTRACT_TOOL_NAME,
    _BASE_BACKOFF_S,
    _TIMEOUT_S,
    _build_content,
    _extract_tool_definition,
    _extraction_input_to_data,
)
from docfield.backends.base import BackendResult, DocumentPayload
from docfield.config import Settings

logger = logging.getLogger(__name__)

VALIDATE_TOOL_NAME: str = "validate_arithmetic"

# Bounded rounds of the post-extraction self-correction loop (revise-or-finalize
# calls after the initial forced extraction). Matches the Gemini/Anthropic
# backends' bounded-retry posture: give the model a real chance to self-correct
# without an unbounded agentic loop.
_MAX_TOOL_ROUNDS: int = 3

_AGENTIC_PROMPT: str = (
    f"{_EXTRACT_PROMPT}\n"
    "You also have a validate_arithmetic tool that recomputes whether your "
    "line items sum to your stated subtotal/total. After your initial "
    "extraction, call validate_arithmetic to check your own work. If it "
    "reports a mismatch, reconsider the document and call extract_document "
    "again with a corrected reading. Once validate_arithmetic confirms your "
    "numbers reconcile (or you are confident they are correct as read), call "
    "extract_document one final time with your final answer."
)

_VALIDATE_TOOL_DEFINITION: dict[str, Any] = {
    "name": VALIDATE_TOOL_NAME,
    "description": (
        "Recompute whether the given line items sum to the given subtotal "
        "(client-side arithmetic, not a model judgment). Use this to check "
        "your own extraction before finalizing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "line_item_amounts": {
                "type": "array",
                "items": {"type": "number"},
                "description": "The 'amount' of each line item you extracted, in order.",
            },
            "subtotal": {
                "type": ["number", "null"],
                "description": "The subtotal (or total, if no subtotal is stated) you extracted.",
            },
        },
        "required": ["line_item_amounts", "subtotal"],
    },
}


def _validate_arithmetic(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Recompute whether line-item amounts sum to the stated subtotal.

    Pure, deterministic, client-side arithmetic -- no LLM involvement, mirroring
    the tolerance policy of ``validation.rules.money_close`` (a fixed absolute
    epsilon plus a small allowance per independently-rounded line item) so the
    tool's verdict agrees with the hard rule that will run after extraction.

    Args:
        tool_input: The tool call's ``input``, matching
            ``_VALIDATE_TOOL_DEFINITION``'s schema.

    Returns:
        A JSON-serializable dict with the recomputed sum, the stated subtotal,
        the residual, and whether they reconcile within tolerance.
    """
    amounts = [float(a) for a in tool_input.get("line_item_amounts") or []]
    subtotal = tool_input.get("subtotal")
    computed_sum = sum(amounts)

    if subtotal is None:
        return {
            "computed_sum": computed_sum,
            "stated_subtotal": None,
            "reconciles": None,
            "message": "No subtotal/total was given to check against.",
        }

    subtotal = float(subtotal)
    tolerance = 0.02 + 0.005 * max(0, len(amounts))
    residual = round(abs(computed_sum - subtotal), 2)
    reconciles = residual <= tolerance
    return {
        "computed_sum": computed_sum,
        "stated_subtotal": subtotal,
        "residual": residual,
        "reconciles": reconciles,
        "message": (
            "Line items reconcile with the stated subtotal/total."
            if reconciles
            else (
                f"Mismatch: line items sum to {computed_sum}, but the stated "
                f"subtotal/total is {subtotal} (off by {residual})."
            )
        ),
    }


class AnthropicAgenticBackend:
    """Claude backend with a bounded tool-calling loop for arithmetic self-correction.

    Extraction proceeds in two phases within one ``extract()`` call:

    1. A forced initial ``extract_document`` call, identical to
       :class:`~docfield.backends.anthropic.AnthropicBackend`.
    2. Up to ``_MAX_TOOL_ROUNDS`` rounds where the model may call
       ``validate_arithmetic`` (executed locally) or re-call
       ``extract_document`` with a revised answer. The loop stops as soon as a
       revised ``extract_document`` call arrives, or after the round budget is
       exhausted (the last accepted extraction is used either way).

    Attributes:
        name: Backend identifier used in logs and the factory registry.
    """

    name = "anthropic-agentic"

    def __init__(self, settings: Settings) -> None:
        """Build the Anthropic client from validated settings.

        Args:
            settings: Validated runtime configuration supplying the API key,
                model identifier, and timeout.
        """
        import anthropic

        self._model: str = settings.anthropic_model
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=_TIMEOUT_S,
        )

    def extract(self, payload: DocumentPayload, schema: type[BaseModel]) -> BackendResult:
        """Extract document fields with bounded self-correction, and bounded retries.

        Args:
            payload: The acquired document representation. Must carry either
                ``image_bytes`` (vision_direct) or ``text`` (text path).
            schema: The Pydantic model defining the output contract; accepted
                for interface conformance, not used directly (see
                ``docfield.backends.anthropic._ExtractionSchema``).

        Returns:
            A ``BackendResult`` with the extracted data dict, ``None``
            field_confidence, and ``raw`` carrying the model name, summed token
            usage across every round, and how many rounds ran.

        Raises:
            RuntimeError: When all ``_MAX_RETRIES`` attempts fail. The core
                catches this and routes the document to review.
        """
        content = _build_content(payload, _AGENTIC_PROMPT)
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            if attempt:
                backoff = _BASE_BACKOFF_S * (2 ** (attempt - 1))
                logger.debug(
                    "anthropic-agentic retry attempt=%d/%d backoff=%.1fs source=%s",
                    attempt + 1,
                    _MAX_RETRIES,
                    backoff,
                    payload.source_path,
                )
                time.sleep(backoff)
            try:
                return self._run_agentic_loop(content)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "anthropic-agentic attempt %d/%d failed source=%s error=%s",
                    attempt + 1,
                    _MAX_RETRIES,
                    payload.source_path,
                    exc,
                )
                last_exc = exc

        raise RuntimeError(
            f"Anthropic agentic extraction failed after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def _run_agentic_loop(self, content: list[dict[str, Any]]) -> BackendResult:
        """Run the forced-extraction + bounded self-correction rounds once.

        Args:
            content: The message content blocks produced by ``_build_content``.

        Returns:
            A ``BackendResult`` for the final accepted extraction.

        Raises:
            RuntimeError: If the initial forced call carries no
                ``extract_document`` tool-use block (e.g. a refusal).
        """
        tools = [_extract_tool_definition(), _VALIDATE_TOOL_DEFINITION]
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        total_input_tokens = 0
        total_output_tokens = 0
        validate_calls = 0
        revised = False

        # Phase 1: forced initial extraction (same shape as AnthropicBackend).
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            tools=tools,
            tool_choice={"type": "tool", "name": EXTRACT_TOOL_NAME},
            messages=messages,
        )
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        extract_call = next(
            (b for b in response.content if b.type == "tool_use"), None
        )
        if extract_call is None:
            raise RuntimeError(
                f"Claude response carried no {EXTRACT_TOOL_NAME!r} tool call "
                f"(stop_reason={response.stop_reason!r})"
            )
        current_data = _extraction_input_to_data(extract_call.input)

        messages.append({"role": "assistant", "content": response.content})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": extract_call.id,
                        "content": "Initial extraction recorded.",
                    }
                ],
            }
        )

        # Phase 2: bounded self-correction rounds.
        rounds_run = 1
        for _ in range(_MAX_TOOL_ROUNDS):
            rounds_run += 1
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                tools=tools,
                tool_choice={"type": "any"},
                messages=messages,
            )
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            messages.append({"role": "assistant", "content": response.content})

            tool_calls = [b for b in response.content if b.type == "tool_use"]
            if not tool_calls:
                break

            tool_results: list[dict[str, Any]] = []
            finalized = False
            for call in tool_calls:
                if call.name == VALIDATE_TOOL_NAME:
                    validate_calls += 1
                    verdict = _validate_arithmetic(call.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": str(verdict),
                        }
                    )
                elif call.name == EXTRACT_TOOL_NAME:
                    current_data = _extraction_input_to_data(call.input)
                    revised = True
                    finalized = True
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": "Revised extraction recorded as final.",
                        }
                    )

            messages.append({"role": "user", "content": tool_results})
            if finalized:
                break

        logger.debug(
            "anthropic-agentic extraction complete model=%s rounds=%d "
            "validate_calls=%d revised=%s fields_populated=%d",
            self._model,
            rounds_run,
            validate_calls,
            revised,
            sum(1 for v in current_data.values() if v not in (None, [], "")),
        )

        return BackendResult(
            data=current_data,
            field_confidence=None,
            raw={
                "model": self._model,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "rounds": rounds_run,
                "validate_calls": validate_calls,
                "revised": revised,
            },
        )
