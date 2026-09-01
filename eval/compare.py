"""Three-way backend comparison: gemini vs anthropic vs anthropic-agentic.

Purely offline -- it reads the caches each backend's predict run wrote (one
``cache_base`` per backend; see ``eval.predict.run_predict``'s
``backend_override``/``cache_base`` params) and answers the question this task
exists to answer: does giving the model a ``validate_arithmetic`` tool during
extraction let it self-correct and auto-accept documents that the existing
hard-rule check (H2/H3 in ``validation/rules.py``) would otherwise force to
review, and at what extra cost/latency compared to the plain single-call
backends.

Two things this module reports, over the same document ids across all three
caches:

- Per-backend summary: auto-accept rate, critical-field precision on the
  auto-accepted subset, cost per document, and p50/p95 latency.
- The direct answer: how many of the agentic backend's auto-accepts are
  documents where the *plain* Anthropic backend's own extraction of the same
  document hard-failed H2 or H3 (line items/total don't reconcile) -- i.e.
  documents that would have been forced to review without the arithmetic tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval.cache import read_entries
from eval.normalize import is_present, values_match
from eval.score import _critical_labeled, _labeled_fields
from eval.splits import DEFAULT_SPLITS_DIR, select

# The hard rules that specifically check arithmetic (subtotal+tax==total;
# line items sum to subtotal/total) -- see validation/rules.py H2/H3. A
# document that only fails H1 (mistyped critical field) or H4 (missing total)
# was not caught *by arithmetic*, so it is not counted as an arithmetic save.
_ARITHMETIC_HARD_CODES: frozenset[str] = frozenset({"H2", "H3"})


def _percentile(values: list[float], fraction: float) -> float | None:
    """Linear-interpolated percentile of a list of floats.

    Args:
        values: The sample; need not be sorted.
        fraction: The percentile as a fraction in ``[0, 1]`` (e.g. 0.5 for p50).

    Returns:
        The interpolated percentile, or ``None`` if ``values`` is empty.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _hard_failure_codes(entry: dict[str, Any]) -> frozenset[str]:
    """The hard-rule codes that failed for a cached entry's validation report."""
    return frozenset(entry.get("validation", {}).get("hard_failures", []))


def _arithmetic_hard_failed(entry: dict[str, Any]) -> bool:
    """Whether a cached entry hard-failed specifically on an arithmetic rule (H2/H3)."""
    return bool(_hard_failure_codes(entry) & _ARITHMETIC_HARD_CODES)


@dataclass(frozen=True)
class BackendSummary:
    """Auto-accept rate, critical precision, cost, and latency for one backend.

    Attributes:
        backend: The backend's registered name.
        n: Number of documents scored (after error exclusion for latency/cost).
        n_errors: Documents that never reached the model (excluded from cost/
            latency/accept-rate denominators below, consistent with
            ``eval.score``'s treatment of infrastructure failures).
        n_accepted: Documents the pipeline auto-accepted (decision == "accept").
        accept_rate: ``n_accepted / n``.
        crit_precision: Precision over the auto-accepted subset, restricted to
            the critical fields (total/tax/invoice_number) *this dataset
            actually labels* (see ``eval.score._critical_labeled`` -- SROIE,
            for example, labels only ``total`` among the three, so counting
            predicted-but-unlabelable ``tax``/``invoice_number`` values would
            understate precision on a metric no gold value could ever confirm).
            ``None`` if nothing critical was predicted among the accepted
            documents.
        cost_per_doc_usd: Mean per-document cost in USD, or ``None`` if no
            entry carried usable token counts.
        total_cost_usd: Summed cost in USD across every entry with usable
            token counts.
        p50_latency_s: Median wall-clock latency per document, or ``None``.
        p95_latency_s: 95th-percentile wall-clock latency per document, or
            ``None``.
    """

    backend: str
    n: int
    n_errors: int
    n_accepted: int
    accept_rate: float
    crit_precision: float | None
    cost_per_doc_usd: float | None
    total_cost_usd: float
    p50_latency_s: float | None
    p95_latency_s: float | None


