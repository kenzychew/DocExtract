"""Modality detection: map a document file to its parse path (pure, no I/O).

The first stage of the core pipeline (architecture section 3, step 1) is intake
and type detection: decide whether a file is a *native PDF* -- which Docling
parses into text/layout directly -- or an *image* (a scan or a phone photo),
which goes through the vision-direct or OCR strategy. That single choice,
``"native_pdf"`` vs ``"image"``, is the only thing this module produces; the
acquisition stage downstream uses it to pick a parser.

Classification is by file extension first, with a standard-library MIME-type
fallback for files whose extension is not enumerated explicitly. No bytes are
read -- the result is a pure function of the path string -- so detection stays
fast, deterministic for the supported extensions, and testable offline. An
unrecognised type raises :class:`UnsupportedModalityError` with an actionable
message; the runner catches it per document and routes to review (CLAUDE.md
rule 6), so a stray file never halts the loop.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Literal

# The two parse paths the pipeline supports. ``image`` covers both flatbed scans
# and phone photos -- they share the same downstream handling (architecture
# section 7), so the distinction is not made here.
Modality = Literal["native_pdf", "image"]

# Extensions parsed as native PDFs (the Docling text/layout path).
NATIVE_PDF_EXTENSIONS: frozenset[str] = frozenset({".pdf"})

# Raster image extensions treated as scans/photos (vision-direct or OCR path).
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
)


class UnsupportedModalityError(ValueError):
    """Raised when a file's type maps to none of the supported modalities."""


def _modality_from_mime(mime_type: str | None) -> Modality | None:
    """Classify a MIME type into a modality, or ``None`` if it maps to neither.

    Args:
        mime_type: A MIME type such as ``"application/pdf"`` or ``"image/png"``,
            or ``None`` when the type could not be guessed.

    Returns:
        ``"native_pdf"`` for PDF, ``"image"`` for any ``image/*`` type, or
        ``None`` when the MIME type is unknown or unsupported.
    """
    if mime_type == "application/pdf":
        return "native_pdf"
    if mime_type is not None and mime_type.startswith("image/"):
        return "image"
    return None


def detect_modality(path: str | Path) -> Modality:
    """Determine the parse modality of a document from its filename.

    Extension matching (case-insensitively) is the authoritative signal for the
    supported types; for any other extension the standard library's MIME guess
    is consulted as a fallback before giving up. The file is never opened, so
    ``path`` need not exist.

    Args:
        path: The document path. Only the filename and its extension are
            inspected; directories and the file's contents are ignored.

    Returns:
        ``"native_pdf"`` for PDFs, ``"image"`` for supported raster images.

    Raises:
        UnsupportedModalityError: If the extension and MIME guess both fail to
            resolve to a supported modality.
    """
    name = Path(path).name
    suffix = Path(name).suffix.lower()

    if suffix in NATIVE_PDF_EXTENSIONS:
        return "native_pdf"
    if suffix in IMAGE_EXTENSIONS:
        return "image"

    guessed = _modality_from_mime(mimetypes.guess_type(name)[0])
    if guessed is not None:
        return guessed

    shown_suffix = suffix or "(none)"
    raise UnsupportedModalityError(
        f"Cannot determine modality for {name!r}: extension {shown_suffix} is not "
        f"a supported document type. Supported: PDF "
        f"({', '.join(sorted(NATIVE_PDF_EXTENSIONS))}) or image "
        f"({', '.join(sorted(IMAGE_EXTENSIONS))})."
    )


def is_supported(path: str | Path) -> bool:
    """Report whether ``path`` resolves to a supported modality.

    A convenience predicate over :func:`detect_modality` for callers (e.g. the
    watcher) that want to skip unsupported files without handling an exception.

    Args:
        path: The document path to test.

    Returns:
        ``True`` if :func:`detect_modality` would succeed, ``False`` otherwise.
    """
    try:
        detect_modality(path)
    except UnsupportedModalityError:
        return False
    return True
