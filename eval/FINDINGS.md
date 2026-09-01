# Evaluation findings

Documented failure cases from evaluation runs, with the evidence and the
mechanism behind each one.

The purpose of this file is to record what actually happened, including where a
plausible explanation turned out to be wrong on inspection.
A failure that is understood is worth more than a number that moved.

Nothing in this file has been acted on by tuning a constant against the single
document that exposed it.
Where a fix is not yet justified, the finding is logged as an open question with
the evidence attached, and the backlog entry is in `PROGRESS_FUTURE.md`.

---

## FC-1 -- A wrong `total` cleared every validation rule and was auto-accepted

**Status:** open question. No constant changed.
**Found:** SROIE held-out slice, 261 documents, first run after the test split
was expanded from 100 to 361.
**Impact:** on the completed held-out slice (261 documents) auto-accept
precision on `total` is **97.9% (92/94)**, against 100% (22/22) on the tuning
slice.
Two documents account for the gap: this one and **FC-3**.
An earlier version of this note called it "the single document that separates
the two figures", which was true of the partial 217-document slice measured
before the quota backfill and is no longer true.

### The document

`X51005806696`, a Malaysian print-shop receipt.

| | value |
|---|---|
| gold `total` | `7.20` |
| predicted `total` | `7.65` |
| predicted `subtotal` | `7.20` |
| predicted `tax` | `0.43` |
| predicted line items | `2.00 + 0.20 + 5.00 = 7.20` |
| confidence | `0.50` |
| rule outcomes | H1-H4 **all pass**, S1-S4 **all pass** |

Every hard rule and every soft rule passed.
There was no signal anywhere in the pipeline that this document was different
from the 61 correct ones it was accepted alongside.

### The mechanism, corrected

The hypothesis when this was first spotted was that H2 cleared on the
`MONETARY_ABS_EPSILON` boundary: `7.20 + 0.43 = 7.63` against a stated total of
`7.65` is a residual of exactly `0.02`, and the absolute epsilon is `0.02`.

**That hypothesis is wrong, and the correction matters.**
`money_close` applies the *larger* of the absolute floor and a relative term:

```
tolerance = max(abs_epsilon, rel_epsilon * max(|left|, |right|))
          = max(0.02, 0.005 * 7.65)
          = max(0.02, 0.03825)
          = 0.03825          <- the relative term binds, not the floor
```

The residual of `0.02` sits inside that with `0.01825` to spare.
It did not squeak through; it passed comfortably.

Two consequences follow, and both point away from the obvious fix:

1. **Tightening `MONETARY_ABS_EPSILON` would not have caught this document.**
   The absolute floor was never the binding constraint. The relative term is
   what admitted it, and at these amounts the relative term is nearly twice the
   floor.
2. **Under an absolute-only rule this document would have failed - by a float
   artifact.** In exact decimal arithmetic the residual is exactly `0.02`, on
   the boundary. In IEEE 754 it is `0.020000000000000462`, marginally *above*
   `0.02`. So an absolute-only comparison would reject it, but only because
   `7.2 + 0.43` does not land exactly on `7.63` in binary. A rule whose verdict
   at the boundary is decided by float representation is fragile regardless of
   what value the constant takes.

### A second reading of the same evidence

The predicted numbers are internally coherent with Malaysian receipt
conventions:

```
6% GST on 7.20            = 0.432  -> predicted tax 0.43
7.20 + 0.43               = 7.63
7.63 rounded to 5 sen     = 7.65   -> predicted total 7.65
line items sum            = 7.20   == predicted subtotal == GOLD total
```

The model's `subtotal` equals the gold `total` exactly, and the extra `0.43`
is 6% GST to the cent, with the total rounded to the nearest 5 sen as Malaysian
cash receipts do.

One plausible explanation is therefore that the model read the rounded grand
total off the receipt while the SROIE annotation records the pre-tax subtotal.
If so, this would be a **gold-label ambiguity** rather than an extraction error.

**This was tested across the corpus, and it is not a pattern.**
Of 317 usable cached documents, only 3 have a `total` that disagrees with gold,
and only this one fits the pre-tax signature.
The other two (`X51005268408`: 169.78 vs 169.80; `X51006401853`: 37.44 vs 37.45)
are one-cent disagreements whose line items sum to the *predicted* value, not to
gold, and no document anywhere in the cache shows the reverse pattern of gold
including tax where the prediction excludes it.
The implied rate here (predicted tax 0.43 against a gold total of 7.20, or
5.97%) is consistent with 6% GST, but one observation cannot establish a rate.

