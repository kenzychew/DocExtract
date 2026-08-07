"""Stateless Gradio web demo for the DocField Extract pipeline.

Architecture rule 1: this module is a thin presentation wrapper over
``core.process_document``. No pipeline logic lives here; the web layer only
calls the core and renders what it returns. In particular the renderers *read*
``result.decision`` -- they never re-derive it.

Rule codes appear only in the details tab. ``_RULE_COPY`` carries the
plain-language wording and ``tests/test_web.py`` fails if a rule ever lacks an
entry, so a new rule cannot silently leak "H5" into the user-facing surface.

Privacy (NFR-2 / docs/04_project_setup.md): the free Gemini tier may train on
inputs, so a visible notice is shown at the top of every page. Only synthetic
or publicly-available documents should be uploaded to the hosted demo.

Stateless: nothing is written to disk or a database. The watcher owns
persistence; the demo renders results and discards them.

Launch: ``uv run python -m docfield.web.app`` (or via this module's
``if __name__ == "__main__"`` block).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

import gradio as gr

from docfield.backends.base import create_backend
from docfield.config import load_config
from docfield.core import ExtractionResult, process_document
from docfield.validation.rules import RuleResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static copy
# ---------------------------------------------------------------------------

# Privacy notice (NFR-2). Deliberately unchanged.
_PRIVACY_NOTICE = """
> **SYNTHETIC / PUBLIC DOCUMENTS ONLY**
> This demo uses the Gemini free tier, which **may train on your inputs**.
> Do **not** upload real invoices, receipts, or any document containing
> personal or financial data. Use only synthetic or publicly-available files.
""".strip()

_INTRO = (
    "Extracts the key fields from a receipt or invoice, then checks the "
    "arithmetic to decide whether the result is safe to use without a person "
    "reading it."
)

_HOW_IT_WORKS = """
A language model reads the document and fills in the fields. It is not trusted
on its own. A fixed set of arithmetic and presence checks then runs over what it
produced: do the line items add up, does the subtotal plus tax equal the total,
is there a total at all. Those checks are ordinary code, not the model, so they
give the same answer every time.

