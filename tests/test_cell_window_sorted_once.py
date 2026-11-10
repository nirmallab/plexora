"""`select_display_cells` sorts a channel's cells once, for the same cells.

It used to sort the whole column twice (once for the anchors, once for the
other bands): a tenth of a second for each channel of a large table, on the
path of the first auto-contrast.
"""

import numpy as np
import pytest

from plexora.agent.evidence import cell_window
from plexora.agent.evidence.cell_window import CELL_WINDOW, _band, _body, _cells


def _as_before(values, ids, xs, ys, *, seed=0, spec=CELL_WINDOW):
    """The two-sort version, kept here as the reference."""
    values = np.asarray(values, dtype=np.float64)
    ids = np.asarray(ids)
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    order = _body(values, xs, ys)
    info = {"n_body": int(order.size), "seed": int(seed)}
    if order.size < spec["min_cells"]:
        return None, info
    rng = np.random.default_rng(seed)
    negatives = _cells(_band(order, spec["negative_pct"], spec["n_each"], rng), ids, xs, ys, values)
    info.update(n_negative=len(negatives))
    if not negatives:
        return None, info
    order = _body(values, xs, ys)
    out = {"negatives": negatives}
    for offset, (band, pct, take) in enumerate((("low", "low_pct", "n_low"),
                                                ("near", "near_pct", "n_near"),
                                                ("bright", "bright_pct", "n_bright"))):
        rng = np.random.default_rng([int(seed), offset + 1])
        out[band] = _cells(_band(order, spec[pct], spec[take], rng), ids, xs, ys, values)
    info.update({f"n_{band}": len(out[band]) for band in ("low", "near", "bright")})
    return out, info


def _column(kind, n=5000, seed=3):
    rng = np.random.default_rng(seed)
    values = rng.lognormal(5, 1.2, n)
    if kind == "zeros":
        values[rng.random(n) < 0.4] = 0
    elif kind == "nan":
        values[rng.random(n) < 0.1] = np.nan
    elif kind == "signed":
        values = rng.normal(0, 1, n)
    elif kind == "small":
        values = values[:150]
    ids = np.arange(values.size) + 1
    xs = rng.uniform(0, 4000, values.size)
    ys = rng.uniform(0, 4000, values.size)
    return values, ids, xs, ys


@pytest.mark.parametrize("kind", ["plain", "zeros", "nan", "signed", "small"])
@pytest.mark.parametrize("seed", [0, 7])
def test_the_same_cells_as_before(kind, seed):
    values, ids, xs, ys = _column(kind)
    assert cell_window.select_display_cells(values, ids, xs, ys, seed=seed) \
        == _as_before(values, ids, xs, ys, seed=seed)


def test_one_sort_per_channel(monkeypatch):
    values, ids, xs, ys = _column("plain")
    calls = []
    real = cell_window._body
    monkeypatch.setattr(cell_window, "_body", lambda *a: calls.append(1) or real(*a))
    cell_window.select_display_cells(values, ids, xs, ys)
    assert len(calls) == 1
