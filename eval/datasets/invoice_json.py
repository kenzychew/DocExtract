"""Invoice-JSON dataset adapter -- scaffold, not yet wired.

The ``GokulRajaR/invoice-ocr-json`` set pairs native invoice images with
structured JSON ground truth (key-values covering invoice number, dates, and
monetary totals) -- convenient for the eval harness because the labels are
already close to the ``Document`` schema.

Intended field mapping onto the ``Document`` schema (to implement when wired):

- invoice number field  -> ``invoice_number``  (critical)
- invoice/issue date    -> ``document_date``
- due date              -> ``due_date``
- tax/VAT amount        -> ``tax``              (critical)
- grand total           -> ``total``            (critical)
- vendor/seller name    -> ``vendor_name``

This is the dataset that would exercise ``invoice_number`` and ``tax`` -- the two
critical fields SROIE does not label. It is intentionally left unwired for T10;
``load`` raises so a predict run cannot spend quota on an unvalidated mapping.
"""

from __future__ import annotations

from collections.abc import Iterator

from eval.datasets.base import GoldExample


class InvoiceJsonAdapter:
    """Scaffold adapter for the invoice-OCR-JSON benchmark (not wired)."""

    name: str = "invoice_json"
    hf_id: str = "GokulRajaR/invoice-ocr-json"
    split: str = "train"
    labeled_fields: tuple[str, ...] = (
        "vendor_name",
        "invoice_number",
        "document_date",
        "due_date",
        "tax",
        "total",
    )

    def load(self, limit: int | None = None) -> Iterator[GoldExample]:
        """Not implemented -- this set is scaffolded but not wired for T10.

        Args:
            limit: Unused.

        Raises:
            NotImplementedError: Always; implement the JSON->Document mapping
                first.
        """
        raise NotImplementedError(
            "invoice_json adapter is scaffolded but not wired (T10 is "
            "SROIE-first). Implement the JSON -> Document mapping and add "
            "'invoice_json' to WIRED_DATASETS before running predict against it."
        )
        yield  # pragma: no cover -- makes this a generator for the Protocol.
