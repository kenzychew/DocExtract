"""CORD (v2) dataset adapter -- scaffold, not yet wired.

CORD is ~11,000 Indonesian receipts captured in the wild with rich key-value
and line-item labels. The ``naver-clova-ix/cord-v2`` build stores ground truth
as a JSON string in ``ground_truth`` (a ``gt_parse`` object with ``menu`` line
items and a ``sub_total`` / ``total`` block).

Intended field mapping onto the ``Document`` schema (to implement when wired):

- ``gt_parse.total.total_price``       -> ``total``    (critical)
- ``gt_parse.sub_total.subtotal_price``-> ``subtotal``
- ``gt_parse.sub_total.tax_price``     -> ``tax``      (critical)
- ``gt_parse.menu[*]``                 -> ``line_items`` (name/cnt/price)
- store name (where present)           -> ``vendor_name``

This adapter is intentionally left unwired for T10 (SROIE-first). ``load`` raises
so an accidental predict run against CORD fails fast rather than spending quota
on an unvalidated mapping.
"""

from __future__ import annotations

from collections.abc import Iterator

from eval.datasets.base import GoldExample


class CordAdapter:
    """Scaffold adapter for the CORD v2 receipt benchmark (not wired)."""

    name: str = "cord"
    hf_id: str = "naver-clova-ix/cord-v2"
    split: str = "test"
    # Fields CORD would provide once the gt_parse mapping is implemented.
    labeled_fields: tuple[str, ...] = ("vendor_name", "subtotal", "tax", "total")

    def load(self, limit: int | None = None) -> Iterator[GoldExample]:
        """Not implemented -- CORD is scaffolded but not wired for T10.

        Args:
            limit: Unused.

        Raises:
            NotImplementedError: Always; wire the ``gt_parse`` mapping first.
        """
        raise NotImplementedError(
            "CORD adapter is scaffolded but not wired (T10 is SROIE-first). "
            "Implement the gt_parse -> Document mapping and add 'cord' to "
            "WIRED_DATASETS before running the predict phase against it."
        )
        yield  # pragma: no cover -- makes this a generator for the Protocol.