So the ambiguity reading stands as a credible account of *this* document and
nothing more.
It is recorded because it is the best available explanation of the numbers, not
because the corpus supports it.

This does **not** change the measured number.
Precision is measured against the labels the dataset ships, and against those
labels this accept is wrong; 98.4% stands as reported.
But it changes what the failure *means*, and it is the reason no constant was
tuned in response to it.

### Why nothing was changed

Tuning `MONETARY_ABS_EPSILON` (or the relative term) against the one document
that exposed it would be fitting a constant to a sample of one - and, per the
correction above, tuning the absolute epsilon would not even address the
mechanism.
It would also be the same class of error this project already removed once: the
eval's money comparison originally inherited this same relative tolerance, which
would have scored a `$2`-wrong total on a `$500` receipt as correct.
That was caught and made cent-exact.

The open questions are recorded in `PROGRESS_FUTURE.md` (**F11**), and want more
evidence before any constant moves:

- How many held-out documents sit within the relative tolerance but outside the
  absolute floor? One case cannot distinguish a systematic gap from an outlier.
- How many SROIE `total` labels record a pre-tax subtotal rather than the grand
  total? If that is common, the corpus disagrees with the schema and the right
  fix is in the adapter, not in the validation rules.
- Should `money_close` compare in `Decimal` rather than `float`? That is a
  correctness question about boundary behaviour, independent of what the
  tolerance should be, and can be settled on its own merits.

### Related

An unrelated but larger exposure was found while investigating this document:
the relative monetary tolerance that admitted it is structurally mis-specified.
See **FC-2**. FC-1 is one document; FC-2 is a property of the rule.

**FC-3** is the second false accept on held-out, and unlike this one it is not
a candidate for any rule to catch.
FC-1 remains the only *materially* wrong total in the corpus.

### Reproducing

```bash
uv run python -m eval.run_eval score --dataset sroie --split heldout --revalidate
```

The document is `eval/cache/sroie/X51005806696.json` once the held-out slice has
been predicted. The cache is git-ignored; regenerate it with the predict phase.

---

## FC-2 -- The relative monetary tolerance scales with value, not with rounding

**Status:** open. Structural, not fitted to any single document.
**Found:** while investigating FC-1, across the full 361-document cache.

### The asymmetry

The README records that the *measurement* side of this project once reused the
pipeline's reconciliation tolerance, including a 0.5% relative term, and that it
was made cent-exact because it would have scored a $2-wrong total on a $500
receipt as correct.

That fix was applied to the measuring instrument only.
The same relative term is still live in the rules that **gate acceptance**:

| side | comparison | source |
|---|---|---|
| measurement (scoring) | `round(left, 2) == round(right, 2)` -- cent-exact | `eval/normalize.py` |
| validation (H2, H3) | `max(0.02, 0.005 * max(abs(left), abs(right)))` | `validation/rules.py` |

So the failure mode described as fixed is still live on the decision side, where
its consequence is a document being written rather than a metric being wrong.

### Why the term is mis-specified

The intent, per the code comment, is that "large invoices tolerate the
accumulated rounding of many line items".
That intent is sound; the implementation does not express it.
Accumulated rounding scales with the **number of line items** -- each rounded to
the cent -- not with the **value** of the document.
A 100,000 invoice with two line items receives 500 of tolerance under the
current rule, which no rounding process could justify.

The relative term overtakes the 0.02 floor at a document value of **4.00**, so
on this corpus it is the operative tolerance for 92.7% of documents, and it
grows without bound:

```
total        100  ->  tolerance     0.50
total        500  ->  tolerance     2.50
total     10,000  ->  tolerance    50.00
total    100,000  ->  tolerance   500.00
```

### Measured exposure on the current cache

```
rule checks evaluated (non-error docs) : 622
  passed under current rule            : 400
  would pass an absolute-only rule     : 393
  IN THE GAP (relative admits, absolute rejects) : 7

accepted documents                     : 88
accepted documents relying on the term :  5  (5.7%)
```

Of those five, four have a correct `total` and one -- FC-1 -- does not.
The term is buying real recall as well as carrying risk, which is why the size
of the trade needed measuring before any change.

SROIE keeps this latent: median total 27.50, p99 458.55, max 848.00.
The project's stated scope includes invoices, where the amounts are exactly the
regime in which a 0.5% term becomes material.

---

## FC-3 -- A false accept that no arithmetic rule could have caught

