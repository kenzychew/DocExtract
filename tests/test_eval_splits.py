"""Tests for tuning/held-out split selection (``eval.splits``).

The property that matters is that the split is a function of *which documents
are cached*, not of the order they happen to be read in. A positional rule
("the first 100 files") would silently reassign documents between tuning and
held-out when the glob order changes, when the cache is regenerated on another
machine, or when new documents are added -- turning a held-out claim into an
in-sample one with no visible failure. These tests pin the id-set semantics.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from eval.score import build_report, format_report
from eval.splits import SplitError, describe, load_tuning_ids, manifest_path, select

from tests.test_eval_revalidate import _clean_document, _real_entry


def _write_manifest(tmp_path: Path, dataset: str, ids: list[str]) -> Path:
    path = manifest_path(dataset, splits_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dataset": dataset, "ids": ids}), encoding="utf-8")
    return path


def _entries(ids: list[str]) -> list[dict[str, Any]]:
    return [{"id": i} for i in ids]


# ---------------------------------------------------------------------------
# Selection semantics
# ---------------------------------------------------------------------------


def test_all_returns_every_entry(tmp_path: Path) -> None:
    entries = _entries(["a", "b", "c"])
    assert select(entries, "all", dataset="d", splits_dir=tmp_path) == entries


def test_tuning_and_heldout_partition_the_cache(tmp_path: Path) -> None:
    """Every cached document lands in exactly one of the two splits."""
    _write_manifest(tmp_path, "d", ["a", "b"])
    entries = _entries(["a", "b", "c", "d"])

    tuning = select(entries, "tuning", dataset="d", splits_dir=tmp_path)
    heldout = select(entries, "heldout", dataset="d", splits_dir=tmp_path)

    assert [e["id"] for e in tuning] == ["a", "b"]
    assert [e["id"] for e in heldout] == ["c", "d"]
    assert len(tuning) + len(heldout) == len(entries)
    assert not {e["id"] for e in tuning} & {e["id"] for e in heldout}


def test_split_is_independent_of_read_order(tmp_path: Path) -> None:
    """Shuffling the cache does not move a single document between splits."""
    _write_manifest(tmp_path, "d", ["a", "c", "e"])
    ids = ["a", "b", "c", "d", "e", "f"]

    baseline = {
        name: {e["id"] for e in select(_entries(ids), name, dataset="d", splits_dir=tmp_path)}
        for name in ("tuning", "heldout")
    }

    rng = random.Random(0)
    for _ in range(20):
        shuffled = ids[:]
        rng.shuffle(shuffled)
        for name in ("tuning", "heldout"):
            got = {e["id"] for e in select(_entries(shuffled), name, dataset="d",
                                           splits_dir=tmp_path)}
            assert got == baseline[name], (name, shuffled)


def test_selection_preserves_input_order(tmp_path: Path) -> None:
    """Surviving entries keep their relative order, so reports are stable."""
    _write_manifest(tmp_path, "d", ["c", "a"])
    entries = _entries(["a", "b", "c"])
    assert [e["id"] for e in select(entries, "tuning", dataset="d", splits_dir=tmp_path)] == [
        "a",
        "c",
    ]


def test_manifest_membership_ignores_absent_ids(tmp_path: Path) -> None:
    """A manifest id not present in the cache simply selects nothing extra."""
    _write_manifest(tmp_path, "d", ["a", "ghost"])
    entries = _entries(["a", "b"])

    tuning = select(entries, "tuning", dataset="d", splits_dir=tmp_path)
    assert [e["id"] for e in tuning] == ["a"]


# ---------------------------------------------------------------------------
# Failure modes -- a split must never resolve silently to the wrong thing
# ---------------------------------------------------------------------------


def test_missing_manifest_raises_with_guidance(tmp_path: Path) -> None:
    with pytest.raises(SplitError, match="No tuning manifest"):
        select(_entries(["a"]), "tuning", dataset="nope", splits_dir=tmp_path)


def test_unknown_split_name_raises(tmp_path: Path) -> None:
    with pytest.raises(SplitError, match="Unknown split"):
        select(_entries(["a"]), "train", dataset="d", splits_dir=tmp_path)


def test_empty_selection_raises_rather_than_reporting_nothing(tmp_path: Path) -> None:
    """A held-out split on an unexpanded cache is an error, not an empty report."""
    _write_manifest(tmp_path, "d", ["a", "b"])
    with pytest.raises(SplitError, match="selected 0 of"):
        select(_entries(["a", "b"]), "heldout", dataset="d", splits_dir=tmp_path)


def test_malformed_manifest_raises(tmp_path: Path) -> None:
    path = manifest_path("d", splits_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SplitError, match="not valid JSON"):
        load_tuning_ids("d", splits_dir=tmp_path)


def test_manifest_without_ids_raises(tmp_path: Path) -> None:
    path = manifest_path("d", splits_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dataset": "d", "ids": []}), encoding="utf-8")
    with pytest.raises(SplitError, match="declares no 'ids'"):
        load_tuning_ids("d", splits_dir=tmp_path)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_describe_marks_tuning_as_contaminated() -> None:
    assert "not independent" in describe("tuning", 100, 361)
    assert "HELD OUT" in describe("heldout", 261, 361)
    assert "mixes tuning and held-out" in describe("all", 361, 361)


def test_report_header_states_the_split(tmp_path: Path) -> None:
    """A rendered report names its split and the size it was drawn from."""
    from eval.cache import write_entry

    cache = tmp_path / "cache"
    splits = tmp_path / "splits"
    for name in ("keep", "drop"):
        write_entry(
            cache, "d", _real_entry(name, _clean_document(), gold={"total": "11.00"})
        )
    _write_manifest(splits, "d", ["keep"])

    report = build_report(
        "d", cache_base=cache, split="heldout", splits_dir=splits, revalidate=True
    )
    text = format_report(report)

    assert report.n == 1
    assert report.n_cached == 2
    assert "Split:  heldout (1 of 2 cached)" in text
    assert "HELD OUT" in text
