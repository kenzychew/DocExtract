# Requirements Specification — Document Extraction Agent

## 1. Overview

An autonomous agent that ingests invoices, receipts, and similar
semi-structured financial documents, extracts their key fields into a
validated structured record, and routes anything it is not confident about
to a human review queue. The agent runs unattended over a stream of incoming
documents and is also exposed through a small public web demo.

The reasoning step (turning document text/images into structured fields) is
performed by a swappable LLM backend. Everything around it — triggering,
parsing, validation, confidence scoring, routing, persistence, logging — is
application code. The engineering value of this project is the system around
the model, not the model itself.

## 2. Problem statement

Manually keying fields off invoices and receipts is slow and error-prone, and
the documents arrive in inconsistent formats and qualities (clean PDFs,
flatbed scans, phone photos). We want a pipeline that processes them
automatically, is confident only when it should be, and surfaces the rest for
a human — with measurable accuracy.

## 3. Goals

- Ingest documents from a watched location with no human trigger per document.
- Support three input modalities: native-text PDFs, scanned images, and phone
  photos.
- Extract a defined set of fields into a strict JSON schema.
- Validate extracted values, including arithmetic consistency checks.
- Assign a confidence to each document and route low-confidence documents to
  review rather than auto-accepting them.
- Persist accepted records and export them to CSV.
- Provide a public demo URL where a single uploaded document is processed and
  its result shown.
- Run entirely on free infrastructure and free model access.
- Be measurable against ground-truth datasets (precision, recall, F1).

## 4. Non-goals (explicitly out of scope for v1)

- Fine-tuning or training a model. Off-the-shelf models only.
- A full review *application* with auth, multi-user workflows, or audit trails.
  The review queue is a directory plus a CSV, not a product.
- Persistent multi-tenant storage in the cloud demo. The demo is
  presentation-only and stateless.
- Handling non-financial document types (contracts, IDs, medical records).
- Real-time / low-latency guarantees. This is a background batch system.
- Production hardening (SLAs, horizontal scale, queue infrastructure).

## 5. Users and usage modes

1. **Autonomous batch mode (primary).** Operator drops files into an `inbox/`
   directory (local or mounted). The agent processes each, writes accepted
   records to storage, and moves uncertain ones to `review/`. No interaction
   per document.
2. **Demo mode (secondary).** A visitor uploads one document to the public web
   UI and sees the extracted fields, per-field confidence, validation results,
   and the accept/review decision. Nothing is persisted.

Both modes call the same core pipeline.

## 6. Functional requirements

- **FR-1 Ingestion.** Detect new files in `inbox/` (file-watcher or poll) and
  enqueue them for processing. Supported types: `.pdf`, `.png`, `.jpg`,
  `.jpeg`, `.webp`, `.tif/.tiff`.
- **FR-2 Parsing / text acquisition.** For native-text PDFs, extract text and
  layout. For scans/photos, obtain content either via OCR or via a multimodal
  model that reads the image directly. The chosen path is backend-dependent
  (see architecture).
- **FR-3 Field extraction.** Produce a JSON object conforming to the schema in
  the data spec, using the active model backend with structured-output
  enforcement.
- **FR-4 Validation.** Apply type/format checks and arithmetic cross-checks.
  Each field carries a validation status.
- **FR-5 Confidence + routing.** Compute a document-level confidence from
  model signal, validation results, and required-field completeness. If it
  clears the threshold, auto-accept; otherwise route to review.
- **FR-6 Persistence.** Append accepted records to a local SQLite database and
  export to CSV. Move source files to `processed/` or `review/` accordingly.
- **FR-7 Logging.** Emit structured logs for every document: inputs, backend
  used, decision, validation failures, and timings. Never crash the loop on a
  single bad document — isolate, log, and continue.
- **FR-8 Web demo.** Accept one uploaded document, run the core pipeline, and
  render fields, confidence, validation, and decision. Stateless.
- **FR-9 Backend selection.** The model backend is chosen by configuration at
  startup with no code change (Gemini free tier or local Ollama).
- **FR-10 Evaluation.** A harness runs the pipeline over a labelled dataset and
  reports field-level precision, recall, and F1, plus document-level routing
  statistics.

## 7. Non-functional requirements

- **NFR-1 Cost.** Zero spend for development and demo. Local model = no quota;
  hosted model = free tier only.
- **NFR-2 Privacy.** Free hosted backends may use inputs for training; the
  public demo must process only synthetic/public documents. This must be
  stated in the demo UI. Sensitive data is handled only via the local backend.
- **NFR-3 Swappability.** Adding or replacing a backend requires implementing
  one interface and changing config — nothing else.
- **NFR-4 Robustness.** A malformed or unreadable document produces a logged
  failure and a review routing, never a crash.
- **NFR-5 Reproducibility.** Pinned dependencies; deterministic config;
  documented setup that runs from a clean checkout.
- **NFR-6 Portability.** The core pipeline is independent of both entry points
  and of any specific host.

## 8. Success criteria

The project is successful when:

- The agent processes a mixed batch (native PDFs + scans + phone photos)
  end-to-end with no per-document intervention, persisting accepted records and
  correctly diverting uncertain ones to review.
- On a held-out labelled set, **auto-accept precision on the critical fields
  (`total`, `tax`, `invoice_number`) is ≥ 0.98**, with recall reported at that
  operating point. (Rationale and method in the data spec.)
- A public demo URL processes an uploaded document of each modality and
  displays a correct, validated result.
- Swapping between the Gemini and Ollama backends requires only a config
  change.

## 9. Key assumptions

- Documents are predominantly English. Multilingual handling is best-effort.
- Volume during development is low (tens to low hundreds of documents), well
  within free-tier limits.
- The operator's local machine or chosen free host can run lightweight Python
  continuously; the model itself runs locally (Ollama) or via free API.
- Free-tier quotas and free hosting behaviour (idle sleep, CPU-only) are
  acceptable for a portfolio demo.

## 10. Glossary

- **Auto-accept:** a document whose confidence clears the threshold and whose
  record is persisted without human review.
- **Review:** a document routed to a human because confidence is below
  threshold or a hard validation rule failed.
- **Critical fields:** fields where a confidently-wrong value is most costly —
  `total`, `tax`, `invoice_number`.
- **Backend:** an implementation of the model interface that turns a document
  into structured fields.
- **Core pipeline:** the host- and entry-point-independent function that takes
  a document and returns an extraction result.
