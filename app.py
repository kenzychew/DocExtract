"""Hugging Face Space entry point.

A thin launcher for the Gradio demo. All UI and pipeline logic lives in
``doc_agent.web.app``; this file only makes the ``src/`` package importable on
the Space (which runs this file from the repo root without pip-installing the
package) and then builds and launches the demo. Configuration and secrets
(GEMINI_API_KEY, EXTRACTION_BACKEND, IMAGE_STRATEGY, GEMINI_MODEL) are read from
the environment -- set them as Space repository secrets, never in a file.

Logging is configured here because the Space imports this file rather than
running any module's ``__main__`` block, so nothing else would configure it. An
unconfigured root logger defaults to WARNING, which silently dropped the core's
per-document INFO record: failures appeared in the Space log pane and successes
left no trace at all.

The record deliberately carries no extracted field values -- only the filename,
modality, backend, decision, confidence, and the *codes* of any failed
validation rules. The Space log pane is readable by visitors, and the privacy
notice asks people not to upload real documents but cannot enforce it, so the
log must not expose document content if someone ignores it. A decision plus a
rule code is enough to debug from.
"""

import logging
import sys
from pathlib import Path

# src-layout: make `doc_agent` importable without installing the package.
sys.path.insert(0, str(Path(__file__).parent / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# httpx logs a line per request, so an INFO root logger turns every Gemini call
# and every Gradio telemetry ping into log noise that buries the one record
# worth reading. Their content is already covered: a successful call shows up in
# the per-document record, and a failed one in the backend's own warning.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

from doc_agent.web.app import build_demo  # noqa: E402  (import after path setup)

demo = build_demo()

if __name__ == "__main__":
    demo.launch()
