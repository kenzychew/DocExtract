"""The reusable core pipeline: ``process_document(path) -> ExtractionResult``.

This is the one piece of logic both entry points share (architecture section 1).
It chains the six stages of the data flow (architecture section 7) -- detect
modality, acquire a payload, call the backend, validate, score, route -- and
returns a result object. It performs **no** side effects: no file moves, no
database writes (CLAUDE.md architectural rule 1). The watcher persists and moves
files; the web demo renders and discards; the core only computes.

Two seams keep the core decoupled from its environment:

- **The backend** (``ExtractionBackend``) is injected or built from config via
  the factory. The core never imports a provider SDK (rule 2).
- **Acquisition** (``Acquire``) -- turning a path into a ``DocumentPayload`` with
  Docling/OCR -- is a callable the core depends on but does not implement here.
  The real parser is build-plan phases 2.2/2.3 (a supervised TOMORROW task); for
  now the default raises a clear error and callers inject an acquirer (the smoke
  test a fixed payload, the web demo its uploaded bytes).

Robustness follows the precision posture and rule 6 ("the loop never dies on one
document"): a failure anywhere in the per-document pipeline is caught, logged
with full context, and turned into a ``review`` result rather than propagating.
A missing field is cheap (review catches it); a crash that halts the batch, or a
confidently-wrong number written on auto-accept, are the costly outcomes this
guards against. Misconfiguration (an unbuildable backend) still fails fast,
before the per-document loop -- that is a startup error, not a document error.

See ``docs/02_architecture.md`` sections 3 and 7.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from docfield.backends.base import (
    DocumentPayload,
    ExtractionBackend,
    create_backend,
)
from docfield.config import Settings, load_config
from docfield.parsing.detect import Modality, detect_modality
from docfield.routing.score import route, score
from docfield.schema.models import Decision, Document
from docfield.validation.rules import ValidationReport, validate

logger = logging.getLogger(__name__)

# The acquisition seam: map a document path + its detected modality to the
# payload a backend consumes. The real implementation (Docling for native PDFs,
# OCR for images) is build-plan phases 2.2/2.3 -- a supervised TOMORROW task --
# so the core depends on this interface but does not implement it here, keeping
# acquisition swappable and the core testable offline.
Acquire = Callable[[Path, Modality], DocumentPayload]


@dataclass(frozen=True)
class ExtractionResult:
    """The outcome of running the core pipeline over one document.

    A plain value object: it carries everything an entry point needs to persist,
    render, or queue the document, and nothing it needs to recompute. The
    populated ``document`` (with ``field_confidence``, ``validation``, and
    ``decision`` filled in) is the record to store; the remaining fields surface
    the routing rationale for logging and the web demo's explanation panel.

    Attributes:
        document: The validated ``Document`` with its pipeline fields populated.
            On an error this is an empty document (so downstream code can rely on
            the shape); inspect ``error`` to distinguish.
        report: The structured validation report (hard/soft rule outcomes).
        confidence: The document-level confidence score in ``[0, 1]``.
        decision: The routing decision ("accept" | "review"); mirrors
            ``document.decision``.
        modality: The detected parse path, or ``None`` if detection itself failed.
        backend_name: The backend that produced (or would have produced) the data.
        source_path: The original document path, for logging/diagnostics.
        model_signal: The aggregated backend confidence fed to scoring, or
            ``None`` when the backend exposed none (scoring treated it as neutral).
        error: ``None`` on success; a short message when the document was routed
            to review because a stage raised (rule 6).
    """

    document: Document
    report: ValidationReport
    confidence: float
    decision: Decision
    modality: Modality | None
    backend_name: str
    source_path: Path | None = None
    model_signal: float | None = None
    error: str | None = None

    @property
    def accepted(self) -> bool:
        """Whether the document was auto-accepted (decision == "accept")."""
        return self.decision == "accept"


# MIME types for image modality, keyed by lower-case file extension.
_MIME_BY_SUFFIX: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}


def _load_image_payload(path: Path) -> DocumentPayload:
    """Load an image file's raw bytes for vision-direct extraction.

    Args:
        path: Path to the image file to read.

    Returns:
        A ``DocumentPayload`` with ``image_bytes`` and ``image_mime`` set.
    """
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "image/jpeg")
    return DocumentPayload(
        modality="image",
        source_path=path,
        image_bytes=path.read_bytes(),
        image_mime=mime,
    )


def _make_acquire(settings: Settings) -> Acquire:
    """Create the default acquire callable wired to current settings.

    Handles ``image`` + ``vision_direct`` by reading raw bytes (T2). All other
    paths (``native_pdf``, ``ocr_then_text``) raise until T3/T4/T5 wire them in.

    Args:
        settings: Validated runtime configuration (``image_strategy`` is read).

    Returns:
        An ``Acquire`` callable that maps (path, modality) to a payload.
    """

    def _acquire(path: Path, modality: Modality) -> DocumentPayload:
        if modality == "image" and settings.image_strategy == "vision_direct":
            return _load_image_payload(path)
        if modality == "native_pdf":
            from docfield.parsing.docling_parser import parse_pdf

            text = parse_pdf(path)
            return DocumentPayload(
                modality="native_pdf",
                source_path=path,
                text=text,
                metadata={"parser": "docling"},
            )
        raise NotImplementedError(
            f"Acquisition for modality={modality!r} with "
            f"IMAGE_STRATEGY={settings.image_strategy!r} is not yet wired. "
            "The OCR path (image+ocr_then_text) needs T4. "
            "Inject an acquire callable or use IMAGE_STRATEGY=vision_direct with an image."
        )

    return _acquire


def aggregate_model_signal(field_confidence: dict[str, float] | None) -> float | None:
    """Reduce a backend's per-field confidence to one document-level signal.

    Scoring consumes a single scalar (``routing.score.score``), so the per-field
    map a backend exposes is averaged into one number. The mean is a documented,
    tunable default; the operative precision lever is the arithmetic cross-check
    in validation, not this signal, and the auto-accept threshold is set
    empirically by the evaluation harness. An absent or empty map returns
    ``None`` so the scorer falls back to its neutral prior rather than inventing
    confidence.

    Args:
        field_confidence: The backend's per-field confidence, or ``None``.

    Returns:
        The mean confidence in ``[0, 1]``, or ``None`` when no signal was given.
    """
    if not field_confidence:
        return None
    values = list(field_confidence.values())
    return sum(values) / len(values)


def process_document(
    path: str | Path,
    *,
    settings: Settings | None = None,
    backend: ExtractionBackend | None = None,
    acquire: Acquire | None = None,
    today: date | None = None,
) -> ExtractionResult:
    """Run the full extraction pipeline over one document.

    Chains detect -> acquire -> ``backend.extract`` -> validate -> score ->
    route and returns an :class:`ExtractionResult`. The function has no side
    effects (no file moves, no persistence); callers own those. Any failure in
    the per-document stages is caught and turned into a ``review`` result with
    the error recorded, so one bad document never crashes a batch and the web
    demo never shows a stack trace (rule 6, precision posture).

    Backend selection follows the factory: an injected ``backend`` is used as-is
    (tests, an entry point forcing one); otherwise it is built from ``settings``.
    A misconfigured/unbuildable backend raises *before* the per-document try
    block -- that is a startup error and should fail fast, not silently route a
    document to review.

    Args:
        path: The document to process. Only the path is needed; the file is
            opened (if at all) by the injected acquirer, not here.
        settings: Validated configuration; loaded from the environment when
            ``None``. Supplies ``confidence_threshold`` and, when ``backend`` is
            not injected, the backend selection.
        backend: An explicit backend to use; built from ``settings`` via the
            factory when ``None``.
        acquire: The acquisition callable mapping ``(path, modality)`` to a
            payload; defaults to a placeholder that raises until the real parser
            is wired in (phases 2.2/2.3).
        today: Reference date for the validation future-date check; defaults to
            ``date.today()``. Injected in tests for determinism.

    Returns:
        An :class:`ExtractionResult` with a populated document and a decision.
        Never raises for a document-level problem -- those route to review.

    Raises:
        ConfigError: If configuration is invalid or the configured backend
            cannot be built (a startup error, surfaced before processing).
    """
    settings = settings or load_config()
    backend = backend or create_backend(settings)
    acquire = acquire or _make_acquire(settings)
    reference_date = today if today is not None else date.today()
    source_path = Path(path)
    backend_name = backend.name

    modality: Modality | None = None
    try:
        modality = detect_modality(source_path)
        payload = acquire(source_path, modality)
        backend_result = backend.extract(payload, Document)

        # Structured output is enforced, not regex-parsed (rule 4): the backend's
        # data dict is validated into a Document, which raises on malformed input
        # and routes the document to review via the except below.
        document = Document.model_validate(backend_result.data)

        report = validate(document, today=reference_date)
        model_signal = aggregate_model_signal(backend_result.field_confidence)
        confidence = score(document, report, model_signal)
        decision = route(confidence, report, threshold=settings.confidence_threshold)

        populated = document.model_copy(
            update={
                "field_confidence": dict(backend_result.field_confidence or {}),
                "validation": report.to_dict(),
                "decision": decision,
            }
        )

        logger.info(
            "processed document path=%s modality=%s backend=%s decision=%s "
            "confidence=%.3f model_signal=%s hard_failures=%s soft_failures=%s",
            source_path,
            modality,
            backend_name,
            decision,
            confidence,
            f"{model_signal:.3f}" if model_signal is not None else "none",
            [r.code for r in report.hard_failures],
            [r.code for r in report.soft_failures],
        )

        return ExtractionResult(
            document=populated,
            report=report,
            confidence=confidence,
            decision=decision,
            modality=modality,
            backend_name=backend_name,
            source_path=source_path,
            model_signal=model_signal,
        )
    except Exception as exc:  # noqa: BLE001 -- rule 6: never let one document halt the loop.
        return _review_on_error(source_path, modality, backend_name, exc, reference_date)


def _review_on_error(
    source_path: Path,
    modality: Modality | None,
    backend_name: str,
    exc: Exception,
    today: date,
) -> ExtractionResult:
    """Build a ``review`` result for a document whose pipeline stage raised.

    Implements rule 6: log the failure with full context (so the bug is visible)
    and route to review with the reason recorded, rather than propagating. The
    document is an empty one validated through the same rules (so its shape and
    ``validation`` report are consistent with the success path); an absent
    ``total`` hard-fails ``H4``, which already forces review.

    Args:
        source_path: The document that failed.
        modality: The detected modality, or ``None`` if detection failed first.
        backend_name: The backend in use, for the log record.
        exc: The exception raised by a pipeline stage.
        today: Reference date passed through to validation.

    Returns:
        An :class:`ExtractionResult` with ``decision="review"`` and ``error`` set.
    """
    logger.error(
        "document processing failed; routing to review path=%s modality=%s backend=%s error=%s",
        source_path,
        modality,
        backend_name,
        exc,
        exc_info=True,
    )

    document = Document()
    report = validate(document, today=today)
    populated = document.model_copy(
        update={
            "validation": {**report.to_dict(), "error": str(exc)},
            "decision": "review",
        }
    )
    return ExtractionResult(
        document=populated,
        report=report,
        confidence=0.0,
        decision="review",
        modality=modality,
        backend_name=backend_name,
        source_path=source_path,
        model_signal=None,
        error=str(exc),
    )
