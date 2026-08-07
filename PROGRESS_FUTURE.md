# PROGRESS_FUTURE.md -- Post-Ship Backlog

The project is shipped: full pipeline, unattended watcher, measured eval
(SROIE n=100), live demo on Hugging Face Spaces. Everything in this file is
optional. It exists so any item can be picked up cold -- by you or a future
Claude Code session -- with the same task format as the other ledgers.

**Priority rule:** Tier 1 closes gaps in claims the README already makes.
Tier 2 makes an existing claim demonstrably true. Tier 3 is genuine
improvement with diminishing portfolio return. Work top-down; stopping after
any tier leaves a coherent project.

**Protocol:** same as PROGRESS_TOMORROW.md -- interactive, one task at a time,
review the diff, verify on real documents where a model is touched, one commit
per task, tick the box. Specs in `docs/` remain the source of truth; do not
edit them casually. If an item needs design beyond what the docs cover, write
the design *when the item is next*, not before.

---

## Tier 1 -- Close the measurement gap (do these first)

The README claims three critical fields (`total`, `tax`, `invoice_number`) but
only `total` is measured -- SROIE doesn't label the other two. These finish the
evidence story. The harness, cache, and sweep already exist; each task is one
adapter + one eval run.

- [ ] **F1 -- Wire the CORD adapter** (measures `tax` + line items)
  Implement the scaffolded `eval/datasets/cord.py` against
  `naver-clova-ix/cord-v2`: parse the `ground_truth` JSON string, map
  `gt_parse` per the mapping documented in the scaffold (subtotal, tax, total,
  line_items, vendor where present). Labeled fields = only what CORD labels.
  Note: CORD receipts are Indonesian -- expect lower text-field accuracy; that
  is signal about multilingual behaviour, not a harness bug. Run predict on a
  SMALL slice (20) first, then 100. Costs Gemini quota -- run deliberately.
  Check: `uv run python -m eval.run_eval predict --dataset cord --limit 20`
  then `score --dataset cord` produces tables; tests still offline-green.
  Commit: `eval: wire CORD adapter (tax + line-item coverage)`

- [ ] **F2 -- Wire the invoice-JSON adapter** (measures `invoice_number`)
  Implement `eval/datasets/invoice_json.py` against
  `mychen76/invoices-and-receipts_ocr_v1` (or `GokulRajaR/invoice-ocr-json`
  if the shape is cleaner -- probe both, pick one, document why). Map invoice
  number, dates, totals, tax per the scaffold. Same small-slice-first rule.
  Check: predict (20) + score produce tables including `invoice_number`.
  Commit: `eval: wire invoice-JSON adapter (invoice_number coverage)`

- [ ] **F3 -- Update the README results section**
  Extend the results table to all three datasets; update the framing to state
  which critical fields are measured where; refresh the auto-accept precision
  claim if the numbers move it. Keep the honest caveats (confidence ceiling,
  slice sizes).
  Check: README table covers total/tax/invoice_number with dataset provenance.
  Commit: `docs: eval results across SROIE + CORD + invoices`

## Tier 2 -- Make the offline claim true (T4 + T6, deferred from launch)

The swappable-backend design currently has one real backend. These make
"runs fully free, offline, and private" demonstrable rather than aspirational.

- [ ] **F4 -- OCR path** (build plan 2.3; ledger T4)
  `src/doc_agent/parsing/ocr.py` behind the payload interface; wire into
  `acquire` for `IMAGE_STRATEGY=ocr_then_text`.
  DECISION: try `uv add paddleocr`; if it won't resolve on 3.11, fall back to
  `uv add pytesseract` + the Tesseract binary, and record the choice here.
  Check: a sample receipt image yields text; `process_document` runs in
  `ocr_then_text` mode with the stub backend (no model needed to test the path).
  Commit: `phase 2.3: OCR acquire path (ocr_then_text)`

