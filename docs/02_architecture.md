# Technical Architecture — Document Extraction Agent

## 1. Guiding principle

One reusable core; thin replaceable edges. The core is a pure-ish function:

```
process_document(path) -> ExtractionResult
```

Everything that triggers it (folder watcher, web upload) and everything it
depends on (the model backend) sits behind interfaces so the core never knows
which entry point invoked it or which model produced the fields. This is what
keeps the project portable across local and cloud, and it is the main thing
that makes the design read as engineering rather than prompting.

## 2. System overview

```
                         ┌─────────────────────────────┐
   inbox/ (watcher) ─────▶                             │
                         │        CORE PIPELINE         │────▶ SQLite + CSV
   web upload (demo) ────▶   parse → extract → validate │      (batch mode)
                         │   → score → route            │
                         │                             │────▶ review/ (batch)
                         └───────────────┬─────────────┘
                                         │
                                  Model Backend (interface)
                                  ├── GeminiBackend  (free API, multimodal)
                                  └── OllamaBackend   (local, offline)
```

In batch mode the pipeline persists and moves files. In demo mode the same
pipeline returns a result that is rendered and then discarded.

## 3. Core pipeline stages

1. **Intake & type detection.** Identify modality from extension/MIME:
   `native_pdf`, `image` (scan or photo). This determines the parse path.
2. **Text/representation acquisition.**
   - `native_pdf`: parse with Docling to get text + layout. Optionally also
     keep the page image for backends that prefer vision.
   - `image`: two supported strategies, backend-dependent —
     (a) *vision-direct*: pass the image to a multimodal backend (Gemini),
     no separate OCR; (b) *ocr-then-text*: run OCR (e.g. PaddleOCR/Tesseract)
     to text, then pass text to a text-only backend (Ollama small models).
3. **Extraction.** Call `backend.extract(document_payload, schema)` and get
   back a structured object plus whatever confidence signal the backend
   exposes. Structured output is enforced (JSON schema / grammar), not parsed
   out of free text.
4. **Validation.** Run the rule set (types, formats, required fields,
   arithmetic cross-checks). Produce a per-field validation status and a list
   of failures.
5. **Confidence scoring.** Combine model signal, validation outcome, and
   required-field completeness into a single document-level score.
6. **Routing.** Compare score to threshold and apply hard-fail overrides
   (a failed critical cross-check forces review regardless of score). Emit an
   `ExtractionResult` with `decision ∈ {accept, review}`.

## 4. Components and responsibilities

- **Watcher / runner** (`ingest/`): detects new files, calls the core,
  performs file moves and persistence side-effects in batch mode. Owns retry
  and isolation so one document never halts the loop.
- **Parser** (`parsing/`): modality detection and text/layout acquisition.
  Wraps Docling and the OCR option behind a single `acquire(path) -> Payload`.
- **Backend interface** (`backends/base.py`): defines `extract()`. Concrete
  adapters: `gemini.py`, `ollama.py`. Selected by config via a factory.
- **Schema & models** (`schema/`): the Pydantic models that define the output
  contract and are used to enforce structured output and to validate types.
- **Validation** (`validation/`): pure functions over the parsed object →
  validation report. No I/O.
- **Confidence & routing** (`routing/`): pure functions → score and decision.
- **Persistence** (`store/`): SQLite writer + CSV exporter. Batch mode only.
- **Web demo** (`web/`): a Gradio app that uploads one file, calls the core,
  and renders the result. No persistence.
- **Eval** (`eval/`): runs the core over a labelled dataset and computes
  metrics.

## 5. Model backend abstraction

```python
class ExtractionBackend(Protocol):
    name: str
    def extract(self, payload: DocumentPayload, schema: type[BaseModel]) -> BackendResult: ...

# BackendResult: { data: dict, field_confidence: dict | None, raw: Any }
```

- **GeminiBackend.** Calls the Gemini free tier. Multimodal: accepts the page
  image directly for scans/photos and text for native PDFs. Requests
  schema-constrained JSON output. Used by the cloud demo (CPU host can't run a
  local model) and available locally.
- **OllamaBackend.** Calls a local Ollama server (e.g. a 3B–7B model). Text-in
  only, so images must go through the OCR path first. Uses grammar/JSON-schema
  constrained decoding for reliable structure. Used for offline, private, and
  no-quota local runs.

**Backend rule:** never hardcode a single remote model *name* in logic — model
identifiers are config, because free catalogs change without notice. Treat a
missing/renamed model as a recoverable config error.

## 6. Dual entry points

- **Watcher (autonomous).** A long-running process using a filesystem watcher
  (or a poll loop for portability). On a new file: `process_document()`, then
  persist + move. This is the "runs on its own" capability.
- **Web demo (URL).** A Gradio interface with a single upload control. On
  upload: `process_document()`, render fields + confidence + validation +
  decision, then discard. Carries an explicit "synthetic/public documents
  only" notice (see NFR-2).

Both are ~50–100 lines of glue. All real logic lives in the core.

## 7. Data flow for one document

```
file ─▶ detect modality ─▶ acquire payload (Docling | OCR | raw image)
     ─▶ backend.extract(payload, schema) ─▶ raw structured data
     ─▶ validate(data) ─▶ validation report
     ─▶ score(data, report, model_signal) ─▶ confidence
     ─▶ route(confidence, report) ─▶ {accept | review}
     ─▶ [batch] persist + move file   /   [demo] render + discard
```

## 8. Confidence and routing logic

Document confidence blends three inputs:

- **Model signal** — backend-exposed token/field confidence where available;
  otherwise treated as neutral.
- **Validation** — a hard-failed critical cross-check (e.g. totals don't
  reconcile) forces `review` regardless of score. Soft failures reduce score.
- **Completeness** — missing required fields reduce score.

Routing:

```
if any(critical_hard_failures): decision = review
elif confidence >= THRESHOLD:    decision = accept
else:                            decision = review
```

`THRESHOLD` is a single tunable constant. The evaluation harness exists
precisely to set it (see data spec — optimize auto-accept precision on
critical fields, accept the resulting recall, and report both).

## 9. Error handling

- Per-document `try/except` in the runner; failures log full context and route
  to `review/` with a reason. The loop continues.
- Backend calls are wrapped with bounded retries and timeouts; exhausted
  retries route to review, they do not crash.
- Idempotency: a content hash prevents reprocessing the same file twice across
  restarts.

## 10. Technology choices (and why)

- **Python** — ecosystem fit for parsing/ML.
- **Docling** — open-source, free, structured PDF/scan parsing with layout and
  tables, agent-framework friendly.
- **Pydantic** — the output contract doubles as a validation and
  structured-output schema.
- **Gradio** — minimal code to a public demo UI; native to Hugging Face Spaces.
- **SQLite + CSV** — zero-infra local persistence and a portable export.
- **Gemini free tier + local Ollama** — two free inference paths covering
  cloud-demo and offline/private use behind one interface.

## 11. Deployment topology

- **Local / batch:** watcher + core + (Gemini or Ollama) + SQLite/CSV on the
  operator's machine. Fully free; Ollama gives no quotas and full privacy.
- **Cloud / demo:** Gradio app + core + Gemini backend on Hugging Face Spaces
  (free, CPU-only, sleeps when idle). No local model in the cloud; no
  persistence. Public URL for the portfolio.

See the setup doc for concrete structure, configuration, and deployment steps.
