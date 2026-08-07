"""Unit tests for the configuration loader.

Tests pass ``_env_file=None`` and explicit field overrides so they are fully
isolated from any real ``.env`` file or ambient environment variables.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from docfield.config import (
    MULTIMODAL_BACKENDS,
    ConfigError,
    Settings,
    load_config,
)


def _load(**overrides: Any) -> Settings:
    """Load config in isolation from the ambient environment.

    Args:
        **overrides: Field overrides forwarded to ``load_config``.

    Returns:
        A validated ``Settings`` instance.
    """
    return load_config(_env_file=None, **overrides)


def test_valid_gemini_config_parses() -> None:
    """A Gemini backend with an API key and vision_direct parses cleanly."""
    settings = _load(
        extraction_backend="gemini",
        gemini_api_key="test-key",
        image_strategy="vision_direct",
    )
    assert settings.extraction_backend == "gemini"
    assert settings.gemini_api_key == "test-key"
    assert settings.image_strategy == "vision_direct"


def test_valid_ollama_config_parses() -> None:
    """An Ollama backend with ocr_then_text needs no API key and parses."""
    settings = _load(
        extraction_backend="ollama",
        image_strategy="ocr_then_text",
    )
    assert settings.extraction_backend == "ollama"
    assert settings.image_strategy == "ocr_then_text"


def test_gemini_requires_api_key() -> None:
    """Selecting Gemini without a key fails fast with an actionable message."""
    with pytest.raises(ConfigError) as exc_info:
        _load(
            extraction_backend="gemini",
            gemini_api_key="",
            image_strategy="ocr_then_text",
        )
    assert "GEMINI_API_KEY" in str(exc_info.value)


def test_gemini_blank_api_key_is_rejected() -> None:
    """A whitespace-only key is treated as missing."""
    with pytest.raises(ConfigError):
        _load(
            extraction_backend="gemini",
            gemini_api_key="   ",
            image_strategy="ocr_then_text",
        )


def test_vision_direct_requires_multimodal_backend() -> None:
    """vision_direct with a text-only backend fails fast with guidance."""
    with pytest.raises(ConfigError) as exc_info:
        _load(
            extraction_backend="ollama",
            image_strategy="vision_direct",
        )
    message = str(exc_info.value)
    assert "vision_direct" in message
    assert "ocr_then_text" in message


def test_unknown_backend_is_rejected() -> None:
    """An unsupported backend name is rejected by the type constraint."""
    with pytest.raises(ConfigError):
        _load(extraction_backend="openai", gemini_api_key="x")


def test_confidence_threshold_out_of_range_is_rejected() -> None:
    """A threshold outside [0, 1] fails validation."""
    with pytest.raises(ConfigError):
        _load(
            extraction_backend="ollama",
            image_strategy="ocr_then_text",
            confidence_threshold=1.5,
        )


def test_defaults_are_applied() -> None:
    """Unset optional fields fall back to the documented defaults."""
    settings = _load(
        extraction_backend="ollama",
        image_strategy="ocr_then_text",
    )
    assert settings.gemini_model == "gemini-flash-latest"
    assert settings.ollama_host == "http://localhost:11434"
    assert settings.ollama_model == "qwen2.5:7b"
    assert settings.confidence_threshold == pytest.approx(0.50)


def test_paths_are_path_objects() -> None:
    """Path-typed settings are coerced to ``pathlib.Path``."""
    settings = _load(
        extraction_backend="ollama",
        image_strategy="ocr_then_text",
    )
    assert isinstance(settings.inbox_dir, Path)
    assert isinstance(settings.processed_dir, Path)
    assert isinstance(settings.review_dir, Path)
    assert isinstance(settings.export_dir, Path)
    assert isinstance(settings.db_path, Path)


def test_settings_model_raises_validation_error_directly() -> None:
    """Constructing ``Settings`` directly surfaces a pydantic ValidationError."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            extraction_backend="gemini",
            gemini_api_key="",
            image_strategy="ocr_then_text",
        )


def test_only_gemini_is_multimodal() -> None:
    """Ollama is not a multimodal backend; Gemini is."""
    assert "gemini" in MULTIMODAL_BACKENDS
    assert "ollama" not in MULTIMODAL_BACKENDS
