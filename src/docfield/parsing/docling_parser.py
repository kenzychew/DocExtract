"""Native PDF parser using Docling.

Converts a PDF file to a Markdown text representation suitable for passing
to a text-capable extraction backend (Gemini with a text part, or Ollama).
Docling handles layout analysis, table detection, and text ordering so the
output preserves the document's logical structure rather than raw PDF stream
order, which matters for invoices where totals and line items appear in tables.

The ``DocumentConverter`` is built once (module-level singleton) because it
loads layout models at construction time -- rebuilding it per document would
re-load models on every call. The first call after import triggers a one-time
model download if the models are not already cached locally.

See ``docs/02_architecture.md`` section 5 (Docling) and build-plan task 2.2.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level singleton: built on first call to parse_pdf so model loading
# happens lazily (not at import time) and only once per process.
_converter = None


def _get_converter():
    """Return the shared DocumentConverter, initialising it on first call.

    OCR is disabled because this parser is only used for the ``native_pdf``
    modality -- the text is already embedded in the file and OCR would just
    pull in a large model dependency that fails on some platforms.

    Returns:
        The cached ``DocumentConverter`` instance.
    """
    global _converter
    if _converter is None:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = False

        logger.info("docling: initialising DocumentConverter (OCR disabled)")
        _converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
        logger.info("docling: DocumentConverter ready")
    return _converter


def parse_pdf(path: Path) -> str:
    """Parse a native PDF to a Markdown text representation.

    Uses Docling's layout-aware conversion so tables, columns, and reading
    order are preserved in the output text that the extraction backend receives.

    Args:
        path: Path to the PDF file to parse.

    Returns:
        Markdown-formatted text of the document's content.

    Raises:
        RuntimeError: If Docling fails to convert the document.
    """
    converter = _get_converter()
    logger.debug("docling: parsing %s", path)

    result = converter.convert(str(path))
    text = result.document.export_to_markdown()

    logger.debug("docling: extracted %d chars from %s", len(text), path)
    return text
