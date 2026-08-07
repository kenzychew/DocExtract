"""Read/write the per-example prediction cache and reconstruct reports.

The predict phase writes one JSON file per example under
``eval/cache/<dataset>/<id>.json``; the score phase reads them back. Keeping the
model output on disk is what makes tuning free: the threshold sweep replays the
pure ``route`` function over the cached ``(confidence, validation)`` pairs and
never touches a model.

A cached entry has this shape::

    {
      "id": "X00016469670",
      "dataset": "sroie",
      "gold": {"vendor_name": ..., "total": ..., ...},
      "labeled_fields": ["vendor_name", "vendor_address", "document_date", "total"],
      "predicted": { ...Document.model_dump(mode="json")... },
      "confidence": 0.5,
      "decision": "review",         # decision at the predict-run threshold (informational)
      "modality": "image",
      "backend": "gemini",
      "validation": { "hard_failed": bool, "results": [...], ... },
      "error": null
    }
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from docfield.validation.rules import RuleResult, ValidationReport

# Default location for the cache; git-ignored (no evaluation data in the repo).
DEFAULT_CACHE_BASE = Path("eval/cache")

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(example_id: str) -> str:
    """Turn an example id into a filesystem-safe file stem."""
    return _UNSAFE_ID.sub("_", example_id)


def dataset_dir(cache_base: Path, dataset: str) -> Path:
    """Return the cache directory for a dataset (not created)."""
    return Path(cache_base) / dataset


def write_entry(cache_base: Path, dataset: str, entry: dict[str, Any]) -> Path:
    """Write one cache entry to ``<cache_base>/<dataset>/<id>.json``.

    Args:
        cache_base: Root cache directory.
        dataset: Dataset name (subdirectory).
        entry: The entry dict; must contain an ``"id"`` key.

    Returns:
        The path the entry was written to.
    """
    directory = dataset_dir(cache_base, dataset)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_safe_filename(str(entry['id']))}.json"
    path.write_text(json.dumps(entry, indent=2, default=str), encoding="utf-8")
    return path


def read_entries(cache_base: Path, dataset: str) -> list[dict[str, Any]]:
    """Load all cached entries for a dataset, sorted by filename.

    Args:
        cache_base: Root cache directory.
        dataset: Dataset name (subdirectory).

    Returns:
        A list of entry dicts (empty if the directory does not exist).
    """
    directory = dataset_dir(cache_base, dataset)
    if not directory.exists():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]


def existing_ids(cache_base: Path, dataset: str) -> set[str]:
    """Return the set of example ids already cached for a dataset."""
    return {str(entry["id"]) for entry in read_entries(cache_base, dataset)}


def errored_ids(cache_base: Path, dataset: str) -> set[str]:
    """Return the ids of cached entries whose pipeline run recorded an error.

    These are documents that produced no extraction -- a quota outage, a
    timeout, an unreadable file -- as distinct from documents the model read
    and the rules then rejected. Only these are worth re-running: a successful
    prediction must stay frozen so that before/after rule comparisons remain
    attributable to the rule change.

    Args:
        cache_base: Root cache directory.
        dataset: Dataset name (subdirectory).

    Returns:
        The set of example ids with a non-empty ``error`` field.
    """
    return {str(e["id"]) for e in read_entries(cache_base, dataset) if e.get("error")}


def report_from_dict(validation: dict[str, Any]) -> ValidationReport:
    """Reconstruct a :class:`ValidationReport` from its cached dict form.

    This lets the score phase replay the real ``route`` function over cached
    results -- in particular ``report.hard_failed`` is recomputed from the
    per-rule results, so the hard-failure override is honored during the sweep.

    Args:
        validation: The ``validation`` sub-dict of a cache entry (as produced by
            ``ValidationReport.to_dict``).

    Returns:
        A ``ValidationReport`` whose ``results`` mirror the cached rule outcomes.
    """
    results = tuple(
        RuleResult(
            code=item["code"],
            severity=item["severity"],
            status=item["status"],
            message=item.get("message", ""),
        )
        for item in validation.get("results", [])
    )
    return ValidationReport(results=results)
