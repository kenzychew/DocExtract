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
**Impact:** auto-accept precision on `total` is **98.4% (61/62)** on held-out
data, not the 100% (18/18) measured on the tuning slice.
This is the single document that separates the two figures.

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
