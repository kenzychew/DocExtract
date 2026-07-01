"""Common types for dataset adapters.

A :class:`GoldExample` is the unit the predict phase consumes: an id, the input
(a PIL image for image datasets, or a file path for file-based ones), and the
gold labels mapped onto ``Document`` field names. :class:`DatasetAdapter` is the
protocol every concrete adapter satisfies.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class GoldExample:
    """One evaluation example with its ground-truth labels.

    Attributes:
        id: Stable, unique identifier for the example (used as the cache key).
        gold: Ground-truth values keyed by ``Document`` field name. A value is
            ``None`` (or absent) when the dataset does not label that field for
            this example. Values are raw (strings/numbers as the dataset stores
            them); the scorer normalizes them before comparison.
        image: The input image as a PIL ``Image`` for image datasets, or
            ``None`` for file-based datasets.
        source_path: Path to an input file for file-based datasets, or ``None``
            when the input is an in-memory ``image``.
        suffix: File extension to use when writing ``image`` to a temp file so
            modality detection sees the right type (e.g. ".png").
    """

    id: str
    gold: dict[str, Any]
    image: Any = None
    source_path: str | None = None
    suffix: str = ".png"
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DatasetAdapter(Protocol):
    """Interface every dataset adapter implements.

    Attributes:
        name: Stable identifier for the dataset (e.g. "sroie").
        hf_id: The Hugging Face dataset id it loads from.
        labeled_fields: The ``Document`` field names this dataset provides gold
            labels for; the scorer computes metrics only for these.
    """

    name: str
    hf_id: str
    labeled_fields: tuple[str, ...]

    def load(self, limit: int | None = None) -> Iterator[GoldExample]:
        """Yield gold examples, at most ``limit`` of them.

        Args:
            limit: Maximum number of examples to yield; ``None`` for all.

        Yields:
            :class:`GoldExample` records in a fixed, deterministic order so a
            given ``limit`` always selects the same held-out slice.
        """
        ...
