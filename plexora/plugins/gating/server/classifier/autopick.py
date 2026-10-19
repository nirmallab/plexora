"""The wand's examples: about ten clear positives and ten clear negatives,
spread across the image, picked without a click.

Clear by the marker's own distribution: the mixture fit Thresholding's Auto
uses (`model.gmm_for`) proposes where positive starts. Positives are drawn
from the brighter part of the cells above it -- not the very brightest,
which is where debris and edge artefacts sit -- and negatives from a wide
spread of the cells below it, dim and middling both, so the model learns
more than "bright against black". Nothing is drawn from near the proposed
gate: those are the cells the model is for. A marker the fit cannot split
falls back to the top and bottom of its ranks.

Spread by place: the image is cut into a grid and the picks go round its
squares in turn, so ten positives are ten parts of the tissue, not one
bright patch. Seeded, so the same project picks the same cells again.

Never a cell QC rules out, never one without a value or a position, and
never one a person has already labelled for this marker.
"""

from __future__ import annotations

import math

import numpy as np

from plexora.plugins.gating.server.classifier import ClassifierRefused

#: Examples of each class the wand picks.
N_PER_CLASS = 10

#: Where in each class's ranks (0 dimmest, 1 brightest) picks may come from.
POSITIVE_RANKS = (0.35, 0.98)
NEGATIVE_RANKS = (0.02, 0.75)

#: Without a fitted gate: the brightest and dimmest shares of all cells.
FALLBACK_POSITIVE = (0.93, 0.995)
FALLBACK_NEGATIVE = (0.0, 0.5)


def _band(values, rows, ranks):
    """The rows of `rows` whose value lies within the `ranks` share of them."""
    if rows.size == 0:
        return rows
    subset = values[rows]
    low, high = np.quantile(subset, ranks)
    return rows[(subset >= low) & (subset <= high)]


def _spread(rows, xs, ys, count, rng):
    """`count` of `rows`, round-robin over a grid of the tissue's extent."""
    if rows.size <= count:
        return rows
    side = max(2, math.ceil(math.sqrt(count * 1.5)))
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
    picked = []
    while len(picked) < count:
        moved = False
        for key in order:
            if squares[key] and len(picked) < count:
                picked.append(squares[key].pop())
                moved = True
        if not moved:
            break
    return np.asarray(picked, dtype=np.int64)


def pick(ds, marker, *, exclude=(), count=N_PER_CLASS, seed=0):
    """(positive cell ids, negative cell ids, info)."""
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.classifier import runs
    from plexora.plugins.gating.server.classifier.geometry import cells

    geometry = cells(ds)
    values = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
    eligible = geometry.valid & np.isfinite(values)
    skip, _fp, _mode = runs.qc_state(ds, marker)
    if skip is not None and skip.shape[0] == eligible.shape[0]:
        eligible &= ~skip
    if exclude:
        eligible &= ~np.isin(geometry.ids, np.asarray(list(exclude), dtype=np.int64))
    rows = np.flatnonzero(eligible)
    if rows.size < 2 * count:
        raise ClassifierRefused(f"Too few cells with a value for {marker} to pick examples from")
    gate = None
    try:
        gate = (model.gmm_for(ds, marker) or {}).get("gate")
    except Exception:
        gate = None
    how = "gate"
    if gate is not None:
        above = rows[values[rows] > float(gate)]
        below = rows[values[rows] <= float(gate)]
        positives = _band(values, above, POSITIVE_RANKS)
        negatives = _band(values, below, NEGATIVE_RANKS)
    if gate is None or positives.size < 3 or negatives.size < 3:
        how = "ranks"
        positives = _band(values, rows, FALLBACK_POSITIVE)
        negatives = _band(values, rows, FALLBACK_NEGATIVE)
    rng = np.random.default_rng(seed)
    pos_rows = _spread(positives, geometry.xs, geometry.ys, count, rng)
    neg_rows = _spread(negatives, geometry.xs, geometry.ys, count, rng)
    if pos_rows.size < 3 or neg_rows.size < 3:
        raise ClassifierRefused(f"No clear positive and negative cells to pick for {marker}; "
                                "label a few yourself and press Train")
    return ([int(v) for v in geometry.ids[pos_rows]], [int(v) for v in geometry.ids[neg_rows]],
            {"how": how, "gate": None if gate is None else float(gate),
             "candidates_positive": int(positives.size),
             "candidates_negative": int(negatives.size)})
