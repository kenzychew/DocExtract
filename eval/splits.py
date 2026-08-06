"""Named subsets of a dataset's cache (tuning vs held-out).

Threshold selection and rule diagnosis have to happen on *some* documents, and
metrics reported on those same documents are contaminated: the operating point
was fitted to them. Splitting the cache keeps the two roles apart, so a held-out
number can be quoted without that caveat.

**Membership is by id, never by position.** A manifest file pins the exact ids
belonging to a split::

    eval/splits/<dataset>_tuning.json   {"ids": ["X00016469670", ...], ...}

Everything cached for the dataset that is *not* in the manifest is held out.
That definition is stable under any of the things that break a positional
"first N" rule: cache files are read in filesystem-glob order, which need not
match the order the dataset streamed them in; new documents can be added later;
a re-predict can rewrite a file's timestamp. Only the id set matters, so the
same documents land in the same split on every machine and every run.

The manifest is committed even though ``eval/cache/`` is git-ignored. The cache
can be regenerated from the dataset; the record of *which documents were used to
tune* cannot be, and losing it would silently turn a held-out claim into an
in-sample one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

# Committed alongside the code, unlike the cache itself.
DEFAULT_SPLITS_DIR = Path("eval/splits")

SplitName = Literal["all", "tuning", "heldout"]
SPLIT_NAMES: tuple[str, ...] = ("all", "tuning", "heldout")


class SplitError(RuntimeError):
    """Raised when a named split cannot be resolved for a dataset."""


def manifest_path(dataset: str, *, splits_dir: Path = DEFAULT_SPLITS_DIR) -> Path:
    """Return the tuning-manifest path for a dataset (not checked for existence)."""
    return Path(splits_dir) / f"{dataset}_tuning.json"


def load_tuning_ids(dataset: str, *, splits_dir: Path = DEFAULT_SPLITS_DIR) -> frozenset[str]:
    """Load the set of tuning-slice ids for a dataset.

    Args:
        dataset: Dataset name.
        splits_dir: Directory holding split manifests.

    Returns:
        The frozen set of example ids in the tuning slice.

    Raises:
        SplitError: If the manifest is missing, unreadable, or declares no ids.
    """
    path = manifest_path(dataset, splits_dir=splits_dir)
    if not path.exists():
        raise SplitError(
            f"No tuning manifest for dataset {dataset!r} at {path}. "
            "A tuning/held-out split needs one; score the whole cache with "
            "--split all, or write the manifest first."
        )
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SplitError(f"Tuning manifest {path} is not valid JSON: {exc}") from exc

    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise SplitError(f"Tuning manifest {path} declares no 'ids' list.")
    return frozenset(str(value) for value in ids)


def select(
    entries: list[dict[str, Any]],
    split: str,
    *,
    dataset: str,
    splits_dir: Path = DEFAULT_SPLITS_DIR,
) -> list[dict[str, Any]]:
    """Filter cached entries down to a named split.

    Selection is set membership on the entry id, so the result depends only on
    which documents are cached -- not on the order they were read in. Input order
    is preserved among the surviving entries.

    Args:
        entries: Cached entries for the dataset.
        split: One of ``"all"``, ``"tuning"``, or ``"heldout"``.
        dataset: Dataset name, used to locate the manifest.
        splits_dir: Directory holding split manifests.

    Returns:
        The subset of ``entries`` belonging to ``split``.

    Raises:
        SplitError: If ``split`` is unknown, the manifest is needed but missing,
            or the split resolves to no documents at all.
    """
    if split not in SPLIT_NAMES:
        raise SplitError(f"Unknown split {split!r}; expected one of {', '.join(SPLIT_NAMES)}.")
    if split == "all":
        return list(entries)

    tuning_ids = load_tuning_ids(dataset, splits_dir=splits_dir)
    if split == "tuning":
        selected = [e for e in entries if str(e.get("id")) in tuning_ids]
    else:
        selected = [e for e in entries if str(e.get("id")) not in tuning_ids]

    if not selected:
        raise SplitError(
            f"Split {split!r} selected 0 of {len(entries)} cached entries for "
            f"dataset {dataset!r}. The cache may not have been expanded beyond "
            "the tuning slice yet."
        )
    return selected


def describe(split: str, n_selected: int, n_total: int) -> str:
    """Render a one-line description of the active split for a report header.

    Args:
        split: The split name.
        n_selected: Documents in the split.
        n_total: Documents cached for the dataset overall.

    Returns:
        A human-readable summary naming the split and its size.
    """
    if split == "all":
        note = "WHOLE CACHE -- mixes tuning and held-out documents"
    elif split == "tuning":
        note = "TUNING slice -- the operating point was fitted on these, not independent"
    else:
        note = "HELD OUT -- never used to select the threshold or diagnose rules"
    return f"{split} ({n_selected} of {n_total} cached): {note}"
