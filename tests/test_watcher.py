"""Unit tests for the folder watcher / batch runner (build-plan task 4.2).

All tests are fully offline: they use ``tmp_path`` for isolated directories,
the ``StubBackend`` for deterministic extraction, and inject an ``acquire``
callable that skips real parsing. No network, no Docling, no real models.

Acceptance criteria (T8):
- A mixed batch (PDF + image + corrupt file) all process without stopping.
- Valid files move to processed/ or review/ based on their decision.
- A corrupt file (unreadable) routes to review/ with a logged reason.
- A duplicate (same content hash) is persisted only once.
- Unsupported file types are skipped, not crashed on.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

from doc_agent.backends.base import DocumentPayload
from doc_agent.backends.stub import DEFAULT_STUB_DOCUMENT, StubBackend
from doc_agent.config import load_config
from doc_agent.ingest.watcher import _process_one, process_inbox
from doc_agent.store.db import record_count


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path):
    """Settings pointing all paths into a tmp_path tree."""
    return load_config(
        extraction_backend="ollama",
        image_strategy="ocr_then_text",
        inbox_dir=str(tmp_path / "inbox"),
        processed_dir=str(tmp_path / "processed"),
        review_dir=str(tmp_path / "review"),
        db_path=str(tmp_path / "agent.db"),
    )


def _stub_backend_accept():
    return StubBackend()


def _stub_backend_review():
    """Stub whose data fails H2 so the pipeline routes to review."""
    broken = dict(DEFAULT_STUB_DOCUMENT)
    broken["total"] = 999.99
    return StubBackend(data=broken, field_confidence={})


def _make_pdf(inbox: Path, name: str = "doc.pdf") -> Path:
    """Write a minimal valid-looking PDF file."""
    p = inbox / name
    p.write_bytes(b"%PDF-1.4 fake content")
    return p


def _make_image(inbox: Path, name: str = "photo.jpeg") -> Path:
    p = inbox / name
    p.write_bytes(b"\xff\xd8\xff fake jpeg")
    return p


def _make_corrupt(inbox: Path, name: str = "corrupt.pdf") -> Path:
    """Write a file that will cause file_sha256 to fail (directory trick via mock)."""
    p = inbox / name
    p.write_bytes(b"")  # exists but empty; sha256 succeeds; we corrupt at process time
    return p


def _patched_process_inbox(settings, backend, today=date(2024, 6, 1)):
    """Run process_inbox with an injected backend and acquire stub."""

    def _acquire(path: Path, modality):
        return DocumentPayload(modality=modality, source_path=path, text="stub text")

    with patch("doc_agent.ingest.watcher.create_backend", return_value=backend), \
         patch("doc_agent.core._make_acquire", return_value=_acquire), \
         patch("doc_agent.core.date") as mock_date:
        mock_date.today.return_value = today
        return process_inbox(settings)


# ---------------------------------------------------------------------------
# _process_one: per-document behaviour
# ---------------------------------------------------------------------------


def test_accepted_document_moves_to_processed(tmp_path: Path) -> None:
    """An auto-accepted document ends up in processed/ and is persisted."""
    settings = _settings(tmp_path)
    inbox = Path(settings.inbox_dir)
    inbox.mkdir(parents=True)
    src = _make_pdf(inbox)

    def _acquire(path, modality):
        return DocumentPayload(modality=modality, source_path=path, text="stub")

    with patch("doc_agent.core._make_acquire", return_value=_acquire):
        _process_one(src, settings, _stub_backend_accept())

    assert not src.exists()
    assert (Path(settings.processed_dir) / src.name).exists()
    assert record_count(Path(settings.db_path)) == 1


def test_review_document_moves_to_review(tmp_path: Path) -> None:
    """A document routed to review ends up in review/ and is NOT persisted."""
    settings = _settings(tmp_path)
    inbox = Path(settings.inbox_dir)
    inbox.mkdir(parents=True)
    src = _make_pdf(inbox)

    def _acquire(path, modality):
        return DocumentPayload(modality=modality, source_path=path, text="stub")

    with patch("doc_agent.core._make_acquire", return_value=_acquire):
        _process_one(src, settings, _stub_backend_review())

    assert not src.exists()
    assert (Path(settings.review_dir) / src.name).exists()
    assert record_count(Path(settings.db_path)) == 0


def test_corrupt_file_routes_to_review_loop_continues(tmp_path: Path) -> None:
    """A file that raises during hashing is moved to review; no exception propagates."""
    settings = _settings(tmp_path)
    inbox = Path(settings.inbox_dir)
    inbox.mkdir(parents=True)
    src = _make_pdf(inbox, "corrupt.pdf")

    with patch("doc_agent.ingest.watcher.file_sha256", side_effect=OSError("unreadable")):
        _process_one(src, settings, _stub_backend_accept())  # must not raise

    assert not src.exists()
    assert (Path(settings.review_dir) / src.name).exists()
    assert record_count(Path(settings.db_path)) == 0


def test_already_moved_file_is_skipped_silently(tmp_path: Path) -> None:
    """_process_one is a no-op when the file no longer exists (race guard)."""
    settings = _settings(tmp_path)
    ghost = tmp_path / "inbox" / "ghost.pdf"  # never created

    _process_one(ghost, settings, _stub_backend_accept())  # must not raise

    assert record_count(Path(settings.db_path)) == 0


# ---------------------------------------------------------------------------
# process_inbox: batch mode
# ---------------------------------------------------------------------------


def test_batch_processes_mixed_files(tmp_path: Path) -> None:
    """A batch of PDF + image files all process; counts add up."""
    settings = _settings(tmp_path)
    inbox = Path(settings.inbox_dir)
    inbox.mkdir(parents=True)
    _make_pdf(inbox, "a.pdf")
    _make_image(inbox, "b.jpeg")

    counts = _patched_process_inbox(settings, _stub_backend_accept())

    assert counts["processed"] == 2
    assert counts["skipped"] == 0


def test_batch_unsupported_files_are_skipped(tmp_path: Path) -> None:
    """Files with unsupported extensions are skipped, not crashed on."""
    settings = _settings(tmp_path)
    inbox = Path(settings.inbox_dir)
    inbox.mkdir(parents=True)
    (inbox / "readme.txt").write_text("hello")
    (inbox / "data.csv").write_text("a,b")
    _make_pdf(inbox, "invoice.pdf")

    counts = _patched_process_inbox(settings, _stub_backend_accept())

    assert counts["skipped"] == 2
    assert counts["processed"] == 1


def test_batch_corrupt_file_does_not_stop_loop(tmp_path: Path) -> None:
    """A corrupt file routes to review and the rest of the batch continues."""
    settings = _settings(tmp_path)
    inbox = Path(settings.inbox_dir)
    inbox.mkdir(parents=True)
    _make_pdf(inbox, "good.pdf")
    _make_pdf(inbox, "corrupt.pdf")
    _make_image(inbox, "photo.jpeg")

    def _acquire(path, modality):
        return DocumentPayload(modality=modality, source_path=path, text="stub")

    def _flaky_hash(path):
        if "corrupt" in path.name:
            raise OSError("disk error")
        from doc_agent.utils.hash import file_sha256 as _real
        return _real(path)

    with patch("doc_agent.ingest.watcher.create_backend", return_value=_stub_backend_accept()), \
         patch("doc_agent.core._make_acquire", return_value=_acquire), \
         patch("doc_agent.ingest.watcher.file_sha256", side_effect=_flaky_hash):
        counts = process_inbox(settings)

    assert counts["processed"] == 3  # all three attempted
    assert (Path(settings.review_dir) / "corrupt.pdf").exists()


def test_batch_duplicate_hash_persisted_once(tmp_path: Path) -> None:
    """Two files with identical content produce only one DB record."""
    settings = _settings(tmp_path)
    inbox = Path(settings.inbox_dir)
    inbox.mkdir(parents=True)
    content = b"%PDF-1.4 identical"
    (inbox / "a.pdf").write_bytes(content)
    (inbox / "b.pdf").write_bytes(content)

    counts = _patched_process_inbox(settings, _stub_backend_accept())

    assert counts["processed"] == 2
    assert record_count(Path(settings.db_path)) == 1


def test_batch_creates_directories(tmp_path: Path) -> None:
    """process_inbox creates inbox, processed, and review dirs if absent."""
    settings = _settings(tmp_path)
    # Directories do not exist yet.
    assert not Path(settings.inbox_dir).exists()

    _patched_process_inbox(settings, _stub_backend_accept())

    assert Path(settings.inbox_dir).exists()
    assert Path(settings.processed_dir).exists()
    assert Path(settings.review_dir).exists()
