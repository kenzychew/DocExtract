"""Entry point: ``python -m doc_agent.ingest.watcher`` starts the watcher."""

import logging

from doc_agent.ingest.watcher import run_watcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

run_watcher()
