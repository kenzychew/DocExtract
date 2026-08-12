---
title: DocExtract
short_description: Document Field Extraction With Arithmetic Validation
colorFrom: indigo
colorTo: blue
sdk: gradio
sdk_version: 6.19.0
python_version: "3.11"
app_file: app.py
pinned: false
---

# DocExtract

Key-value field extraction from business documents.
A document goes in; a validated, structured record comes out, tagged either `accept` or `review`.
The extraction step is an LLM call against a fixed schema; the accept-or-review decision is made after it by deterministic arithmetic rules, because a probabilistic model should not decide when to trust itself.

**[Live Demo](https://huggingface.co/spaces/knzychw/docfield_extract)**
(upload one PDF, scan, or photo) | **[Specs and architecture](docs/)** |
**[Backlog](PROGRESS_FUTURE.md)**

Receipts are the benchmark, not the scope.
The schema is general business-document fields - vendor name and address, document date, due date, invoice number, currency, subtotal, tax, total, and line items - and the evaluation uses a public receipt corpus because that is where real human labels exist.

## What this is, in IDP terms

This does **key-value extraction**: named fields that sit at arbitrary positions, with layout differing from document to document.
It is not **table extraction**, which recovers repeating rows sharing one column schema.

The distinction matters because the two need different machinery.
Key-value extraction can be posed as "fill in this fixed schema"; table extraction needs row segmentation and column alignment, and its accuracy is measured per cell rather than per field.

The schema does contain a `line_items` array, and the model populates it opportunistically, but that is not table extraction.
There is no row segmentation and no column-schema inference, and line items carry no ground truth in the evaluation below.
Treat them as unvalidated extras.
Table extraction as a real mode is a possible future direction, not a current capability.

## Evaluation

The evaluation harness is the part of this project worth reading first.

Structure: two phases, in `eval/`.
`predict` runs the pipeline over a held-out slice and caches gold labels, the predicted record, the confidence score, and the full validation report per document.
It is the only phase that calls a model, and it is idempotent - already-cached ids are skipped, so an interrupted run resumes instead of re-billing.
`score` reads that cache and computes every metric offline.
Re-scoring, re-tuning, and sweeping the threshold cost zero API calls and can be repeated indefinitely.

The threshold sweep replays the real production router, imported directly from the routing module rather than reimplemented for evaluation.
A hard-rule failure is therefore forced to review at every threshold in the sweep, exactly as it would be in production.

### SROIE (ICDAR 2019), full 361-document test split

Real human-annotated labels from the ICDAR 2019 SROIE competition, `test` split, streamed from the `jsdnrs/ICDAR2019-SROIE` mirror.
Backend was Gemini in vision-direct mode (`GEMINI_MODEL=gemini-2.5-flash`), so every document went image-bytes-to-model with no OCR stage.
Predicted and gold values are normalized before comparison: money cent-exact, dates on ISO equality, text case- and whitespace-insensitive.

The split is reported in two parts, never combined.
The **tuning** slice is the first 100 documents cached; the auto-accept threshold was chosen against them and the S2 rule mismatch (below) was diagnosed from them, so metrics over those documents are in-sample.
The remaining 261 are **held out** and played no part in either.
Membership is pinned by id in `eval/splits/sroie_tuning.json` rather than by position, so the split cannot drift when the cache is regenerated.

**Held-out is the number that means anything. Tuning is shown for comparison, not as a result.**

| | tuning (100) | **held-out (261)** |
|---|---|---|
| Auto-accepted | 22 (22.0%) | **94 (36.0%)** |
| Auto-accept precision on `total` | 100% (22 / 22) | **97.9% (92 / 94)** |
| Auto-accept recall on `total` | 22.0% | **35.2%** |
| `total` P / R / F1 | 99.0 / 99.0 / 99.0 | **98.5 / 98.5 / 98.5** |
| `vendor_name` P / R / F1 | 86.0 / 86.0 / 86.0 | 85.1 / 85.1 / 85.1 |
| `document_date` P / R / F1 | 81.0 / 98.8 / 89.0 | 76.2 / 99.0 / 86.1 |
| `vendor_address` P / R / F1 | 53.0 / 53.0 / 53.0 | 48.7 / 48.7 / 48.7 |

Reading these numbers:

- **The tuning slice overstates the accepted path.** 100% on 22 documents did not survive contact with held-out data, where two documents were auto-accepted with a wrong `total`. That gap is the reason the split exists.
- **Precision on the accepted path comes from arithmetic, not model self-assessment.** Documents failing a hard rule are forced to review regardless of score.
- **Both false accepts are written up.** `X51005806696` cleared the reconciliation checks with a total of 7.65 against a gold of 7.20; `X51007846355` produced a record where the line items, subtotal and total all agree at 8.96 against a gold of 8.95, so no cross-check over the extracted values could have separated it from a correct document. Full analysis, including a correction to the first explanation offered for the former, is in [`eval/FINDINGS.md`](eval/FINDINGS.md).
- **Low `vendor_address` accuracy is expected and absorbed by design.** Noisy free-form fields surface in the review queue rather than being trusted.
- **The trade is explicit and one-directional:** 36% auto-accepted, and the other 64% becomes review-queue volume.

Reproduce:

```bash
# Phase 1 -- runs the model (spends free-tier quota); idempotent.
uv run python -m eval.run_eval predict --dataset sroie --limit 400

# Phase 2 -- offline; recomputes metrics and the threshold sweep from the cache.
uv run python -m eval.run_eval score --dataset sroie --split heldout --revalidate
uv run python -m eval.run_eval score --dataset sroie --split tuning  --revalidate
```

`--revalidate` recomputes validation and confidence from the cached model output under current rules, instead of the values frozen in at predict time.
Without it a rule change since the last predict run is invisible to the report; with it, the same run also reports how far the cache has drifted from the code.

## Limitations of the evaluation

These bound what the numbers above can be said to show.

- **SROIE labels only four fields.**
  Of the three fields the code declares critical (`total`, `tax`, `invoice_number`), this dataset provides ground truth for `total` only.
  **`tax` and `invoice_number` are entirely unscored.**
  No claim about their accuracy is supported by anything in this repository.
- **Line items are unscored.**
  SROIE does not label them.
  The model emits them and validation reconciles them against the subtotal, but their correctness has never been measured.
- **A one-cent misread counts the same as a materially wrong total.**
  Comparison against gold is cent-exact, deliberately, so that no relative
  tolerance can score a badly wrong number as correct.
  The cost is that the counts conflate severities.
  Across all 361 documents there are 5 disagreements on `total`, and 4 of them
  are within 5 sen and consistent with Malaysian cash rounding; only one
  (7.65 against 7.20) is materially wrong.
  The same applies on the accepted path: of the two false accepts on held-out,
  one is a one-cent difference, so the **materially-wrong auto-accept rate is
  1 in 94**, against a measured 97.9%.
  Read the precision figures as "exactly right to the cent", not as "close
  enough to use".
- **Confidence scoring is currently vestigial.**
  The Gemini backend returns `field_confidence=None` unconditionally, so the scorer falls back to a neutral 0.50 prior that can only be penalized downward.
  Every clean document scores exactly 0.50 and the threshold is effectively a binary gate rather than a tunable dial: on held-out, 245 of 261 documents scored 0.50 and the remaining 16 scored 0.40.
  Sweeping the threshold above 0.50 accepts nothing at all.
  The precision on the accepted path is delivered by the arithmetic validation rules.
  It is not delivered by any model confidence signal, because there is not one.
- **No provenance.**
  An extracted value cannot be traced back to a page, bounding box, cell, or character span.
  The intermediate text representation is not retained, and for the image path no text representation is produced at all.
  If a number is wrong, the only available context is which file it came from.
- **The PDF path is not covered by the evaluation or by automated tests.**
  All 361 evaluation documents took the image path.
  The Docling PDF branch works and has been verified by hand on real documents, but no test exercises it and no measured number depends on it.
- **The cache does not record which model produced each prediction.**
  It stores the backend name but not the model identifier, so the claim that
  every document was extracted by the same model rests on the configuration
  in force at run time, not on recorded evidence.
  Entries predicted at different times could in principle carry different
  models with nothing in the data to show it.

## Architecture

One reusable core, two thin entry points.
The core computes and returns; the entry points own all side effects.

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

It is a pipeline, not an agent.
Six stages run in a fixed order, and five of them are plain code.
There is no tool loop, no planner, and no autonomous decision-making anywhere in the system.
The LLM appears in exactly one stage: a single API call that fills in the fields of a fixed schema.
Its output is data, never control flow - it cannot call tools, retry itself, skip a stage, or influence what happens next.

The three parts worth naming:

- **A modality router.**
  Classifies the input as native PDF or image from its extension, which selects the acquisition path: Docling layout-aware parsing to Markdown for PDFs, raw bytes straight to a multimodal model for images.
  The alternative OCR-then-text path for images is declared in config but not implemented; selecting it raises immediately with an explanatory error rather than silently degrading.
- **An LLM extraction step.**
  One call, schema-constrained.
  The Pydantic model that defines the data contract is also the JSON schema handed to the API, so output shape is enforced at generation time and no JSON is ever regexed out of free text.
  Shape is enforced; values are not - the model can return any number that type-checks, which is precisely why the next stage exists.
- **Rule-based validation.**
  Hard rules (`subtotal + tax ~= total`, line items sum to the subtotal, total present and non-negative, critical fields correctly typed) force review on any failure regardless of what the model reported.
  Soft rules (missing vendor, implausible date, unknown currency, per-line arithmetic drift) reduce the score without forcing review.
  Both rule sets are pure functions with no I/O and are unit-tested independently of the pipeline.

Supporting properties: the watcher runs each document inside its own try/except so one corrupt file never halts a batch; a content-hash UNIQUE constraint makes ingestion idempotent across crash-and-restart; all model access goes through a single `ExtractionBackend` interface with the model identifier read from config rather than hardcoded.

## Why I built this

I wanted a project that demonstrates the engineering *around* a model rather than the model itself - the trust decision, not the extraction - and a testbed for building software against a frozen spec package.
Document field extraction was the right problem because the failure mode is so concrete.
A confidently-wrong total written to the books propagates silently, while a document sitting in a review queue is visible and recoverable.
So the system optimizes precision on the auto-accepted path and pays for it in review volume, as an explicit and measured trade.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Contract and validation | Python 3.11, Pydantic v2 | One schema defines the data contract, validates model output, and constrains the API's JSON generation. |
| Model access | google-genai (Gemini, vision-direct) | Multimodal free tier reads receipt photos directly; no OCR stage needed for the demo path. |
| PDF parsing | Docling (OCR disabled) | Native PDFs carry embedded text; layout-aware parsing without the OCR model stack. |
| Entry points | watchdog (folder watcher), Gradio (demo) | Filesystem events for unattended batch runs; a stateless UI for inspection. |
| Storage | stdlib sqlite3 + csv | Append-only records with an idempotency constraint; no server, no ORM. |
| Tooling | uv, pytest, ruff | Locked reproducible installs; 218 offline tests; lint kept at zero. |

## Technical challenges

**Model confidence is a mirage on the free tier.**
The evaluation exposed that Gemini's free tier returns no per-field confidence, which collapses the confidence score into a constant and makes the accept threshold a binary gate.
The fix was architectural rather than numeric: arithmetic cross-checks gate acceptance instead.
This is a workaround, not a solution - see the roadmap below for what an actual confidence signal would require.

**Hugging Face Spaces created a dependency deadlock.**
The Space build force-installs `gradio[oauth,mcp]`, whose `mcp` extra caps `pydantic<=2.12.5`, while `google-genai` requires `>=2.12.5`.
Exactly one version satisfies both.
Resolving the platform's full install set locally with `uv pip compile` found it; `requirements.txt` pins `pydantic==2.12.5` with the reasoning documented inline.

## What the autonomous build got wrong

The overnight loop produced working, tested code. What it did not produce was
correct judgement about measurement, and every instance was caught later by a
human or an adversarial review rather than by the tests it wrote for itself.

- **The evaluation reused the pipeline's own monetary tolerance.** That tolerance
  includes a 0.5% relative term, so a $2 error on a $500 total would have scored
  as correct and inflated the headline precision. Caught by an independent
  review pass before the first real evaluation ran; the comparator is now
  cent-exact with a named regression test.
- **The same fix was applied to the measurement side but not the decision side.**
  For some time afterwards the eval measured cent-exactly while H2 and H3 -- the
  rules that actually gate auto-accept -- still granted 0.5% of the document
  value. At $100,000 that is $500 of slack. Found only when the first false
  accept prompted a direct check of the two code paths against each other.
- **A validation rule contradicted the policy stated in its own module.** S2
  failed any document that stated no currency code, while the docstring at the
  top of the same module declared that absent inputs are skipped rather than
  failed, naming one deliberate exception. S2 was an undeclared second one. It
  penalised the 9% of documents that print no currency for a property that says
  nothing about whether their arithmetic is right; fixing it recovered four
  auto-accepts on each slice with no loss of precision.
- **The threshold was fitted and reported on the same 100 documents.** No
  held-out split existed until the test split was expanded; the original
  headline was in-sample and not labelled as such.

The pattern is consistent: the loop was reliable at building what it was asked
to build and unreliable at noticing when the thing being measured was not the
thing that mattered. The tests it wrote all passed throughout.

## Known gaps / roadmap

Listed as gaps because none of this exists yet.

- **Post-hoc anchoring of extracted values to source spans.**
  Retain the intermediate representation with geometry preserved, then match each emitted value back to a token span in the source, so any output can be traced to a page and box.
  This is the prerequisite for the next item.
- **Real per-field confidence derived from anchor rate.**
  A value that cannot be located anywhere in the source document is a strong wrongness signal.
  Anchoring would supply a genuine, model-independent confidence input and turn the currently vestigial threshold into a real dial.
- **An OCR path to replace the unimplemented branch.**
  The `ocr_then_text` image strategy is declared in config but raises `NotImplementedError`.
  Implementing it is what unblocks the text-only local Ollama backend, and with it fully offline and private processing.
- **Gold labels for the two unscored critical fields.**
  `tax` and `invoice_number` have no ground truth in the current dataset.
  A CORD adapter (`tax`, line items) and an invoice-JSON adapter (`invoice_number`) are scaffolded but not wired.
- **Record the model identifier in the evaluation cache.**
  The cache stores the backend name but not the model version, so the exact model behind a cached run is not recoverable from the cache itself.
- **Test coverage and measurement for the PDF path.**
  It is currently hand-verified only.

## Getting started

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # create the venv and install from uv.lock
cp .env.example .env    # add your Gemini key (free, from Google AI Studio)
```

Run the web demo (single upload, result rendered, nothing stored):

```bash
uv run python -m docfield.web.app
```

Run the folder watcher (drop files into `data/inbox/`; accepted records land in SQLite, accepted files move to `data/processed/`, everything else to `data/review/`; CSV export is a separate step over the accumulated records):

```bash
uv run python -m docfield.ingest.watcher
```

Or call the core directly - it has no side effects and no dependency on either entry point:

```python
from docfield.config import load_config
from docfield.core import process_document

result = process_document("receipt.jpg", settings=load_config())
print(result.decision)      # "accept" | "review"
print(result.confidence)    # document-level confidence
```

Run the tests (218 tests, fully offline - no API key needed):

```bash
uv run pytest -q
```

The implemented backend is Gemini (`EXTRACTION_BACKEND=gemini`).
A local Ollama backend with an OCR path, for fully offline and private runs, is scaffolded in config but not yet built.

## How it was built

Half of this project was written by an autonomous loop overnight; the split was by risk, not convenience.

A spec package came first: requirements, architecture, data spec, and a phased build plan with per-task acceptance criteria (`docs/`), plus a `CLAUDE.md` encoding the architectural rules the code must not break.
The specs are kept frozen as the original design inputs, including their original naming; where the build diverged from them, the code and this README are authoritative.
The deterministic core - schema, validation, routing, backend interface, stub pipeline - landed as 19 commits in one unattended overnight run: a driver script ran Claude Code headless against a task ledger ([`PROGRESS.md`](PROGRESS.md), harness in `run-overnight.ps1`), one task per fresh-context iteration, each proven by its acceptance check before commit, with a hard scope boundary the loop could not cross.

The model-touching half was built interactively ([`PROGRESS_TOMORROW.md`](PROGRESS_TOMORROW.md)), with every extraction verified on real documents, because a plausible-looking wrong total passes a smoke test.
The ledgers and the loop harness are committed as part of the repo's history.

## Caveats

- The hosted demo runs on the Gemini free tier, which may use inputs for training.
  **Synthetic or public documents only** - never real financial data.
  Fully private local processing is planned, not yet implemented.
- The free Space sleeps when idle; the first request after a quiet period is a cold start, and the first PDF triggers a one-time parser model download.

## License

MIT - see [LICENSE](LICENSE).
The benchmark datasets used for evaluation (SROIE and others) carry their own research licenses and are not redistributed in this repository.