**Status:** closed as understood. Nothing to fix.
**Found:** in the 44 documents backfilled after the quota outage, so it was
absent from every held-out figure reported before that backfill.
**Impact:** the second of the two documents behind held-out auto-accept
precision of 97.9% (92/94).

### The document

`X51007846355`, an AEON supermarket receipt.

| | value |
|---|---|
| gold `total` | `8.95` |
| predicted `total` | `8.96` |
| predicted `subtotal` | `8.96` |
| predicted `tax` | `0.00` |
| predicted line items | `2.83 + 6.13 = 8.96` |
| rule outcomes | H1-H4 pass, S1-S3 pass, S4 skip |

### Why no rule could catch it

Every figure the model produced agrees with every other figure it produced.
The line items sum to `8.96`, the subtotal is `8.96`, tax is `0.00`, and the
total is `8.96`.
H2 and H3 both reconcile exactly - not within a tolerance, exactly.

There is no arithmetic relationship among the extracted values that is
violated, so no cross-check over those values can distinguish this document
from a correct one.
The error is only visible against the gold label, which the pipeline does not
have at decision time and would not need a model for if it did.

**This is the ceiling of the approach, not a missing rule.**
Arithmetic cross-checks detect *internal inconsistency*. A model that misreads
a document consistently produces a self-consistent record, and consistency is
exactly what the checks measure. The README states this limit in the abstract;
FC-3 is the concrete instance of it, observed on held-out data.

Catching this class of error requires evidence from outside the extracted
values - anchoring each value back to a span in the source document, which is
the provenance gap already recorded as a known limitation.

### The rounding pattern, and its limits as an explanation

`8.96` rounded to the nearest 5 sen is `8.95`, so the likeliest reading is that
the model summed the line items while the receipt states the rounded cash
total, which is what SROIE annotated. That is the mirror of FC-1, where the
model reported the rounded figure and the annotation held the unrounded one.

Across all 361 cached documents there are only **5** disagreements on `total`:

| id | predicted | gold | delta | consistent with 5-sen rounding |
|---|---|---|---|---|
| X51006401853 | 37.44 | 37.45 | -0.01 | yes |
| X51007846355 | 8.96 | 8.95 | +0.01 | yes |
| X51005268408 | 169.78 | 169.80 | -0.02 | yes |
| X51007846358 | 28.02 | 28.00 | +0.02 | yes |
| X51005806696 | 7.65 | 7.20 | +0.45 | no |

Four of the five are within 5 sen and consistent with cash rounding; only FC-1
is materially wrong. So **the corpus contains exactly one materially-wrong
`total` in 361 documents**, and the measured error rate is dominated by
sub-5-sen disagreements that a cent-exact comparator counts at full weight.

That is a reason to read the precision figures carefully, not a reason to
loosen the comparator. Cent-exact comparison is deliberate: any tolerance in
the *measuring* instrument would also admit genuinely wrong values, which is
the mistake this project already removed once (see FC-2).

---

## FC-4 -- Giving the model an arithmetic tool recovers real documents, at ~3x cost -- and introduces a new precision-miss pattern of its own

**Status:** closed as understood. Reported as-is; nothing tuned against it.
**Found:** live three-way comparison of `gemini`, `anthropic`, and
`anthropic-agentic` on the full 361-document SROIE test split (this run
measures backend behavior at the existing fixed `CONFIDENCE_THRESHOLD=0.50`,
it does not fit anything, so tuning/held-out contamination does not apply --
the full split is used for the biggest honest N, not just the 261-document
held-out subset that the main pipeline eval reserves for threshold-fitting
independence). Run with `claude-haiku-4-5` (Anthropic) and `gemini-2.5-flash`,
2026-09-01. An earlier version of this run used a 35-document slice; the
ranking it reported for the two single-call backends does not hold at full
scale (see below) and its numbers are superseded here. Reproduce with
`uv run python -m eval.run_eval predict --dataset sroie --limit 361 --backend <name> --cache-base eval/cache/cmp-<name>-full`
per backend, then `uv run python -m eval.run_eval compare --dataset sroie
--backend-cache gemini=eval/cache/cmp-gemini-full --backend-cache
anthropic=eval/cache/cmp-anthropic-full --backend-cache
anthropic-agentic=eval/cache/cmp-anthropic-agentic-full`.

**The question:** today, `routing/score.py` forces a document to review the
instant `validate.rules` H2/H3 finds line items or a subtotal that don't
reconcile with the stated total -- no matter how confident the model was. Would
giving the model a `validate_arithmetic` tool (client-side recomputation, no
LLM in the check) *during* extraction let it catch and fix that itself, so the
document auto-accepts instead?

