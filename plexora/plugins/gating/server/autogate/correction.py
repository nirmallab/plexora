"""Correcting a gate the vision model approved, by the cell model's calls.

A gate is one threshold; the cell model (`cellcall.py`) says, per cell near
it, whether the stain is the cell's own. Once a look has approved a gate
`g_v`, some cells the model is confident about sit on the wrong side of it:
confidently not own above it (a neighbour's glow, background) and confidently
own below it (a dim real stain). No threshold can put every such cell right
-- their values overlap -- so the correction looks for the gate that leaves
the fewest of them on the wrong side while changing as few as possible of
the calls the model has no confident word about:

    J(g) = flagged(g) + lam * unflagged_changed(g, g_v)

  flagged(g)              confident own cells at or below g + confident not
                          own cells above g (among the cells the model read)
  unflagged_changed       cells whose call differs between g and g_v and that
                          `g_v` did not already miscall by the model -- every
                          cell between the two gates the model read without a
                          confident word, or did not read at all, or calls
                          the way g_v does

Crossing a stretch of values pays only when the cells there that g_v
confidently miscalls outnumber the confidently right ones plus `lam` times
the rest. The candidate gates are a grid the caller supplies (the unit's
frozen lattice, so a corrected gate is a point a look could have chosen),
inside a window around `g_v` the caller has already clipped to what the looks
judged. Ties go to the point nearest `g_v`. Everything is in the fit space
(`profile.Column.to_fit`), where the band halves are measured.

Pure: no table, no image, no session. A move is a proposal only; the
session confirms it with one specific question to the vision model
(`packets.correction_check`) before it is written.
"""

from __future__ import annotations

import numpy as np

#: [cal] study S4 (docs/internal/bench/AUTOGATE_STEP_LEDGER.md E6, E8): the
#: held-out model's runs at seven recorded sessions' final gates on
#: lsp11385_2 and at the scoring starts of an IMC and a CODEX image, scored on
#: each image's expert cells. `lam` the price, in flagged cells, of changing
#: one cell's call the model has no confident word about: at 0.6 the expert's
#: near-gate cells agreed +5 points more on lsp11385_2 (no marker worse) and
#: +16 on CODEX (none worse), +13 on IMC (CD68 9 points worse); at 1 or more
#: the look-decided gates of lsp11385_2 almost never moved. `window_halves`
#: how far either way of the approved gate the correction may look (band
#: halves); `far_halves` how far the model's own unanchored cut must lie to be
#: recorded as a disagreement; `min_steps` the smallest move worth asking
#: about (grid steps).
CORRECTION = {"lam": 0.6, "window_halves": 4.0, "far_halves": 8.0, "min_steps": 1}


def _sorted(vf, mask):
    return np.sort(np.asarray(vf, dtype=np.float64)[mask])


def counts(vf, p, confident):
    """(own values sorted, not-own values sorted): the cells the model is
    confident about, by their fit-space value."""
    vf = np.asarray(vf, dtype=np.float64)
    p = np.asarray(p, dtype=np.float64)
    ok = np.isfinite(vf) & np.isfinite(p)
    return _sorted(vf, ok & (p >= 1.0 - confident)), _sorted(vf, ok & (p <= confident))


def flagged(grid, own, not_own):
    """For each gate in `grid`: confident own cells at or below it plus
    confident not-own cells above it (positive is `value > gate`)."""
    grid = np.asarray(grid, dtype=np.float64)
    below_own = np.searchsorted(own, grid, side="right")
    above_not = not_own.size - np.searchsorted(not_own, grid, side="right")
    return below_own + above_not


def objective(grid, g_v, *, own, not_own, between, lam):
    """(J, flagged, unflagged_changed, justified) for each gate of `grid`.

    `between(a, b)` -> how many of the table's cells (every eligible cell,
    read by the model or not) have a fit-space value in (a, b]."""
    grid = np.asarray(grid, dtype=np.float64)
    f = flagged(grid, own, not_own)
    changed = np.array([between(min(g, g_v), max(g, g_v)) for g in grid], dtype=np.float64)
    # The cells between g_v and g that g_v already miscalled by the model:
    # raising past them puts a confident not-own cell right, lowering past
    # them a confident own one.
    up = grid > g_v
    lo = np.minimum(grid, g_v)
    hi = np.maximum(grid, g_v)

    def within(values):
        return np.searchsorted(values, hi, side="right") - np.searchsorted(values, lo, side="right")

    justified = np.where(up, within(not_own), within(own)).astype(np.float64)
    unflagged = np.maximum(changed - justified, 0.0)
    return f + lam * unflagged, f, unflagged, justified


def solve(grid, g_v, *, vf, p, between, confident, lam=None, min_steps=None,
          clip=(-np.inf, np.inf)) -> dict:
    """The corrected gate among `grid` (fit space) for an approved `g_v`.

    {g_star, moved, direction, flagged_before, flagged_after, changed,
    justified, J, steps}: `moved` False when the best point is `g_v` itself
    or fewer than `min_steps` grid steps away. `clip` (lo, hi) bounds the
    gates allowed (the looks' judged intervals). `g_v` itself is always a
    candidate."""
    cfg = CORRECTION
    lam = float(cfg["lam"] if lam is None else lam)
    min_steps = int(cfg["min_steps"] if min_steps is None else min_steps)
    grid = np.asarray(sorted({float(g) for g in grid} | {float(g_v)}), dtype=np.float64)
    allowed = (grid >= clip[0]) & (grid <= clip[1]) | (grid == g_v)
    grid = grid[allowed]
    own, not_own = counts(vf, p, confident)
    j, f, unflagged, justified = objective(grid, g_v, own=own, not_own=not_own,
                                           between=between, lam=lam)
    at = int(np.flatnonzero(grid == g_v)[0])
    # The smallest J; among ties, the point nearest g_v (then the lower).
    order = np.lexsort((grid, np.abs(grid - g_v), np.round(j, 9)))
    best = int(order[0])
    steps = abs(best - at)
    moved = best != at and steps >= min_steps
    pick = best if moved else at
    return {"g_v": float(g_v), "g_star": float(grid[pick]), "moved": bool(moved),
            "direction": None if not moved else ("up" if grid[pick] > g_v else "down"),
            "steps": int(steps if moved else 0),
            "flagged_before": int(f[at]), "flagged_after": int(f[pick]),
            "changed": int(round(between(min(grid[pick], g_v), max(grid[pick], g_v)))),
            "justified": int(justified[pick]), "unflagged_changed": int(unflagged[pick]),
            "J_before": float(j[at]), "J_after": float(j[pick]),
            "n_own": int(own.size), "n_not_own": int(not_own.size)}


def model_cut(grid, *, vf, p, confident) -> float | None:
    """The gate the model's confident calls alone would choose (the fewest
    flagged cells, unanchored; the median of the tied best). None without a
    confident call. Recorded only: far from the approved gate it is a
    disagreement to flag, never a move."""
    own, not_own = counts(vf, p, confident)
    if not own.size and not not_own.size:
        return None
    grid = np.asarray(sorted({float(g) for g in grid}), dtype=np.float64)
    if not grid.size:
        return None
    f = flagged(grid, own, not_own)
    best = grid[f == f.min()]
    return float(np.median(best))


def changed_cells(vf, g_v, g_star):
    """Bool over `vf`: the cells whose call differs between the two gates."""
    vf = np.asarray(vf, dtype=np.float64)
    lo, hi = min(g_v, g_star), max(g_v, g_star)
    return (vf > lo) & (vf <= hi)
