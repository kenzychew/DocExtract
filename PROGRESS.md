# PROGRESS.md — Autonomous Build Ledger

This file is the memory for the overnight run. It outlives any single agent
session. The loop reads it at the start of every iteration, does the **next
unchecked task in the TONIGHT section only**, proves the task's acceptance
check, commits, and ticks the box.

## Protocol (read every iteration)

1. Do the next unchecked task under **TONIGHT**, exactly one per iteration.
2. Implement it per `docs/05_build_plan.md` and the other specs in `docs/`.
3. Run the task's **Check** command and paste the output (the loop's evaluator
   only sees what is printed).
4. If it passes: commit just that task's changes with the listed **Commit**
   message; tick the box; commit the ledger update.
5. If it fails after reasonable attempts: add a one-line note under **BLOCKED**,
   commit, and move to the next task. Do not halt the whole run.
6. **Never** start a task under **TOMORROW**.
7. **Never** add a dependency outside the night set in task **N1**.
8. The specs already exist in `docs/` — do not regenerate or edit them.

When every TONIGHT box is checked, run `uv run pytest -q` and
`uv run ruff check .`; if both are clean, print `DONE_ALL` and stop.

---

## Night dependency set (the ONLY packages to add tonight)

- Runtime: `pydantic`, `pydantic-settings`
- Dev: `pytest`, `ruff`

Everything else (docling, paddleocr/pytesseract, google-genai, ollama, gradio,
watchdog) belongs to TOMORROW and must NOT be added tonight.

---

## TONIGHT

- [x] **N1 — Project scaffold** (build plan 0.1)
  Run `uv init`; `uv python pin 3.11`; set `requires-python = ">=3.11"`. Create
  the `src/doc_agent/` package tree and `tests/` from the setup-doc layout (the
  `docs/` specs are already present — leave them). Add `.gitignore` (ignore
  `data/`, `.env`, `.venv/`, caches; **commit** `uv.lock` and `.python-version`)
  and an empty `README.md`. Add night deps: `uv add pydantic pydantic-settings`
  and `uv add --dev pytest ruff`.
  Check: `uv sync && uv run python -c "import sys; assert sys.version_info[:2]==(3,11)" && cat .python-version`
  Commit: `phase 0.1: project scaffold (uv, py3.11, package layout)`

- [x] **N2 — Config loader** (0.2)
  `src/doc_agent/config.py` with pydantic-settings: load env, validate combos
  (gemini requires key; `vision_direct` requires a multimodal backend), fail
  fast with clear messages. Add `tests/test_config.py`.
  Check: `uv run pytest tests/test_config.py -q`
  Commit: `phase 0.2: config loader with validation`

- [x] **N3 — Document schema** (1.1)
  `src/doc_agent/schema/models.py`: `Document` and `LineItem` per
  `docs/03_data_and_extraction_spec.md`, with money→float and date→ISO
  normalizers. Add `tests/test_schema.py`.
  Check: `uv run pytest tests/test_schema.py -q`
  Commit: `phase 1.1: pydantic document schema + normalizers`

- [ ] **N4 — Validation rules** (1.2)
  `src/doc_agent/validation/rules.py`: hard rules H1–H4, soft rules S1–S4,
  monetary epsilon, returning a structured report. Pure functions, no I/O. Add
  `tests/test_validation.py` (reconciling totals pass H2/H3; mismatches fail;
  soft failures recorded without forcing review).
  Check: `uv run pytest tests/test_validation.py -q`
  Commit: `phase 1.2: validation rules (hard/soft + arithmetic checks)`

- [ ] **N5 — Confidence & routing** (1.3)
  `src/doc_agent/routing/score.py`: pure `score(data, report, model_signal)` and
  `route(score, report)` with hard-failure short-circuit. Add
  `tests/test_routing.py` (hard fail ⇒ review regardless of score; threshold
  boundary; missing required fields lower score).
  Check: `uv run pytest tests/test_routing.py -q`
  Commit: `phase 1.3: confidence scoring + routing decision`

