"""The wand's examples: clear positives and clear negatives, spread across the
image, picked without a click.

Clear by the marker's own distribution: the mixture fit Thresholding's Auto
uses (`model.gmm_for`) proposes where positive starts. Positives are drawn
from the brighter part of the cells above it -- not the very brightest,
which is where debris and edge artefacts sit -- and negatives from a wide
spread of the cells below it, dim and middling both, so the model learns
more than "bright against black". Nothing is drawn from near the proposed
gate: those are the cells the model is for. A marker the fit cannot split
falls back to the top and bottom of its ranks.

With the marker's pixels in hand (features.py), three more rules make the
picks harder to get wrong (`Recipe`):

- **Two measures agree.** A positive must read positive by its pixels as
  well as by the table -- its mean above the pixel level that cuts off the
  same share of cells as the gate, and brighter than the tissue around it
  -- and a negative must read negative by both.
- **Not glow.** A positive whose brightest neighbour is more than twice as
  bright is passed over: that is how light spilling from a neighbour looks,
  and labelled positive it teaches the model the one mistake it is for.
- **Hard negatives.** A share of the negatives are cells below the gate
  that sit beside a cell reading positive and are much dimmer than it --
  the cells a threshold gets wrong when glow lifts them, so the model sees
  what a dim cell beside a bright one is.

Spread by place: the image is cut into a grid and the picks go round its
squares in turn, so the positives are many parts of the tissue, not one
bright patch. Seeded, so the same project picks the same cells again.

Measured (2026-10-19) on the user's own labelled cells near the gate -- 822
across three images (CyCIF, IMC, CODEX), three seeds each -- the first wand
(FIRST: ten a class, by value alone) agreed with them 61%, below the
mixture-fit gate it picks from (71%); this recipe 72%, with half the amber
and steadier calls from seed to seed. Nearly all of the gain is the hard
negatives, and it is their glow rule that does it: negatives drawn as near
the gate without it scored 66%. More picks alone changed nothing but the
amber; 20 a class scored 70.5%, 80 no better than 40; alternating between
brightness bands made no difference and was dropped. On the cells far from
the gate nothing got worse (77%, as the gate).

Never a cell QC rules out, never one without a value or a position, and
never one a person has already labelled for this marker.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from plexora.plugins.gating.server.classifier import ClassifierRefused

#: Bumped whenever the same project, marker, gate and seed would pick other
#: cells.
VERSION = "2"

#: Examples of each class the wand picks.
N_PER_CLASS = 40

#: Where in each class's ranks (0 dimmest, 1 brightest) picks may come from.
POSITIVE_RANKS = (0.35, 0.98)
NEGATIVE_RANKS = (0.02, 0.75)
#: Hard negatives: nearer the gate than the others, but still below it.
HARD_NEGATIVE_RANKS = (0.3, 0.95)

#: A cell is glow-like when its brightest neighbour is at least this many
#: times brighter than it.
GLOW_RATIO = 2.0

#: Without a fitted gate: the brightest and dimmest shares of all cells.
FALLBACK_POSITIVE = (0.93, 0.995)
FALLBACK_NEGATIVE = (0.0, 0.5)


@dataclass(frozen=True)
class Recipe:
    """How the wand picks; RECIPE is what it does, FIRST what it did first
    (kept so the two can be compared)."""

    count: int = N_PER_CLASS
    #: Positives and negatives must agree with the pixels as well as the table.
    agree: bool = True
    #: Positives dominated by a brighter neighbour are passed over.
    glow: bool = True
    #: Share of the negatives that are hard ones.
    hard_share: float = 1 / 3


RECIPE = Recipe()
FIRST = Recipe(count=10, agree=False, glow=False, hard_share=0.0)


def _band(values, rows, ranks):
    """The rows of `rows` whose value lies within the `ranks` share of them."""
    if rows.size == 0:
        return rows
    subset = values[rows]
    low, high = np.quantile(subset, ranks)
    return rows[(subset >= low) & (subset <= high)]


def _order(rows, xs, ys, side, rng):
    """Every row of `rows`, round-robin over a side x side grid of their
    extent, so any prefix is spread across the tissue."""
    if rows.size == 0:
        return rows
    x, y = xs[rows], ys[rows]
    gx = np.minimum(((x - x.min()) / max(np.ptp(x), 1e-9) * side).astype(int), side - 1)
    gy = np.minimum(((y - y.min()) / max(np.ptp(y), 1e-9) * side).astype(int), side - 1)
    squares = {}
    for row, key in zip(rows.tolist(), (gy * side + gx).tolist()):
        squares.setdefault(key, []).append(row)
    order = list(squares)
    rng.shuffle(order)
    for key in order:
        rng.shuffle(squares[key])
    out = []
    while True:
        moved = False
        for key in order:
            if squares[key]:
                out.append(squares[key].pop())
                moved = True
        if not moved:
            break
    return np.asarray(out, dtype=np.int64)


def _spread(rows, xs, ys, count, rng):
    """`count` of `rows`, round-robin over a grid of the tissue's extent."""
    if rows.size <= count:
        return rows
    side = max(2, math.ceil(math.sqrt(count * 1.5)))
    return _order(rows, xs, ys, side, rng)[:count]


