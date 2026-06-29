"""Unit tests for modality detection.

Covers the acceptance criteria for build-plan task 2.1: every supported
extension is classified correctly as ``native_pdf`` or ``image``. Also exercises
case-insensitivity, ``str``/``Path`` inputs, paths with directories, the MIME
fallback, the unsupported-type error, the ``is_supported`` predicate, and the
purity guarantee (no filesystem access).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_agent.parsing.detect import (
    IMAGE_EXTENSIONS,
    NATIVE_PDF_EXTENSIONS,
    UnsupportedModalityError,
    detect_modality,
    is_supported,
)


@pytest.mark.parametrize("ext", sorted(NATIVE_PDF_EXTENSIONS))
def test_native_pdf_extensions(ext: str) -> None:
    """Every PDF extension classifies as ``native_pdf``."""
    assert detect_modality(f"invoice{ext}") == "native_pdf"


@pytest.mark.parametrize("ext", sorted(IMAGE_EXTENSIONS))
def test_image_extensions(ext: str) -> None:
    """Every supported image extension classifies as ``image``."""
    assert detect_modality(f"receipt{ext}") == "image"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("scan.PDF", "native_pdf"),
        ("photo.JPG", "image"),
        ("photo.JpEg", "image"),
        ("page.TIFF", "image"),
    ],
)
def test_extension_match_is_case_insensitive(name: str, expected: str) -> None:
    """Extension matching ignores case."""
    assert detect_modality(name) == expected


def test_accepts_path_object() -> None:
    """A ``Path`` input is handled identically to a string."""
    assert detect_modality(Path("docs/invoice.pdf")) == "native_pdf"


def test_full_path_with_directories() -> None:
    """Only the filename's extension matters, not the leading directories."""
    assert detect_modality("/data/inbox/2024/q1/receipt.png") == "image"
    assert detect_modality(r"C:\data\inbox\bill.pdf") == "native_pdf"


def test_filename_with_multiple_dots() -> None:
    """Only the final suffix is used for classification."""
    assert detect_modality("vendor.invoice.final.pdf") == "native_pdf"
    assert detect_modality("scan.v2.tiff") == "image"


def test_mime_fallback_for_unenumerated_extension() -> None:
    """An extension absent from the explicit sets falls back to the MIME guess.

    ``.tif`` is in the explicit image set; ``.jfif`` is a real JPEG extension
    that the standard library maps to ``image/jpeg`` but which is intentionally
    not enumerated, so it exercises the fallback path.
    """
    assert ".jfif" not in IMAGE_EXTENSIONS
    assert detect_modality("photo.jfif") == "image"


@pytest.mark.parametrize("name", ["notes.txt", "report.docx", "archive.zip", "noext"])
def test_unsupported_type_raises(name: str) -> None:
    """An unsupported or extension-less file raises a clear error."""
    with pytest.raises(UnsupportedModalityError) as exc_info:
        detect_modality(name)
    # The message is actionable: it names the offending file and the supported set.
    message = str(exc_info.value)
    assert name in message
    assert "pdf" in message.lower()


def test_is_supported_predicate() -> None:
    """``is_supported`` mirrors ``detect_modality`` without raising."""
    assert is_supported("invoice.pdf") is True
    assert is_supported("receipt.png") is True
    assert is_supported("notes.txt") is False
    assert is_supported("noext") is False


def test_detection_does_not_touch_the_filesystem() -> None:
    """Detection is pure: a nonexistent path still classifies by name alone."""
    nonexistent = Path("this/path/does/not/exist/phantom.pdf")
    assert not nonexistent.exists()
    assert detect_modality(nonexistent) == "native_pdf"
