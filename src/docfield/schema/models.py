"""Pydantic data contract for extracted documents.

The unified ``Document`` schema spans receipts and invoices; any field absent
from a given document is ``None``. These models are the single source of truth
for the data contract (see CLAUDE.md): they enforce structured model output,
validate types, normalize messy values, and serialize to storage.

Normalization is deliberately tolerant on the way in -- monetary strings may
arrive with currency symbols or thousands separators, and dates in a variety of
human formats -- because real OCR/model output is noisy. The posture is
precision-safe (see CLAUDE.md "Precision posture"):

- A genuinely absent value (empty string, ``"N/A"``, ``"-"``) normalizes to
  ``None``; a missing field is caught downstream by review.
- A value that is *present but unparseable* (a number that is not a number, a
  date that is not a date) raises a ``ValidationError`` so the pipeline routes
  the document to review rather than recording a confidently-wrong number.

Dates are normalized to ``datetime.date`` and therefore serialize to ISO 8601
(``YYYY-MM-DD``) via Pydantic's JSON mode. See
``docs/03_data_and_extraction_spec.md`` section 2.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DocType = Literal["receipt", "invoice", "other"]
Decision = Literal["accept", "review"]

# Strings that represent an absent value in extracted/OCR'd output. Compared
# case-insensitively after stripping whitespace.
_NULL_TOKENS: frozenset[str] = frozenset(
    {"", "-", "--", "n/a", "na", "none", "null", "nil", "."}
)

# Date formats tried in order after the ISO 8601 fast path. Day-first variants
# precede month-first so an ambiguous DD/MM vs MM/DD string resolves day-first
# (the dataset majority -- SROIE/CORD/MC-OCR are non-US), while month-first
# still wins when day-first is impossible (e.g. 04/13/2024).
_DATE_FORMATS: tuple[str, ...] = (
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%Y%m%d",
)


def _blank_to_none(value: Any) -> Any:
    """Normalize blank/sentinel strings to ``None``; stringify scalar numbers.

    Args:
        value: The raw value for a free-text field.

    Returns:
        ``None`` if the value is a blank or null-sentinel string, the stripped
        string otherwise, or the stringified form of an ``int``/``float`` (so a
        numeric ``invoice_number`` survives as text).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in _NULL_TOKENS:
            return None
        return stripped
    return value


def _to_plain_decimal(cleaned: str) -> str:
    """Resolve thousands/decimal separators into a plain ``float``-parseable string.

    Handles US (``1,234.56``), European (``1.234,56``), bare grouping
    (``1,234`` / ``1.234.567``), and decimal-comma (``12,50``) conventions.
    Grouping is validated strictly -- a value whose separators do not form
    well-formed thousands groups (e.g. ``12.3.4``) raises rather than being
    silently mangled into a plausible-but-wrong number.

    Args:
        cleaned: A string containing only digits, commas, and dots.

    Returns:
        A string using ``.`` as the sole decimal separator and no grouping
        separators.

    Raises:
        ValueError: If the separator layout is not a valid number.
    """
    has_dot = "." in cleaned
    has_comma = "," in cleaned

    # Decide which separator (if any) is the decimal point; the other groups.
    if has_dot and has_comma:
        if cleaned.rfind(",") > cleaned.rfind("."):
            decimal_sep, group_sep = ",", "."
        else:
            decimal_sep, group_sep = ".", ","
    elif has_comma:
        parts = cleaned.split(",")
        # A single comma trailing 1-2 digits is a decimal comma (12,50);
        # anything else is thousands grouping (1,234 / 1,234,567).
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            decimal_sep, group_sep = ",", ""
        else:
            decimal_sep, group_sep = "", ","
    elif has_dot and cleaned.count(".") == 1:
        decimal_sep, group_sep = ".", ""
    elif has_dot:
        # Multiple dots can only be thousands grouping: 1.234.567.
        decimal_sep, group_sep = "", "."
    else:
        decimal_sep, group_sep = "", ""

    if decimal_sep:
        int_part, _, frac_part = cleaned.rpartition(decimal_sep)
    else:
        int_part, frac_part = cleaned, ""

    if group_sep:
        groups = int_part.split(group_sep)
        # First group is 1-3 digits; every subsequent group is exactly 3.
        if not groups[0] or len(groups[0]) > 3 or any(len(g) != 3 for g in groups[1:]):
            raise ValueError("invalid thousands grouping")
        int_digits = "".join(groups)
    else:
        int_digits = int_part

    int_digits = int_digits or "0"  # e.g. ".56" / ",56" -> "0.56"
    if not int_digits.isdigit() or (frac_part and not frac_part.isdigit()):
        raise ValueError("invalid number layout")

    return f"{int_digits}.{frac_part}" if frac_part else int_digits


