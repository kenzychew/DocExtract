# CLAUDE.md — Working Conventions for This Project

## What this project is

A key-value field-extraction pipeline for business documents. A reusable core
pipeline (`process_document`) turns a document into a validated, structured
record and decides whether to **auto-accept** it or route it to **review**. The
core is invoked by two thin entry points (a folder watcher and a Gradio web
demo) and depends on a swappable model backend (Gemini free tier or local
Ollama). Full detail in `docs/`.

Read `docs/01_requirements.md`, `docs/02_architecture.md`,
`docs/03_data_and_extraction_spec.md`, and `docs/04_project_setup.md` before
making design decisions. Follow `docs/05_build_plan.md` for task order.

## Tech stack

Python **3.11** (pinned via `.python-version`; `requires-python = ">=3.11"`),
**uv** (package/venv management — `uv add`, `uv sync`, `uv run`; commit
`uv.lock` and `.python-version`), Docling (parsing), Pydantic v2
(contract/validation), google-genai (Gemini) + local Ollama, Gradio (demo),
SQLite + CSV (storage), watchdog (watcher), pytest (tests).

## Architectural rules (do not violate)

1. **Core stays decoupled.** `core.py` must not import from `ingest/`, `web/`,
   or `store/`. Entry points depend on the core, never the reverse. The core
   returns a result object; it performs no file moves and no DB writes.
2. **Backends sit behind the interface.** All model access goes through
   `ExtractionBackend`. No entry point or core code calls a provider SDK
   directly. Adding a backend = implement the interface + register in the
   factory; nothing else changes.
3. **Model identifiers are config, never literals.** Free model catalogs change
   without notice — a hardcoded model name is a latent outage. Read names from
   config; treat a missing/renamed model as a recoverable config error with a
   clear message.
4. **Structured output is enforced, not parsed.** Use schema/grammar-constrained
   output (Pydantic schema for Gemini, JSON-schema/grammar for Ollama). Never
   regex JSON out of free-form text.
5. **Validation gates auto-accept.** A hard-rule failure (especially an
   arithmetic cross-check) forces `review` regardless of model confidence. The
   model is treated as fallible by design.
6. **The loop never dies on one document.** Every document is processed in
   isolation with try/except; failures log full context and route to review,
   then processing continues.
7. **Pure functions stay pure.** `validation/rules.py` and `routing/score.py`
   do no I/O and are fully unit-tested.

## Precision posture (important)

Optimize **precision on the auto-accepted path** for the critical fields
`total`, `tax`, `invoice_number`. A confidently-wrong number is the costly
error because it is written and propagates silently; a missing field is caught
by review. When in doubt, route to review. Recall is measured and traded
against precision via the single `CONFIDENCE_THRESHOLD`, set empirically in
evaluation. Arithmetic cross-checks are the mechanism that keeps precision high
without destroying recall.

## Coding conventions

- Type-hint everything; keep functions small and single-purpose.
- Pydantic models are the single source of truth for the data contract.
- Fail fast on misconfiguration at startup with actionable messages.
- Structured logging (one record per document: inputs, backend, decision,
  validation failures, timings). No secrets in logs.
- Bounded retries + timeouts on all network/model calls.
- No secrets in code or git. Config via `.env` / Space secrets only.
- Manage dependencies with uv; commit `uv.lock` so installs are reproducible.
  Add deps via `uv add`, never by hand-editing pins. Run commands via `uv run`.

## Testing & definition of done

- A task is done when its build-plan **[AC]** is met and tests pass.
- Pure logic (schema, validation, routing) must have unit tests before the
  pipeline is wired together.
- `test_core_smoke.py` runs the pipeline end-to-end with a stub backend (no
  network) so the core is testable offline.
- The watcher must survive a deliberately corrupt file (routes to review,
  loop continues).

## Privacy & cost guardrails

- Development and demo must remain **free**: local Ollama (no quota) or Gemini
  free tier; Hugging Face Spaces free CPU tier for hosting.
- The public demo is **stateless** and must show a "synthetic/public documents
  only" notice. Free hosted backends may train on inputs — never send real
  financial data through the demo or a free API. Sensitive data is handled only
  via the local Ollama backend.

## Autonomous / overnight runs

- The authoritative task ledger is `PROGRESS.md`. Do the next unchecked task in
  its **TONIGHT** section; **never** start a task under **TOMORROW**; **never**
  add a dependency outside the night set in `PROGRESS.md` task **N1**.
- Commit cadence: **one commit per completed task**, using the message given in
  `PROGRESS.md`. Never commit failing tests. Tick the task's box in the same or
  an immediately following commit.
- Prove completion in the transcript: run the task's acceptance check and paste
  the output — an unattended loop's evaluator only sees what you print.
- If blocked, record a one-line note under **BLOCKED** in `PROGRESS.md` and move
  to the next task rather than halting the whole run.
- The specs in `docs/` are inputs, not work products — do not edit them.

## When unsure

Prefer the choice that (a) keeps the core independent of entry points and
backends, (b) routes uncertain results to review rather than auto-accepting,
and (c) keeps the project runnable for free. Surface assumptions in code
comments and the README rather than silently deciding.
