"""Stateless Gradio web demo for the DocField Extract pipeline.

Architecture rule 1: this module is a thin presentation wrapper over
``core.process_document``. No pipeline logic lives here; the web layer only
calls the core and renders what it returns.

Privacy (NFR-2 / docs/04_project_setup.md): the free Gemini tier may train on
inputs, so a visible notice is shown at the top of every page. Only synthetic
or publicly-available documents should be uploaded to the hosted demo.

Stateless: nothing is written to disk or a database. The watcher owns
persistence; the demo renders results and discards them.

Launch: ``uv run python -m doc_agent.web.app`` (or via this module's
``if __name__ == "__main__"`` block).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

import gradio as gr

from doc_agent.backends.base import create_backend
from doc_agent.config import load_config
from doc_agent.core import ExtractionResult, process_document

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Privacy notice (NFR-2)
# ---------------------------------------------------------------------------

_PRIVACY_NOTICE = """
> **SYNTHETIC / PUBLIC DOCUMENTS ONLY**
> This demo uses the Gemini free tier, which **may train on your inputs**.
> Do **not** upload real invoices, receipts, or any document containing
> personal or financial data. Use only synthetic or publicly-available files.
""".strip()

# ---------------------------------------------------------------------------
# Result rendering helpers
# ---------------------------------------------------------------------------

_STATUS_ICON = {"pass": "OK", "fail": "FAIL", "skip": "SKIP"}
_SEVERITY_LABEL = {"hard": "Hard rule", "soft": "Soft rule"}


def _fmt_money(value: float | None, currency: str | None = None) -> str:
    if value is None:
        return "-"
    prefix = f"{currency} " if currency else ""
    return f"{prefix}{value:,.2f}"


def _fmt_date(value: Any) -> str:
    if value is None:
        return "-"
    return str(value)


def _fmt_conf(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.0%}"


def _render_fields(result: ExtractionResult) -> str:
    """Build the extracted-fields markdown block."""
    doc = result.document
    conf = doc.field_confidence
    currency = doc.currency

    rows: list[tuple[str, str, str]] = [
        ("Type",            str(doc.doc_type),                                conf.get("doc_type")),
        ("Vendor",          doc.vendor_name or "-",                           conf.get("vendor_name")),
        ("Address",         doc.vendor_address or "-",                        conf.get("vendor_address")),
        ("Invoice No.",     doc.invoice_number or "-",                        conf.get("invoice_number")),
        ("Date",            _fmt_date(doc.document_date),                     conf.get("document_date")),
        ("Due date",        _fmt_date(doc.due_date),                          conf.get("due_date")),
        ("Currency",        doc.currency or "-",                              conf.get("currency")),
        ("Subtotal",        _fmt_money(doc.subtotal, currency),               conf.get("subtotal")),
        ("Tax",             _fmt_money(doc.tax, currency),                    conf.get("tax")),
        ("Total",           _fmt_money(doc.total, currency),                  conf.get("total")),
        ("Line items",      str(len(doc.line_items)),                         None),
    ]

    lines = ["| Field | Value | Confidence |", "|---|---|---|"]
    for label, value, raw_conf in rows:
        c = _fmt_conf(raw_conf)
        lines.append(f"| {label} | {value} | {c} |")

    if doc.line_items:
        lines.append("")
        lines.append("**Line items**")
        lines.append("| # | Description | Qty | Unit price | Amount |")
        lines.append("|---|---|---|---|---|")
        for i, item in enumerate(doc.line_items, 1):
            desc = item.description or "-"
            qty = f"{item.quantity}" if item.quantity is not None else "-"
            up = _fmt_money(item.unit_price, currency)
            amt = _fmt_money(item.amount, currency)
            lines.append(f"| {i} | {desc} | {qty} | {up} | {amt} |")

    return "\n".join(lines)


def _render_validation(result: ExtractionResult) -> str:
    """Build the validation-report markdown block."""
    report = result.report
    lines: list[str] = []

    lines.append("| Rule | Severity | Status | Message |")
    lines.append("|---|---|---|---|")
    for r in report.results:
        icon = _STATUS_ICON.get(r.status, r.status)
        severity = _SEVERITY_LABEL.get(r.severity, r.severity)
        lines.append(f"| {r.code} | {severity} | {icon} | {r.message} |")

    if report.hard_failed:
        codes = ", ".join(r.code for r in report.hard_failures)
        lines.append(f"\n**Hard failures: {codes}** -- document routed to review.")
    if report.soft_failures:
        codes = ", ".join(r.code for r in report.soft_failures)
        lines.append(f"\n_Soft failures: {codes}_")

    return "\n".join(lines)


def _render_decision(result: ExtractionResult) -> str:
    """Build the decision-summary markdown block."""
    icon = "ACCEPT" if result.accepted else "REVIEW"
    lines = [
        f"## Decision: {icon}",
        "",
        f"- **Confidence:** {result.confidence:.0%}",
        f"- **Backend:** {result.backend_name}",
        f"- **Modality:** {result.modality or 'unknown'}",
    ]
    if result.model_signal is not None:
        lines.append(f"- **Model signal:** {result.model_signal:.0%}")
    if result.error:
        lines.append(f"\n**Pipeline error:** {result.error}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def _process(file_obj: Any) -> tuple[str, str, str]:
    """Gradio callback: run the pipeline over the uploaded file.

    Args:
        file_obj: Gradio UploadButton value (a file path string or None).

    Returns:
        A three-tuple of (fields_markdown, validation_markdown, decision_markdown).
    """
    if file_obj is None:
        return "No file uploaded.", "", ""

    src = Path(file_obj)

    # Copy to a named temp file preserving the original extension so modality
    # detection works on the suffix, then clean up after processing.
    suffix = src.suffix or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    shutil.copy2(src, tmp_path)

    try:
        settings = load_config()
        backend = create_backend(settings)
        result: ExtractionResult = process_document(tmp_path, settings=settings, backend=backend)
    except Exception as exc:
        logger.exception("web: pipeline failed for %s", src.name)
        msg = f"Pipeline error: {exc}"
        return msg, "", msg
    finally:
        tmp_path.unlink(missing_ok=True)

    return (
        _render_fields(result),
        _render_validation(result),
        _render_decision(result),
    )


# ---------------------------------------------------------------------------
# Gradio interface
# ---------------------------------------------------------------------------

def build_demo() -> gr.Blocks:
    """Construct and return the Gradio Blocks interface.

    Returns:
        The assembled ``gr.Blocks`` demo (not yet launched).
    """
    with gr.Blocks(title="DocField Extract") as demo:
        gr.Markdown("# DocField Extract")
        gr.Markdown(_PRIVACY_NOTICE)
        gr.Markdown(
            "Upload a **native PDF**, a **scanned PDF**, or a **photo** of a receipt "
            "or invoice. The pipeline extracts structured fields, runs validation "
            "rules, and decides whether the document is safe to auto-accept."
        )

        with gr.Row():
            upload = gr.File(
                label="Upload document (PDF / JPG / PNG / WEBP)",
                file_types=[".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif"],
                type="filepath",
            )

        run_btn = gr.Button("Extract", variant="primary")

        with gr.Tab("Extracted fields"):
            fields_out = gr.Markdown()

        with gr.Tab("Validation report"):
            validation_out = gr.Markdown()

        with gr.Tab("Decision"):
            decision_out = gr.Markdown()

        run_btn.click(
            fn=_process,
            inputs=[upload],
            outputs=[fields_out, validation_out, decision_out],
        )

    return demo


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    build_demo().launch()
