"""Gemini extraction backend using the Google GenAI SDK.

Calls the Gemini API with multimodal (image) or text input, requests
schema-constrained JSON output (CLAUDE.md rule 4), and applies bounded retries
with exponential backoff before surfacing a ``RuntimeError`` that the core
catches and routes to review (rule 6 -- exhausted retries never crash the loop).

Architecture rules honoured here:
- Rule 2: no direct SDK import at module load; ``google.genai`` is imported
  inside ``__init__`` (lazy, so this module stays a dependency leaf until
  the Gemini backend is actually selected).
- Rule 3: the model identifier comes from ``Settings.gemini_model`` (config),
  never hardcoded.
- Rule 4: schema-constrained JSON output via ``response_schema``; no regex.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from doc_agent.backends.base import BackendResult, DocumentPayload
from doc_agent.config import Settings

logger = logging.getLogger(__name__)

_EXTRACT_PROMPT: str = """\
You are a document-extraction assistant. Extract every available field from \
this document and return them as JSON.

Rules:
- Set any absent or illegible field to null.
- Dates must be ISO 8601 strings (YYYY-MM-DD) or null.
- Monetary amounts must be plain numbers with no currency symbols.
- doc_type must be exactly "receipt", "invoice", or "other".
- currency must be an ISO 4217 code (e.g. "USD", "SGD") or null.
"""

_MAX_RETRIES: int = 3
_BASE_BACKOFF_S: float = 1.0
_TIMEOUT_MS: int = 60_000  # 60 s in milliseconds (HttpOptions.timeout unit)


class _LineItem(BaseModel):
    """Gemini-serializable line item (all primitives so the JSON schema is clean)."""

    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class _ExtractionSchema(BaseModel):
    """Schema sent to Gemini for constrained JSON output.

    Uses ``str`` for date fields so Gemini's JSON schema remains simple;
    ``Document``'s validators downstream coerce them to ``datetime.date``
    (CLAUDE.md rule 4 -- structured output is enforced at validation time,
    not by regex).
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


class GeminiBackend:
    """Extraction backend that calls the Gemini multimodal API.

    Accepts image bytes (``vision_direct`` mode) or plain text (native-PDF /
    OCR path), enforces schema-constrained JSON output, and retries up to
    ``_MAX_RETRIES`` times with exponential backoff on transient failures.

    Attributes:
        name: Backend identifier used in logs and the factory registry.
    """

    name = "gemini"

    def __init__(self, settings: Settings) -> None:
        """Build the Gemini client from validated settings.

        Imports ``google.genai`` lazily here so the module stays a dependency
        leaf until this backend is actually selected (architecture rule 2).

        Args:
            settings: Validated runtime configuration supplying the API key,
                model identifier, and timeout.
        """
        from google import genai
        from google.genai import types as _t

        self._model: str = settings.gemini_model
        self._types = _t
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=_t.HttpOptions(timeout=_TIMEOUT_MS),
        )

    def extract(self, payload: DocumentPayload, schema: type[BaseModel]) -> BackendResult:
        """Extract document fields from a payload with bounded retries.

        Args:
            payload: The acquired document representation.  Must carry either
                ``image_bytes`` (vision_direct) or ``text`` (text path).
            schema: The Pydantic model defining the output contract (the core
                passes ``Document``).  The backend uses ``_ExtractionSchema``
                for the API call and returns a dict the core validates into
                ``schema``.

        Returns:
            A ``BackendResult`` with the extracted data dict and ``None``
            field_confidence (the Gemini free tier exposes no per-field signal;
            scoring falls back to a neutral prior).

        Raises:
            RuntimeError: When all ``_MAX_RETRIES`` attempts fail.  The core
                catches this and routes the document to review.
        """
        contents = self._build_contents(payload)
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            if attempt:
                backoff = _BASE_BACKOFF_S * (2 ** (attempt - 1))
                logger.debug(
                    "gemini retry attempt=%d/%d backoff=%.1fs source=%s",
                    attempt + 1,
                    _MAX_RETRIES,
                    backoff,
                    payload.source_path,
                )
                time.sleep(backoff)
            try:
                return self._call_api(contents)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "gemini API attempt %d/%d failed source=%s error=%s",
                    attempt + 1,
                    _MAX_RETRIES,
                    payload.source_path,
                    exc,
                )
                last_exc = exc

        raise RuntimeError(
            f"Gemini extraction failed after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def _build_contents(self, payload: DocumentPayload) -> list[Any]:
        """Build the Gemini content list from a document payload.

        Args:
            payload: The document payload carrying image bytes or text.

        Returns:
            A list of genai ``Part`` objects for the API call.

        Raises:
            ValueError: If the payload has neither ``image_bytes`` nor ``text``.
        """
        types = self._types
        parts: list[Any] = [types.Part.from_text(text=_EXTRACT_PROMPT)]

        if payload.image_bytes is not None:
            mime = payload.image_mime or "image/jpeg"
            parts.append(types.Part.from_bytes(data=payload.image_bytes, mime_type=mime))
        elif payload.text is not None:
            parts.append(types.Part.from_text(text=payload.text))
        else:
            raise ValueError(
                "DocumentPayload must supply either image_bytes (vision_direct) "
                "or text (OCR / native-PDF path)."
            )

        return parts

    def _call_api(self, contents: list[Any]) -> BackendResult:
        """Make one Gemini API call and parse the schema-constrained response.

        Args:
            contents: The genai content parts produced by ``_build_contents``.

        Returns:
            A ``BackendResult`` with the extracted data dict.
        """
        types = self._types
        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_ExtractionSchema,
            ),
        )

        extracted = _ExtractionSchema.model_validate_json(response.text)
        data: dict[str, Any] = extracted.model_dump()
        data["line_items"] = [li.model_dump() for li in extracted.line_items]

        # Field count, not field values: the log must not carry document
        # content (see the note in the Space entry point).
        logger.debug(
            "gemini extraction complete model=%s fields_populated=%d line_items=%d",
            self._model,
            sum(1 for v in data.values() if v not in (None, [], "")),
            len(extracted.line_items),
        )

        return BackendResult(
            data=data,
            # Gemini free tier exposes no per-field confidence; the scorer
            # handles None with a neutral prior (architecture section 8).
            field_confidence=None,
            raw={"model": self._model},
        )
