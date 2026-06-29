"""Hard and soft validation rules over a parsed ``Document`` (pure, no I/O).

Validation is the precision lever for the auto-accept path (see CLAUDE.md
"Precision posture"). Two classes of rule run over a parsed ``Document`` and
produce a structured ``ValidationReport``:

- **Hard rules (H1-H4):** a failure forces ``review`` regardless of model
  confidence. These are the arithmetic cross-checks and critical-field
  presence/type guards -- the mechanism that catches a confidently-wrong number
  before it is written.
- **Soft rules (S1-S4):** a failure reduces confidence but does not by itself
  force review. They surface "looks off" signals (missing vendor, implausible
  date, unknown currency, per-line arithmetic drift).

A rule whose inputs are absent is **skipped** (status ``"skip"``), not failed:
an absent subtotal must not spuriously fail the reconciliation check and push a
valid document to review (that would cost recall for no precision gain). The one
deliberate exception is ``H4`` -- an absent ``total`` is a hard failure, because
a document with no total is never safe to auto-accept.

Every function here is pure: no file, network, clock, or database access. The
``S1`` future-date check takes an injected ``today`` reference so the rule stays
deterministic and unit-testable; the core passes ``date.today()`` in production.

See ``docs/03_data_and_extraction_spec.md`` section 3 for the rule definitions
and the monetary-epsilon policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from doc_agent.schema.models import Document, LineItem

# --- Monetary comparison policy -------------------------------------------------

# Epsilon for monetary reconciliation accommodates rounding. Per the data spec,
# the tolerance is the larger of an absolute floor and a small relative term, so
# small receipts are compared to the cent while large invoices tolerate the
# accumulated rounding of many line items.
MONETARY_ABS_EPSILON: float = 0.02
MONETARY_REL_EPSILON: float = 0.005

# How many days a ``document_date`` may sit ahead of the reference "today"
# before ``S1`` considers it implausibly future-dated (absorbs timezone skew).
FUTURE_DATE_GRACE_DAYS: int = 1

# Critical fields, precision-prioritised (see CLAUDE.md). ``H1`` type-guards
# these; missing/zero among them drives routing elsewhere.
CRITICAL_FIELDS: tuple[str, ...] = ("total", "tax", "invoice_number")

# Known ISO 4217 codes for the soft currency check. Intentionally a common
# subset weighted toward the evaluation datasets (SROIE/CORD/MC-OCR cover
# Singapore, Indonesia, Vietnam); an unrecognized-but-valid rare code only
# incurs a small soft penalty, which is the precision-safe direction.
KNOWN_CURRENCIES: frozenset[str] = frozenset(
    {
        "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "HKD",
        "SGD", "MYR", "IDR", "THB", "VND", "PHP", "INR", "KRW", "TWD", "MOP",
        "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON", "RUB", "TRY", "UAH",
        "ZAR", "BRL", "MXN", "ARS", "CLP", "COP", "AED", "SAR", "QAR", "ILS",
        "EGP", "NGN", "KES", "PKR", "BDT", "LKR",
    }
)

RuleSeverity = Literal["hard", "soft"]
RuleStatus = Literal["pass", "fail", "skip"]


def money_close(
    left: float,
    right: float,
    *,
    abs_epsilon: float = MONETARY_ABS_EPSILON,
    rel_epsilon: float = MONETARY_REL_EPSILON,
) -> bool:
    """Compare two monetary amounts within the rounding tolerance.

    The tolerance is ``max(abs_epsilon, rel_epsilon * max(|left|, |right|))`` --
    the larger of an absolute floor and a small relative term (data spec
    section 3).

    Args:
        left: First amount.
        right: Second amount.
        abs_epsilon: Absolute tolerance floor. Defaults to
            ``MONETARY_ABS_EPSILON``.
        rel_epsilon: Relative tolerance fraction. Defaults to
            ``MONETARY_REL_EPSILON``.

    Returns:
        ``True`` if the amounts are equal within tolerance.
    """
    tolerance = max(abs_epsilon, rel_epsilon * max(abs(left), abs(right)))
    return abs(left - right) <= tolerance


@dataclass(frozen=True)
class RuleResult:
    """Outcome of a single validation rule.

    Attributes:
        code: Rule identifier ("H1"-"H4", "S1"-"S4").
        severity: "hard" (a failure forces review) or "soft" (reduces score).
        status: "pass", "fail", or "skip" (inputs absent / not applicable).
        message: Short human-readable explanation of the outcome.
    """

    code: str
    severity: RuleSeverity
    status: RuleStatus
    message: str

    def to_dict(self) -> dict[str, str]:
        """Serialize to a plain JSON-friendly dict.

        Returns:
            A dict with ``code``, ``severity``, ``status``, and ``message``.
        """
        return {
            "code": self.code,
            "severity": self.severity,
            "status": self.status,
            "message": self.message,
        }


@dataclass(frozen=True)
class ValidationReport:
    """Structured result of running every rule over one ``Document``.

    The report is pure data: routing consumes ``hard_failed`` to short-circuit
    to review and ``soft_failures`` to penalize the confidence score. It is
    JSON-serializable via ``to_dict`` for storage in ``Document.validation``.

    Attributes:
        results: One ``RuleResult`` per rule, in rule order.
    """

    results: tuple[RuleResult, ...]

    @property
    def hard_failures(self) -> tuple[RuleResult, ...]:
        """The hard rules that failed (empty if none)."""
        return tuple(
            r for r in self.results if r.severity == "hard" and r.status == "fail"
        )

    @property
    def soft_failures(self) -> tuple[RuleResult, ...]:
        """The soft rules that failed (empty if none)."""
        return tuple(
            r for r in self.results if r.severity == "soft" and r.status == "fail"
        )

    @property
    def hard_failed(self) -> bool:
        """Whether any hard rule failed (forces ``review`` downstream)."""
        return bool(self.hard_failures)

    def by_code(self, code: str) -> RuleResult | None:
        """Return the result for a rule code, or ``None`` if absent.

        Args:
            code: A rule identifier such as "H2".

        Returns:
            The matching ``RuleResult``, or ``None``.
        """
        for result in self.results:
            if result.code == code:
                return result
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the report for storage in ``Document.validation``.

        Returns:
            A dict with ``hard_failed`` flag, the full ``results`` list, and the
            codes of the hard and soft failures for quick inspection.
        """
        return {
            "hard_failed": self.hard_failed,
            "results": [r.to_dict() for r in self.results],
            "hard_failures": [r.code for r in self.hard_failures],
            "soft_failures": [r.code for r in self.soft_failures],
        }