def summarize_backend(backend_name: str, entries: list[dict[str, Any]]) -> BackendSummary:
    """Compute a :class:`BackendSummary` from one backend's cached entries.

    Args:
        backend_name: Label for the summary (need not match ``entry["backend"]``
            verbatim, though it should).
        entries: Cached prediction entries for this backend's run (already
            filtered to the comparison slice).

    Returns:
        The computed summary.
    """
    n_errors = sum(1 for e in entries if e.get("error"))
    reached = [e for e in entries if not e.get("error")]
    n = len(reached)

    accepted = [e for e in reached if e.get("decision") == "accept"]
    n_accepted = len(accepted)
    accept_rate = n_accepted / n if n else 0.0

    critical_fields = _critical_labeled(_labeled_fields(reached), reached)
    crit_pred = crit_match = 0
    for entry in accepted:
        for field in critical_fields:
            predicted = entry.get("predicted", {}).get(field)
            gold = entry.get("gold", {}).get(field)
            if is_present(field, predicted):
                crit_pred += 1
                if values_match(field, predicted, gold):
                    crit_match += 1
    crit_precision = crit_match / crit_pred if crit_pred else None

    costs = [e["cost_usd"] for e in reached if e.get("cost_usd") is not None]
    latencies = [e["latency_s"] for e in reached if e.get("latency_s") is not None]

    return BackendSummary(
        backend=backend_name,
        n=n,
        n_errors=n_errors,
        n_accepted=n_accepted,
        accept_rate=accept_rate,
        crit_precision=crit_precision,
        cost_per_doc_usd=(sum(costs) / len(costs)) if costs else None,
        total_cost_usd=sum(costs),
        p50_latency_s=_percentile(latencies, 0.50),
        p95_latency_s=_percentile(latencies, 0.95),
    )


def count_arithmetic_saves(
    agentic_entries: list[dict[str, Any]],
    baseline_entries: list[dict[str, Any]],
) -> list[str]:
    """Ids the agentic backend auto-accepted that the baseline hard-failed on arithmetic.

    This is the direct answer to the question this backend exists to test: for
    the *same document*, did the plain backend's extraction fail H2/H3 (forcing
    review), while the agentic backend -- given the ``validate_arithmetic``
    tool -- caught it, revised, and auto-accepted instead.

    Args:
        agentic_entries: Cached entries from the anthropic-agentic run.
        baseline_entries: Cached entries from a plain backend's run (typically
            the non-agentic Anthropic backend, so the only variable is the
            arithmetic tool) over the same document ids.

    Returns:
        The ids meeting both conditions, so the caller can inspect them
        individually as well as count them.
    """
    baseline_by_id = {str(e["id"]): e for e in baseline_entries}
    saved: list[str] = []
    for entry in agentic_entries:
        if entry.get("decision") != "accept":
            continue
        baseline = baseline_by_id.get(str(entry["id"]))
        if baseline is not None and _arithmetic_hard_failed(baseline):
            saved.append(str(entry["id"]))
    return saved


@dataclass(frozen=True)
class ComparisonReport:
    """Everything the three-way comparison computed.

    Attributes:
        dataset: Dataset name.
        split: Which split was compared ("all", "tuning", or "heldout").
        n_slice: Documents in the compared slice (before per-backend error
            exclusion).
        summaries: One :class:`BackendSummary` per backend, in the order given
            to :func:`build_comparison`.
        arithmetic_saves: Ids the agentic backend auto-accepted that the
            baseline backend hard-failed on arithmetic (H2/H3); see
            :func:`count_arithmetic_saves`. Empty (not ``None``) when the
            comparison did not include both an agentic and a baseline run.
        baseline_backend: Which backend's cache was used as the "would this
            have been forced to review without the tool" baseline for
            ``arithmetic_saves``, or ``None`` if not computed.
    """

    dataset: str
    split: str
    n_slice: int
    summaries: list[BackendSummary]
    arithmetic_saves: list[str]
    baseline_backend: str | None


