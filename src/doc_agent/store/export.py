"""CSV export from the SQLite document store.

Exports all persisted records from the SQLite database to a flat CSV file,
one row per document. Line items (stored as a JSON column) are serialised as
a count rather than expanded into multiple rows, keeping the CSV flat and
easy to open in a spreadsheet.

See ``docs/02_architecture.md`` section 4 and build-plan task 4.1.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path

from doc_agent.store.db import _connect

logger = logging.getLogger(__name__)

# Columns written to the CSV, in order. Matches the DB columns except
# line_items is replaced with line_item_count for readability.
_CSV_FIELDS = [
    "id",
    "content_hash",
    "source_path",
    "processed_at",
    "doc_type",
    "vendor_name",
    "vendor_address",
    "invoice_number",
    "document_date",
    "due_date",
    "currency",
    "subtotal",
    "tax",
    "total",
    "line_item_count",
    "confidence",
    "backend",
    "modality",
    "decision",
]

_SELECT = """
SELECT
    id, content_hash, source_path, processed_at,
    doc_type, vendor_name, vendor_address, invoice_number,
    document_date, due_date, currency,
    subtotal, tax, total, line_items,
    confidence, backend, modality, decision
FROM documents
ORDER BY id
"""


def export_csv(db_path: Path, output_path: Path) -> int:
    """Export all records from the database to a CSV file.

    Args:
        db_path: Path to the SQLite database file.
        output_path: Path to write the CSV; parent directories are created
            if they do not exist.

    Returns:
        Number of rows written (0 if the database is empty or absent).
    """
    if not db_path.exists():
        logger.warning("export: database not found at %s, writing empty CSV", db_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(",".join(_CSV_FIELDS) + "\n", encoding="utf-8")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with _connect(db_path) as conn:
        rows = conn.execute(_SELECT).fetchall()

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            (
                id_, hash_, src, ts,
                doc_type, vendor, addr, inv_num,
                doc_date, due_date, currency,
                subtotal, tax, total, line_items_json,
                confidence, backend, modality, decision,
            ) = row
            try:
                line_item_count = len(json.loads(line_items_json or "[]"))
            except (json.JSONDecodeError, TypeError):
                line_item_count = 0
            writer.writerow({
                "id": id_,
                "content_hash": hash_,
                "source_path": src,
                "processed_at": ts,
                "doc_type": doc_type,
                "vendor_name": vendor,
                "vendor_address": addr,
                "invoice_number": inv_num,
                "document_date": doc_date,
                "due_date": due_date,
                "currency": currency,
                "subtotal": subtotal,
                "tax": tax,
                "total": total,
                "line_item_count": line_item_count,
                "confidence": confidence,
                "backend": backend,
                "modality": modality,
                "decision": decision,
            })
            written += 1

    logger.info("export: wrote %d rows to %s", written, output_path)
    return written
