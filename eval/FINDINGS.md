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

## FC-4 -- Giving the model an arithmetic tool recovers real documents, at ~3x cost, without hurting precision -- and isn't free of side effects

**Status:** closed as understood. Reported as-is; nothing tuned against it.
**Found:** live three-way comparison of `gemini`, `anthropic`, and
`anthropic-agentic` on the same 35-document SROIE slice (the first 35
documents in dataset order; a bounded slice chosen for live-API time/cost, not
a held-out claim -- this run measures backend behavior at the existing fixed
`CONFIDENCE_THRESHOLD=0.50`, it does not fit anything, so tuning/held-out
contamination does not apply). Run with `claude-haiku-4-5` (Anthropic) and
`gemini-2.5-flash`, 2026-09-01. Reproduce with
`uv run python -m eval.run_eval predict --dataset sroie --limit 35 --backend <name> --cache-base eval/cache/cmp-<name>`
per backend, then `uv run python -m eval.run_eval compare --dataset sroie
--backend-cache gemini=eval/cache/cmp-gemini --backend-cache
anthropic=eval/cache/cmp-anthropic --backend-cache
anthropic-agentic=eval/cache/cmp-anthropic-agentic`.

**The question:** today, `routing/score.py` forces a document to review the
instant `validate.rules` H2/H3 finds line items or a subtotal that don't
reconcile with the stated total -- no matter how confident the model was. Would
giving the model a `validate_arithmetic` tool (client-side recomputation, no
LLM in the check) *during* extraction let it catch and fix that itself, so the
document auto-accepts instead?

### The numbers (n=35, zero infrastructure errors on any backend)

| backend | auto-accept | crit. P (`total`, the only SROIE-labeled critical field) | $/doc | p50 latency | p95 latency |
|---|---|---|---|---|---|
| gemini | 8/35 (22.9%) | 8/8 (100%) | $0.00090 | 9.30s | 17.25s |
| anthropic | 9/35 (25.7%) | 9/9 (100%) | $0.00479 | 4.55s | 6.70s |
| anthropic-agentic | 12/35 (34.3%) | 12/12 (100%) | $0.01470 | 9.10s | 14.34s |

Pricing and its citation/date: `eval/cost.py`. Every auto-accepted document's
`total` matched gold exactly on every backend -- the hard-rule gate held, with
or without the extra tool.

### The direct answer

4 documents (`X51005230621`, `X51005442322`, `X51005442343`, `X51005444044`)
were auto-accepted by `anthropic-agentic` where the plain `anthropic` backend's
own extraction of the *same document* hard-failed H2 or H3. One is illustrative:
`X51005230621` (gold `total` `7.30`) --

| | `anthropic` (baseline) | `anthropic-agentic` |
|---|---|---|
| decision | review (H2, H3 fail) | accept (clean) |
| `subtotal` / `tax` / `total` | `7.3` / `6.69` / `7.3` | `7.3` / `0.0` / `7.3` |
| line items | `1.887 + 5.0 = 6.887` | `2.0 + 5.3 = 7.3` |

The baseline misread the tax field (`6.69`, nonsensical against a `7.3` total)
and the line items didn't sum either way. Given `validate_arithmetic`, the
model re-read the document, corrected both the tax reading and the line
items, and landed on a self-consistent record that also matches gold -- the
tool caught what H2/H3 would have caught downstream anyway, just early enough
for the model to act on it instead of the document going to review.

### It is not strictly monotonic

Net accept-rate gain is +3 (12 vs 9), not +4: `anthropic-agentic` also lost one
document `anthropic` had cleanly accepted, `X51005230616` (gold `total`
`38.90`, correct on both backends):

| | `anthropic` (accepted) | `anthropic-agentic` (review) |
|---|---|---|
| `subtotal` / `tax` / `total` | `None` / `2.2` / `38.9` | `38.9` / `2.2` / `38.9` |

The baseline left `subtotal` blank, so H2 (which *skips*, not fails, when an
input is absent) never ran against it. Prompted to double-check, the agentic
backend filled in a `subtotal` of `38.9` -- and `38.9 + 2.2 != 38.9`, so H2 now
correctly fires on an inconsistency between the model's own tax and subtotal
readings that was always latent, just previously invisible by omission. `total`
itself is unchanged and still correct in both versions. Whether "the model
volunteered a number that exposed a real internal contradiction" counts as the
tool working as intended or as a side effect is a fair question either way; it
is reported here rather than smoothed over.

### Cost and latency

The agentic backend's extra bounded self-correction rounds cost real tokens:
~3.07x anthropic's cost per document and ~2x its p50 latency (both from the
same `claude-haiku-4-5` calls -- price and speed are not confounded by a model
swap). Whether an 8.6-point accept-rate gain (25.7% -> 34.3%) at 3x the
per-document cost is worth it is a product decision this finding does not make
for the reader; the trade is stated in full so that decision can be made.

### A live-API bug this run surfaced and fixed

The first attempt at this run hit a real 400 from the Anthropic API on one
document: `messages.2: tool_use ids were found without tool_result blocks
immediately after`. Cause: `AnthropicAgenticBackend`'s forced initial
`extract_document` call assumed exactly one `tool_use` block in the response
and replied to only the first one found; a `tool_choice` that forces a named
tool does not guarantee the model calls it only once. The bounded per-document
retry (rule 6) papered over it -- the whole loop re-ran and the second attempt
succeeded -- so it never surfaced as a failed document, only as one wasted
round-trip's worth of latency. Fixed in `src/docfield/backends/anthropic_agentic.py`
to reply to every `tool_use` block in a turn, with a regression test
(`tests/test_anthropic_agentic.py::test_initial_forced_call_with_duplicate_tool_use_gets_every_block_a_result`);
the comparison above is the re-run with the fix applied, not the run that hit it.

### Caveats

- n=35 is a bounded slice for a same-day live comparison, not a
  statistically-powered sample; treat the 100% critical precision on every
  backend as "no wrong `total` was auto-accepted on this slice", not as a
  precision guarantee at scale.
- SROIE labels only `total` among the three critical fields (see FC-3's
  discussion of the same gap), so this run cannot speak to precision on
  `tax`/`invoice_number` at all -- `eval/compare.py` restricts the metric to
  labeled-and-gold-present fields for exactly this reason (mirroring
  `eval/score.py`'s existing `_critical_labeled`).
