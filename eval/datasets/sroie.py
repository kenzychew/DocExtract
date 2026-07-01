"""SROIE (ICDAR 2019) dataset adapter -- wired end-to-end.

SROIE is ~1,000 real scanned receipts with four labeled key fields. Using the
``jsdnrs/ICDAR2019-SROIE`` mirror, the "test" split is the held-out evaluation
slice (it is never tuned against). Each example exposes:

- ``key``       -> the example id
- ``image``     -> a PIL image of the receipt (scan modality)
- ``entities``  -> {"company", "date", "address", "total"}

Field mapping onto the ``Document`` schema (data spec section 2):

- ``entities["company"]`` -> ``vendor_name``
- ``entities["address"]`` -> ``vendor_address``
- ``entities["date"]``    -> ``document_date``  (day-first D/M/Y, e.g. 15/01/2019)
- ``entities["total"]``   -> ``total``

SROIE does **not** label ``tax`` or ``invoice_number``, so of the three critical
fields only ``total`` is scored here. The dataset is loaded in streaming mode so
a small slice does not download the full split.
"""

from __future__ import annotations

from collections.abc import Iterator

from eval.datasets.base import GoldExample


class SroieAdapter:
    """Adapter for the SROIE scanned-receipt benchmark."""

    name: str = "sroie"
    hf_id: str = "jsdnrs/ICDAR2019-SROIE"
    split: str = "test"
    labeled_fields: tuple[str, ...] = (
        "vendor_name",
        "vendor_address",
        "document_date",
        "total",
    )

    def load(self, limit: int | None = None) -> Iterator[GoldExample]:
        """Yield the first ``limit`` SROIE test examples as gold examples.

        Streaming keeps a small slice cheap (no full-split download). The first
        ``limit`` examples form a fixed, reproducible slice.

        Args:
            limit: Maximum number of examples to yield; ``None`` for the whole
                split.

        Yields:
            :class:`GoldExample` records with the receipt image and mapped gold.
        """
        from datasets import load_dataset

        dataset = load_dataset(self.hf_id, split=self.split, streaming=True)
        for index, example in enumerate(dataset):
            if limit is not None and index >= limit:
                break
            entities = example.get("entities") or {}
            gold = {
                "vendor_name": entities.get("company"),
                "vendor_address": entities.get("address"),
                "document_date": entities.get("date"),
                "total": entities.get("total"),
            }
            yield GoldExample(
                id=str(example["key"]),
                gold=gold,
                image=example["image"],
                suffix=".png",
                metadata={"dataset": self.name, "index": index},
            )
