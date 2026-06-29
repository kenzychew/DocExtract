"""Application configuration loaded from the environment.

Configuration is read from environment variables (and an optional ``.env``
file) via ``pydantic-settings``. The loader validates cross-field combinations
that the specs require and fails fast with an actionable message, so a
misconfiguration is caught at startup rather than at the first model call.

See ``docs/04_project_setup.md`` section 3 for the canonical variable list and
``docs/02_architecture.md`` section 5 for the backend capabilities that drive
the validation rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Backends able to consume an image directly (vision-direct). Ollama is
# text-only and must go through the OCR path first (architecture section 5).
MULTIMODAL_BACKENDS: frozenset[str] = frozenset({"gemini"})

BackendName = Literal["gemini", "ollama"]
ImageStrategy = Literal["vision_direct", "ocr_then_text"]


class ConfigError(RuntimeError):
    """Raised when configuration is missing or internally inconsistent.

    Carries a human-readable, actionable message intended to be shown at
    startup so the operator can fix the environment and retry.
    """


class Settings(BaseSettings):
    """Validated runtime configuration for the extraction agent.

    Attributes:
        extraction_backend: Which model backend to use ("gemini" | "ollama").
        gemini_api_key: Google AI Studio key; required when using Gemini.
        gemini_model: Gemini model identifier (config, never hardcoded).
        ollama_host: Base URL of the local Ollama server.
        ollama_model: Ollama model identifier.
        image_strategy: How images are handled ("vision_direct" |
            "ocr_then_text"). vision_direct requires a multimodal backend.
        confidence_threshold: Auto-accept threshold in [0, 1], tuned via eval.
        inbox_dir: Batch-mode inbox directory.
        processed_dir: Destination for accepted documents in batch mode.
        review_dir: Destination for documents routed to review.
        export_dir: CSV export directory.
        db_path: SQLite database path.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Backend selection.
    extraction_backend: BackendName = "gemini"

    # Gemini (free tier).
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"

    # Ollama (local).
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"

    # Image handling strategy.
    image_strategy: ImageStrategy = "vision_direct"

    # Routing.
    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # Paths (batch mode).
    inbox_dir: Path = Path("./data/inbox")
    processed_dir: Path = Path("./data/processed")
    review_dir: Path = Path("./data/review")
    export_dir: Path = Path("./data/exports")
    db_path: Path = Path("./data/agent.db")

    @model_validator(mode="after")
    def _validate_combinations(self) -> "Settings":
        """Validate cross-field combinations the specs require.

        Returns:
            The validated settings instance.

        Raises:
            ValueError: If the Gemini backend is selected without an API key,
                or if vision_direct is paired with a text-only backend.
        """
        if self.extraction_backend == "gemini" and not self.gemini_api_key.strip():
            raise ValueError(
                "EXTRACTION_BACKEND=gemini requires GEMINI_API_KEY to be set "
                "(get a free key from Google AI Studio)."
            )

        if (
            self.image_strategy == "vision_direct"
            and self.extraction_backend not in MULTIMODAL_BACKENDS
        ):
            supported = ", ".join(sorted(MULTIMODAL_BACKENDS))
            raise ValueError(
                f"IMAGE_STRATEGY=vision_direct requires a multimodal backend "
                f"({supported}); EXTRACTION_BACKEND={self.extraction_backend} is "
                "text-only -- use IMAGE_STRATEGY=ocr_then_text instead."
            )

        return self


def _format_validation_error(exc: ValidationError) -> str:
    """Render a pydantic ValidationError into an actionable one-line summary.

    Args:
        exc: The validation error raised while constructing ``Settings``.

    Returns:
        A semicolon-joined string of the underlying error messages.
    """
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "config"
        messages.append(f"{location}: {error.get('msg', 'invalid value')}")
    return "; ".join(messages)


def load_config(**overrides: Any) -> Settings:
    """Load and validate configuration, failing fast on misconfiguration.

    Args:
        **overrides: Optional field overrides passed directly to ``Settings``;
            primarily used by tests to bypass the environment.

    Returns:
        A validated ``Settings`` instance.

    Raises:
        ConfigError: If the environment is missing required values or contains
            an unsupported combination of settings.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