### The numbers (n=361, zero infrastructure errors on any backend after one bug fix -- see below)

| backend | auto-accept | crit. P (`total`, the only SROIE-labeled critical field) | $/doc | total $ | p50 latency | p95 latency |
|---|---|---|---|---|---|---|
| gemini | 117/361 (32.4%) | 116/117 (99.1%) | $0.00090 | $0.33 | 8.94s | 20.62s |
| anthropic | 79/361 (21.9%) | 76/79 (96.2%) | $0.00482 | $1.74 | 4.09s | 6.63s |
| anthropic-agentic | 126/361 (34.9%) | 121/126 (96.0%) | $0.01538 | $5.55 | 8.48s | 13.83s |

Pricing and its citation/date: `eval/cost.py`. Total spend across all three
backends: $7.62, in line with the ~$7-8 estimated ahead of the run.

**The single-call ranking flips at scale.** The 35-document slice had
`anthropic` (25.7%) auto-accepting more than `gemini` (22.9%). At n=361 that
reverses: `gemini` auto-accepts 32.4% against `anthropic`'s 21.9%, a 10.5-point
gap in the other direction. `anthropic-agentic` is still the highest
auto-accept rate of the three, but the baseline it should be measured against
matters: its edge is +13.0 points over `anthropic` and only **+2.5 points over
`gemini`**, the backend that turned out to be the stronger single-call option.

### Critical precision is no longer a clean 100% on any backend

Every backend's accepted `total` matched gold exactly on the 35-document
slice; that does not hold at n=361. One document, `X51005806696`, is wrong on
all three backends -- it is the same false accept already documented in FC-1
(every hard and soft rule passes; no signal anywhere in the pipeline that it
differs from a correct one). The remaining misses are backend-specific: 0 more
for `gemini` (its 1 miss is `X51005806696` alone), 2 more for `anthropic`
(`X51005745213`, `X51007103687`), and 4 more for `anthropic-agentic`
(`X51006328967`, `X51006388081`, `X51006619784`, `X51007339638`).

### The tool's new failure mode: reconciling a document that wasn't wrong

All 4 of `anthropic-agentic`'s backend-specific misses share a shape: the
plain `anthropic` backend read a `total` that already matched gold, but that
`total` didn't arithmetically reconcile with the same backend's own
(misread) `tax` or `subtotal` reading -- so H2/H3 correctly forced review, for
the right underlying reason even though the headline number was fine. Given
`validate_arithmetic` and prompted to make its numbers agree, the agentic
backend didn't re-read the source for the actual error; it adjusted `total`
(or `tax`) until the arithmetic closed, landing on an internally consistent
but factually wrong number that then cleared every rule and auto-accepted.
`X51006328967` (gold `total` `62.00`) is representative:

| | `anthropic` (baseline) | `anthropic-agentic` |
|---|---|---|
| decision | review (H2 fails: 62.0 + 3.51 != 62.0) | accept (clean) |
| `subtotal` / `tax` / `total` | `62.0` / `3.51` / `62.0` (total correct, inconsistent) | `62.0` / `3.51` / `65.51` (total now = subtotal+tax, and wrong) |

The line items and `subtotal` never changed between runs and were correct
throughout; only `total` moved, from a correct-but-inconsistent value to a
consistent-but-wrong one. This is the exact failure this project's precision
posture exists to catch -- a confidently-wrong number that reconciles and
writes silently -- produced here by the self-correction mechanism itself
rather than by a plain misread. `X51006388081`, `X51006619784`, and
`X51007339638` follow the same shape (see the cache entries for the full
per-document detail; not reproduced here for space).

### The recovery mechanism still works, and at a larger scale

50 documents were auto-accepted by `anthropic-agentic` where `anthropic`'s own
extraction of the *same document* hard-failed H2 or H3 -- the generalization
of the 4-document gain the 35-doc slice showed. `X51005442322` (gold `total`
`269.40`) is illustrative:

| | `anthropic` (baseline) | `anthropic-agentic` |
|---|---|---|
| decision | review (H2 fails: 231.06 + 15.25 != 269.40) | accept (clean) |
| `subtotal` / `tax` / `total` | `231.06` / `15.25` / `269.4` | `231.06` / `38.35` / `269.4` |

The baseline misread `tax` (`15.25`, inconsistent with its own correct
`total`); given the tool, the model corrected `tax` to `38.35` (231.06 + 38.35
= 269.41, within tolerance of 269.40) and kept the already-correct `total` --
the tool fixing the actual misread field rather than moving the correct one,
unlike the failure mode above.