- [ ] **N6 — Modality detection** (2.1)
  `src/doc_agent/parsing/detect.py`: map a file to `native_pdf | image` by
  extension/MIME. Add `tests/test_detect.py`.
  Check: `uv run pytest tests/test_detect.py -q`
  Commit: `phase 2.1: modality detection`

- [ ] **N7 — Backend interface + offline stub** (2.4 + stub)
  `src/doc_agent/backends/base.py`: the `ExtractionBackend` protocol,
  `BackendResult`, and a factory built from config.
  `src/doc_agent/backends/stub.py`: a `StubBackend` returning deterministic,
  schema-valid `Document` data with no network (this is the stub the smoke test
  and CLAUDE.md reference). **Do NOT implement `gemini.py` or `ollama.py`
  tonight.** Add `tests/test_backends.py` (factory returns configured backend;
  unknown backend ⇒ clear error; stub returns schema-valid data).
  Check: `uv run pytest tests/test_backends.py -q`
  Commit: `phase 2.4: backend interface + factory + offline stub backend`

- [ ] **N8 — Core pipeline orchestration** (3.1, stub-backed)
  `src/doc_agent/core.py`: `process_document(path) -> ExtractionResult` chaining
  detect → acquire → `backend.extract` → validate → score → route. Pure of
  side-effects (no file moves, no DB). For tonight, define the `acquire`
  interface and a minimal injectable path so the smoke test can supply a fake
  payload — **real Docling/OCR `acquire` is a TOMORROW task**. Define the
  `ExtractionResult` type here. Add `tests/test_core_smoke.py` running
  end-to-end with `StubBackend` + an injected payload (no network), asserting a
  populated result with a decision.
  Check: `uv run pytest tests/test_core_smoke.py -q`
  Commit: `phase 3.1: core pipeline orchestration (stub-backed smoke test)`

- [ ] **N9 — Idempotency helper** (3.2)
  A content-hash helper so the same file is not reprocessed across runs. Add
  `tests/test_hash.py` (same file → same hash).
  Check: `uv run pytest tests/test_hash.py -q`
  Commit: `phase 3.2: content-hash idempotency helper`

- [ ] **N-FINAL — Full green gate**
  Run the whole suite and linter; fix anything red until clean.
  Check: `uv run pytest -q && uv run ruff check .`
  Commit: `chore: full suite + lint green` (only if any fixes were needed)

---

## ⛔ STOP — END OF AUTONOMOUS SCOPE

Do not proceed past this line. Everything below requires API keys, a local
model server, large/finicky native dependencies, or live datasets — all for the
morning, supervised.

---

## TOMORROW (DO NOT START — supervised, needs setup)

- [ ] Add deferred deps: `docling`, (`paddleocr` or `pytesseract`),
  `google-genai`, `ollama`, `gradio`, `watchdog`.
- [ ] 2.2 Docling parser (downloads layout models on first run).
- [ ] 2.3 OCR path (PaddleOCR/Tesseract — watch the Paddle/3.11 wheel risk;
  fall back to pytesseract if it won't resolve).
- [ ] Wire the real Docling/OCR `acquire` into `core.py`.
- [ ] 2.5 Gemini backend — **needs `GEMINI_API_KEY`**.
- [ ] 2.6 Ollama backend — **needs a local Ollama server + pulled model**.
- [ ] 4.1 persistence (SQLite + CSV); 4.2 watcher; 4.3 Gradio web demo.
- [ ] 5 evaluation harness — needs datasets + a real backend; **counts against
  the Gemini free-tier daily limit**, so run deliberately.
- [ ] 6 deploy to Hugging Face Spaces.

---

## BLOCKED

(none yet — the loop appends one-line entries here)

## RUN LOG

(the loop may append short per-iteration notes here)
