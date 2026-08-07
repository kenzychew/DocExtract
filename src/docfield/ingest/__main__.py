"""Entry point: ``python -m docfield.ingest.watcher`` starts the watcher."""

import logging

from docfield.ingest.watcher import run_watcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

run_watcher()
