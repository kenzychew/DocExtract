# Build Plan — Ordered Tasks for Claude Code

Execute phases in order. Each task lists what to build and its acceptance
criteria. Every phase ends in something runnable or testable. Keep the core
decoupled from entry points and backends throughout (see CLAUDE.md).

Legend: **[AC]** = acceptance criteria.

---

## Phase 0 — Scaffolding

**0.1 Project skeleton.** Create the repo layout from the setup doc and
initialize the project with **uv** (`uv init`). Pin the interpreter to 3.11
(`uv python pin 3.11`) and set `requires-python = ">=3.11"` in `pyproject.toml`.
Add dependencies via `uv add` (committing `uv.lock`); add `.env.example` and
`.gitignore` (ignore `data/`, `.env`, `.venv/`, caches — but **commit**
`uv.lock` and `.python-version`); add an empty `README.md`.
**[AC]** `.python-version` reads `3.11`; `uv sync` succeeds and
`uv run python -c "import sys, doc_agent; assert sys.version_info[:2]==(3,11)"`
passes.

**0.2 Config loader.** Implement `config.py` using pydantic-settings: load env,
validate combinations (gemini requires key; vision_direct requires multimodal
backend), fail fast with clear messages.
**[AC]** Unit test: invalid combos raise; valid config parses.

---

## Phase 1 — Contract & validation (pure, no model yet)

**1.1 Schema.** Implement `schema/models.py` (`Document`, `LineItem`) per the
data spec, with normalization helpers (money → float, date → ISO).
**[AC]** `test_schema.py`: valid dicts parse; malformed money/date handled.

**1.2 Validation rules.** Implement `validation/rules.py`: hard rules H1–H4,
soft rules S1–S4, monetary epsilon, returning a structured report.
**[AC]** `test_validation.py`: reconciling totals pass H2/H3; mismatches fail;
soft failures recorded without forcing review.

**1.3 Confidence & routing.** Implement `routing/score.py`: pure
`score(data, report, model_signal) -> float` and
`route(score, report) -> decision`, with hard-failure short-circuit.
**[AC]** `test_routing.py`: hard failure ⇒ review regardless of score;
threshold boundary behaves; missing required fields lower score.

These three modules are fully testable before any model exists.

---

## Phase 2 — Parsing & backends

**2.1 Modality detection.** `parsing/detect.py`: map file → `native_pdf` |
`image` by extension/MIME.
**[AC]** Correctly classifies the supported extensions.

**2.2 Docling parser.** `parsing/docling_parser.py`: native PDF → text/layout
payload; optionally retain page image.
**[AC]** A sample text PDF yields non-empty structured text.

**2.3 OCR path (optional strategy).** `parsing/ocr.py`: image → text via
PaddleOCR/Tesseract, behind the same payload interface.
**[AC]** A sample receipt image yields text; absence of the OCR engine degrades
gracefully with a clear error.

**2.4 Backend interface + factory.** `backends/base.py`: the
`ExtractionBackend` protocol, `BackendResult`, and a factory that builds the
backend from config.
**[AC]** Factory returns the configured backend; unknown backend ⇒ clear error.

**2.5 Gemini backend.** `backends/gemini.py`: multimodal call to the free tier;
text for native PDFs, image for scans/photos; schema-constrained JSON output;
bounded retries + timeout; model id from config.
**[AC]** Given a sample document, returns schema-valid JSON; transient errors
retry then route to review (don't crash).

**2.6 Ollama backend.** `backends/ollama.py`: local call with JSON-schema/grammar
constrained decoding; text-in (pairs with OCR path).
**[AC]** With a local model present, returns schema-valid JSON for a text
payload. Skipped/marked if no local server (documented).

---

## Phase 3 — Core pipeline

**3.1 Assemble core.** `core.py`: `process_document(path) -> ExtractionResult`
chaining detect → acquire → extract → validate → score → route. Pure of
side-effects (no file moves, no DB) — returns a result object.
**[AC]** `test_core_smoke.py`: runs end-to-end on a couple of sample files with
a stub backend; returns a populated result with a decision.

**3.2 Idempotency.** Content-hash helper so the same file isn't reprocessed.
**[AC]** Same file hashed identically across runs.

---

## Phase 4 — Entry points

**4.1 Persistence.** `store/db.py` (SQLite append of accepted records) and
`store/export.py` (CSV export).
**[AC]** Accepted record persists and appears in CSV; schema columns match.

**4.2 Watcher / batch runner.** `ingest/watcher.py`: watch (or poll) `inbox/`,
call core, persist accepted, move source to `processed/` or `review/`,
per-document try/except with structured logging.
**[AC]** Dropping a batch (mixed PDFs/scans/photos) processes all; one
deliberately corrupt file routes to review with a logged reason and does not
stop the loop.

**4.3 Web demo.** `web/app.py`: Gradio single-upload UI rendering fields,
per-field confidence, validation report, and decision; explicit
"synthetic/public only" notice; stateless.
**[AC]** Local Gradio URL processes one upload of each modality and displays a
correct, validated result.

---

## Phase 5 — Evaluation

**5.1 Dataset loaders.** `eval/datasets/`: scripts to fetch a held-out slice of
SROIE and the labelled invoice JSON set; map gold labels to the schema. No data
committed to git.
**[AC]** Loader yields (document, gold) pairs for a slice.

**5.2 Metrics harness.** `eval/run_eval.py`: run core over a slice; normalize;
compute per-field and per-critical-field precision/recall/F1; compute
auto-accept precision on critical fields; sweep threshold.
**[AC]** Prints a metrics table; produces the precision/recall trade-off across
thresholds; recommends a `CONFIDENCE_THRESHOLD`.

**5.3 Tune & record.** Set `CONFIDENCE_THRESHOLD` so auto-accept precision on
critical fields ≥ 0.98; record the resulting recall.
**[AC]** README contains a results table per dataset (precision/recall/F1 +
routing stats) — the portfolio evidence.

---

## Phase 6 — Deploy & document

**6.1 Hugging Face Space.** Add Space `app.py` and a `requirements.txt` for the
Gradio builder, generated from uv (`uv export --no-hashes --no-dev -o
requirements.txt`); set secrets (`GEMINI_API_KEY`, backend=gemini,
vision_direct). Deploy.
**[AC]** Public URL processes an uploaded document of each modality.

**6.2 README.** Quickstart (both modes), the swappable-backend explanation, the
results table, the demo URL, and the free-tier/privacy caveats.
**[AC]** A reader can run locally from scratch and understands the design and
the evidence.

---

## Build order rationale

Pure logic first (Phases 1) so the hard-to-test parts (validation, routing) are
locked down before any model variance enters. Parsing and backends next
(Phase 2), then the core that composes them (Phase 3). Entry points (Phase 4)
are thin glue added only once the core is proven. Evaluation (Phase 5) sets the
one tunable threshold with evidence. Deployment (Phase 6) is last and small
because the core was kept host-independent from the start.