- [ ] **F5 -- Ollama backend** (build plan 2.6; ledger T6)
  Requires a local Ollama server + pulled model (e.g. `qwen2.5:7b`).
  `src/doc_agent/backends/ollama.py`: JSON-schema/grammar-constrained
  decoding, text-in (pairs with F4), registered in the factory, model id from
  config. Mocked unit tests + one manual smoke against the live server.
  Check: `EXTRACTION_BACKEND=ollama` + `IMAGE_STRATEGY=ocr_then_text` returns
  schema-valid data on a real receipt, fully offline.
  Commit: `phase 2.6: ollama backend (local/offline path)`

- [ ] **F6 -- Offline eval comparison** (small, high-signal)
  Run the SROIE 20-slice through the Ollama path and add a one-row comparison
  to the README (Gemini vs local 7B on the same slice). This is the concrete
  payoff of the swappable design: same harness, two backends, honest numbers.
  Check: comparison row in README with slice size stated.
  Commit: `eval: gemini vs ollama comparison (SROIE-20)`

## Tier 3 -- Genuine improvements, diminishing portfolio returns

Defensible engineering; none changes how the project reads to a reviewer.
Pick by interest, not obligation.

- [ ] **F7 -- Real confidence signal.** Surface a usable model signal
  (logprobs where the API exposes them, or k-sample self-consistency voting)
  so `CONFIDENCE_THRESHOLD` becomes a live dial; re-run the sweep and update
  the README (this would retire the "confidence ceiling" caveat). Design
  needed before building: self-consistency multiplies per-document cost by k.
- [ ] **F8 -- Review-queue UI.** A minimal local page over `review/`: show the
  document, the extraction, the validation failures; accept-with-edits writes
  to the store. Keeps the "not a product" scope -- single user, no auth.
- [ ] **F9 -- Watcher hardening.** Bounded retries with backoff for transient
  backend failures, a dead-letter state distinct from review, and a startup
  reconciliation pass over files that arrived while the watcher was down.
- [ ] **F10 -- Second document domain.** One new document type (e.g. utility
  bills or purchase orders): schema fields, validation rules, a small labeled
  eval slice. Proves the architecture generalizes beyond receipts/invoices.

- [ ] **F11 -- Settle the FC-1 monetary-tolerance questions.** One held-out
  document (`X51005806696`) was auto-accepted with a `total` that disagrees
  with gold, clearing every rule; it is the difference between 100% and 98.4%
  auto-accept precision. Written up in full, with the mechanism and a
  correction to the first hypothesis about it, in `eval/FINDINGS.md`. Do not
  tune a constant against it -- that is a sample of one, and the constant most
  people would reach for is not the one that admitted it. Gather evidence
  first: (a) how many held-out documents fall inside the *relative* tolerance
  but outside the absolute floor, (b) how many SROIE `total` labels record a
  pre-tax subtotal rather than the grand total (if common, the fix belongs in
  the adapter, not in validation), (c) whether `money_close` should compare in
  `Decimal` -- at this residual the float verdict differs from the exact one,
  which is a correctness question separate from the tolerance value.

- [x] **F12 -- Backfill the 44 unextracted held-out documents.** Done: all 44
  re-predicted via `--retry-errors`, 0 errors, the 317 successful predictions
  left byte-identical. The run that
  expanded SROIE to the full 361-document test split hit a Gemini monthly
  spend cap; 44 held-out documents returned 429 and have no extraction. They
  are cached as errors, and predict is idempotent, so a plain re-run **skips
  them** -- they need `--overwrite`, scoped to those ids, once quota resets.
  Until then held-out is 217 usable of 261 and every held-out figure carries
  that hole. The report names it explicitly (Document outcomes block).

## Not doing, and why

Explicit non-goals -- declining these is a design decision, not an omission:

- **Fine-tuning a model.** The project's thesis is engineering *around*
  off-the-shelf models; fine-tuning is a different project and would compete
  on the one axis (benchmark F1) where purpose-built models win.
- **Multi-tenant / production deployment.** Auth, queues, horizontal scale,
  SLAs -- out of scope per requirements section 4; the Space is a demo, not a service.
- **A full review application.** F8 stays a single-user local page; workflow
  tooling, audit trails, and roles are product work, not portfolio work.
- **Chasing SROIE/CORD leaderboards.** The datasets are the measuring
  instrument, not the objective (see README framing).
