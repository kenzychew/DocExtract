"""Unit tests for SQLite persistence and CSV export (build-plan task 4.1).

All tests are fully offline: they use pytest's ``tmp_path`` fixture for
isolated, throwaway databases and CSV files -- no real documents, no network.
The ``ExtractionResult`` objects are built from the offline ``StubBackend``
so the Document shape is correct without touching the pipeline.

Acceptance criteria (T7):
- An accepted record persists and can be retrieved (record_count increases).
- The same record inserted twice is a no-op (idempotency via content_hash).
- export_csv writes a header + one data row per record.
- A freshly-exported row contains the original field values.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from doc_agent.backends.base import DocumentPayload
from doc_agent.backends.stub import StubBackend
from doc_agent.config import load_config
from doc_agent.core import ExtractionResult, process_document
from doc_agent.store.db import append_record, record_count
from doc_agent.store.export import export_csv

# Fixed timestamp injected so tests are deterministic.
_TS = "2024-06-01T00:00:00+00:00"
_HASH = "abc123def456" * 4  # 48-char fake SHA-256


def _offline_settings():
    return load_config(extraction_backend="ollama", image_strategy="ocr_then_text")


def _acquire_fixed(path: Path, modality):
    return DocumentPayload(modality=modality, source_path=path, text="stub")


def _accepted_result(source: str = "invoice.pdf") -> ExtractionResult:
    """Build a clean, auto-accepted ExtractionResult via the stub pipeline."""
    return process_document(
        source,
        settings=_offline_settings(),
        backend=StubBackend(),
        acquire=_acquire_fixed,
        today=date(2024, 6, 1),
    )


# ---------------------------------------------------------------------------
# append_record
# ---------------------------------------------------------------------------


def test_append_inserts_record(tmp_path: Path) -> None:
    """An accepted result is persisted; record_count increases by 1 (AC)."""
    db = tmp_path / "test.db"
    result = _accepted_result()

    inserted = append_record(result, db, _HASH, processed_at=_TS)

    assert inserted is True
    assert record_count(db) == 1


def test_append_idempotent_on_duplicate_hash(tmp_path: Path) -> None:
    """Inserting the same content_hash twice is a silent no-op (AC)."""
    db = tmp_path / "test.db"
    result = _accepted_result()

    first = append_record(result, db, _HASH, processed_at=_TS)
    second = append_record(result, db, _HASH, processed_at=_TS)

    assert first is True
    assert second is False
    assert record_count(db) == 1


def test_append_different_hashes_both_persist(tmp_path: Path) -> None:
    """Two records with distinct hashes both persist."""
    db = tmp_path / "test.db"
    result = _accepted_result()
    hash_a = "a" * 64
    hash_b = "b" * 64

    append_record(result, db, hash_a, processed_at=_TS)
    append_record(result, db, hash_b, processed_at=_TS)

    assert record_count(db) == 2


def test_record_count_zero_when_db_absent(tmp_path: Path) -> None:
    """record_count returns 0 when the database file does not yet exist."""
    assert record_count(tmp_path / "nonexistent.db") == 0


def test_append_creates_parent_dirs(tmp_path: Path) -> None:
    """append_record creates parent directories if they do not exist."""
    db = tmp_path / "deep" / "nested" / "store.db"
    result = _accepted_result()

    append_record(result, db, _HASH, processed_at=_TS)

    assert db.exists()
    assert record_count(db) == 1


def test_persisted_fields_match_document(tmp_path: Path) -> None:
    """The persisted row reflects the document's extracted field values."""
    import sqlite3

    db = tmp_path / "test.db"
    result = _accepted_result()
    append_record(result, db, _HASH, processed_at=_TS)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT vendor_name, total, invoice_number, decision, confidence, processed_at "
        "FROM documents WHERE content_hash = ?",
        (_HASH,),
    ).fetchone()
    conn.close()

    assert row is not None
    vendor, total, inv_num, decision, confidence, ts = row
    assert vendor == result.document.vendor_name
    assert total == pytest.approx(result.document.total)
    assert inv_num == result.document.invoice_number
    assert decision == "accept"
    assert 0.0 <= confidence <= 1.0
    assert ts == _TS


def test_line_items_stored_as_json(tmp_path: Path) -> None:
    """Line items are serialised as a JSON array in the database."""
    import sqlite3

    db = tmp_path / "test.db"
    result = _accepted_result()
    append_record(result, db, _HASH, processed_at=_TS)

    conn = sqlite3.connect(db)
    (raw,) = conn.execute("SELECT line_items FROM documents").fetchone()
    conn.close()

    items = json.loads(raw)
    assert isinstance(items, list)
    assert len(items) == len(result.document.line_items)


# ---------------------------------------------------------------------------
# export_csv
# ---------------------------------------------------------------------------


def test_export_csv_writes_header_and_row(tmp_path: Path) -> None:
    """export_csv produces a CSV with a header and one data row (AC)."""
    db = tmp_path / "test.db"
    csv_path = tmp_path / "out.csv"
    result = _accepted_result()
    append_record(result, db, _HASH, processed_at=_TS)

    count = export_csv(db, csv_path)

    assert count == 1
    assert csv_path.exists()
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 1


def test_export_csv_row_values_match_record(tmp_path: Path) -> None:
    """Exported CSV row values match the persisted document fields (AC)."""
    db = tmp_path / "test.db"
    csv_path = tmp_path / "out.csv"
    result = _accepted_result()
    append_record(result, db, _HASH, processed_at=_TS)
    export_csv(db, csv_path)

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    row = rows[0]

    assert row["vendor_name"] == (result.document.vendor_name or "")
    assert float(row["total"]) == pytest.approx(result.document.total)
    assert row["decision"] == "accept"
    assert row["content_hash"] == _HASH
    assert int(row["line_item_count"]) == len(result.document.line_items)


def test_export_csv_empty_db_writes_header_only(tmp_path: Path) -> None:
    """export_csv on an absent DB writes only the header row."""
    csv_path = tmp_path / "empty.csv"

    count = export_csv(tmp_path / "nonexistent.db", csv_path)

    assert count == 0
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert rows == []


def test_export_csv_multiple_records(tmp_path: Path) -> None:
    """All records appear in the exported CSV, one row each."""
    db = tmp_path / "test.db"
    csv_path = tmp_path / "out.csv"
    result = _accepted_result()

    for i in range(3):
        append_record(result, db, f"hash{i}" + "0" * 61, processed_at=_TS)

    count = export_csv(db, csv_path)

    assert count == 3
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 3


def test_export_csv_creates_parent_dirs(tmp_path: Path) -> None:
    """export_csv creates parent directories of the output path."""
    db = tmp_path / "test.db"
    csv_path = tmp_path / "exports" / "sub" / "out.csv"
    result = _accepted_result()
    append_record(result, db, _HASH, processed_at=_TS)

    export_csv(db, csv_path)

    assert csv_path.exists()
