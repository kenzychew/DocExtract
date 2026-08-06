"""Folder watcher and batch runner for unattended document ingestion.

Two modes share the same per-document logic:

- **Batch / one-shot** (``process_inbox``): scan ``inbox_dir`` once, process
  every supported file, then return. Used for testing and scheduled runs.
- **Continuous** (``run_watcher``): process any files already in ``inbox_dir``
  on startup, then watch for new arrivals via watchdog until interrupted.

Per-document flow (architecture section 7, rule 6):
  hash -> process_document -> persist if accepted -> move to processed/ or review/

Every step for a single file is wrapped in a try/except so one bad document
never stops the loop. Failures are logged with full context and the file is
moved to ``review_dir`` with an error suffix so nothing is silently lost.

Idempotency: ``append_record`` uses a UNIQUE content-hash constraint, so
restarting after a crash never double-counts a record that was already
persisted before the move completed.

Entry point: ``python -m doc_agent.ingest.watcher`` starts continuous mode.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from doc_agent.backends.base import ExtractionBackend, create_backend
from doc_agent.config import Settings, load_config
from doc_agent.core import process_document
from doc_agent.parsing.detect import is_supported
from doc_agent.store.db import append_record
from doc_agent.utils.hash import file_sha256

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_dirs(settings: Settings) -> None:
    """Create inbox, processed, and review directories if absent."""
    for path in (settings.inbox_dir, settings.processed_dir, settings.review_dir):
        path.mkdir(parents=True, exist_ok=True)


def _move_safely(src: Path, dest_dir: Path) -> Path:
    """Move ``src`` into ``dest_dir``, appending a nanosecond suffix on collision.

    Args:
        src: The file to move.
        dest_dir: The destination directory (created if absent).

    Returns:
        The final destination path after the move.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.stem}_{src.stat().st_mtime_ns}{src.suffix}"
    shutil.move(str(src), dest)
    return dest


def _process_one(path: Path, settings: Settings, backend: ExtractionBackend) -> None:
    """Process a single document file end-to-end (rule 6: never raises).

    Hashes the file, calls ``process_document``, persists accepted records,
    and moves the source to ``processed/`` or ``review/``. Any exception at
    any step is caught, logged, and the file is routed to ``review/``.

    Args:
        path: The document to process.
        settings: Validated runtime configuration.
        backend: The extraction backend to use.
    """
    if not path.exists():
        return  # raced with another handler or already moved

    logger.info("watcher: processing %s", path.name)

    # Step 1: hash (catches corrupt / unreadable files before the model call).
    try:
        content_hash = file_sha256(path)
    except Exception as exc:
        logger.error("watcher: cannot hash %s -- routing to review: %s", path.name, exc)
        try:
            _move_safely(path, settings.review_dir)
        except Exception as move_exc:
            logger.error("watcher: failed to move %s to review: %s", path.name, move_exc)
        return

    # Step 2: run the core pipeline (always returns a result, never raises).
    result = process_document(path, settings=settings, backend=backend)

    # Step 3: persist if accepted.
    if result.accepted:
        try:
            append_record(result, settings.db_path, content_hash)
        except Exception as exc:
            logger.error("watcher: persistence failed for %s: %s", path.name, exc)

    # Step 4: move the source file.
    dest_dir = settings.processed_dir if result.accepted else settings.review_dir
    try:
        _move_safely(path, dest_dir)
        logger.info(
            "watcher: %s -> %s/ decision=%s confidence=%.3f",
            path.name,
            dest_dir.name,
            result.decision,
            result.confidence,
        )
        if result.error:
            logger.warning("watcher: document error for %s: %s", path.name, result.error)
    except Exception as exc:
        logger.error("watcher: failed to move %s -> %s: %s", path.name, dest_dir.name, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def process_inbox(settings: Settings | None = None) -> dict[str, int]:
    """Process all supported files currently in ``inbox_dir`` and return counts.

    One-shot batch mode: useful for testing, scheduled runs, and the T8 check.
    Does not watch for new files after the initial scan.

    Args:
        settings: Validated runtime configuration; loaded from the environment
            when ``None``.

    Returns:
        A dict with keys ``"processed"``, ``"accepted"``, ``"review"``,
        ``"skipped"`` (unsupported file types).
    """
    settings = settings or load_config()
    _ensure_dirs(settings)
    backend = create_backend(settings)

    counts: dict[str, int] = {"processed": 0, "accepted": 0, "review": 0, "skipped": 0}

    for path in sorted(settings.inbox_dir.iterdir()):
        if not path.is_file():
            continue
        if not is_supported(path):
            logger.debug("watcher: skipping unsupported file %s", path.name)
            counts["skipped"] += 1
            continue

        _process_one(path, settings, backend)
        counts["processed"] += 1
        # Determine outcome by checking where the file ended up.
        if (settings.processed_dir / path.name).exists():
            counts["accepted"] += 1
        else:
            counts["review"] += 1

    logger.info("watcher: batch complete %s", counts)
    return counts


def run_watcher(settings: Settings | None = None) -> None:
    """Run the continuous folder watcher until interrupted (Ctrl-C).

    Processes any files already in ``inbox_dir`` on startup, then watches for
    new arrivals. Uses watchdog's ``Observer`` (inotify on Linux, FSEvents on
    macOS, ReadDirectoryChanges on Windows).

    Args:
        settings: Validated runtime configuration; loaded from the environment
            when ``None``.
    """
    settings = settings or load_config()
    _ensure_dirs(settings)
    backend = create_backend(settings)

    logger.info("watcher: starting; inbox=%s", settings.inbox_dir)

    # Drain any files that arrived while the watcher was down.
    for path in sorted(settings.inbox_dir.iterdir()):
        if path.is_file() and is_supported(path):
            _process_one(path, settings, backend)

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):  # type: ignore[override]
            if event.is_directory:
                return
            path = Path(event.src_path)
            if is_supported(path):
                _process_one(path, settings, backend)

    observer = Observer()
    observer.schedule(_Handler(), str(settings.inbox_dir), recursive=False)
    observer.start()
    logger.info("watcher: watching %s (Ctrl-C to stop)", settings.inbox_dir)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        logger.info("watcher: stopped")
