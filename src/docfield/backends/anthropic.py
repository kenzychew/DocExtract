"""Anthropic (Claude) extraction backend using forced tool-use.

Calls the Claude Messages API with a forced tool call to get schema-constrained
JSON output (CLAUDE.md rule 4) -- the same idea as Gemini's ``response_schema``,
expressed through Claude's tool-use mechanism instead: one tool is defined whose
input schema mirrors the extraction contract, and ``tool_choice`` forces the
model to call it, so the response is a validated tool-call input rather than
free-form text. Bounded retries with exponential backoff mirror the Gemini
backend's pattern; exhausting them raises ``RuntimeError`` so the core routes
the document to review (rule 6).

Architecture rules honoured here:
- Rule 2: no direct SDK import at module load; ``anthropic`` is imported inside
  ``__init__`` (lazy, so this module stays a dependency leaf until the
  Anthropic backend is actually selected).
- Rule 3: the model identifier comes from ``Settings.anthropic_model``
  (config), never hardcoded.
- Rule 4: schema-constrained output via a forced tool call; no regex.

See ``src/docfield/backends/anthropic_agentic.py`` for the sibling backend that
adds a self-correction tool-use loop on top of this one.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from docfield.backends.base import BackendResult, DocumentPayload
from docfield.config import Settings

logger = logging.getLogger(__name__)

_EXTRACT_PROMPT: str = """\
You are a document-extraction assistant. Extract every available field from \
this document and call the extract_document tool with the result.

Rules:
- Set any absent or illegible field to null.
- Dates must be ISO 8601 strings (YYYY-MM-DD) or null.
- Monetary amounts must be plain numbers with no currency symbols.
- doc_type must be exactly "receipt", "invoice", or "other".
- currency must be an ISO 4217 code (e.g. "USD", "SGD") or null.
"""

_MAX_RETRIES: int = 3
_BASE_BACKOFF_S: float = 1.0
_TIMEOUT_S: float = 60.0

EXTRACT_TOOL_NAME: str = "extract_document"


class _LineItem(BaseModel):
    """Claude-serializable line item (all primitives so the JSON schema is clean)."""

    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class _ExtractionSchema(BaseModel):
    """Schema behind the forced ``extract_document`` tool call.

    Mirrors Gemini's ``_ExtractionSchema``: date fields are plain strings so the
    generated JSON schema stays simple; ``Document``'s validators downstream
    coerce them to ``datetime.date`` (CLAUDE.md rule 4 -- structured output is
    enforced at validation time, not by regex).
    """

    doc_type: str = "other"
    vendor_name: str | None = None
    vendor_address: str | None = None
    invoice_number: str | None = None
    document_date: str | None = None
    due_date: str | None = None
    currency: str | None = None
    line_items: list[_LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None


def _extract_tool_definition() -> dict[str, Any]:
    """Build the forced tool definition from :class:`_ExtractionSchema`.

    The input schema is generated from the Pydantic model
    (``model_json_schema``) rather than hand-written, so the tool contract and
    the schema used to validate the model's own field types can never drift
    apart.

    Returns:
        A Claude tool definition dict ready for the ``tools`` request field.
    """
    schema = _ExtractionSchema.model_json_schema()
    # Claude's tool input_schema has no notion of pydantic's $defs indirection
    # for a top-level schema with inline nested models; model_json_schema()
    # already inlines $ref/$defs correctly for the API's JSON Schema subset.
    return {
        "name": EXTRACT_TOOL_NAME,
        "description": (
            "Record the extracted fields for this document. Call this exactly "
            "once with your best extraction."
        ),
        "input_schema": schema,
    }


def _build_content(payload: DocumentPayload, prompt: str) -> list[dict[str, Any]]:
    """Build the Claude message content blocks from a document payload.

    Args:
        payload: The document payload carrying image bytes or text.
        prompt: The instruction text to send alongside the document.

    Returns:
        A list of Claude content block dicts for the user message.

    Raises:
        ValueError: If the payload has neither ``image_bytes`` nor ``text``.
    """
    import base64

    if payload.image_bytes is not None:
        mime = payload.image_mime or "image/jpeg"
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.standard_b64encode(payload.image_bytes).decode("ascii"),
                },
            },
            {"type": "text", "text": prompt},
        ]
    if payload.text is not None:
        return [{"type": "text", "text": f"{prompt}\n\n{payload.text}"}]

    raise ValueError(
        "DocumentPayload must supply either image_bytes (vision_direct) "
        "or text (OCR / native-PDF path)."
    )


def _extraction_input_to_data(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Validate a tool call's input against :class:`_ExtractionSchema` and flatten it.

    Args:
        tool_input: The raw ``input`` dict of an ``extract_document`` tool call.

    Returns:
        A dict ready for ``Document.model_validate`` (line items flattened to
        plain dicts).
    """
    extracted = _ExtractionSchema.model_validate(tool_input)
    data: dict[str, Any] = extracted.model_dump()
    data["line_items"] = [li.model_dump() for li in extracted.line_items]
    return data