If every must-pass check clears and confidence is high enough, the document is
accepted automatically. Otherwise it goes to review. Review is the safe default,
not a failure: a field that needs a second look costs a few seconds of someone's
attention, while a wrong number that gets accepted is copied onward silently.
""".strip()

# ---------------------------------------------------------------------------
# Plain-language rule copy
# ---------------------------------------------------------------------------


class _RuleCopy(NamedTuple):
    """Wording for one rule: ``title`` for skips, ``ok``/``bad`` for outcomes.

    Three strings rather than one, because a single assertion plus a status
    column reads as a contradiction on failure ("Line items add up -- Failed").
    ``bad`` doubles as the verdict deciding reason, so it must read standalone.
    """

    title: str
    ok: str
    bad: str


# Keyed by rule code. Lives here rather than in validation/rules.py: this is UI
# copy with a different audience and revision cadence than the rules themselves,
# and rules.py is a pure, I/O-free leaf whose RuleResult.message is already the
# technical surface. Colocation would not prevent drift anyway -- the drift guard
# in tests/test_web.py is what does that.
_RULE_COPY: dict[str, _RuleCopy] = {
    "H1": _RuleCopy(
        "Amounts and references are the right kind of value",
        "The total, tax and invoice number are each the right kind of value.",
        "The total, tax or invoice number is not the kind of value it should be.",
    ),
    "H2": _RuleCopy(
        "Subtotal plus tax equals the total",
        "The subtotal plus the tax equals the stated total.",
        "The subtotal plus the tax does not equal the stated total.",
    ),
    # Deliberately not "the subtotal": the rule reconciles against subtotal when
    # present and falls back to total otherwise, so naming either one would be
    # false half the time. Picking the noun here by reading document.subtotal
    # would duplicate the rule's own branch in the web layer.
    "H3": _RuleCopy(
        "Line items add up to the stated amount",
        "The line items add up to the amount the document states.",
        "The line items do not add up to the amount the document states.",
    ),
    "H4": _RuleCopy(
        "The document has a total",
        "The document has a total, and it is not negative.",
        "No usable total was found on the document.",
    ),
    "S1": _RuleCopy(
        "The document date is plausible",
        "The document has a date, and it is not in the future.",
        "The document has no date, or its date is in the future.",
    ),
    "S2": _RuleCopy(
        "The currency code is a known currency",
        "The currency code on the document is a known currency.",
        "The currency code on the document is not a known currency.",
    ),
    "S3": _RuleCopy(
        "The document names a vendor",
        "The document names a vendor.",
        "No vendor name was found on the document.",
    ),
    "S4": _RuleCopy(
        "Each line's quantity times price matches its amount",
        "On every line, quantity times unit price matches the line amount.",
        "On at least one line, quantity times unit price does not match the line amount.",
    ),
}

# Fallback for a rule with no copy yet. Generic plain language, never the raw
# code: a bare "H5" appearing in the user-facing surface is exactly what this
# module exists to prevent, and it would only ever fire unnoticed.
_UNKNOWN_RULE = _RuleCopy(
    "Additional validation check",
    "An additional check passed.",
    "An additional check did not pass.",
)

_STATUS_LABEL = {"pass": "Passed", "fail": "Failed", "skip": "Not applicable"}

_SEVERITY_GROUP: dict[str, tuple[str, str]] = {
    "hard": (
        "Must pass to auto-accept",
        "If any of these fails, the document goes to review no matter how "
        "confident the model was.",
    ),
    "soft": (
        "Quality signals",
        "These do not force review on their own. Each one that fails lowers the "
        "confidence score.",
    ),
}

# ---------------------------------------------------------------------------
# Empty states -- shared by the widget defaults, the no-file path, and the tests
# ---------------------------------------------------------------------------

_EMPTY_VERDICT = (
    "### No document yet\n\n"
    "Upload a receipt or invoice and press **Extract**. The verdict, and the "
    "reasons behind it, appear here."
)
_EMPTY_FIELDS = "_Nothing extracted yet._"
_EMPTY_CHECKS = "_No checks have run yet._"
_EMPTY_DETAILS = "_No run yet._"

_NO_FILE_VERDICT = "### No file selected\n\nChoose a file above, then press **Extract**."

# ---------------------------------------------------------------------------
# Field rendering
# ---------------------------------------------------------------------------


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

    rows: list[tuple[str, str, float | None]] = [
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
        lines.append(f"| {label} | {value} | {_fmt_conf(raw_conf)} |")

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


# ---------------------------------------------------------------------------
# Verdict and plain-language checks
# ---------------------------------------------------------------------------


def _rule_copy(code: str) -> _RuleCopy:
    """Plain-language copy for a rule code; never returns the raw code."""
    return _RULE_COPY.get(code, _UNKNOWN_RULE)


def _render_check_line(rule: RuleResult) -> str:
    """Render one rule outcome as a plain-language bullet (never a rule code).

    Evidence is attached on failure (the actionable part) and on skip (what was
    not applicable), but omitted on pass -- restating eight satisfied checks in
    worse English turns the plain-language surface back into a log dump.
    """
    copy = _rule_copy(rule.code)
    status = _STATUS_LABEL.get(rule.status, rule.status)

    if rule.status == "pass":
        return f"- **{status}** -- {copy.ok}"
    if rule.status == "fail":
        return f"- **{status}** -- {copy.bad} _({rule.message})_"
    return f"- **{status}** -- {copy.title}. _({rule.message})_"


def _render_checks(result: ExtractionResult) -> str:
    """Build the plain-language checks block, grouped hard then soft."""
    lines: list[str] = []

    if result.error:
        # Without this the tab reads as findings about the document ("No vendor
        # name was found") when in fact nothing was ever read from it.
        lines.append(
            "_No fields were extracted, so these checks ran against an empty document._"
        )
        lines.append("")

    for severity in ("hard", "soft"):
        heading, blurb = _SEVERITY_GROUP[severity]
        # Preserve report order (H1..H4, S1..S4); do not sort.
        rules = [r for r in result.report.results if r.severity == severity]
        if not rules:
            continue
        lines.append(f"### {heading}")
        lines.append("")
        lines.append(f"_{blurb}_")
        lines.append("")
        lines.extend(_render_check_line(r) for r in rules)
        lines.append("")

    return "\n".join(lines).rstrip()


def _review_reason(result: ExtractionResult, *, threshold: float) -> str | None:
    """Explain, in one plain-language passage, why a document went to review.

    Precedence: a processing failure outranks a hard-rule failure, which
    outranks falling short of the confidence threshold. The decision itself is
    read from ``result``, never recomputed here (architecture rule 1). Returns
    ``None`` when the document was accepted.
    """
    if result.decision != "review":
        return None

    if result.error:
        return (
            "Something went wrong while reading this document, so it was sent to "
            f"review instead of being accepted. _({result.error})_"
        )

    hard = result.report.hard_failures
    if len(hard) == 1:
        rule = hard[0]
        return f"{_rule_copy(rule.code).bad} _({rule.message})_"
    if hard:
        # Rule order is arbitrary, so "the specific failure" is legitimately
        # plural here -- picking one to blame would be a coin toss.
        bullets = "\n".join(
            f"- {_rule_copy(r.code).bad} _({r.message})_" for r in hard
        )
        return "More than one must-pass check failed:\n\n" + bullets

    # Exactly the negation of route()'s ``confidence >= threshold``, so a
    # document sitting on the boundary cannot fall through to the catch-all.
    if result.confidence < threshold:
        return (
            "Every must-pass check cleared, but overall confidence came out at "
            f"{result.confidence:.0%}, below the {threshold:.0%} needed to accept "
            "automatically."
        )

    return "This document was held back for a person to confirm."


def _render_verdict(result: ExtractionResult, *, threshold: float) -> str:
    """Build the headline verdict block shown above the tabs.

    The numeric confidence is deliberately absent from the accepted verdict: the
    current backend exposes no per-field signal, so a genuine accept would read
    "50%, threshold 50%", which looks like a coin flip and explains nothing. It
    remains in the details view and in the low-confidence reason, where it is
    the actual explanation.
    """
    if result.decision != "review":
        return (
            "## Accepted automatically\n\n"
            "Every must-pass check cleared and confidence was high enough, so this "
            "document would be written straight through with no human step.\n\n"
            "_Check-by-check detail is in **Checks**._"
        )

    reason = _review_reason(result, threshold=threshold)
    return (
        "## Sent to review\n\n"
        f"{reason}\n\n"
        "Review is the safe default here, not a failure. The extracted fields are "
        "below for a person to confirm.\n\n"
        "_Check-by-check detail is in **Checks**._"
    )


def _render_details(result: ExtractionResult, *, threshold: float) -> str:
    """The technical view -- the one surface where rule codes belong."""
    signal = "not reported" if result.model_signal is None else f"{result.model_signal:.0%}"
    lines = [
        "### Run",
        "",
        f"- Decision: {result.decision}",
        f"- Confidence: {result.confidence:.0%} (auto-accept threshold: {threshold:.0%})",
        f"- Backend: {result.backend_name}",
        f"- Modality: {result.modality or 'unknown'}",
        f"- Model signal: {signal}",
    ]
    if result.error:
        lines.append(f"- Pipeline error: {result.error}")

    lines += ["", "### Validation rules", "", "| Rule | Severity | Status | Message |", "|---|---|---|---|"]
    for rule in result.report.results:
        lines.append(f"| {rule.code} | {rule.severity} | {rule.status} | {rule.message} |")

    return "\n".join(lines)


def _render_startup_error(exc: Exception) -> str:
    """Build the verdict block for a failure that happened before processing.

    ``load_config`` and ``create_backend`` raise before any ``ExtractionResult``
    exists, so this state cannot be expressed as a document outcome. It is the
    likeliest failure on a freshly deployed Space (a missing API key), and it
    must not read as though the uploaded document were at fault.
    """
    return (
        "## Could not run\n\n"
        "The extractor could not start, so nothing was processed. This is a setup "
        "problem with the demo, not a problem with your document.\n\n"
        f"_{exc}_"
    )


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def _process(file_obj: Any) -> tuple[str, str, str, str]:
    """Gradio callback: returns (verdict, fields, checks, details) markdown."""
    if file_obj is None:
        return _NO_FILE_VERDICT, _EMPTY_FIELDS, _EMPTY_CHECKS, _EMPTY_DETAILS

    src = Path(file_obj)

    # Copy to a named temp file preserving the original extension so modality
    # detection works on the suffix, then clean up after processing.
    suffix = src.suffix or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    shutil.copy2(src, tmp_path)

    try:
        settings = load_config()
        threshold = settings.confidence_threshold
        backend = create_backend(settings)
        result: ExtractionResult = process_document(tmp_path, settings=settings, backend=backend)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the user, never raised into Gradio.
        logger.exception("web: could not process %s", src.name)
        return (
            _render_startup_error(exc),
            _EMPTY_FIELDS,
            _EMPTY_CHECKS,
            f"**Startup error:** {exc}",
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    return (
        _render_verdict(result, threshold=threshold),
        _render_fields(result),
        _render_checks(result),
        _render_details(result, threshold=threshold),
    )


# ---------------------------------------------------------------------------
# Gradio interface
# ---------------------------------------------------------------------------


def build_demo() -> gr.Blocks:
    """Construct the Gradio Blocks interface.

    Configuration is read inside the callback, not here, so the module imports
    cleanly on a Space whose secrets are missing -- the failure then renders as a
    verdict rather than crashing the app at startup.
    """
    with gr.Blocks(title="DocField Extract") as demo:
        gr.Markdown("# DocField Extract")
        gr.Markdown(_PRIVACY_NOTICE)
        gr.Markdown(_INTRO)

        with gr.Accordion("How the decision is made", open=False):
            gr.Markdown(_HOW_IT_WORKS)

        with gr.Row():
            upload = gr.File(
                label="Upload document (PDF / JPG / PNG / WEBP)",
                file_types=[".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif"],
                type="filepath",
            )

        run_btn = gr.Button("Extract", variant="primary")

        verdict_out = gr.Markdown(value=_EMPTY_VERDICT)

        with gr.Tab("Extracted fields"):
            fields_out = gr.Markdown(value=_EMPTY_FIELDS)

        with gr.Tab("Checks"):
            checks_out = gr.Markdown(value=_EMPTY_CHECKS)

        with gr.Tab("Details"):
            details_out = gr.Markdown(value=_EMPTY_DETAILS)

        run_btn.click(
            fn=_process,
            inputs=[upload],
            outputs=[verdict_out, fields_out, checks_out, details_out],
        )

    return demo


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    build_demo().launch()
