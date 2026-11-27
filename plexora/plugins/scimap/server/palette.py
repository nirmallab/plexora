"""One colour per category, by Scimap Pro's own rule.

The figures are Scimap Pro's (figures.py), so a cluster's colour is whatever
`scimappro.pl._theme.categoryColors` gives it: `uns['<column>_colors']` when the
file carries a palette of the right length, else Okabe-Ito, Vega 20 or Zeileis
28 by count, over the categories in Scimap Pro's order. The overlay in the
viewer and the page's swatches ask here, so the tissue, the table and the plot
agree.
"""

from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_cache: dict = {}

# Scimap Pro's palettes (pl/_theme.py), for when it cannot be imported.
OKABE_ITO = ("#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442", "#000000")
_VEGA_20 = ("#1f77b4", "#ff7f0e", "#279e68", "#d62728", "#aa40fc", "#8c564b", "#e377c2", "#b5bd61",
            "#17becf", "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5", "#c49c94", "#f7b6d2",
            "#dbdb8d", "#9edae5", "#ad494a", "#8c6d31")


def _read(path, column):
    import h5py
    import pandas as pd
    from anndata.io import read_elem

    with h5py.File(path, "r") as f:
        values = read_elem(f["obs"][column])
        stored = None
        name = f"{column}_colors"
        if "uns" in f and name in f["uns"]:
            raw = f["uns"][name][()]
            stored = [v.decode() if isinstance(v, bytes) else str(v) for v in list(raw)]
    return pd.Series(values), stored


def colors(path, column) -> dict[str, str]:
    """`{category: '#rrggbb'}` for one obs column of the working file."""
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    key = (str(path), column, stamp)
    with _lock:
        if key in _cache:
            return _cache[key]
    series, stored = _read(path, column)
    out = assign(series, stored)
    with _lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = out
    return out


def assign(series, stored=None) -> dict[str, str]:
    """Scimap Pro's colours for the categories of `series` (a pandas Series)."""
    try:
        from scimappro.pl._theme import categoryColors, categoryOrder

        names = categoryOrder(series)
        if stored is not None and len(stored) == len(names):
            out = dict(zip(names, stored))
        else:
            out = categoryColors(series, verbose=False)
    except Exception:  # noqa: BLE001 -- the same rule, by hand
        names = sorted({str(v) for v in series.astype(str)})
        pool = OKABE_ITO if len(names) <= len(OKABE_ITO) else _VEGA_20
        out = {n: pool[i % len(pool)] for i, n in enumerate(names)}
    return {str(k): str(v) for k, v in out.items()}