class AnthropicBackend:
    """Extraction backend that calls the Claude Messages API with a forced tool call.

    Accepts image bytes (``vision_direct`` mode) or plain text (native-PDF /
    OCR path), forces a single ``extract_document`` tool call for
    schema-constrained output, and retries up to ``_MAX_RETRIES`` times with
    exponential backoff on transient failures.

    Attributes:
        name: Backend identifier used in logs and the factory registry.
    """

    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        """Build the Anthropic client from validated settings.

        Imports ``anthropic`` lazily here so the module stays a dependency leaf
        until this backend is actually selected (architecture rule 2).

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
        """Extract document fields from a payload with bounded retries.

        Args:
            payload: The acquired document representation. Must carry either
                ``image_bytes`` (vision_direct) or ``text`` (text path).
            schema: The Pydantic model defining the output contract (the core
                passes ``Document``); accepted for interface conformance, not
                used to constrain the API call (see ``_ExtractionSchema``).

        Returns:
            A ``BackendResult`` with the extracted data dict, ``None``
            field_confidence (Claude exposes no per-field signal), and ``raw``
            carrying the model name and token usage for cost accounting.

        Raises:
            RuntimeError: When all ``_MAX_RETRIES`` attempts fail. The core
                catches this and routes the document to review.
        """
        content = _build_content(payload, _EXTRACT_PROMPT)
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            if attempt:
                backoff = _BASE_BACKOFF_S * (2 ** (attempt - 1))
                logger.debug(
                    "anthropic retry attempt=%d/%d backoff=%.1fs source=%s",
                    attempt + 1,
                    _MAX_RETRIES,
                    backoff,
                    payload.source_path,
                )
                time.sleep(backoff)
            try:
                return self._call_api(content)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "anthropic API attempt %d/%d failed source=%s error=%s",
                    attempt + 1,
                    _MAX_RETRIES,
                    payload.source_path,
                    exc,
                )
                last_exc = exc

        raise RuntimeError(
            f"Anthropic extraction failed after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def _call_api(self, content: list[dict[str, Any]]) -> BackendResult:
        """Make one Claude API call forcing the ``extract_document`` tool.

        Args:
            content: The message content blocks produced by ``_build_content``.

        Returns:
            A ``BackendResult`` with the extracted data dict.

        Raises:
            RuntimeError: If Claude's response carries no ``extract_document``
                tool-use block (e.g. a refusal) -- treated as a failed attempt
                so the bounded retry loop can try again.
        """
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            tools=[_extract_tool_definition()],
            tool_choice={"type": "tool", "name": EXTRACT_TOOL_NAME},
            messages=[{"role": "user", "content": content}],
        )

        tool_use = next(
            (block for block in response.content if block.type == "tool_use"), None
        )
        if tool_use is None:
            raise RuntimeError(
                f"Claude response carried no {EXTRACT_TOOL_NAME!r} tool call "
                f"(stop_reason={response.stop_reason!r})"
            )

        data = _extraction_input_to_data(tool_use.input)

        # Field count, not field values: the log must not carry document
        # content (see the note in the Gemini backend / Space entry point).
        logger.debug(
            "anthropic extraction complete model=%s fields_populated=%d",
            self._model,
            sum(1 for v in data.values() if v not in (None, [], "")),
        )

        usage = response.usage
        return BackendResult(
            data=data,
            # Claude exposes no per-field confidence; the scorer handles None
            # with a neutral prior (architecture section 8), same as Gemini.
            field_confidence=None,
            raw={
                "model": self._model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "rounds": 1,
            },
        )
