"""Evaluation harness for the DocField Extract pipeline (build-plan phase 5).

The harness is deliberately split into two phases so that model inference
happens exactly once and threshold tuning is free:

- **predict** (``eval.predict``) runs ``core.process_document`` over a dataset
  slice and caches each result (gold labels, predicted document, confidence,
  validation report) to ``eval/cache/`` keyed by example id. This is the only
  phase that calls a model backend and the only phase that spends API quota.
- **score** (``eval.score``) loads the cache and computes every metric plus the
  threshold sweep purely offline. Re-tuning never re-runs inference.

See ``docs/03_data_and_extraction_spec.md`` section 6 for the evaluation
methodology and ``docs/05_build_plan.md`` phase 5 for the task definition.
"""
