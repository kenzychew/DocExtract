---
title: Document Extraction Agent
short_description: Validate and auto-accept or review invoice/receipt fields
colorFrom: indigo
colorTo: blue
sdk: gradio
sdk_version: 6.19.0
python_version: "3.11"
app_file: app.py
pinned: false
---

# Document Extraction Agent

Drop invoices and receipts into a folder. The agent extracts structured
fields with an LLM, cross-checks the arithmetic, auto-accepts only what it
can verify, and routes everything else to human review. It runs unattended,
on free infrastructure, and treats the model as fallible by design.

**[Live Demo](https://huggingface.co/spaces/knzychw/document-extract-agent)**
(upload one PDF, scan, or photo) | **[Specs and architecture](docs/)** |
**[Backlog](PROGRESS_FUTURE.md)**

**Headline result:** on a 100-document held-out SROIE slice, every
auto-accepted `total` was correct -- **100% precision (18/18)**. The one
wrong total in the slice failed a line-item arithmetic check and went to
review instead of being written.

<!-- TODO: batch-mode GIF here -- drop a mixed folder into inbox/, watch
files sort themselves into processed/ and review/ with zero touches. -->

## Architecture

One reusable core, two thin entry points. The core computes and returns; the
entry points own all side effects.

```
                      +----------------------------------+
inbox/  (watcher) --->|           CORE PIPELINE          |---> SQLite (accepted)
                      |  detect -> acquire -> extract -> |
upload  (web demo) -->|  validate -> score -> route      |---> review/
                      +-----------------+----------------+
                                        |
                            Model backend (interface)
                            |-- Gemini (multimodal API)  -- implemented
                            |-- Ollama (local, offline)  -- planned
```

- **The watcher is the agent.** It wakes on new files, decides
  accept-or-escalate per document with no human in the loop, persists what it
  trusts, and quarantines the rest. One corrupt file never halts the run:
  every document runs inside its own try/except, and failures log and route
  to review.
- **The web demo is a window onto the same core.** One upload, one pass,
  result rendered, nothing stored.
- **The LLM is a replaceable part.** All model access goes through one
  `ExtractionBackend` interface, and the model name is config, not code.
  Gemini powers the hosted demo today; the interface is built for a local
  Ollama backend (offline, private), which is scaffolded in config but not
  yet implemented.
- **Validation gates acceptance.** Hard rules -- including
  `subtotal + tax ~= total` and line-item reconciliation -- force review on
  any failure, regardless of what the model says.

## Why I built this

A confidently-wrong total written to the books propagates silently. A
document sitting in a review queue is visible and recoverable. So the system
optimizes precision on the auto-accepted path and pays for it in review
volume -- an explicit, measured trade (18% auto-accepted at 100%
critical-field precision on SROIE).

What that took:

- **Arithmetic cross-checks as the acceptance gate.** The model's own
  confidence turned out to be unusable (see Challenges), so reconciliation
  rules carry the precision instead.
- **A decoupled core.** `core.py` imports nothing from the watcher, web, or
  storage layers, performs no I/O side effects, and is fully testable
  offline with a stub backend.
- **Crash-proof, idempotent ingestion.** Per-document isolation plus a
  content-hash UNIQUE constraint in SQLite: a crash-and-restart never
  double-writes a record and never loses a file.
- **A two-phase eval harness.** Inference runs once and is cached; every
  metric and the full threshold sweep replay offline from the cache.
  Re-tuning costs zero API calls.
- **Schema-enforced model output.** The extraction schema is a Pydantic
  model that doubles as the API's constrained-output schema. No JSON is ever
  regexed out of free text.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Contract and validation | Python 3.11, Pydantic v2 | One schema defines the data contract, validates model output, and constrains the API's JSON generation. |
| Model access | google-genai (Gemini, vision-direct) | Multimodal free tier reads receipt photos directly; no OCR stage needed for the demo path. |
| PDF parsing | Docling (OCR disabled) | Native PDFs carry embedded text; layout-aware parsing without the OCR model stack. |
| Entry points | watchdog (folder agent), Gradio (demo) | Filesystem events for autonomy; a stateless UI for inspection. |
| Storage | stdlib sqlite3 + csv | Append-only records with an idempotency constraint; no server, no ORM. |
| Tooling | uv, pytest, ruff | Locked reproducible installs; 218 offline tests; lint kept at zero. |

## Technical challenges

**Model confidence is a mirage on the free tier.**
The eval exposed that Gemini's free tier returns no per-field confidence, so
the pipeline's confidence score falls back to a neutral 0.5 prior and can
only be penalized downward -- every clean document scores exactly 0.50 and
the accept threshold becomes a binary switch. The fix was architectural, not
numeric: arithmetic cross-checks (H2: `subtotal + tax ~= total`, H3: line
items sum to the subtotal) gate acceptance instead. On the eval slice, 74 of
100 documents failed a hard rule; of the 77 that scored 0.50, the 59
hard-failures were forced to review, and all 18 documents that survived both
gates had correct totals.

**An adversarial review caught a metric-inflating bug before the eval ran.**
The eval's money comparison originally reused the pipeline's reconciliation
tolerance, which includes a 0.5% relative term -- meaning a $2-wrong total on
a $500 receipt would have scored as "correct" and silently inflated the
headline precision. Independent review agents flagged it; the comparison is
now cent-exact with a named regression test
(`test_money_rejects_relative_tolerance_error`).

**Hugging Face Spaces created a dependency deadlock.**
The Space build force-installs `gradio[oauth,mcp]`, whose `mcp` extra caps
`pydantic<=2.12.5`, while `google-genai` requires `>=2.12.5`. Exactly one
version satisfies both. Resolving the platform's full install set locally
with `uv pip compile` found it; `requirements.txt` pins `pydantic==2.12.5`
with the reasoning documented inline.

**Half the project was built by an autonomous loop overnight.**
The work was split by risk. A spec package came first: requirements,
architecture, data spec, and a phased build plan with per-task acceptance
criteria (`docs/`), plus a `CLAUDE.md` encoding the architectural rules. The
deterministic core -- schema, validation, routing, backend interface, stub
pipeline -- landed as 19 commits in one unattended overnight run
(`PROGRESS.md` ledger, `run-overnight.ps1` harness), each task proven by its
acceptance check before commit. The model-touching half was built
interactively (`PROGRESS_TOMORROW.md`), with every extraction verified on
real documents, because a plausible-looking wrong total passes a smoke test.

## Evaluation

Two-phase harness in `eval/`: `predict` runs the pipeline over a held-out
slice and caches every result (the only phase that touches the model);
`score` computes all metrics and the threshold sweep offline from that
cache.

### SROIE (ICDAR 2019), 100-document held-out test slice

Gemini `gemini-2.5-flash`, vision-direct. Predicted and gold values are
normalized before comparison: money cent-exact, dates on ISO equality, text
case- and whitespace-insensitive. SROIE labels four schema fields; of the
three critical fields (`total`, `tax`, `invoice_number`) it labels only
`total`.

| Field | Precision | Recall | F1 |
|---|---|---|---|
| `total` (critical) | **99.0%** | 99.0% | 99.0% |
| `vendor_name` | 86.0% | 86.0% | 86.0% |
| `document_date` | 81.0% | 98.8% | 89.0% |
| `vendor_address` | 53.0% | 53.0% | 53.0% |

Routing at `CONFIDENCE_THRESHOLD = 0.50`:

| Metric | Value |
|---|---|
| Auto-accepted | 18 / 100 (18%) |
| Auto-accept precision on `total` | **100% (18 / 18)** |
| Routed to review | 82 / 100 |

Reading these numbers:

- The precision comes from arithmetic, not model self-assessment. The one
  wrong total in the slice (99/100 correct overall) failed line-item
  reconciliation and went to review; it never reached the accepted set.
- Low `vendor_address` accuracy is expected and absorbed by design: noisy
  free-form fields surface in the review queue rather than being trusted.
- This slice labels `total` only. The CORD adapter (`tax`, line items) and
  an invoice-JSON adapter (`invoice_number`) are scaffolded next steps --
  tracked in [`PROGRESS_FUTURE.md`](PROGRESS_FUTURE.md).
- Honest limit: arithmetic consistency correlates with correctness but does
  not prove it. A document whose numbers are all wrong yet mutually
  consistent would pass. On this slice the filter yielded 18/18.

Reproduce:

```bash
# Phase 1 -- runs the model over a slice (spends free-tier quota); idempotent.
uv run python -m eval.run_eval predict --dataset sroie --limit 100

# Phase 2 -- offline; recomputes metrics and the threshold sweep from the cache.
uv run python -m eval.run_eval score --dataset sroie
```

## Getting started

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # create the venv and install from uv.lock
cp .env.example .env    # add your Gemini key (free, from Google AI Studio)
```

Run the web demo (single upload, result rendered, nothing stored):

```bash
uv run python -m doc_agent.web.app
```

Run the autonomous watcher (drop files into `data/inbox/`; accepted records
land in SQLite, accepted files move to `data/processed/`, everything else to
`data/review/`; CSV export is a separate step over the accumulated records):

```bash
uv run python -m doc_agent.ingest.watcher
```

Run the tests (218 tests, fully offline -- no API key needed):

```bash
uv run pytest -q
```

The implemented backend is Gemini (`EXTRACTION_BACKEND=gemini`). A local
Ollama backend with an OCR path -- for fully offline, private runs -- is
scaffolded in config but not yet built
([`PROGRESS_FUTURE.md`](PROGRESS_FUTURE.md)).

## Caveats

- The hosted demo runs on the Gemini free tier, which may use inputs for
  training. **Synthetic or public documents only** -- never real financial
  data. Fully private local processing is planned, not yet implemented.
- The free Space sleeps when idle; the first request after a quiet period is
  a cold start, and the first PDF triggers a one-time parser model download.