def _coerce_number(value: Any) -> float | None:
    """Coerce a possibly-messy monetary/quantity value to ``float`` or ``None``.

    Args:
        value: ``None``, a number, or a string that may carry a currency
            symbol, thousands separators, or accounting-style parentheses.

    Returns:
        The parsed ``float``, or ``None`` for an absent value.

    Raises:
        ValueError: If the value is a boolean, an unsupported type, or a
            non-empty string with no parseable number. ``ValueError`` (not
            ``TypeError``) so Pydantic surfaces it as a ``ValidationError``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("monetary/quantity field cannot be a boolean")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("monetary/quantity value must be finite")
        return number
    if not isinstance(value, str):
        raise ValueError(
            f"monetary/quantity field must be a number or string, "
            f"got {type(value).__name__}"
        )

    raw = value.strip()
    if raw.lower() in _NULL_TOKENS:
        return None

    negative = raw.startswith("-")
    # Accounting-style negatives: "(123.45)" -> -123.45.
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1]

    cleaned = re.sub(r"[^0-9.,]", "", raw)
    if not any(char.isdigit() for char in cleaned):
        raise ValueError(f"could not parse a number from {value!r}")

    try:
        number = float(_to_plain_decimal(cleaned))
    except ValueError as exc:
        raise ValueError(f"could not parse a number from {value!r}") from exc
    return -number if negative else number


def _coerce_date(value: Any) -> date | None:
    """Coerce a value to an ISO ``date`` or ``None``.

    Tries an ISO 8601 fast path first, then a fixed list of common human date
    formats (commas treated as separators, whitespace collapsed).

    Args:
        value: ``None``, a ``date``/``datetime``, or a date string.

    Returns:
        A ``datetime.date``, or ``None`` for an absent value.

    Raises:
        ValueError: If the value is an unsupported type or a non-empty string
            matching no known date format. ``ValueError`` (not ``TypeError``)
            so Pydantic surfaces it as a ``ValidationError``.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"date field must be a string or date, got {type(value).__name__}")

    raw = value.strip()
    if raw.lower() in _NULL_TOKENS:
        return None

    # ISO 8601 fast path (date and full datetime forms).
    try:
        return date.fromisoformat(raw)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        pass

    candidate = re.sub(r"\s+", " ", raw.replace(",", " ")).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"could not parse a date from {value!r}")


class LineItem(BaseModel):
    """A single line on a receipt or invoice.

    Attributes:
        description: Free-text item description, or ``None``.
        quantity: Quantity ordered (normalized number), or ``None``.
        unit_price: Price per unit (normalized number), or ``None``.
        amount: Line total (normalized number), or ``None``.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None

    @field_validator("description", mode="before")
    @classmethod
    def _normalize_description(cls, value: Any) -> Any:
        """Map blank/sentinel descriptions to ``None``."""
        return _blank_to_none(value)

    @field_validator("quantity", "unit_price", "amount", mode="before")
    @classmethod
    def _normalize_numbers(cls, value: Any) -> float | None:
        """Coerce messy numeric strings to ``float`` (or ``None``)."""
        return _coerce_number(value)


class Document(BaseModel):
    """Unified extracted record for a receipt or invoice.

    A single schema spans both document kinds; fields absent from a given
    document are ``None``. The trailing three fields are populated by the
    pipeline (not the model) after extraction.

    Attributes:
        doc_type: Document classification; unknown values normalize to "other".
        vendor_name: Issuing vendor/merchant name, or ``None``.
        vendor_address: Vendor address, or ``None``.
        invoice_number: Invoice/receipt identifier (critical field), or ``None``.
        document_date: Issue date as an ISO ``date``, or ``None``.
        due_date: Payment due date as an ISO ``date``, or ``None``.
        currency: ISO 4217 code where detectable (upper-cased), or ``None``.
        line_items: Parsed line items (possibly empty).
        subtotal: Pre-tax subtotal (normalized number), or ``None``.
        tax: Tax amount (critical field, normalized number), or ``None``.
        total: Document total (critical field, normalized number), or ``None``.
        field_confidence: Per-field confidence in [0, 1]; pipeline-populated.
        validation: Structured validation report; pipeline-populated.
        decision: Routing decision ("accept" | "review"); pipeline-populated.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    doc_type: DocType = "other"
    vendor_name: str | None = None
    vendor_address: str | None = None
    invoice_number: str | None = None
    document_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None

    # Populated by the pipeline, not the model.
    field_confidence: dict[str, float] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    decision: Decision | None = None

    @field_validator("doc_type", mode="before")
    @classmethod
    def _normalize_doc_type(cls, value: Any) -> Any:
        """Lower-case ``doc_type`` and map anything unrecognized to "other"."""
        if value is None:
            return "other"
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"receipt", "invoice", "other"}:
                return normalized
            return "other"
        return value

    @field_validator("vendor_name", "vendor_address", "invoice_number", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> Any:
        """Map blank/sentinel free-text fields to ``None``."""
        return _blank_to_none(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: Any) -> str | None:
        """Upper-case the currency code; map blanks to ``None``."""
        cleaned = _blank_to_none(value)
        if isinstance(cleaned, str):
            return cleaned.upper()
        return cleaned

    @field_validator("document_date", "due_date", mode="before")
    @classmethod
    def _normalize_dates(cls, value: Any) -> date | None:
        """Coerce date strings to ISO ``date`` (or ``None``)."""
        return _coerce_date(value)

    @field_validator("subtotal", "tax", "total", mode="before")
    @classmethod
    def _normalize_amounts(cls, value: Any) -> float | None:
        """Coerce monetary strings to ``float`` (or ``None``)."""
        return _coerce_number(value)
