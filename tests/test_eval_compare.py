"""Unit tests for eval/compare.py: the three-way backend comparison.

Fully offline -- entries are hand-built (mirroring tests/test_eval.py's
``_entry`` helper) and written to temp cache directories with
``eval.cache.write_entry``, then read back through the real
``build_comparison`` path. No model calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eval.cache import write_entry
from eval.compare import (
    build_comparison,
    count_arithmetic_saves,
    format_comparison,
    summarize_backend,
)


def _validation(hard_codes: list[str] | None = None) -> dict[str, Any]:
    codes = hard_codes or []
    return {
        "hard_failed": bool(codes),
        "results": [{"code": c, "severity": "hard", "status": "fail", "message": "x"} for c in codes],
        "hard_failures": codes,
        "soft_failures": [],
    }


def _entry(
    example_id: str,
    *,
    backend: str,
    decision: str,
    predicted: dict[str, Any],
    gold: dict[str, Any],
    hard_codes: list[str] | None = None,
    latency_s: float | None = 1.0,
    cost_usd: float | None = 0.001,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "id": example_id,
        "dataset": "synthetic",
        "gold": gold,
        "labeled_fields": ["total", "tax", "invoice_number"],
        "predicted": predicted,
        "confidence": 0.9,
        "decision": decision,
        "modality": "image",
        "backend": backend,
        "validation": _validation(hard_codes),
        "error": error,
        "latency_s": latency_s,
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": cost_usd,
    }


# ---------------------------------------------------------------------------
# summarize_backend
# ---------------------------------------------------------------------------


def test_summarize_backend_accept_rate_and_precision() -> None:
    entries = [
        _entry(
            "e1", backend="anthropic", decision="accept",
            predicted={"total": 100.0}, gold={"total": "100.00"},
        ),
        _entry(
            "e2", backend="anthropic", decision="accept",
            predicted={"total": 50.0}, gold={"total": "999.00"},  # wrong -> precision hit
        ),
        _entry(
            "e3", backend="anthropic", decision="review",
            predicted={"total": 10.0}, gold={"total": "10.00"},
        ),
    ]

    summary = summarize_backend("anthropic", entries)

    assert summary.n == 3
    assert summary.n_accepted == 2
    assert summary.accept_rate == pytest.approx(2 / 3)
    # Only e1/e2 are in the accepted subset; e1 correct, e2 wrong -> 1/2.
    assert summary.crit_precision == pytest.approx(0.5)


def test_summarize_backend_excludes_errored_documents() -> None:
    entries = [
        _entry("e1", backend="gemini", decision="accept", predicted={}, gold={}),
        _entry("e2", backend="gemini", decision="review", predicted={}, gold={}, error="timeout"),
    ]

    summary = summarize_backend("gemini", entries)

    assert summary.n == 1  # the errored document never reached the model
    assert summary.n_errors == 1


def test_summarize_backend_cost_and_latency_percentiles() -> None:
    entries = [
        _entry("e1", backend="anthropic", decision="review", predicted={}, gold={},
               latency_s=1.0, cost_usd=0.001),
        _entry("e2", backend="anthropic", decision="review", predicted={}, gold={},
               latency_s=2.0, cost_usd=0.002),
        _entry("e3", backend="anthropic", decision="review", predicted={}, gold={},
               latency_s=3.0, cost_usd=0.003),
    ]

    summary = summarize_backend("anthropic", entries)

    assert summary.cost_per_doc_usd == pytest.approx(0.002)
    assert summary.total_cost_usd == pytest.approx(0.006)
    assert summary.p50_latency_s == pytest.approx(2.0)
    assert summary.p95_latency_s == pytest.approx(2.9)  # linear interpolation toward the max


def test_summarize_backend_missing_cost_data_is_none() -> None:
    entries = [_entry("e1", backend="stub", decision="review", predicted={}, gold={},
                       latency_s=None, cost_usd=None)]
    summary = summarize_backend("stub", entries)
    assert summary.cost_per_doc_usd is None
    assert summary.p50_latency_s is None


# ---------------------------------------------------------------------------
# count_arithmetic_saves
# ---------------------------------------------------------------------------


def test_counts_only_docs_agentic_accepted_and_baseline_arithmetic_hard_failed() -> None:
    baseline = [
        _entry("e1", backend="anthropic", decision="review", predicted={}, gold={}, hard_codes=["H2"]),
        _entry("e2", backend="anthropic", decision="review", predicted={}, gold={}, hard_codes=[]),
        _entry("e3", backend="anthropic", decision="review", predicted={}, gold={}, hard_codes=["H3"]),
        _entry("e4", backend="anthropic", decision="review", predicted={}, gold={}, hard_codes=["H1"]),
    ]
    agentic = [
        _entry("e1", backend="anthropic-agentic", decision="accept", predicted={}, gold={}),
        _entry("e2", backend="anthropic-agentic", decision="accept", predicted={}, gold={}),
        _entry("e3", backend="anthropic-agentic", decision="review", predicted={}, gold={}),
        _entry("e4", backend="anthropic-agentic", decision="accept", predicted={}, gold={}),
    ]

    saved = count_arithmetic_saves(agentic, baseline)

    # e1: baseline H2 hard-failed + agentic accepted -> a save.
    # e2: baseline clean -- not a save (nothing to correct).
    # e3: baseline H3 hard-failed but agentic still reviewed it -- not a save.
    # e4: baseline H1 (not arithmetic) -- not a save even though agentic accepted.
    assert saved == ["e1"]


def test_count_arithmetic_saves_empty_when_nothing_matches() -> None:
    baseline = [_entry("e1", backend="anthropic", decision="review", predicted={}, gold={}, hard_codes=[])]
    agentic = [_entry("e1", backend="anthropic-agentic", decision="review", predicted={}, gold={})]
    assert count_arithmetic_saves(agentic, baseline) == []


# ---------------------------------------------------------------------------
# build_comparison (end to end over real cache directories)
# ---------------------------------------------------------------------------


def test_build_comparison_end_to_end(tmp_path: Path) -> None:
    gemini_dir = tmp_path / "gemini"
    anthropic_dir = tmp_path / "anthropic"
    agentic_dir = tmp_path / "agentic"

    write_entry(gemini_dir, "synthetic", _entry("e1", backend="gemini", decision="review",
                                                 predicted={}, gold={}, hard_codes=["H2"]))
    write_entry(anthropic_dir, "synthetic", _entry("e1", backend="anthropic", decision="review",
                                                    predicted={}, gold={}, hard_codes=["H2"]))
    write_entry(agentic_dir, "synthetic", _entry("e1", backend="anthropic-agentic", decision="accept",
                                                  predicted={"total": 5.0}, gold={"total": "5.00"}))

    report = build_comparison(
        {"gemini": gemini_dir, "anthropic": anthropic_dir, "anthropic-agentic": agentic_dir},
        "synthetic",
    )

    assert report.n_slice == 1
    assert {s.backend for s in report.summaries} == {"gemini", "anthropic", "anthropic-agentic"}
    assert report.baseline_backend == "anthropic"
    assert report.arithmetic_saves == ["e1"]

    # format_comparison must not raise and should surface the headline number.
    rendered = format_comparison(report)
    assert "1 document(s)" in rendered
    assert "anthropic-agentic" in rendered


def test_build_comparison_raises_for_missing_cache(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_comparison({"gemini": tmp_path / "nope"}, "synthetic")


def test_build_comparison_skips_arithmetic_saves_without_both_backends(tmp_path: Path) -> None:
    gemini_dir = tmp_path / "gemini"
    write_entry(gemini_dir, "synthetic", _entry("e1", backend="gemini", decision="accept",
                                                 predicted={}, gold={}))

    report = build_comparison({"gemini": gemini_dir}, "synthetic")

    assert report.baseline_backend is None
    assert report.arithmetic_saves == []
    rendered = format_comparison(report)
    assert "not computed" in rendered
