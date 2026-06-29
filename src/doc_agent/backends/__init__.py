"""Swappable extraction backends behind the ``ExtractionBackend`` interface.

The public interface and factory are re-exported here for convenience. Concrete
adapters (the stub, and later Gemini/Ollama) are intentionally *not* imported at
package load: they are built lazily by the factory so selecting one backend
never imports another's dependencies.
"""

from doc_agent.backends.base import (
    BackendBuilder,
    BackendResult,
    DocumentPayload,
    ExtractionBackend,
    available_backends,
    create_backend,
)

__all__ = [
    "BackendBuilder",
    "BackendResult",
    "DocumentPayload",
    "ExtractionBackend",
    "available_backends",
    "create_backend",
]
