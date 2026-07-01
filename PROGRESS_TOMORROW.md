# PROGRESS_TOMORROW.md — Supervised Build Ledger

The second half of the build: real models, real parsing, the entry points,
evaluation, and deployment. Everything past the `⛔ STOP` line in `PROGRESS.md`.

**This half is interactive, not looped.** You drive it in a normal `claude`
session, one task at a time, and verify each task on a real document before
moving on. There is no `DONE_ALL` and no `run-overnight.ps1` here — these tasks
call real models (non-deterministic), need API keys and a local server, and
have decisions only you should make. Leaving them to an unattended loop would
burn quota on work you can't blindly trust.

## Protocol (per task)

1. Prompt Claude Code for the **next unchecked task**, one at a time.
2. Let it implement, then **review the diff** and **eyeball the result on a real
   sample document** — not just that tests pass. Model output that looks
   plausible can still be wrong; you are the check.
3. When satisfied, commit (one commit per task) and tick the box.
4. Add dependencies **per task, not all at once** (see policy below), so an
   install failure is isolated to the task that needs it.
5. Default to **Sonnet** (`/model sonnet`). These are routine integration tasks;
   Opus is not needed and costs ~3x.

## Setup before T1

- **Gemini key:** get a free API key from Google AI Studio. Put it in `.env` as
  `GEMINI_API_KEY=...` — this is the key for *the app's* calls to Gemini, and is
  completely separate from your Claude Code login. Do **not** set
  `ANTHROPIC_API_KEY` (that would silently bill your Claude Code usage to the
  API).
- **Sample documents:** have one real **receipt photo**, one **scan**, and one
  **native-text PDF invoice** on hand. You'll use them to eyeball real
  extractions (synthetic/public docs only — the free tier may train on inputs).
- **Branch:** lock in last night's milestone and start fresh (see the chat
  instructions accompanying this file).

## Dependency policy

Add only what each task needs, when it needs it:
`google-genai` (T2) · `docling` (T3) · `paddleocr` **or** `pytesseract` (T4) ·
`ollama` (T6) · `watchdog` (T8) · `gradio` (T9). This isolates the
Paddle-on-3.11 risk to T4 instead of breaking everything at once.

---

## TASKS

- [x] **T1 — Gemini config + key** (prep)
  Add `uv add google-genai`. In `.env` set `GEMINI_API_KEY`,
  `EXTRACTION_BACKEND=gemini`, `IMAGE_STRATEGY=vision_direct`, and
  `GEMINI_MODEL` per `docs/04_project_setup.md`. No new code beyond confirming
  `config.py` accepts it.
  Check: `uv run python -c "from doc_agent.config import load_config; c=load_config(); print(c.extraction_backend, bool(c.gemini_api_key))"` → `gemini True`.

- [x] **T2 — Gemini backend + real image acquire** (build plan 2.5) ⭐ milestone
  Implement `src/doc_agent/backends/gemini.py` per architecture §5: multimodal
  call, schema-constrained JSON output, bounded retries + timeout, model id from
  config; register its builder in the factory. Wire a minimal real `acquire` for
  images in `vision_direct` mode (load image bytes into the payload) so
  `process_document` runs end-to-end on a photo/scan with no Docling/OCR yet.
  Tests: `tests/test_gemini.py` with a **mocked** Gemini response (deterministic,
  no network/quota) covering schema-valid parsing, retry, and timeout.
  Check (auto): `uv run pytest tests/test_gemini.py -q`.
  Check (manual): run `process_document` on your real **receipt photo** and
  confirm `vendor_name`, `total`, `document_date` are actually correct.
  *This is where the project stops being scaffolding.*

- [x] **T3 — Docling parser** (2.2)
  `uv add docling` (first run downloads layout models — needs internet).
  `src/doc_agent/parsing/docling_parser.py`: native PDF → text/layout payload;
  wire into `acquire` for `native_pdf`.
  Check (manual): `process_document` on your **PDF invoice** via Gemini returns
  correct fields.

