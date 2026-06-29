# Data & Extraction Specification

## 1. Datasets

All free, all on Hugging Face and/or Kaggle. data.gov.sg is **not** a source —
it publishes statistical open data, not document images. The set below is
chosen to cover every required input modality *and* to provide ground-truth
labels, which the evaluation harness needs.

| Dataset | What it is | Modality covered | Labels | Where |
|---|---|---|---|---|
| **SROIE (ICDAR 2019)** | ~1,000 real scanned receipts from shops/restaurants; variable print & scan quality | Scans | company, address, date, total | HF: `Voxel51/scanned_receipts`; also `priyank-m/SROIE_2019_text_recognition` |
| **CORD** | ~11,000 Indonesian receipts captured in the wild; noisy, low quality; multi-level labels (store/menu/subtotal/total + subclasses) | Scans / in-the-wild | rich key-value + line items | HF: search `CORD` (naver-clova-ix) |
| **MC-OCR** | 2,436 receipts captured on mobile devices | Phone photos | quality + key fields | Search "MC-OCR RIVF 2021" (HF mirrors / challenge page) |
| **High Quality Invoice Images for OCR** | ~6,700 synthetic but realistic invoices | Native-style invoices | invoice fields | Kaggle: `osamahosamabdellatif/high-quality-invoice-images-for-ocr`; HF: `Voxel51/high-quality-invoice-images-for-ocr` |
| **invoices-and-receipts_ocr_v1** | Mixed invoices + receipts with OCR/structure | Mixed | structured | HF: `mychen76/invoices-and-receipts_ocr_v1` |
| **invoice-ocr-json** | Invoices with structured JSON ground truth | Native invoices | JSON key-values | HF: `GokulRajaR/invoice-ocr-json` |
| **FUNSD** | 199 noisy scanned forms | Messy scans/forms | entities + relations | HF: search `funsd` |
| **DocILE** (optional, large) | Large invoice benchmark for key-info localization & extraction | Native invoices | KILE/LIR | DocILE project (HF/registration) |

### Recommended working split

- **Messy real receipts:** SROIE (baseline real) + CORD (noisy in-the-wild).
- **Phone photos:** MC-OCR.
- **Native invoices:** High Quality Invoice Images + `invoice-ocr-json` (has
  clean JSON labels, convenient for the eval harness).
- **Messy forms (stretch):** FUNSD.

Hold out a fixed evaluation slice per source and never tune against it.

### Licensing note

These are research/benchmark datasets with their own terms; several invoice
sets are synthetic. Use them for development, evaluation, and demo only, and
keep real/sensitive documents off the public demo and off free hosted
backends (see NFR-2). Confirm each dataset's license before any redistribution.

## 2. Output schema

A single unified schema spans receipts and invoices; fields not present in a
given document are `null`. Define with Pydantic so the same model enforces
structured output, validates types, and serializes to storage.

```python
class LineItem(BaseModel):
    description: str | None
    quantity: float | None
    unit_price: float | None
    amount: float | None

class Document(BaseModel):
    doc_type: Literal["receipt", "invoice", "other"]
    vendor_name: str | None
    vendor_address: str | None
    invoice_number: str | None          # critical
    document_date: date | None          # ISO 8601
    due_date: date | None
    currency: str | None                # ISO 4217 where detectable
    line_items: list[LineItem]
    subtotal: float | None
    tax: float | None                   # critical
    total: float | None                 # critical
    # populated by the pipeline, not the model:
    field_confidence: dict[str, float] = {}
    validation: dict = {}
    decision: Literal["accept", "review"] | None = None
```

**Field requirements**

- Always attempt: `doc_type`, `vendor_name`, `document_date`, `total`.
- Critical (precision-prioritised): `total`, `tax`, `invoice_number`.
- Monetary fields are numbers (no currency symbols/thousands separators);
  normalize during extraction.
- Dates are ISO 8601 (`YYYY-MM-DD`); store raw string alongside if parsing is
  ambiguous.

## 3. Validation rules

Validation is pure functions over the parsed `Document` → a report. Two
classes of rule:

**Hard rules (a failure forces `review`):**

- `H1` All critical fields parse to the correct type when present.
- `H2` Arithmetic reconciliation, when the inputs exist:
  `subtotal + tax ≈ total` within a small epsilon (rounding tolerance).
- `H3` Line-item reconciliation, when line items exist:
  `sum(line_items.amount) ≈ subtotal` (or `total` if no subtotal).
- `H4` `total` is present and non-negative.

**Soft rules (reduce confidence, do not force review):**

- `S1` `document_date` present and plausible (not in the far future).
- `S2` `currency` resolves to a known code.
- `S3` `vendor_name` non-empty.
- `S4` Per-line arithmetic: `quantity * unit_price ≈ amount`.

Epsilon for monetary comparisons accommodates rounding (e.g. ±0.02 absolute or
a small relative tolerance, whichever is larger).

## 4. Confidence scoring

Document confidence ∈ [0, 1] blends:

- **Model signal** (weighted) — backend field/token confidence where exposed;
  neutral (0.5) when unavailable.
- **Validation** — start from model signal; subtract penalties for each soft
  failure; any hard failure short-circuits to `review`.
- **Completeness** — penalty proportional to missing required fields.

Exact weights live in config and are set empirically via the eval harness. Keep
the function pure and unit-tested with hand-built cases.

## 5. Routing

```
decision = review  if any hard rule fails
         = accept  if confidence >= THRESHOLD
         = review  otherwise
```

`THRESHOLD` is one constant, tuned in evaluation.

## 6. Evaluation methodology

This is what turns "seems to work" into evidence, and it is how the
precision/recall question is answered concretely.

**Definitions (field level, against ground truth):**

- *Precision* = correct extracted values / all values the system produced
  (and auto-accepted).
- *Recall* = correct extracted values / all values present in ground truth.
- *F1* = harmonic mean.

**Procedure:**

1. Run the core over each held-out dataset slice.
2. Normalize predicted and gold values (numbers, dates, casing/whitespace)
   before comparison.
3. Compute precision, recall, F1 **per field** and **per critical field**.
4. Compute document-level routing stats: % auto-accepted, % to review, and —
   crucially — **precision on the auto-accepted subset** for critical fields.
5. Sweep `THRESHOLD` and report the precision/recall trade-off curve.

**Target / operating point:**

- Optimize so **auto-accept precision on `total`, `tax`, `invoice_number` ≥
  0.98**, then report recall at that point. Recall "lost" to the threshold is
  simply review-queue volume — acceptable, because the asymmetric cost favours
  not writing wrong numbers. Arithmetic cross-checks (H2/H3) are the lever that
  raises precision without collapsing recall, since they let confident,
  internally-consistent values through while catching the inconsistent ones.

Report a small table per dataset (precision/recall/F1 per field, plus routing
stats) in the project README — this is the portfolio's evidence of rigor.

## 7. Modality handling summary

- **Native PDF:** Docling → text/layout → backend (text or vision).
- **Scan:** vision-direct (Gemini reads the image) **or** OCR → text → backend.
- **Phone photo:** same as scan; vision-direct is more robust to skew/lighting,
  which is why the Gemini path is preferred for the demo.
