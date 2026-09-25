"""A standard Visium run's spots, read for the panel out of the sample's table.

The HD panel colours a counted GRID, which core stores as bin tiles. A
standard Visium slide has no grid to store: it is a few thousand 55 micron
spots, and they are already the rows of the sample's table -- the feature
matrix converted gene-major by `server/utils/tenx_matrix.py`, whose `X` is
`csc_matrix` over spots x features. So this reads that file directly: one
gene is one contiguous slice of `X`, the positions are `obsm["spatial"]` in
the run's full-res pixels, and the per-gene totals were written into
`var["plx_sum"]` during the conversion.

Nothing is built and nothing is cached on disk. A slide has at most 15,000
spots, so a gene is a few kilobytes and the only number worth keeping in
memory is each spot's total UMI, which needs one pass over the matrix.

h5py and anndata are imported inside the functions: the boundary tests pin
that building the app imports neither.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

#: The feature type whose counts are a spot's "total": its UMIs. Antibody
#: counts are on another scale entirely and would swamp them.
GENE_EXPRESSION = "Gene Expression"

#: What the panel names when nothing is picked -- the HD panel's own word.
TOTAL = "total"

#: Entries read per slab when summing a spot's UMIs.
_SLAB = 8 << 20

_lock = threading.Lock()
#: `(path, size, mtime) -> {"totals", ...}`, one entry per converted table.
_cache: dict = {}


def table_of(project):
    """The converted spot table of this project, or None.

    Only a table `tenx_matrix` wrote from a standard Visium run: its
    provenance says `kind: spots`. A sample whose table somebody replaced
    with their own has no spots for this panel to read.
    """
    if project is None or not project.has_table:
        return None
    path = Path(str(project.dataset.src or ""))
    if path.suffix.lower() != ".h5ad" or not path.is_file():
        return None
    from plexora.server.utils import tenx_matrix

    provenance = tenx_matrix.read_provenance(path) or {}
    return path if provenance.get("kind") == "spots" else None


def _key(path):
    stat = Path(path).stat()
    return (str(path), stat.st_size, stat.st_mtime_ns)


def summary(path):
    """Everything the panel needs up front, computed once per table.

    `{genes, gene_counts, feature_types, x, y, totals, total_count}`.
    """
    import h5py
    from anndata.io import read_elem

    key = _key(path)
    with _lock:
        found = _cache.get(key)
    if found is not None:
        return found
    with h5py.File(path, "r") as handle:
        # anndata's own reader: string columns are stored in more than one
        # encoding across versions, and 18,000 rows cost nothing to read.
        var = read_elem(handle["var"])
        genes = [str(name) for name in var.index]
        kinds = (var["feature_types"].astype(str).to_numpy()
                 if "feature_types" in var else
                 np.asarray([GENE_EXPRESSION] * len(genes)))
        counts = (var["plx_sum"].to_numpy(dtype=np.float64)
                  if "plx_sum" in var else np.zeros(len(genes)))
        spatial = np.asarray(handle["obsm"]["spatial"][()], dtype=np.float64)
        group = handle["X"]
        n_rows = int(group.attrs["shape"][0])
        indptr = group["indptr"][()].astype(np.int64)
        # A spot's UMIs: every Gene Expression entry, summed by row. X is
        # gene-major, so the Gene Expression genes' entries are contiguous
        # runs of it and one bincount per slab sums them.
        totals = np.zeros(n_rows, dtype=np.float64)
        wanted = np.flatnonzero(np.asarray(kinds) == GENE_EXPRESSION)
        if len(wanted):
            runs = _runs(wanted)
            for first, last in runs:
                start, stop = int(indptr[first]), int(indptr[last + 1])
                for lo in range(start, stop, _SLAB):
                    hi = min(lo + _SLAB, stop)
                    totals += np.bincount(
                        group["indices"][lo:hi],
                        weights=group["data"][lo:hi].astype(np.float64),
                        minlength=n_rows)
    found = {
        "genes": genes,
        "gene_counts": counts,
        "feature_types": [str(k) for k in kinds],
        "x": spatial[:, 0],
        "y": spatial[:, 1],
        "totals": totals,
        "total_count": float(totals.sum()),
        "indptr": indptr,
        "n_rows": n_rows,
        "index": {name: i for i, name in enumerate(genes)},
    }
    with _lock:
        # One table at a time is the common case; keep a handful.
        while len(_cache) >= 4:
            _cache.pop(next(iter(_cache)))
        _cache[key] = found
    return found


def _runs(positions):
    """Sorted integer positions as `[(first, last)]` runs of consecutive ones."""
    positions = np.asarray(positions, dtype=np.int64)
    if not len(positions):
        return []
    breaks = np.flatnonzero(np.diff(positions) != 1)
    starts = np.r_[positions[0], positions[breaks + 1]]
    ends = np.r_[positions[breaks], positions[-1]]
    return list(zip(starts.tolist(), ends.tolist()))


def values(path, names):
    """`{name: float32 array over spots}` for these genes, `total` included.

    A name the table does not have is left out rather than zero-filled: a
    panel that asked for a gene from another sample must not be told it is
    absent from every spot.
    """
    import h5py

    found = summary(path)
    out = {}
    wanted = [n for n in names if n == TOTAL or n in found["index"]]
    if not wanted:
        return out
    with h5py.File(path, "r") as handle:
        group = handle["X"]
        for name in wanted:
            if name == TOTAL:
                out[name] = found["totals"].astype(np.float32)
                continue
            gene = found["index"][name]
            start, stop = (int(v) for v in found["indptr"][gene:gene + 2])
            dense = np.zeros(found["n_rows"], dtype=np.float32)
            if stop > start:
                dense[group["indices"][start:stop]] = group["data"][start:stop]
            out[name] = dense
    return out


def auto_window(field):
    """The count at which one field saturates: the 99th percentile of its
    non-empty spots, floor one -- `bin_tiles.auto_window`'s rule, so a spot
    heatmap and a bin heatmap stretch a gene the same way."""
    field = np.asarray(field, dtype=np.float64)
    live = field[field > 0]
    if not len(live):
        return 1.0
    return max(1.0, float(np.percentile(live, 99)))


def spot_radius(project, layer):
    """The layer's spot radius in its own (full-res) pixels."""
    render = dict(layer.render or {}) if layer is not None else {}
    diameter = render.get("spotDiameter")
    if not diameter:
        for bundle in project.bundles:
            if bundle.get("spotDiameterFullres"):
                diameter = bundle["spotDiameterFullres"]
                break
    return float(diameter or 0) / 2.0


def encode(array, decimals=3):
    """A float array as a JSON-able list, rounded: a spot's counts are
    integers and its coordinates need no more than a thousandth of a pixel."""
    return np.round(np.asarray(array, dtype=np.float64), decimals).tolist()