class _Pixels:
    """The marker's per-cell pixel mean (linear), its contrast with the
    tissue around it, and its brightest neighbour's mean -- or the table's
    value when there are no pixels and the table is not logged."""

    def __init__(self, ds, geometry, values, matrix, names):
        from plexora.plugins.gating.server.classifier.features import _neighbour_values

        self.source = None
        self.mean = self.contrast = self.peak = None
        names = list(names or [])
        if matrix is not None and "px_mean" in names:
            self.source = "pixels"
            # features._transform_pixels: log1p of the clipped mean.
            self.mean = np.expm1(np.asarray(matrix[:, names.index("px_mean")], np.float64))
            if "px_contrast" in names:
                self.contrast = np.asarray(matrix[:, names.index("px_contrast")], np.float64)
        elif not ds.table.log_transformed:
            self.source = "table"
            self.mean = values
        if self.mean is not None:
            _mean, self.peak = _neighbour_values(geometry, self.mean)
            if self.peak is None:
                self.source = None


def pick(ds, marker, *, exclude=(), seed=0, recipe=RECIPE, matrix=None, names=None,
         gate=None, exclude_range=None):
    """(positive cell ids, negative cell ids, info).

    `matrix`/`names`: the marker's feature matrix (features.feature_matrix),
    whose pixel columns the agreement, glow and hard-negative rules read.
    Without them the picks are by the table alone.

    `gate`: the value positive starts at, in the table's units -- a gating
    session's gate; None picks from the mixture fit's (`model.gmm_for`), as
    the panel's wand does. `exclude_range` (low, high): no cell whose value
    lies inside it is picked -- the bands just either side of a session's
    gate, which are what its layer is asked to judge, never what it learns
    from.
    """
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.classifier import runs
    from plexora.plugins.gating.server.classifier.geometry import cells

    count = int(recipe.count)
    geometry = cells(ds)
    values = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
    eligible = geometry.valid & np.isfinite(values)
    skip, _fp, _mode = runs.qc_state(ds, marker)
    if skip is not None and skip.shape[0] == eligible.shape[0]:
        eligible &= ~skip
    if exclude:
        eligible &= ~np.isin(geometry.ids, np.asarray(list(exclude), dtype=np.int64))
    if exclude_range is not None:
        lo, hi = (float(v) for v in exclude_range)
        eligible &= ~((values >= lo) & (values <= hi))
    rows = np.flatnonzero(eligible)
    if rows.size < 6:
        raise ClassifierRefused(f"Too few cells with a value for {marker} to pick examples from")
    gate_from = "given" if gate is not None else "mixture"
    if gate is None:
        try:
            gate = (model.gmm_for(ds, marker) or {}).get("gate")
        except Exception:
            gate = None
    how = "gate"
    hard = np.zeros(0, dtype=np.int64)
    glow_test = None
    if gate is not None:
        gate = float(gate)
        above = rows[values[rows] > gate]
        below = rows[values[rows] <= gate]
        positives = _band(values, above, POSITIVE_RANKS)
        negatives = _band(values, below, NEGATIVE_RANKS)
        near = _band(values, below, HARD_NEGATIVE_RANKS)
        pixels = _Pixels(ds, geometry, values, matrix, names) if (
            recipe.agree or recipe.glow or recipe.hard_share > 0) else None
        if pixels is not None and pixels.source is not None:
            glow_test = pixels.source
            mean, peak = pixels.mean, pixels.peak
            # The pixel level cutting off the same share of cells as the gate.
            share_below = below.size / max(rows.size, 1)
            finite = rows[np.isfinite(mean[rows])]
            cut = (float(np.quantile(mean[finite], share_below)) if finite.size
                   else float("nan"))
            if recipe.agree and pixels.source == "pixels" and np.isfinite(cut):
                keep = mean[positives] > cut
                if pixels.contrast is not None:
                    keep &= pixels.contrast[positives] > 0
                positives = positives[keep]
                negatives = negatives[mean[negatives] <= cut]
                near = near[mean[near] <= cut]
            if recipe.glow:
                lit = np.isfinite(peak[positives])
                positives = positives[~lit | (peak[positives] < GLOW_RATIO * mean[positives])]
            if recipe.hard_share > 0 and np.isfinite(cut):
                bright_neighbour = np.isfinite(peak[near]) & (peak[near] > cut)
                hard = near[bright_neighbour & (peak[near] >= GLOW_RATIO * mean[near])]
    if gate is None or positives.size < 3 or negatives.size < 3:
        how = "ranks"
        positives = _band(values, rows, FALLBACK_POSITIVE)
        negatives = _band(values, rows, FALLBACK_NEGATIVE)
        hard = np.zeros(0, dtype=np.int64)
    rng = np.random.default_rng(seed)
    pos_rows = _spread(positives, geometry.xs, geometry.ys, count, rng)
    hard_rows = _spread(hard, geometry.xs, geometry.ys, int(round(count * recipe.hard_share)),
                        rng)
    negatives = negatives[~np.isin(negatives, hard_rows)]
    neg_rows = np.concatenate([
        hard_rows, _spread(negatives, geometry.xs, geometry.ys, count - hard_rows.size, rng)])
    if pos_rows.size < 3 or neg_rows.size < 3:
        raise ClassifierRefused(f"No clear positive and negative cells to pick for {marker}; "
                                "label a few yourself and press Train")
    return ([int(v) for v in geometry.ids[pos_rows]], [int(v) for v in geometry.ids[neg_rows]],
            {"how": how, "gate": gate, "gate_from": gate_from,
             "exclude_range": None if exclude_range is None else [float(v) for v in exclude_range],
             "version": VERSION,
             "candidates_positive": int(positives.size),
             "candidates_negative": int(negatives.size),
             "hard_negatives": int(hard_rows.size),
             "glow_test": glow_test,
             "recipe": asdict(recipe)})
