# Classifier wand: picking its own examples, 2026-10-19

The Single Cell Classifier's wand picks positive and negative examples
itself and trains on them (`plugins/gating/server/classifier/autopick.py`).
This bench asks two questions:

- Which way of picking agrees best with cells a person labelled?
- Does it help to then label a few uncertain cells?

**Yardstick.** The user's own labelled cells, never used as picks:

- the near-gate label galleries of 2026-10-09: lsp11385_2 (CyCIF),
  imc-immucan-example (IMC) and codex-hubmap-lymphnode (CODEX), 822 cells
  in all;
- the far-from-gate set of 2026-10-10 on lsp11385_2 (73 cells).

"own" counts as positive; "glow" and "none" count as negative; "unsure"
labels and inconsistent repeats are dropped. The CRC set was left out: its
DICOM image reads slowly from a synced folder.

**Method.** Each marker, recipe and seed (3 seeds) runs the shipped picker
and learner (`learner.fit`, never calibrated, as for a wand run). The
mixture-fit gate it picks from is the baseline: `gate(gmm)`.

The scripts are in `tools/bench_classifier_wand/`. Run them against a copy
of the data root.

## Picking (near-gate cells, pooled over 3 images x 3 seeds)

| recipe | agreement | balanced | amber, labelled cells | amber, all cells | same call across seeds |
|---|---|---|---|---|---|
| gate(gmm), the baseline | 70.8% | 0.708 | — | — | — |
| first wand: 10 a class, by value | 60.8% | 0.671 | 13.7% | ~4% | 0.97 |
| 40 a class, by value | 60.9% | 0.674 | 7.2% | ~2% | 0.99 |
| + positives agree with pixels, not glow | 61.8% | 0.678 | 7.8% | ~2% | 0.99 |
| + a third hard negatives (**shipped**) | **72.1%** | **0.733** | 11.2% | ~2% | 0.99 |
| same, no agreement or glow rule on positives | 70.8% | 0.728 | 11.6% | | |
| control: a third of the negatives drawn as near the gate, no glow rule | 66.2% | 0.698 | 10.5% | | |
| hard negatives, 20 a class | 70.5% | 0.723 | 14.6% | | |
| hard negatives, 80 a class | 71.9% | 0.732 | 7.0% | | |
| hard negatives, half the negatives | 72.7% | 0.734 | 10.6% | | |
| hard negatives + alternating brightness thirds | 71.6% | 0.730 | 11.3% | | |

**Per image, shipped recipe against the gate it picks from:**

| image | gate | shipped | first wand |
|---|---|---|---|
| lsp11385_2 | 55.5% | 63.2% | 49.9% |
| IMC | 76.0% | 74.5% | 67.8% |
| CODEX LN | 81.4% | 79.0% | 65.2% |

**Far from the gate** (lsp11385_2 only): every recipe scores 77–79%, the
same as the gate's 77%. Nothing got worse.

**Reading:**

- The first wand was clearly worse than the gate it picks from.
- Nearly all of the gain comes from the hard negatives: cells below the gate
  that sit beside a much brighter cell reading positive.
- It is their glow rule that helps, not just being nearer the gate. The
  control that draws negatives as near the gate without the rule loses 6
  points against it.
- More picks alone only cut the amber.
- The agreement and glow rules on positives add about a point.
- Neither brightness bands nor a larger hard share earned their keep. The
  brightness bands were removed from the code.

**Also tried and dropped:** using the gate's call wherever the model is
amber. It moved agreement −1.3 to +1.0 points by image.

## Reviewing uncertain cells (simulated)

Each marker's labelled cells are split in two halves. The model asks about
k cells of one half, the user's labels for them are added at full weight,
and the model is scored on the other half.

| | agreement on the held-out half |
|---|---|
| gate | 70.8% |
| wand only | 71.7% |
| + 4 asked, least agreed-on first | 76.1% |
| + 4 asked, at random from the half | 76.8% |
| + 8 asked, least agreed-on first | 79.1% |
| + 8 asked, at random from the half | 78.8% |

A few labels on cells near the boundary add 5–7 points. Ordering them by
disagreement adds nothing over choosing any of them: every cell in this
pool is a near-gate cell already.

What the simulation cannot test: whether amber cells beat cells chosen at
random from the whole image. Mostly those are easy cells.

So the panel's review offers the amber cells spread across the tissue
(`panel.review_cells`), not ordered by disagreement.

## Caveats

- Three images, one each of CyCIF, IMC and CODEX.
- The near-gate sets were centred on the human gate (lsp11385_2) or on the
  automatic proposal of the day (the others), not on today's mixture-fit
  gate.
- The recipe's numbers were read off these same images. Nothing was held
  out.