### It is not strictly monotonic

9 documents `anthropic` had accepted were sent to review by
`anthropic-agentic` instead. Of those, 8 are true regressions: `anthropic`'s
`total` was correct and cleanly accepted, and the agentic backend's
self-correction volunteered additional detail that broke a consistency check
the shorter answer had passed. `X51005288570` (gold `total` `1.00`) is
representative: the baseline reported one line item (`Parking Fee`, `0.94`)
against `subtotal 0.94` / `tax 0.06` / `total 1.00` -- consistent, and
correct, so it accepted. Prompted to double-check, the agentic backend split
the receipt into two line items (`Parking fee 0.94`, `Add GST 0.06`) without
updating `subtotal` to match their sum -- so H3 (line items vs subtotal) now
fires on an inconsistency the single-line reading never exposed. `total`
itself is unchanged and correct in both versions. The 9th case,
`X51007103687`, is not a regression: `anthropic`'s own `total` (`2.0`) was
already wrong against gold (`1.90`), and the agentic backend correctly
declined to accept it -- a precision save routed through the recall column,
not a loss.

### Cost and latency

The agentic backend's extra bounded self-correction rounds cost real tokens:
~3.2x anthropic's cost per document ($0.01538 vs $0.00482) and ~2.1x its p50
latency (8.48s vs 4.09s) -- both from the same `claude-haiku-4-5` calls, so
price and speed are not confounded by a model swap. This closely matches the
35-document slice's ~3.07x/~2x figures, so the cost/latency multiplier
replicates cleanly at scale even though the accept-rate and precision numbers
it's traded against do not. Whether a several-point accept-rate gain (over
whichever single-call backend is actually stronger) at ~3x the per-document
cost, plus the new precision-miss pattern above, is worth it is a product
decision this finding does not make for the reader; the trade is stated in
full so that decision can be made.

### Two live-API bugs this project's runs have surfaced and fixed

The 35-document run hit a real 400 from the Anthropic API on one document:
`messages.2: tool_use ids were found without tool_result blocks immediately
after`. Cause: `AnthropicAgenticBackend`'s forced initial `extract_document`
call assumed exactly one `tool_use` block in the response and replied to only
the first one found; a `tool_choice` that forces a named tool does not
guarantee the model calls it only once. Fixed in
`src/docfield/backends/anthropic_agentic.py` to reply to every `tool_use`
block in a turn, with a regression test
(`tests/test_anthropic_agentic.py::test_initial_forced_call_with_duplicate_tool_use_gets_every_block_a_result`).

The full-split run surfaced a second, independent bug: `_validate_arithmetic`
crashed with `TypeError: float() argument must be a string or a real number,
not 'NoneType'` on one document. Cause: `_VALIDATE_TOOL_DEFINITION` declares
`line_item_amounts` items as `{"type": "number"}`, but tool-call output is not
schema-enforced, and the model called the tool with a `null` entry in the
list; `[float(a) for a in tool_input.get("line_item_amounts") or []]` guards
against the whole list being absent but not against a `None` inside it. The
bounded per-document retry (rule 6) exhausted all 3 attempts on this document
-- unlike the first bug, this one did not self-heal on retry, since the model
kept sending the same `null` entry -- so the document surfaced as a genuine
`error=True` cache entry, distinct from a rule-driven review. Fixed in
`src/docfield/backends/anthropic_agentic.py` to treat a `None` amount the same
way `validation.rules._sum_line_amounts` treats a missing line-item amount --
reconciliation reported as incomplete rather than raised -- with a regression
test
(`tests/test_anthropic_agentic.py::test_validate_arithmetic_handles_null_line_item_amount`).
The single affected document was re-predicted with `--retry-errors` after the
fix landed; the numbers above include that corrected entry, not the crash.

### Caveats

- SROIE labels only `total` among the three critical fields (see FC-3's
  discussion of the same gap), so this run cannot speak to precision on
  `tax`/`invoice_number` at all -- `eval/compare.py` restricts the metric to
  labeled-and-gold-present fields for exactly this reason (mirroring
  `eval/score.py`'s existing `_critical_labeled`).
- n=361 is the full SROIE test split, not a larger benchmark; the single-digit
  miss counts per backend (1, 3, 5) mean a percentage point of critical
  precision here is worth roughly 3-4 documents -- read the precision deltas
  between backends as directional, not as statistically separated from each
  other.
