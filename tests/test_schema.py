"""Unit tests for the document schema and its normalizers.

Covers the acceptance criterion for build-plan task 1.1: valid dicts parse,
and malformed money/date values are handled (absent -> ``None``, present but
unparseable -> ``ValidationError``).
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from doc_agent.schema.models import Document, LineItem


def test_minimal_document_uses_defaults() -> None:
    """An empty payload yields a fully-defaulted, pipeline-ready document."""
    document = Document()
    assert document.doc_type == "other"
    assert document.vendor_name is None
    assert document.invoice_number is None
    assert document.document_date is None
    assert document.line_items == []
    assert document.total is None
    assert document.field_confidence == {}
    assert document.validation == {}
    assert document.decision is None


def test_full_valid_dict_parses() -> None:
    """A realistic, slightly messy dict parses and normalizes end-to-end."""
    document = Document.model_validate(
        {
            "doc_type": "Invoice",
            "vendor_name": "  Acme Corp  ",
            "vendor_address": "1 Market St",
            "invoice_number": "INV-2024-001",
            "document_date": "2024-01-15",
            "due_date": "2024/02/14",
            "currency": "usd",
            "line_items": [
                {"description": "Widget", "quantity": "2", "unit_price": "10.00", "amount": "20.00"},
            ],
            "subtotal": "$20.00",
            "tax": "1.60",
            "total": "$21.60",
        }
    )
    assert document.doc_type == "invoice"
    assert document.vendor_name == "Acme Corp"
    assert document.invoice_number == "INV-2024-001"
    assert document.document_date == date(2024, 1, 15)
    assert document.due_date == date(2024, 2, 14)
    assert document.currency == "USD"
    assert document.subtotal == pytest.approx(20.00)
    assert document.tax == pytest.approx(1.60)
    assert document.total == pytest.approx(21.60)
    assert len(document.line_items) == 1
    item = document.line_items[0]
    assert item.description == "Widget"
    assert item.quantity == pytest.approx(2.0)
    assert item.unit_price == pytest.approx(10.0)
    assert item.amount == pytest.approx(20.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,234.56", 1234.56),  # US: symbol + thousands + decimal
        ("1.234,56", 1234.56),  # European: dot grouping, comma decimal
        ("1,234", 1234.0),  # bare thousands grouping
        ("1,234,567", 1234567.0),  # multi-group thousands
        ("12,50", 12.5),  # decimal comma
        ("1.234.567", 1234567.0),  # European dot grouping
        ("USD 99.99", 99.99),  # leading currency code
        ("  42  ", 42.0),  # surrounding whitespace
        ("(123.45)", -123.45),  # accounting-style negative
        ("-50.00", -50.0),  # explicit negative
        ("0", 0.0),  # zero is a real value, not absent
        (2, 2.0),  # native int
        (3.5, 3.5),  # native float
    ],
)
def test_money_normalization(raw: object, expected: float) -> None:
    """Monetary strings strip symbols/separators and parse to ``float``."""
    document = Document.model_validate({"total": raw})
    assert document.total == pytest.approx(expected)


@pytest.mark.parametrize("token", ["", "   ", "N/A", "na", "-", "--", "none", "null"])
def test_money_null_tokens_become_none(token: str) -> None:
    """Absent-value sentinels normalize to ``None`` rather than raising."""
    document = Document.model_validate({"total": token})
    assert document.total is None


@pytest.mark.parametrize("bad", ["abc", "12.3.4", "1,2,3", "twelve"])
def test_money_unparseable_raises(bad: str) -> None:
    """A present-but-unparseable number raises (routes the doc to review).

    Malformed grouping like ``12.3.4`` must not be silently mangled into a
    plausible-but-wrong number; it raises so the document routes to review.
    """
    with pytest.raises(ValidationError):
        Document.model_validate({"total": bad})


def test_money_rejects_non_finite() -> None:
    """Infinite/NaN numeric inputs are rejected."""
    with pytest.raises(ValidationError):
        Document.model_validate({"total": float("inf")})


def test_money_rejects_boolean() -> None:
    """Booleans are not accepted as monetary values."""
    with pytest.raises(ValidationError):
        Document.model_validate({"total": True})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-01-15", date(2024, 1, 15)),  # ISO 8601
        ("2024-01-15T13:45:00", date(2024, 1, 15)),  # ISO datetime
        ("2024/01/15", date(2024, 1, 15)),
        ("15/01/2024", date(2024, 1, 15)),  # day-first
        ("04/13/2024", date(2024, 4, 13)),  # day-first impossible -> month-first
        ("15-01-2024", date(2024, 1, 15)),
        ("15.01.2024", date(2024, 1, 15)),
        ("15 Jan 2024", date(2024, 1, 15)),
        ("January 15, 2024", date(2024, 1, 15)),
        ("Jan 15, 2024", date(2024, 1, 15)),
        ("20240115", date(2024, 1, 15)),
        (date(2024, 1, 15), date(2024, 1, 15)),  # passthrough
    ],
)
def test_date_normalization(raw: object, expected: date) -> None:
    """Common date formats normalize to ``datetime.date``."""
    document = Document.model_validate({"document_date": raw})
    assert document.document_date == expected


def test_date_ambiguous_resolves_day_first() -> None:
    """An ambiguous DD/MM vs MM/DD string resolves day-first."""
    document = Document.model_validate({"document_date": "03/04/2024"})
    assert document.document_date == date(2024, 4, 3)


@pytest.mark.parametrize("token", ["", "   ", "N/A", "-", "none"])
def test_date_null_tokens_become_none(token: str) -> None:
    """Absent-value date sentinels normalize to ``None``."""
    document = Document.model_validate({"document_date": token})
    assert document.document_date is None


@pytest.mark.parametrize("bad", ["not-a-date", "2024-13-40", "31/31/2024"])
def test_date_unparseable_raises(bad: str) -> None:
    """A present-but-unparseable date raises."""
    with pytest.raises(ValidationError):
        Document.model_validate({"document_date": bad})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("receipt", "receipt"), ("INVOICE", "invoice"), ("  Other  ", "other")],
)
def test_doc_type_normalized(raw: str, expected: str) -> None:
    """``doc_type`` is lower-cased and trimmed."""
    document = Document.model_validate({"doc_type": raw})
    assert document.doc_type == expected


@pytest.mark.parametrize("raw", ["purchase_order", "statement", "unknown"])
def test_unknown_doc_type_falls_back_to_other(raw: str) -> None:
    """An unrecognized ``doc_type`` maps to "other" instead of raising."""
    document = Document.model_validate({"doc_type": raw})
    assert document.doc_type == "other"


def test_currency_is_upper_cased() -> None:
    """Currency codes are upper-cased; blank values become ``None``."""
    assert Document.model_validate({"currency": "sgd"}).currency == "SGD"
    assert Document.model_validate({"currency": "  "}).currency is None


@pytest.mark.parametrize("token", ["", "   ", "N/A", "-"])
def test_blank_text_fields_become_none(token: str) -> None:
    """Blank/sentinel free-text fields normalize to ``None``."""
    document = Document.model_validate(
        {"vendor_name": token, "vendor_address": token, "invoice_number": token}
    )
    assert document.vendor_name is None
    assert document.vendor_address is None
    assert document.invoice_number is None


def test_numeric_invoice_number_becomes_string() -> None:
    """A numeric invoice number is preserved as text, not coerced away."""
    document = Document.model_validate({"invoice_number": 100245})
    assert document.invoice_number == "100245"


def test_line_item_defaults_and_normalization() -> None:
    """``LineItem`` defaults are all ``None`` and its numbers normalize."""
    assert LineItem() == LineItem(
        description=None, quantity=None, unit_price=None, amount=None
    )
    item = LineItem.model_validate(
        {"description": " Coffee ", "quantity": "3", "unit_price": "$2.50", "amount": "7.50"}
    )
    assert item.description == "Coffee"
    assert item.quantity == pytest.approx(3.0)
    assert item.unit_price == pytest.approx(2.5)
    assert item.amount == pytest.approx(7.5)


def test_json_roundtrip_serializes_dates_as_iso() -> None:
    """JSON serialization renders dates as ISO 8601 and round-trips cleanly."""
    document = Document.model_validate(
        {"document_date": "15/01/2024", "total": "$21.60", "currency": "usd"}
    )
    payload = document.model_dump_json()
    assert '"document_date":"2024-01-15"' in payload
    restored = Document.model_validate_json(payload)
    assert restored == document
