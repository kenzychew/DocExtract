"""Standalone FastAPI demo service wrapping the DocField Extract pipeline.

A second, architecturally-consistent demo alongside the existing Gradio Space
(``src/docfield/web/app.py``): same core pipeline, same backend, plain FastAPI
plus a minimal static frontend instead of Gradio, so it fits the uniform
Railway-subdomain pattern used across the portfolio hub's showcase pieces.

This module is a thin presentation wrapper (CLAUDE.md architectural rule 1):
all pipeline logic lives in ``docfield.core.process_document`` and its
backends. This file only handles HTTP concerns -- upload, rate limiting,
JSON rendering -- and never re-derives extraction or routing logic.

Configuration (``GEMINI_API_KEY``, ``EXTRACTION_BACKEND``, etc.) is read from
the environment via ``docfield.config.load_config``, the same path every other
entry point uses; nothing is hardcoded or duplicated here.

Run locally with:  uv run uvicorn demo.app:app --reload
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock
from typing import Any

# src-layout: make `docfield` importable without installing the package, the
# same way the repo-root app.py launcher does for the Gradio Space. Resolved
# from this file's location (not cwd), so it works whether the process is
# started from the repo root or from within demo/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi import FastAPI, File, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from docfield.backends.base import ExtractionBackend, create_backend  # noqa: E402
from docfield.config import ConfigError, Settings, load_config  # noqa: E402
from docfield.core import ExtractionResult, process_document  # noqa: E402
from docfield.parsing.detect import is_supported  # noqa: E402

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx/httpcore log a line per Gemini call; the per-document record in
# docfield.core already covers that, so keep them quiet (mirrors app.py).
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Config (env-overridable; sensible defaults for a public demo hitting a paid
# Gemini backend).
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = int(os.environ.get("DEMO_MAX_UPLOAD_MB", "10")) * 1024 * 1024
RATE_LIMIT_PER_IP = int(os.environ.get("DEMO_RATE_LIMIT_PER_IP", "5"))
RATE_LIMIT_GLOBAL = int(os.environ.get("DEMO_RATE_LIMIT_GLOBAL", "50"))
RATE_LIMIT_WINDOW_S = int(os.environ.get("DEMO_RATE_LIMIT_WINDOW_S", "3600"))

# ---------------------------------------------------------------------------
# Rate limiting: a simple in-memory sliding window, per-IP and global. Not
# distributed across workers/instances -- adequate for a single-instance
# public demo whose only real cost is the paid Gemini backend behind it.
# ---------------------------------------------------------------------------


class _RateLimiter:
    """In-memory sliding-window limiter, per client IP and global."""

    def __init__(self, per_ip_limit: int, global_limit: int, window_s: int) -> None:
        self._per_ip_limit = per_ip_limit
        self._global_limit = global_limit
        self._window_s = window_s
        self._lock = Lock()
        self._by_ip: dict[str, deque[float]] = defaultdict(deque)
        self._global: deque[float] = deque()

    @staticmethod
    def _prune(times: deque[float], now: float, window_s: int) -> None:
        cutoff = now - window_s
        while times and times[0] < cutoff:
            times.popleft()

    def check(self, ip: str) -> tuple[bool, str | None]:
        """Record and permit a request, or reject it with a reason."""
        now = time.monotonic()
        with self._lock:
            self._prune(self._global, now, self._window_s)
            ip_times = self._by_ip[ip]
            self._prune(ip_times, now, self._window_s)

            if len(self._global) >= self._global_limit:
                return False, "The demo has hit its overall request limit for now. Please try again later."
            if len(ip_times) >= self._per_ip_limit:
                return False, "You've hit the per-visitor request limit. Please try again later."

            self._global.append(now)
            ip_times.append(now)
            return True, None


_rate_limiter = _RateLimiter(RATE_LIMIT_PER_IP, RATE_LIMIT_GLOBAL, RATE_LIMIT_WINDOW_S)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Backend: built once from the environment and reused across requests. No
# per-request state, no persistence -- the request handler mirrors the
# Gradio demo's stateless _process callback.
# ---------------------------------------------------------------------------

_settings: Settings | None = None
_backend: ExtractionBackend | None = None


def _get_backend() -> tuple[Settings, ExtractionBackend]:
    """Lazily build (and memoize) settings + backend from the environment.

    Deferred to first request, not import time, so the process can start (and
    ``/api/health`` can respond) even if secrets are momentarily missing; the
    resulting error is then returned as a normal HTTP response instead of
    crashing the whole service at import.
    """
    global _settings, _backend
    if _backend is None:
        _settings = load_config()
        _backend = create_backend(_settings)
    assert _settings is not None
    return _settings, _backend


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="DocField Extract Demo")

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _document_response(result: ExtractionResult, *, threshold: float) -> dict[str, Any]:
    """Render an ExtractionResult as the JSON payload the frontend consumes.

    Reads ``result``, never recomputes it (architecture rule 1) -- the decision,
    confidence, and validation report are exactly what the pipeline produced.
    """
    return {
        "decision": result.decision,
        "confidence": result.confidence,
        "threshold": threshold,
        "backend": result.backend_name,
        "modality": result.modality,
        "error": result.error,
        "document": result.document.model_dump(mode="json"),
    }


@app.post("/api/extract")
async def extract(request: Request, file: UploadFile = File(...)) -> JSONResponse:
    ip = _client_ip(request)
    allowed, reason = _rate_limiter.check(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    filename = file.filename or "upload"
    suffix = Path(filename).suffix
    if not is_supported(filename):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type {suffix or '(none)'}. Upload a PDF or image "
            "(PDF, JPG, PNG, WEBP, GIF, TIFF, BMP).",
        )

    body = await file.read()
    if not body:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large; the demo accepts up to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    try:
        settings, backend = _get_backend()
    except ConfigError as exc:
        logger.error("demo misconfigured: %s", exc)
        raise HTTPException(status_code=503, detail=f"Demo is not configured: {exc}") from exc

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix or ".pdf", delete=False) as tmp:
            tmp.write(body)
            tmp_path = Path(tmp.name)

        result = process_document(tmp_path, settings=settings, backend=backend)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    # Filename and field values never enter the log record; only shape and
    # outcome (docfield.core.process_document already logs the per-document
    # summary -- decision, confidence, rule codes -- with no field content).
    return JSONResponse(_document_response(result, threshold=settings.confidence_threshold))


# Mounted last: FastAPI matches routes in registration order, so /api/health
# and /api/extract above take precedence over this catch-all static mount.
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