# --- Hard rules -----------------------------------------------------------------


def _check_h1_critical_types(document: Document) -> RuleResult:
    """H1: present critical fields hold the correct type.

    The schema already enforces types on construction, so this is a defensive
    contract guard: ``total``/``tax`` must be numeric and ``invoice_number`` a
    string when present.
    """
    bad: list[str] = []
    for name in ("total", "tax"):
        value = getattr(document, name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            bad.append(name)
    if document.invoice_number is not None and not isinstance(document.invoice_number, str):
        bad.append("invoice_number")

    if bad:
        return RuleResult("H1", "hard", "fail", f"critical field(s) mistyped: {', '.join(bad)}")
    return RuleResult("H1", "hard", "pass", "critical fields are correctly typed")


def _check_h2_totals_reconcile(document: Document) -> RuleResult:
    """H2: subtotal + tax approximately equals total, when all three exist."""
    subtotal, tax, total = document.subtotal, document.tax, document.total
    if subtotal is None or tax is None or total is None:
        return RuleResult("H2", "hard", "skip", "subtotal, tax, or total absent")

    if money_close(subtotal + tax, total):
        return RuleResult("H2", "hard", "pass", f"{subtotal} + {tax} == {total}")
    return RuleResult(
        "H2", "hard", "fail", f"{subtotal} + {tax} != {total} (got {subtotal + tax})"
    )


def _sum_line_amounts(line_items: list[LineItem]) -> float | None:
    """Sum line-item amounts, or ``None`` if any amount is missing.

    Reconciliation is only meaningful when every term is present; a single
    missing amount makes the sum incomplete, so the check is skipped rather than
    run against an understated total.
    """
    total = 0.0
    for item in line_items:
        if item.amount is None:
            return None
        total += item.amount
    return total


def _check_h3_line_items_reconcile(document: Document) -> RuleResult:
    """H3: sum(line_items.amount) approximately equals subtotal (or total)."""
    if not document.line_items:
        return RuleResult("H3", "hard", "skip", "no line items")

    line_sum = _sum_line_amounts(document.line_items)
    if line_sum is None:
        return RuleResult("H3", "hard", "skip", "one or more line items lack an amount")

    reference_name = "subtotal" if document.subtotal is not None else "total"
    reference = document.subtotal if document.subtotal is not None else document.total
    if reference is None:
        return RuleResult("H3", "hard", "skip", "no subtotal or total to reconcile against")

    if money_close(line_sum, reference):
        return RuleResult("H3", "hard", "pass", f"line sum {line_sum} == {reference_name} {reference}")
    return RuleResult(
        "H3", "hard", "fail", f"line sum {line_sum} != {reference_name} {reference}"
    )


def _check_h4_total_present(document: Document) -> RuleResult:
    """H4: total is present and non-negative."""
    total = document.total
    if total is None:
        return RuleResult("H4", "hard", "fail", "total is missing")
    if total < 0:
        return RuleResult("H4", "hard", "fail", f"total is negative ({total})")
    return RuleResult("H4", "hard", "pass", f"total present and non-negative ({total})")


# --- Soft rules -----------------------------------------------------------------


def _check_s1_date_plausible(document: Document, today: date | None) -> RuleResult:
    """S1: document_date is present and not implausibly far in the future.

    The future check only runs when a ``today`` reference is supplied (keeping
    the function pure); presence is always checked.
    """
    if document.document_date is None:
        return RuleResult("S1", "soft", "fail", "document_date is missing")
    if today is not None:
        latest = today + timedelta(days=FUTURE_DATE_GRACE_DAYS)
        if document.document_date > latest:
            return RuleResult(
                "S1", "soft", "fail", f"document_date {document.document_date} is in the future"
            )
    return RuleResult("S1", "soft", "pass", f"document_date {document.document_date} is plausible")


def _check_s2_currency_known(document: Document) -> RuleResult:
    """S2: currency resolves to a known ISO 4217 code."""
    currency = document.currency
    if currency is None:
        return RuleResult("S2", "soft", "fail", "currency is missing")
    if currency not in KNOWN_CURRENCIES:
        return RuleResult("S2", "soft", "fail", f"currency {currency!r} is not a known code")
    return RuleResult("S2", "soft", "pass", f"currency {currency} is a known code")


def _check_s3_vendor_present(document: Document) -> RuleResult:
    """S3: vendor_name is non-empty (blank already normalized to ``None``)."""
    if document.vendor_name is None:
        return RuleResult("S3", "soft", "fail", "vendor_name is missing")
    return RuleResult("S3", "soft", "pass", "vendor_name is present")


def _check_s4_line_arithmetic(document: Document) -> RuleResult:
    """S4: quantity * unit_price approximately equals amount, per line."""
    checkable = 0
    failures: list[int] = []
    for index, item in enumerate(document.line_items):
        if item.quantity is None or item.unit_price is None or item.amount is None:
            continue
        checkable += 1
        if not money_close(item.quantity * item.unit_price, item.amount):
            failures.append(index)

    if checkable == 0:
        return RuleResult("S4", "soft", "skip", "no line item has quantity, unit_price, and amount")
    if failures:
        rows = ", ".join(str(i) for i in failures)
        return RuleResult("S4", "soft", "fail", f"per-line arithmetic off at row(s): {rows}")
    return RuleResult("S4", "soft", "pass", "per-line arithmetic reconciles")


def validate(document: Document, *, today: date | None = None) -> ValidationReport:
    """Run every hard and soft rule over a parsed document.

    Pure: no I/O. The ``today`` reference is injected (not read from the clock)
    so the ``S1`` future-date check stays deterministic; the core passes
    ``date.today()`` in production and tests pass a fixed date.

    Args:
        document: The parsed, schema-validated document to check.
        today: Reference date for the ``S1`` future-date plausibility check. If
            ``None``, only date *presence* is checked, not future-dating.

    Returns:
        A ``ValidationReport`` with one ``RuleResult`` per rule, in rule order.
    """
    results = (
        _check_h1_critical_types(document),
        _check_h2_totals_reconcile(document),
        _check_h3_line_items_reconcile(document),
        _check_h4_total_present(document),
        _check_s1_date_plausible(document, today),
        _check_s2_currency_known(document),
        _check_s3_vendor_present(document),
        _check_s4_line_arithmetic(document),
    )
    return ValidationReport(results=results)
