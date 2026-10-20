"""How a cell's call is made when a gate and the classifier both have one.

Two rules (`RULES`):

- **`score_0.5`** -- the classifier alone: positive at a score of 0.5 and
  above. What a panel run draws.
- **`gate_unless_confident`** -- the gate's call stands unless the
  classifier is confident the other way: a positive above the gate is taken
  away when its score is below `c_clean`, a negative below the gate is added
  when its score is above `1 - c_add`. What a gating session's layer calls.

The two margins are separate because the two mistakes are not equally
likely. On images with poor signal the classifier over-calls -- it finds
positives in background -- so its negatives above a gate are the better
evidence and its positives below it the weaker (the user's observation on
2026-10-19, measured per direction in `docs/internal/bench/CLASSIFIER_RULE_*`).
`c_add=None` disables additions: the layer only cleans.

"Gate unless confident" is the one per-cell rule with measured support: on
the removed per-cell model (2026-10-10, `per-cell-model-experiment` in the
bench notes) it was right 61 of 66 times near the gate and 10 of 11 far from
it, where the model's own 0.5 call lost to the gate.

Pure numpy; the session layer, the viewer's codes and the bench all call
`apply`, so what is measured is what ships.
"""

from __future__ import annotations

import numpy as np

#: Bumped whenever `apply` would call any cell differently.
VERSION = "1"

RULES = ("score_0.5", "gate_unless_confident")
DEFAULT_RULE = "gate_unless_confident"

#: [cal] Default margins, from the per-cell rule bench of 2026-10-19
#: (docs/internal/bench/CLASSIFIER_RULE_2026-10-19.md; 895 of the user's own
#: labelled cells on three images). A positive above the gate whose score is
#: below C_CLEAN is called negative: at 0.3 every such removal among the
#: labelled cells was right (23 a seed), balanced accuracy +2.0 points over
#: the gate pooled, +2.3 on the held-out images, no image worse, the far set
#: unchanged. Additions are off (C_ADD None): the classifier's own positives
#: below the gate were right 47 % of the time, and both margins together
#: missed the bar set before the run (+3 points). The benched images are
#: cleaner than most (the LSP ones especially, by the user's account), so the
#: values lean to what suits poorer ones: never tune them on LSP alone.
C_CLEAN = 0.3
C_ADD = None


def confident_negative(scores, c_clean):
    return np.asarray(scores) < float(c_clean)


def confident_positive(scores, c_add):
    return np.asarray(scores) > 1.0 - float(c_add)


def apply(scores, gate_calls, rule=DEFAULT_RULE, *, c_add=C_ADD, c_clean=C_CLEAN,
          agreement=None, min_agreement=None):
    """int8 calls per row: 1 / 0, and -1 where the gate does not call the
    cell (QC ruled it out, no value) or the classifier has no score.

    `agreement` (share of resampled models agreeing with the classifier's own
    call) with `min_agreement`: an override also needs that much agreement.
    `c_add=None` never adds; `c_clean=None` never removes."""
    scores = np.asarray(scores, dtype=np.float32)
    gate_calls = np.asarray(gate_calls, dtype=np.int8)
    if rule not in RULES:
        raise ValueError(f"rule is one of {RULES}, not {rule!r}")
    scored = np.isfinite(scores) & (gate_calls >= 0)
    out = np.full(gate_calls.shape, -1, dtype=np.int8)
    if rule == "score_0.5":
        out[scored] = (scores[scored] >= 0.5).astype(np.int8)
        return out
    out[scored] = gate_calls[scored]
    sure = np.ones(scores.shape, dtype=bool)
    if agreement is not None and min_agreement is not None:
        sure = np.nan_to_num(np.asarray(agreement, dtype=np.float32), nan=0.0) >= float(min_agreement)
    if c_clean is not None:
        out[scored & (gate_calls == 1) & sure & confident_negative(
            np.nan_to_num(scores, nan=1.0), c_clean)] = 0
    if c_add is not None:
        out[scored & (gate_calls == 0) & sure & confident_positive(
            np.nan_to_num(scores, nan=0.0), c_add)] = 1
    return out


def compare(calls, gate_calls, raw_calls=None) -> dict:
    """Where the calls and the gate part ways, counted.

    `n_added` positives below the gate, `n_removed` negatives above it; with
    `raw_calls` (the classifier's own 0.5 call), `n_raw_added` /
    `n_raw_removed` -- how often it disagreed before the rule weighed it."""
    calls = np.asarray(calls, dtype=np.int8)
    gate_calls = np.asarray(gate_calls, dtype=np.int8)
    both = (calls >= 0) & (gate_calls >= 0)
    out = {"n_called": int(both.sum()),
           "n_agree": int((both & (calls == gate_calls)).sum()),
           "n_added": int((both & (calls == 1) & (gate_calls == 0)).sum()),
           "n_removed": int((both & (calls == 0) & (gate_calls == 1)).sum()),
           "n_gate_positive": int((both & (gate_calls == 1)).sum()),
           "n_positive": int((both & (calls == 1)).sum())}
    if raw_calls is not None:
        raw = np.asarray(raw_calls, dtype=np.int8)
        both_raw = both & (raw >= 0)
        out["n_raw_added"] = int((both_raw & (raw == 1) & (gate_calls == 0)).sum())
        out["n_raw_removed"] = int((both_raw & (raw == 0) & (gate_calls == 1)).sum())
    return out


# -- signal: how far the layer may be trusted ---------------------------------

#: [cal] Signal grade (`grade`), on the mixture fit's Ashman D (the two
#: populations' separation in their pooled sd; 2 is "clearly two"): below
#: SNR_LOW the layer never adds. UNMEASURED: the rule bench could not place
#: it (add precision did not fall with D on its 36 markers). There is no
#: grade that skips a marker: a skip at D 0.8 withheld p193's CD8a (D 0.67),
#: where the classifier at the gate found most of the weak positives the
#: user gates (2026-10-19). The pixel contrast of the cells above the gate is
#: reported beside it but not graded.
SNR_LOW = {"d": 2.0}
GRADES = ("ok", "low")


def ashman_d(means, sds) -> float | None:
    """Ashman's D of a two-component fit's background and positive
    components (the two with the lowest and highest mean)."""
    if means is None or sds is None or len(means) < 2 or len(sds) < 2:
        return None
    order = np.argsort(np.asarray(means, dtype=np.float64))
    m = np.asarray(means, dtype=np.float64)[order]
    s = np.asarray(sds, dtype=np.float64)[order]
    pooled = np.sqrt((s[0] ** 2 + s[-1] ** 2) / 2.0)
    if not np.isfinite(pooled) or pooled <= 0:
        return None
    return float(abs(m[-1] - m[0]) / pooled)


def grade(d=None, contrast=None) -> str:
    """"ok" or "low" from the measures with a bar (`SNR_LOW`); with none
    known the grade is "low" (nothing says the classifier sees the stain)."""
    known = [(v, key) for v, key in ((d, "d"), (contrast, "contrast"))
             if key in SNR_LOW and v is not None and np.isfinite(v)]
    if not known:
        return "low"
    if any(v < SNR_LOW[key] for v, key in known):
        return "low"
    return "ok"
