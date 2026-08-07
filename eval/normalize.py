"""Value normalization and per-field comparison for evaluation (pure, no I/O).

Before predicted and gold values are compared they must be normalized so that
cosmetic differences (casing, whitespace, currency symbols, date formats, the
JSON round-trip through the cache) do not count as errors -- exactly the
normalization the evaluation methodology calls for (data spec section 6, step 2).

The normalization reuses the pipeline's own coercion helpers so eval judges
values the same way the pipeline produces them:

- **money** fields go through the schema's number coercion and are compared for
  **exact equality at cent precision** (``round(x, 2)``). This deliberately does
  *not* reuse the validation module's ``money_close``: that check carries a
  rounding allowance whose purpose is to absorb accumulated cent rounding in the
  H2/H3 arithmetic cross-checks (data spec section 3), which has no place in a
  single gold-vs-prediction comparison -- any allowance at all would score a
  materially-wrong total as correct and overstate the ``total``/``tax``
  auto-accept precision this harness exists to measure. Section 6 asks only for
  normalization then comparison; cent-exact equality is that comparison, with the
  cent rounding absorbing sub-cent floating-point representation noise.

  ``money_close`` formerly scaled by 0.5% of the amount, which is where the
  "502.00 vs 500.00" regression test below comes from. That relative term has
  since been removed from validation too (FC-2 in ``eval/FINDINGS.md``); the
  separation of concerns here stands regardless of what the validation
  allowance is.
- **date** fields go through the schema's date coercion (day-first for ambiguous
  D/M/Y, matching SROIE) and are compared for exact ISO-date equality.
- **text** fields are lower-cased and whitespace-collapsed, then compared for
  exact equality.

A value that normalizes to ``None`` (absent, blank, or unparseable) is treated as
"not present": it can never match, so predicting a value where the gold is absent
counts against precision, and missing a gold value counts against recall.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from docfield.schema.models import _coerce_date, _coerce_number

# How each schema field is compared. Fields not listed default to "text".
FIELD_KIND: dict[str, str] = {
    "doc_type": "text",
    "vendor_name": "text",
    "vendor_address": "text",
    "invoice_number": "text",
    "currency": "text",
    "document_date": "date",
    "due_date": "date",
    "subtotal": "money",
    "tax": "money",
    "total": "money",
}


def _normalize_text(value: Any) -> str | None:
    """Lower-case and whitespace-collapse a text value; blanks become ``None``."""
    if value is None:
        return None
    collapsed = re.sub(r"\s+", " ", str(value).strip().lower())
    return collapsed or None


def _normalize_money(value: Any) -> float | None:
    """Coerce a monetary value to ``float``; unparseable/absent becomes ``None``."""
    try:
        return _coerce_number(value)
    except (ValueError, TypeError):
        return None


def _normalize_date(value: Any) -> date | None:
    """Coerce a date value to ``datetime.date``; unparseable/absent becomes ``None``."""
    try:
        return _coerce_date(value)
    except (ValueError, TypeError):
        return None


def normalize(field: str, value: Any) -> Any:
    """Normalize a single value according to its field's comparison kind.

    Args:
        field: The ``Document`` field name.
        value: The raw predicted or gold value.

    Returns:
        A normalized comparable value (``float`` for money, ``date`` for dates,
        lower-cased string for text), or ``None`` when the value is absent,
        blank, or unparseable.
    """
    kind = FIELD_KIND.get(field, "text")
    if kind == "money":
        return _normalize_money(value)
    if kind == "date":
        return _normalize_date(value)
    return _normalize_text(value)


def is_present(field: str, value: Any) -> bool:
    """Whether ``value`` normalizes to a real (non-absent) value for ``field``.

    Args:
        field: The ``Document`` field name.
        value: The raw value to test.

    Returns:
        ``True`` if the value normalizes to something other than ``None``.
    """
    return normalize(field, value) is not None


def values_match(field: str, predicted: Any, gold: Any) -> bool:
    """Whether a predicted value matches the gold value for ``field``.

    Both sides are normalized first. A match requires both to be present;
    monetary fields match at cent precision (rounded to 2 dp, no relative
    tolerance), dates and text match on exact normalized equality.

    Args:
        field: The ``Document`` field name being compared.
        predicted: The pipeline's predicted value (possibly a JSON-cached form).
        gold: The dataset's ground-truth value.

    Returns:
        ``True`` if the values are considered equal after normalization.
    """
    left = normalize(field, predicted)
    right = normalize(field, gold)
    if left is None or right is None:
        return False
    if FIELD_KIND.get(field, "text") == "money":
        # Cent-exact: no relative tolerance, so a materially-wrong total is never
        # scored correct. round() absorbs sub-cent float representation noise.
        return round(left, 2) == round(right, 2)
    return left == right
