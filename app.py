"""Hugging Face Space entry point.

A thin launcher for the Gradio demo. All UI and pipeline logic lives in
``doc_agent.web.app``; this file only builds and launches it so the Space's
Gradio SDK has an ``app.py`` at the repo root to run. Configuration and secrets
(GEMINI_API_KEY, EXTRACTION_BACKEND, IMAGE_STRATEGY, GEMINI_MODEL) are read from
the environment -- set them as Space repository secrets, never in a file.
"""

from doc_agent.web.app import build_demo

demo = build_demo()

if __name__ == "__main__":
    demo.launch()
