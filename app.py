"""Hugging Face Space entry point.

A thin launcher for the Gradio demo. All UI and pipeline logic lives in
``doc_agent.web.app``; this file only makes the ``src/`` package importable on
the Space (which runs this file from the repo root without pip-installing the
package) and then builds and launches the demo. Configuration and secrets
(GEMINI_API_KEY, EXTRACTION_BACKEND, IMAGE_STRATEGY, GEMINI_MODEL) are read from
the environment -- set them as Space repository secrets, never in a file.
"""

import sys
from pathlib import Path

# src-layout: make `doc_agent` importable without installing the package.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from doc_agent.web.app import build_demo  # noqa: E402  (import after path setup)

demo = build_demo()

if __name__ == "__main__":
    demo.launch()
