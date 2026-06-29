# Project Setup, Stack & Deployment

## 1. Repository layout

```
doc-extraction-agent/
├── CLAUDE.md                     # conventions & guardrails for the coding agent
├── README.md                     # quickstart + results table (eval evidence)
├── pyproject.toml                # project + dependency declarations (managed by uv)
├── uv.lock                       # resolved, pinned dependency lock (committed)
├── .python-version               # uv interpreter pin: 3.11 (committed)
├── .env.example                  # config template (no secrets committed)
├── docs/
│   ├── 01_requirements.md
│   ├── 02_architecture.md
│   ├── 03_data_and_extraction_spec.md
│   └── 05_build_plan.md
├── src/doc_agent/
│   ├── __init__.py
│   ├── config.py                 # loads env/config; selects backend
│   ├── core.py                   # process_document(): the reusable pipeline
│   ├── schema/
│   │   └── models.py             # Pydantic Document, LineItem
│   ├── parsing/
│   │   ├── detect.py             # modality detection
│   │   ├── docling_parser.py     # native PDF → text/layout
│   │   └── ocr.py                # image → text (optional path)
│   ├── backends/
│   │   ├── base.py               # ExtractionBackend protocol + factory
│   │   ├── gemini.py             # free-tier multimodal adapter
│   │   └── ollama.py             # local model adapter
│   ├── validation/
│   │   └── rules.py              # hard/soft rules → report
│   ├── routing/
│   │   └── score.py              # confidence + decision (pure)
│   ├── store/
│   │   ├── db.py                 # SQLite writer
│   │   └── export.py             # CSV export
│   ├── ingest/
│   │   └── watcher.py            # folder watcher / poll loop (batch entry)
│   └── web/
│       └── app.py                # Gradio demo (URL entry)
├── eval/
│   ├── run_eval.py               # metrics over labelled datasets
│   └── datasets/                 # download scripts / loaders (no data in git)
├── data/                         # gitignored: inbox/ processed/ review/ exports/
│   ├── inbox/
│   ├── processed/
│   ├── review/
│   └── exports/
└── tests/
    ├── test_validation.py
    ├── test_routing.py
    ├── test_schema.py
    └── test_core_smoke.py
```

## 2. Stack

- **Runtime:** Python **3.11**, pinned via `.python-version` (`uv python pin
  3.11`). Chosen over 3.12 for broadest wheel coverage across the Torch-based
  Docling stack and PaddleOCR/PaddlePaddle, which lags newest Pythons.
  Declared range: `requires-python = ">=3.11"`.
- **Package manager:** `uv` (manages the venv, resolves and locks deps via
  `uv.lock`; add deps with `uv add`, run with `uv run`).
- **Parsing:** `docling` (native PDF/scan structure). Optional OCR:
  `paddleocr` or `pytesseract` + system Tesseract.
- **Modeling:** `google-genai` (Gemini free tier) and a local `ollama` server
  (e.g. `qwen2.5:7b` or a 3B variant) reached over HTTP.
- **Contract/validation:** `pydantic` v2.
- **Web demo:** `gradio`.
- **Storage:** stdlib `sqlite3` + `csv`.
- **Watcher:** `watchdog` (or a stdlib poll loop for max portability).
- **Config:** `pydantic-settings` / `python-dotenv`.
- **Testing:** `pytest`.

Dependencies are declared in `pyproject.toml` and pinned via the committed
`uv.lock` (`uv sync` installs exactly that lock). Do not float the model
identifier in code — it is config (see guardrails).

## 3. Configuration (`.env.example`)

```
# Backend selection: "gemini" | "ollama"
EXTRACTION_BACKEND=gemini

# Gemini (free tier via Google AI Studio key; no card required)
GEMINI_API_KEY=
GEMINI_MODEL=gemini-flash-latest        # identifier is config, not hardcoded

# Ollama (local)
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b

# Image handling: "vision_direct" | "ocr_then_text"
IMAGE_STRATEGY=vision_direct            # vision_direct requires a multimodal backend

# Routing
CONFIDENCE_THRESHOLD=0.85               # tuned via eval

# Paths (batch mode)
INBOX_DIR=./data/inbox
PROCESSED_DIR=./data/processed
REVIEW_DIR=./data/review
EXPORT_DIR=./data/exports
DB_PATH=./data/agent.db
```

`config.py` validates these at startup and fails fast with a clear message if,
e.g., `gemini` is selected with no key, or `vision_direct` is selected with a
text-only backend.

## 4. Local setup

```bash
# 1. Pin the interpreter to 3.11 (writes .python-version; uv fetches it if absent)
uv python pin 3.11

# 2. Install (uv creates the venv on 3.11 and installs from pyproject.toml + uv.lock)
uv sync

# 3a. Gemini path: get a free AI Studio key, put it in .env
#     (free tier, no credit card; quota resets daily)

# 3b. Ollama path (offline/private):
#     install Ollama, then:
ollama pull qwen2.5:7b
#     set EXTRACTION_BACKEND=ollama and IMAGE_STRATEGY=ocr_then_text

# 4. Create working dirs
mkdir -p data/{inbox,processed,review,exports}
```

## 5. Running

**Autonomous batch mode:**

```bash
uv run python -m doc_agent.ingest.watcher
# drop files into data/inbox/ — accepted records land in SQLite + data/exports/,
# uncertain ones move to data/review/
```

**Web demo (local):**

```bash
uv run python -m doc_agent.web.app
# opens a Gradio URL; upload one document to see fields + confidence + decision
```

**Evaluation:**

```bash
uv run python eval/run_eval.py --dataset sroie --split holdout
# prints per-field precision/recall/F1 and auto-accept precision on critical fields
```

## 6. Deployment to Hugging Face Spaces (free public demo URL)

1. Create a new **Space** → SDK: **Gradio** (free CPU tier). Set the Space's
   Python to **3.11** (the `python_version: "3.11"` field in the Space README
   metadata) so the deployed runtime matches the pinned local interpreter.
2. Add `app.py` at the Space root that imports and launches
   `doc_agent.web.app` (or copy the web entry there), plus a `requirements.txt`
   the Gradio builder can read — generate it from the uv-managed project rather
   than hand-maintaining it: `uv export --no-hashes --no-dev -o requirements.txt`.
3. Set **Repository secrets** in the Space: `GEMINI_API_KEY`,
   `EXTRACTION_BACKEND=gemini`, `IMAGE_STRATEGY=vision_direct`,
   `GEMINI_MODEL=gemini-flash-latest`.
4. Push; the Space builds and serves a public URL.

**Free-tier realities to design around (and to note in the UI):**

- CPU-only and the Space **sleeps when idle** → first request after idle has a
  cold start. This is why the cloud demo uses the **Gemini API** for inference
  rather than a local model, and why `vision_direct` (no heavy OCR in the
  Space) is the demo's image path.
- **Stateless:** no persistent DB in the demo. Show the result; don't store it.
- **Privacy:** the free Gemini tier may use inputs for training, so the demo
  must display a "synthetic/public documents only" notice and must not be used
  for real financial data.

## 7. What stays free

- **Inference:** local Ollama (no quota, private) or Gemini free tier
  (~1,500 req/day, resets daily, no card) — far above dev volume.
- **Hosting:** Hugging Face Spaces free CPU tier for the public demo.
- **Storage:** local SQLite/CSV; nothing paid.

No component requires a credit card or paid plan for development or demo.
