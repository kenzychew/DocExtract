"""Backend interface, payload/result types, and the config-driven factory.

Every model call in the system goes through ``ExtractionBackend`` (CLAUDE.md
architectural rule 2): the core and the entry points depend on this interface,
never on a provider SDK. Concrete adapters (Gemini, Ollama) implement
``extract`` and register a builder here; adding a backend is "implement the
interface + register in the factory; nothing else changes."

Three small types make the contract concrete:

- ``DocumentPayload`` -- the acquired representation of one document (Docling
  text/layout for native PDFs, OCR text or raw image bytes for scans/photos)
  handed to a backend. It is the seam between the parsing/acquire stage and
  extraction: a text-only backend reads ``text``; a multimodal backend may read
  ``image_bytes``.
- ``BackendResult`` -- the raw structured output of a backend: the extracted
  ``data`` dict (validated into a ``Document`` by the core, never regex-parsed
  out of free text), an optional per-field ``field_confidence`` signal, and the
  ``raw`` provider response for logging/debugging.
- ``ExtractionBackend`` -- the ``Protocol`` the core programs against.

The factory resolves a backend name (an explicit override or
``Settings.extraction_backend``) against a registry of builders. Builders import
their adapter lazily -- only when that backend is actually built -- so this
module stays a dependency leaf: it imports no concrete adapter at module load,
which keeps provider SDKs (and their heavy/optional deps) out of the import path
until one is selected. An unknown or not-yet-available name is a recoverable
:class:`~doc_agent.config.ConfigError` with an actionable message, never a crash
deep in the pipeline (architecture section 5; CLAUDE.md rule 3).

See ``docs/02_architecture.md`` section 5.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from doc_agent.config import ConfigError, Settings
from doc_agent.parsing.detect import Modality


@dataclass(frozen=True)
class DocumentPayload:
    """The acquired representation of one document handed to a backend.

    Produced by the acquisition stage (Docling for native PDFs, OCR for images,
    or the raw page image for vision-direct backends) and consumed by
    :meth:`ExtractionBackend.extract`. It carries whichever representations were
    produced; a text-only backend (e.g. Ollama) reads ``text`` while a
    multimodal backend (e.g. Gemini) may read ``image_bytes`` directly.

    Attributes:
        modality: The detected parse path ("native_pdf" | "image").
        source_path: Original file path, for logging/diagnostics; may be ``None``
            when the payload is synthesized (tests, the web demo's in-memory
            upload).
        text: Extracted text/layout representation, or ``None`` when only an
            image is supplied.
        image_bytes: Raw page-image bytes for vision-direct extraction, or
            ``None`` for the text-only path.
        image_mime: MIME type of ``image_bytes`` (e.g. "image/png"), or ``None``.
        metadata: Free-form acquisition metadata (page count, parser name,
            timings) for logging; not part of the extraction contract.
    """

    modality: Modality
    source_path: Path | None = None
    text: str | None = None
    image_bytes: bytes | None = None
    image_mime: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendResult:
    """Raw structured output returned by an :class:`ExtractionBackend`.

    The shape mirrors the architecture spec (section 5): ``{ data,
    field_confidence, raw }``. The core validates ``data`` into a ``Document``
    (structured output is enforced, never regex-parsed) and folds
    ``field_confidence`` into the document-level model signal used by scoring.

    Attributes:
        data: The extracted fields as a plain dict, ready to be validated into a
            ``Document``. Keys map to schema field names.
        field_confidence: Optional per-field confidence in ``[0, 1]`` where the
            backend exposes one; ``None`` when the backend reports no signal (the
            scorer then treats confidence as neutral).
        raw: The raw provider response (or a stand-in), retained for logging and
            debugging only; the pipeline never parses it.
    """

    data: dict[str, Any]
    field_confidence: dict[str, float] | None = None
    raw: Any = None


@runtime_checkable
class ExtractionBackend(Protocol):
    """The interface every model backend implements.

    The core programs against this ``Protocol`` (CLAUDE.md architectural
    rule 2), so it never depends on a concrete adapter or a provider SDK.
    Implementations enforce schema-constrained output (Pydantic schema for
    Gemini, JSON-schema/grammar for Ollama) and apply bounded retries/timeouts
    internally; an exhausted call surfaces as an error the core routes to review
    rather than crashing.

    Attributes:
        name: Stable identifier for the backend (e.g. "gemini", "ollama",
            "stub"); used in logs and by the factory registry.
    """

    name: str

    def extract(self, payload: DocumentPayload, schema: type[BaseModel]) -> BackendResult:
        """Extract structured fields from one document payload.

        Args:
            payload: The acquired document representation (text and/or image).
            schema: The Pydantic model class defining the output contract; the
                backend constrains its output to this schema.

        Returns:
            A ``BackendResult`` with the extracted ``data`` dict and any
            confidence signal the backend exposes.
        """
        ...


# A builder turns validated settings into a ready-to-use backend instance.
BackendBuilder = Callable[[Settings], ExtractionBackend]


def _build_stub(settings: Settings) -> ExtractionBackend:
    """Construct the offline stub backend.

    The import is local so :mod:`doc_agent.backends.base` does not import
    :mod:`doc_agent.backends.stub` at module load (avoiding an import cycle, the
    stub imports the result/payload types from here) and so selecting one
    backend never imports another's dependencies.

    Args:
        settings: Validated runtime configuration (unused by the stub).

    Returns:
        A new ``StubBackend`` instance.
    """
    from doc_agent.backends.stub import StubBackend

    return StubBackend()


def _build_gemini(settings: Settings) -> ExtractionBackend:
    """Construct the Gemini backend (lazy import of google-genai).

    The import is local so this module stays a dependency leaf: importing
    ``doc_agent.backends.base`` never pulls in ``google.genai`` unless the
    Gemini backend is actually selected.

    Args:
        settings: Validated runtime configuration (API key, model, timeout).

    Returns:
        A ready-to-use ``GeminiBackend`` instance.
    """
    from doc_agent.backends.gemini import GeminiBackend

    return GeminiBackend(settings)


# Registry of buildable backends: name -> builder. Builders import their adapter
# lazily so selecting one backend never imports another's (possibly heavy or
# optional) provider SDK. Ollama registers its builder here when implemented
# (build plan phase 2.6).
_BACKEND_BUILDERS: dict[str, BackendBuilder] = {
    "gemini": _build_gemini,
    "stub": _build_stub,
}


def available_backends() -> tuple[str, ...]:
    """Return the names of the backends the factory can currently build.

    Returns:
        The registered backend names, sorted for stable display in messages.
    """
    return tuple(sorted(_BACKEND_BUILDERS))


def create_backend(settings: Settings, *, name: str | None = None) -> ExtractionBackend:
    """Build the configured extraction backend from settings.

    Resolves the backend name from ``name`` (an explicit override, e.g. tests or
    an entry point forcing a backend) falling back to
    ``settings.extraction_backend``, then constructs it via the registered
    builder. The resolution is the single place backend selection happens, so
    the rest of the system depends only on the returned interface.

    Args:
        settings: Validated runtime configuration.
        name: Optional explicit backend name; defaults to
            ``settings.extraction_backend`` when ``None``.

    Returns:
        A ready-to-use object satisfying the ``ExtractionBackend`` protocol.

    Raises:
        ConfigError: If the resolved name is not a currently-available backend.
            The message lists the available backends and notes that the Gemini
            and Ollama adapters arrive in a later build phase.
    """
    backend_name = (name or settings.extraction_backend).strip().lower()
    builder = _BACKEND_BUILDERS.get(backend_name)
    if builder is None:
        available = ", ".join(available_backends())
        raise ConfigError(
            f"Unknown or unavailable extraction backend {backend_name!r}. "
            f"Available backends: {available}. "
            f"(The Ollama adapter is added in build phase 2.6.)"
        )
    return builder(settings)
