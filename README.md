---
title: Document Extraction Agent
colorFrom: indigo
colorTo: blue
sdk: gradio
sdk_version: 6.19.0
python_version: "3.11"
app_file: app.py
pinned: false
---

# Document Extraction Agent

An autonomous document-extraction agent for invoices and receipts. A reusable
core pipeline (`process_document`) turns a document into a validated, structured
record and decides whether to **auto-accept** it or route it to **review**, behind
a swappable model backend (Gemini free tier or local Ollama). See `docs/` for the
requirements, architecture, and data/extraction specs.

## Live demo

Hosted on Hugging Face Spaces: <!-- DEMO_URL -->_TBD -- add the Space URL here_.

Upload one invoice or receipt (native PDF, scan, or phone photo) and the demo
shows the extracted fields, per-field confidence, the validation report, and the
accept/review decision. It is stateless and runs on the Gemini free tier, so it
carries a **synthetic/public documents only** notice -- don't upload real
financial data.

## Quickstart

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                        # create the venv and install from uv.lock
cp .env.example .env           # then add your Gemini key (free, from Google AI Studio)
```

**Web demo (local).** Single-upload UI; process one document and see the result.

```bash
uv run python -m doc_agent.web.app
```

**Batch watcher.** Drop files into `data/inbox/`; accepted records are written to
SQLite and `data/exports/`, and anything uncertain moves to `data/review/`.

```bash
uv run python -m doc_agent.ingest.watcher
```

Both entry points call the same `process_document` core. The backend is chosen by
config: Gemini (`EXTRACTION_BACKEND=gemini`, needs a key) or a local Ollama server
(`EXTRACTION_BACKEND=ollama`, `IMAGE_STRATEGY=ocr_then_text`) for fully offline,
private runs.

**Evaluation.** Two phases -- `predict` runs the model over a slice and caches the
results, `score` computes the metrics offline (see below).

```bash
uv run python -m eval.run_eval predict --dataset sroie --limit 100
uv run python -m eval.run_eval score --dataset sroie
```

## Evaluation results

Evaluation uses a two-phase harness (`eval/`): a **predict** phase runs the core
over a held-out dataset slice and caches every result (the only phase that calls
a model), and an offline **score** phase computes the metrics and the
accept-threshold sweep from that cache. Re-tuning never re-runs inference.

### SROIE (ICDAR 2019) -- 100-document held-out test slice

Backend: Gemini `gemini-2.5-flash`, vision-direct. Values are normalized before
comparison (money compared cent-exact, dates on ISO equality, text
case/whitespace-insensitive). SROIE labels four of the schema fields; of the three
critical fields it labels only `total`.

| Field | Precision | Recall | F1 |
|---|---|---|---|
| `total` (critical) | **99.0%** | 99.0% | 99.0% |
| `vendor_name` | 86.0% | 86.0% | 86.0% |
| `document_date` | 81.0% | 98.8% | 89.0% |
| `vendor_address` | 53.0% | 53.0% | 53.0% |

**Routing at `CONFIDENCE_THRESHOLD = 0.50`:**

| Metric | Value |
|---|---|
| Auto-accepted | 18 / 100 (18%) |
| Auto-accept precision on `total` | **100% (18 / 18)** |
| Routed to review | 82 / 100 |

The one incorrect `total` in the slice (99/100 correct overall) fails an
arithmetic reconciliation check and is routed to review -- it never reaches the
auto-accepted set.

**Reading these numbers:**

- **Auto-accept precision on the critical field is delivered by arithmetic
  validation, not by model confidence.** The Gemini free tier exposes no
  per-field confidence, so the pipeline's confidence score falls back to a neutral
  0.50 prior and is structurally capped at 0.50 -- the threshold sweep is
  effectively binary (0.50 accepts the validation-clean documents; anything higher
  accepts nothing). What actually gates the auto-accepted set to 100% precision on
  `total` is the H2/H3 arithmetic cross-checks, which force any internally
  inconsistent document to review regardless of the score. This is the intended
  precision posture: the model is treated as fallible, and a confidently-wrong
  number is caught by arithmetic rather than trusted.
- **Lower accuracy on non-critical fields is expected, and is exactly what the
  human-in-the-loop review path is for.** `vendor_address` in particular is noisy
  (free-form, multi-line, punctuation-variant); those extractions are surfaced for
  review, not silently trusted. The design optimizes precision on the
  auto-accepted critical path, not recall on every field.
- **This is a 100-document held-out slice** of the SROIE test split, which labels
  `total` but not `tax` or `invoice_number`. **CORD** (adds `tax` and line items)
  and the **invoice-JSON** set (adds `invoice_number`) are scaffolded as dataset
  adapters and are the next coverage additions needed to measure the other two
  critical fields.

**Known limitation / future work.** Because the threshold cannot currently
separate documents (all scores sit at 0.50), `CONFIDENCE_THRESHOLD` is not a
meaningful dial today. Surfacing a real model confidence signal -- token logprobs,
or a self-consistency check across repeated samples -- would restore graded
confidence and make the threshold tunable, letting the accept rate rise past 18%
without giving up the precision that arithmetic validation already guarantees.

### Reproduce

```bash
# Phase 1 -- runs the model over a slice (spends free-tier quota); idempotent.
uv run python -m eval.run_eval predict --dataset sroie --limit 100

# Phase 2 -- offline; recomputes metrics and the threshold sweep from the cache.
uv run python -m eval.run_eval score --dataset sroie
```
