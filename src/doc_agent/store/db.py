"""SQLite persistence for auto-accepted extraction records.

Appends one row per accepted document to a local SQLite database. A SHA-256
content hash (``utils.hash.file_sha256``) makes inserts idempotent: inserting
the same document twice is a silent no-op, so the watcher can restart safely
without double-counting records.

Only accepted documents are persisted here; documents routed to review are
logged by the watcher and moved to the review directory -- they never enter
the DB. The caller (watcher or web demo) decides what to persist; the core
pipeline returns a decision but performs no I/O (architecture rule 1).

See ``docs/02_architecture.md`` section 4 and build-plan task 4.1.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from doc_agent.core import ExtractionResult

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash  TEXT    NOT NULL,
    source_path   TEXT,
    processed_at  TEXT    NOT NULL,
    doc_type      TEXT,
    vendor_name   TEXT,
    vendor_address TEXT,
    invoice_number TEXT,
    document_date TEXT,
    due_date      TEXT,
    currency      TEXT,
    subtotal      REAL,
    tax           REAL,
    total         REAL,
    line_items    TEXT,
    confidence    REAL    NOT NULL,
    backend       TEXT    NOT NULL,
    modality      TEXT,
    decision      TEXT    NOT NULL,
    UNIQUE (content_hash)
)
"""

_INSERT = """
INSERT OR IGNORE INTO documents (
    content_hash, source_path, processed_at,
    doc_type, vendor_name, vendor_address, invoice_number,
    document_date, due_date, currency,
    subtotal, tax, total, line_items,
    confidence, backend, modality, decision
) VALUES (
    :content_hash, :source_path, :processed_at,
    :doc_type, :vendor_name, :vendor_address, :invoice_number,
    :document_date, :due_date, :currency,
    :subtotal, :tax, :total, :line_items,
    :confidence, :backend, :modality, :decision
)
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection and ensure the schema exists.

    Args:
        db_path: Path to the SQLite file; parent directories must exist.

    Returns:
        An open ``sqlite3.Connection`` with the documents table ready.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_TABLE)
    conn.commit()
    return conn


def _result_to_row(
    result: ExtractionResult,
    content_hash: str,
    processed_at: str,
) -> dict[str, Any]:
    """Flatten an ExtractionResult into a dict suitable for the INSERT.

    Args:
        result: The accepted extraction result to persist.
        content_hash: SHA-256 hex digest of the source file.
        processed_at: ISO 8601 UTC timestamp string.

    Returns:
        A dict with keys matching the named placeholders in ``_INSERT``.
    """
    doc = result.document
    return {
        "content_hash": content_hash,
        "source_path": str(result.source_path) if result.source_path else None,
        "processed_at": processed_at,
        "doc_type": doc.doc_type,
        "vendor_name": doc.vendor_name,
        "vendor_address": doc.vendor_address,
        "invoice_number": doc.invoice_number,
        "document_date": doc.document_date.isoformat() if doc.document_date else None,
        "due_date": doc.due_date.isoformat() if doc.due_date else None,
        "currency": doc.currency,
        "subtotal": doc.subtotal,
        "tax": doc.tax,
        "total": doc.total,
        "line_items": json.dumps(
            [li.model_dump() for li in doc.line_items], default=str
        ),
        "confidence": result.confidence,
        "backend": result.backend_name,
        "modality": result.modality,
        "decision": result.decision,
    }


def append_record(
    result: ExtractionResult,
    db_path: Path,
    content_hash: str,
    *,
    processed_at: str | None = None,
) -> bool:
    """Persist one accepted record; skip silently if already present.

    Args:
        result: The accepted extraction result to persist.
        db_path: Path to the SQLite database file.
        content_hash: SHA-256 hex digest of the source document; used as the
            idempotency key so restarting the watcher never double-counts.
        processed_at: ISO 8601 UTC timestamp; defaults to ``now`` when
            ``None``. Injected by tests for determinism.

    Returns:
        ``True`` if the row was inserted, ``False`` if it was already present
        (duplicate content hash).
    """
    ts = processed_at or datetime.now(timezone.utc).isoformat()
    row = _result_to_row(result, content_hash, ts)

    with _connect(db_path) as conn:
        cursor = conn.execute(_INSERT, row)
        inserted = cursor.rowcount > 0

    if inserted:
        logger.info(
            "store: persisted record hash=%s source=%s decision=%s",
            content_hash[:12],
            result.source_path,
            result.document.decision,
        )
    else:
        logger.debug("store: duplicate skipped hash=%s", content_hash[:12])

    return inserted


def record_count(db_path: Path) -> int:
    """Return the number of records currently in the database.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Row count, or 0 if the database does not yet exist.
    """
    if not db_path.exists():
        return 0
    with _connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM documents").fetchone()
    return row[0] if row else 0
