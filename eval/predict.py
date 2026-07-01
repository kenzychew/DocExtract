"""Predict phase: run the core over a dataset slice and cache each result.

This is the **only** phase that calls a model backend and the only phase that
spends API quota. It is deliberately idempotent: an example already present in
the cache is skipped unless ``overwrite`` is set, so a re-run after an
interruption resumes rather than re-billing.

For each example the PIL image is written to a temporary file (``core`` takes a
path, not bytes) with an extension that makes modality detection classify it as
an image; ``process_document`` then runs the full detect -> acquire -> extract
-> validate -> score -> route pipeline. The gold labels, predicted document,
confidence, and validation report are cached; nothing is persisted to the app's
SQLite store (eval is not production ingestion).
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from doc_agent.backends.base import create_backend
from doc_agent.config import Settings, load_config
from doc_agent.core import process_document

from eval.cache import DEFAULT_CACHE_BASE, existing_ids, write_entry
from eval.datasets import WIRED_DATASETS, get_adapter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PredictStats:
    """Summary of a predict-phase run.

    Attributes:
        dataset: The dataset name.
        requested: The requested slice size (``limit``), or ``None`` for all.
        processed: Examples actually sent through the pipeline this run.
        skipped: Examples skipped because they were already cached.
        accepted: Processed examples the pipeline auto-accepted.
        review: Processed examples routed to review.
        errors: Processed examples whose pipeline stage raised (routed to review).
        failed: Examples that could not be prepared at all (e.g. the image would
            not encode); logged and skipped so one bad input never aborts the run.
    """

    dataset: str
    requested: int | None
    processed: int
    skipped: int
    accepted: int
    review: int
    errors: int
    failed: int


def _process_example(
    example: Any,
    dataset: str,
    labeled_fields: tuple[str, ...],
    settings: Settings,
    backend: Any,
) -> dict[str, Any]:
    """Save one example's image to a temp file, run the pipeline, build the entry.

    The temp file is created first and unlinked in a ``finally`` so it is cleaned
    up even if ``image.save`` or the pipeline raises. ``process_document`` itself
    never raises (rule 6); only image encoding can, and the caller isolates that.

    Args:
        example: The :class:`~eval.datasets.base.GoldExample` to process.
        dataset: Dataset name (recorded in the entry).
        labeled_fields: The dataset's labeled fields (recorded in the entry).
        settings: Validated configuration.
        backend: The extraction backend to use.

    Returns:
        A JSON-serializable cache entry for the example.
    """
    with tempfile.NamedTemporaryFile(suffix=example.suffix, delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        example.image.save(temp_path)
        result = process_document(temp_path, settings=settings, backend=backend)
    finally:
        temp_path.unlink(missing_ok=True)
    return _build_entry(example, result, dataset, labeled_fields)


def _build_entry(
    example: Any,
    result: Any,
    dataset: str,
    labeled_fields: tuple[str, ...],
) -> dict[str, Any]:
    """Assemble a JSON-serializable cache entry from an example and its result."""
    return {
        "id": example.id,
        "dataset": dataset,
        "gold": example.gold,
        "labeled_fields": list(labeled_fields),
        "predicted": result.document.model_dump(mode="json"),
        "confidence": result.confidence,
        "decision": result.decision,
        "modality": result.modality,
        "backend": result.backend_name,
        "validation": result.report.to_dict(),
        "error": result.error,
    }


def run_predict(
    dataset: str,
    limit: int | None,
    *,
    settings: Settings | None = None,
    cache_base: Path = DEFAULT_CACHE_BASE,
    overwrite: bool = False,
) -> PredictStats:
    """Run the pipeline over a dataset slice and cache each result.

    Args:
        dataset: Name of a wired dataset adapter (e.g. "sroie").
        limit: Number of examples to process (the held-out slice size); ``None``
            for the whole split.
        settings: Validated configuration; loaded from the environment when
            ``None`` (must select a backend that can read images).
        cache_base: Root cache directory. Defaults to ``eval/cache``.
        overwrite: Re-process and overwrite examples already cached. Defaults to
            ``False`` so re-runs resume without re-billing.

    Returns:
        A :class:`PredictStats` summary of the run.

    Raises:
        ValueError: If ``dataset`` is not wired for the predict phase.
    """
    if dataset not in WIRED_DATASETS:
        wired = ", ".join(sorted(WIRED_DATASETS))
        raise ValueError(
            f"Dataset {dataset!r} is scaffolded but not wired for prediction. "
            f"Wired datasets: {wired}."
        )

    adapter = get_adapter(dataset)
    settings = settings or load_config()
    backend = create_backend(settings)
    already = set() if overwrite else existing_ids(cache_base, dataset)

    processed = skipped = accepted = review = errors = failed = 0
    logger.info("eval-predict: dataset=%s limit=%s backend=%s", dataset, limit, backend.name)

    for example in adapter.load(limit):
        if example.id in already:
            skipped += 1
            logger.info("eval-predict: skip cached id=%s", example.id)
            continue

        # Isolate per-example preparation failures (e.g. an un-encodable image) so
        # one bad input logs and is skipped rather than aborting the whole slice.
        try:
            entry = _process_example(example, dataset, adapter.labeled_fields, settings, backend)
        except Exception as exc:  # noqa: BLE001 -- never let one document halt the run.
            failed += 1
            logger.error("eval-predict: could not prepare id=%s -- skipping: %s", example.id, exc)
            continue

        write_entry(cache_base, dataset, entry)

        processed += 1
        if entry["error"]:
            errors += 1
        if entry["decision"] == "accept":
            accepted += 1
        else:
            review += 1
        logger.info(
            "eval-predict: id=%s decision=%s confidence=%.3f error=%s",
            example.id,
            entry["decision"],
            entry["confidence"],
            bool(entry["error"]),
        )

    stats = PredictStats(
        dataset=dataset,
        requested=limit,
        processed=processed,
        skipped=skipped,
        accepted=accepted,
        review=review,
        errors=errors,
        failed=failed,
    )
    logger.info("eval-predict: done %s", stats)
    return stats