def build_comparison(
    cache_bases: dict[str, Path],
    dataset: str,
    *,
    split: str = "all",
    splits_dir: Path = DEFAULT_SPLITS_DIR,
    agentic_backend: str = "anthropic-agentic",
    baseline_backend: str = "anthropic",
) -> ComparisonReport:
    """Load each backend's cache and build the full comparison report.

    Args:
        cache_bases: Backend name -> the ``cache_base`` its predict run used
            (e.g. ``{"gemini": Path("eval/cache_gemini"), ...}``).
        dataset: Dataset name whose caches to compare.
        split: Which cached documents to compare ("all", "tuning", "heldout").
        splits_dir: Directory holding split manifests.
        agentic_backend: Key into ``cache_bases`` for the arithmetic-saves
            numerator. Skipped (empty ``arithmetic_saves``) if not present.
        baseline_backend: Key into ``cache_bases`` for the arithmetic-saves
            denominator/join target. Skipped if not present.

    Returns:
        A :class:`ComparisonReport`.

    Raises:
        FileNotFoundError: If a backend's cache has no entries for ``dataset``.
    """
    entries_by_backend: dict[str, list[dict[str, Any]]] = {}
    n_slice = 0
    for name, cache_base in cache_bases.items():
        all_entries = read_entries(cache_base, dataset)
        if not all_entries:
            raise FileNotFoundError(
                f"No cached predictions for backend {name!r}, dataset {dataset!r} "
                f"under {cache_base}. Run predict for this backend first."
            )
        sliced = select(all_entries, split, dataset=dataset, splits_dir=splits_dir)
        entries_by_backend[name] = sliced
        n_slice = max(n_slice, len(sliced))

    summaries = [
        summarize_backend(name, entries) for name, entries in entries_by_backend.items()
    ]

    arithmetic_saves: list[str] = []
    resolved_baseline: str | None = None
    if agentic_backend in entries_by_backend and baseline_backend in entries_by_backend:
        arithmetic_saves = count_arithmetic_saves(
            entries_by_backend[agentic_backend], entries_by_backend[baseline_backend]
        )
        resolved_baseline = baseline_backend

    return ComparisonReport(
        dataset=dataset,
        split=split,
        n_slice=n_slice,
        summaries=summaries,
        arithmetic_saves=arithmetic_saves,
        baseline_backend=resolved_baseline,
    )


# --- Formatting ------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "  n/a" if value is None else f"{value * 100:5.1f}%"


def _usd(value: float | None) -> str:
    return "    n/a" if value is None else f"${value:.5f}"


def _secs(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:5.2f}s"


def format_comparison(report: ComparisonReport) -> str:
    """Render a :class:`ComparisonReport` as a plain-text table.

    Args:
        report: The computed comparison.

    Returns:
        A multi-line string ready to print.
    """
    lines = [
        "=" * 78,
        f"Backend comparison: {report.dataset}  (split={report.split}, "
        f"slice size={report.n_slice})",
        "=" * 78,
        f"{'backend':<20} {'accept%':>8} {'crit P':>8} {'$/doc':>10} "
        f"{'p50 lat':>9} {'p95 lat':>9} {'errors':>7}",
    ]
    for s in report.summaries:
        lines.append(
            f"{s.backend:<20} {_pct(s.accept_rate):>8} {_pct(s.crit_precision):>8} "
            f"{_usd(s.cost_per_doc_usd):>10} {_secs(s.p50_latency_s):>9} "
            f"{_secs(s.p95_latency_s):>9} {s.n_errors:>7}"
        )

    lines.append("")
    if report.baseline_backend is None:
        lines.append(
            "Arithmetic self-correction: not computed (need both an agentic "
            "and a baseline backend cache)."
        )
    else:
        lines.append(
            f"Arithmetic self-correction (agentic vs {report.baseline_backend!r} baseline):"
        )
        lines.append(
            f"  {len(report.arithmetic_saves)} document(s) the agentic backend "
            f"auto-accepted that {report.baseline_backend!r} hard-failed on "
            "arithmetic (H2/H3) -- i.e. would have been forced to review "
            "without the validate_arithmetic tool."
        )
        if report.arithmetic_saves:
            lines.append(f"  ids: {', '.join(report.arithmetic_saves)}")

    return "\n".join(lines)
