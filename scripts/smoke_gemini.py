"""Quick manual smoke script for T2: run process_document on a real image.

Usage:
    uv run python scripts/smoke_gemini.py path/to/receipt.jpg
"""

import sys
from pathlib import Path

from doc_agent.core import process_document


def main() -> None:
    """Run the pipeline on a single image and print the result."""
    if len(sys.argv) != 2:
        print("Usage: uv run python scripts/smoke_gemini.py <image_path>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    print(f"Processing: {path}")
    result = process_document(path)

    print(f"\ndecision     : {result.decision}")
    print(f"confidence   : {result.confidence:.3f}")
    print(f"backend      : {result.backend_name}")
    print(f"modality     : {result.modality}")
    if result.error:
        print(f"error        : {result.error}")

    doc = result.document
    print("\n--- extracted fields ---")
    print(f"doc_type     : {doc.doc_type}")
    print(f"vendor_name  : {doc.vendor_name}")
    print(f"invoice_num  : {doc.invoice_number}")
    print(f"date         : {doc.document_date}")
    print(f"currency     : {doc.currency}")
    print(f"subtotal     : {doc.subtotal}")
    print(f"tax          : {doc.tax}")
    print(f"total        : {doc.total}")
    print(f"line_items   : {len(doc.line_items)} item(s)")
    for item in doc.line_items:
        print(f"  {item.description}  qty={item.quantity}  price={item.unit_price}  amt={item.amount}")

    print("\n--- validation ---")
    v = result.report
    print(f"hard_failed  : {v.hard_failed}")
    if v.hard_failures:
        print(f"hard failures: {[r.code for r in v.hard_failures]}")
    if v.soft_failures:
        print(f"soft failures: {[r.code for r in v.soft_failures]}")


if __name__ == "__main__":
    main()
