"""Dataset adapters mapping public benchmark labels onto the ``Document`` schema.

Each adapter yields :class:`~eval.datasets.base.GoldExample` records: an id, the
input (a PIL image or a file path), and a gold dict keyed by ``Document`` field
names. Only the fields a dataset actually labels appear in ``labeled_fields``;
the scorer restricts every metric to that set (an unlabeled field is neither a
false positive nor a miss -- there is simply no ground truth for it).

SROIE is wired end-to-end first (T10). CORD and the invoice-JSON set are
scaffolded as adapters with their intended field mappings documented, but are
intentionally not wired yet -- calling ``load`` on them raises.
"""

from __future__ import annotations

from eval.datasets.base import DatasetAdapter, GoldExample
from eval.datasets.cord import CordAdapter
from eval.datasets.invoice_json import InvoiceJsonAdapter
from eval.datasets.sroie import SroieAdapter

# Registry of all known adapters, keyed by stable name.
ADAPTERS: dict[str, type[DatasetAdapter]] = {
    SroieAdapter.name: SroieAdapter,
    CordAdapter.name: CordAdapter,
    InvoiceJsonAdapter.name: InvoiceJsonAdapter,
}

# Adapters proven end-to-end and safe to run the predict phase against. The
# others are scaffolds; ``get_adapter`` still returns them (so their metadata is
# inspectable) but ``eval.predict`` refuses to run an unwired dataset.
WIRED_DATASETS: frozenset[str] = frozenset({SroieAdapter.name})


def get_adapter(name: str) -> DatasetAdapter:
    """Instantiate a dataset adapter by name.

    Args:
        name: The adapter's stable name (e.g. "sroie").

    Returns:
        A new adapter instance.

    Raises:
        KeyError: If no adapter is registered under ``name``.
    """
    if name not in ADAPTERS:
        available = ", ".join(sorted(ADAPTERS))
        raise KeyError(f"Unknown dataset {name!r}; available: {available}")
    return ADAPTERS[name]()


__all__ = [
    "ADAPTERS",
    "WIRED_DATASETS",
    "DatasetAdapter",
    "GoldExample",
    "SroieAdapter",
    "CordAdapter",
    "InvoiceJsonAdapter",
    "get_adapter",
]
