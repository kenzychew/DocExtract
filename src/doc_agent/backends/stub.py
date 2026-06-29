"""Deterministic, offline stub backend (no network, no provider SDK).

``StubBackend`` is the test double referenced by CLAUDE.md and the core smoke
test: it satisfies the :class:`~doc_agent.backends.base.ExtractionBackend`
protocol but returns fixed, schema-valid ``Document`` data instead of calling a
model. That keeps the core end-to-end testable offline -- the smoke test can run
``process_document`` with no API key, no Ollama server, and no quota -- while the
real adapters (build plan phases 2.5/2.6) are deferred.

The default payload is a small, internally-consistent receipt chosen so every
validation rule passes (the happy path that can auto-accept). Tests that need a
specific shape -- a malformed value, a hard-rule violation, a low-confidence
field -- inject their own ``data``/``field_confidence`` rather than mutating a
shared template; :meth:`StubBackend.extract` deep-copies its output so a caller
populating pipeline fields can never corrupt the next call.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

from doc_agent.backends.base import BackendResult, DocumentPayload

# A fixed, internally-consistent receipt used as the stub's default output.
# Chosen so every validation rule passes: subtotal + tax == total (H2), line
# amounts sum to subtotal (H3), per-line quantity*unit_price == amount (S4),
# total present and non-negative (H4), critical fields correctly typed (H1), and
# the soft fields (date, currency, vendor) all present and plausible. This lets
# the core smoke test exercise the clean-document auto-accept path without a
# network or a real model.
DEFAULT_STUB_DOCUMENT: dict[str, Any] = {
    "doc_type": "receipt",
    "vendor_name": "Stub Coffee House",
    "vendor_address": "1 Test Street, Singapore 000000",
    "invoice_number": "STUB-0001",
    "document_date": "2024-01-15",
    "due_date": None,
    "currency": "SGD",
    "line_items": [
        {"description": "Espresso", "quantity": 2, "unit_price": 3.50, "amount": 7.00},
        {"description": "Croissant", "quantity": 1, "unit_price": 4.00, "amount": 4.00},
    ],
    "subtotal": 11.00,
    "tax": 0.77,
    "total": 11.77,
}

# Per-field confidence the stub reports, uniform and high so the core's
# aggregation yields a strong model signal on the happy path. Keyed by schema
# field name, matching the populated fields of ``DEFAULT_STUB_DOCUMENT``.
DEFAULT_STUB_CONFIDENCE: dict[str, float] = {
    "doc_type": 0.99,
    "vendor_name": 0.97,
    "vendor_address": 0.95,
    "invoice_number": 0.96,
    "document_date": 0.98,
    "currency": 0.99,
    "subtotal": 0.98,
    "tax": 0.97,
    "total": 0.99,
}


class StubBackend:
    """An offline ``ExtractionBackend`` returning fixed, schema-valid data.

    The stub ignores the payload entirely and returns its configured ``data`` on
    every call, so its output is deterministic across runs (no clock, no
    randomness, no network). Construct it with custom ``data``/``field_confidence``
    to drive a specific pipeline path in a test.

    Attributes:
        name: The backend identifier ("stub"), used in logs and the factory.
    """

    name = "stub"

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        field_confidence: dict[str, float] | None = None,
    ) -> None:
        """Initialize the stub with the data it should return.

        Args:
            data: The extracted-fields dict to return; defaults to
                ``DEFAULT_STUB_DOCUMENT`` (a clean, reconciling receipt).
            field_confidence: Per-field confidence to return; defaults to
                ``DEFAULT_STUB_CONFIDENCE``. Pass ``None`` explicitly via a
                custom value to simulate a backend that exposes no signal.
        """
        self._data = DEFAULT_STUB_DOCUMENT if data is None else data
        self._field_confidence = (
            DEFAULT_STUB_CONFIDENCE if field_confidence is None else field_confidence
        )

    def extract(self, payload: DocumentPayload, schema: type[BaseModel]) -> BackendResult:
        """Return the configured deterministic data, ignoring the payload.

        The returned ``data`` is a deep copy of the template so a caller that
        mutates it (the core populates pipeline fields on the validated document,
        not this dict, but defensiveness here keeps repeated calls independent)
        cannot affect subsequent calls.

        Args:
            payload: The document payload; ignored by the stub.
            schema: The output-contract model class; accepted for interface
                conformance, not used to constrain the fixed data.

        Returns:
            A ``BackendResult`` whose ``data`` validates against ``schema``.
        """
        return BackendResult(
            data=copy.deepcopy(self._data),
            field_confidence=(
                dict(self._field_confidence) if self._field_confidence is not None else None
            ),
            raw={"backend": self.name, "note": "deterministic offline stub"},
        )