- [ ] **T4 — OCR path** (2.3) — *needed for Ollama / `ocr_then_text`; deferrable*
  `src/doc_agent/parsing/ocr.py`: image → text behind the payload interface;
  wire into `acquire` for `IMAGE_STRATEGY=ocr_then_text`.
  **DECISION:** try `uv add paddleocr`; if it won't resolve on 3.11, fall back to
  `uv add pytesseract` (and install the Tesseract binary).
  Check: a sample image yields text; `process_document` works in
  `ocr_then_text` mode. *With `vision_direct` + Gemini you don't strictly need
  this for the demo — you can skip it now and return before adding Ollama.*

- [x] **T5 — Consolidate real `acquire` into core**
  Make `core.py`'s default `acquire` do the real thing by modality + strategy
  (`native_pdf`→Docling; `image`+`vision_direct`→bytes;
  `image`+`ocr_then_text`→OCR), so `process_document(path)` works on a real path
  for every input without injection. Keep the injectable seam for tests.
  Check (auto): `uv run pytest -q` (full suite still green; smoke tests still use
  the stub). Check (manual): one of **each** input type returns correct fields.

- [ ] **T6 — Ollama backend** (2.6) — *OPTIONAL: offline/private; skip to ship faster*
  `uv add` the ollama client; needs a local Ollama server + a pulled model
  (e.g. `ollama pull qwen2.5:7b`). `src/doc_agent/backends/ollama.py`: local call
  with JSON-schema/grammar-constrained decoding; text-in (pairs with the OCR
  path). Tests: mocked unit tests + a manual smoke against the local server.
  Check: with `EXTRACTION_BACKEND=ollama` + `IMAGE_STRATEGY=ocr_then_text`,
  `process_document` returns schema-valid data.

- [x] **T7 — Persistence** (4.1)
  `src/doc_agent/store/db.py` (SQLite append of accepted records) and
  `store/export.py` (CSV export). Add `tests/test_store.py`.
  Check: `uv run pytest tests/test_store.py -q`; an accepted record persists and
  appears in the CSV.

- [x] **T8 — Watcher / batch runner** (4.2)
  `uv add watchdog`. `src/doc_agent/ingest/watcher.py`: watch (or poll)
  `inbox/`, call core, persist accepted, move source to `processed/` or
  `review/`, per-document try/except + structured logging.
  Check: drop a **mixed batch** (PDF + scan + photo + one deliberately corrupt
  file) into `data/inbox/`; all valid ones process, the corrupt one routes to
  `review/` with a logged reason, and the loop does not stop.

- [x] **T9 — Web demo** (4.3)
  `uv add gradio`. `src/doc_agent/web/app.py`: single-upload UI rendering fields,
  per-field confidence, the validation report, and the decision; a
  "synthetic/public documents only" notice; stateless.
  Check: `uv run python -m doc_agent.web.app`; upload one of each modality and
  confirm a correct, validated result is displayed.

- [ ] **T10 — Evaluation harness** (5)
  `eval/datasets/` loaders for a held-out **SROIE** slice + the labelled invoice
  JSON set, mapping gold labels to the schema. `eval/run_eval.py`: run core over
  a slice, normalize, compute per-field and per-critical-field
  precision/recall/F1, auto-accept precision on critical fields, and sweep the
  threshold.
  **DECISION:** *you* pick `CONFIDENCE_THRESHOLD` so auto-accept precision on
  `total`/`tax`/`invoice_number` ≥ 0.98; record the resulting recall.
  **COST:** this calls the model over many documents and counts against the
  Gemini free-tier daily limit — run a **small slice first**, deliberately.
  Check: `run_eval` prints the metrics table; you set the threshold; the results
  table goes into the README.

- [ ] **T11 — Deploy** (6)
  Create a Hugging Face **Space** (Gradio SDK, free), `python_version: "3.11"`,
  `requirements.txt` via `uv export --no-hashes --no-dev -o requirements.txt`,
  secrets (`GEMINI_API_KEY`, `EXTRACTION_BACKEND=gemini`,
  `IMAGE_STRATEGY=vision_direct`). Write the README: quickstart (both modes), the
  swappable-backend note, the results table, the demo URL, and the
  free-tier/privacy caveats.
  Check: the public URL processes an uploaded document of each modality.

---

## Phase done when

A real document of each input type flows end-to-end through a live model into a
validated record; the watcher processes a mixed batch unattended and routes
exceptions to review; the eval harness has produced a precision/recall table and
set the threshold; and the demo is live at a public URL. T4 and T6 are optional
and can be revisited after the demo ships.
