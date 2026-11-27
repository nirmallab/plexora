"""The embedding behind a lasso: every cell's coordinates, its identity in the
viewer, its colour, and labels written back.

The page draws every cell (scimapScatterGL.js, WebGL2), so what it needs is
compact and sent once: two float32 per cell for an embedding, one uint16 code
or one float32 per cell for a colour. These go as binary (`pack`), never JSON:
four million cells of JSON numbers is a hundred megabytes of text to parse.

**Identity.** A cell picked here must be the same cell on the tissue. The
viewer keys cells by (project, mask label) -- the id every cell layer's colour
table is indexed by (`label_overlay.cell_ids`: the cell-id role's column, else
the table's positional id). `identity()` maps each working-file row to that
pair from the working file itself (`_identity`: the cell-id column, else row order), once
per working file and set of projects, cached beside the notebook. The page
then speaks mask labels to core's selection (services/cellSelection.js), and
Cell Explorer draws it without asking anyone.

**Labels.** `write_label` puts a label on the chosen rows of one obs column,
a categorical, in place in the working file -- the samples.py pattern, under
the file's lock, so it can never land in the middle of a step's write. Only a
column made by labelling (or a new one) can be written: a step's result or a
column that came with the data is refused, so a label cannot quietly change
what a clustering said. The previous state of the column is kept, once, for
Undo.
"""

from __future__ import annotations

import json
import os
import re
import struct
import threading

import numpy as np

MISSING = 0xFFFF
UNLABELED = "Unlabeled"

#: The embeddings people mean by name, in the order the picker lists them.
KNOWN = (("X_umap", "UMAP"), ("X_tsne", "t-SNE"), ("X_pca", "PCA"), ("X_diffmap", "Diffusion map"),
         ("X_phate", "PHATE"), ("X_draw_graph_fa", "Force-directed"))
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

_lock = threading.Lock()
_values_cache: dict = {}


# -- binary framing -------------------------------------------------------


def pack(meta: dict, arrays: list[tuple[str, np.ndarray]]) -> bytes:
    """`PXB1 | uint32 json length | json | pad to 4 | arrays, each 4-aligned`.

    `meta["arrays"]` lists each array's name, dtype, length and byte offset,
    so the page makes typed-array views over one buffer with no copy."""
    blobs = []
    described = []
    offset = 0
    for name, array in arrays:
        data = np.ascontiguousarray(array)
        raw = data.tobytes()
        described.append({"name": name, "dtype": str(data.dtype), "length": int(data.size), "offset": offset})
        pad = (-len(raw)) % 4
        blobs.append(raw + b"\0" * pad)
        offset += len(raw) + pad
    head = json.dumps({**meta, "arrays": described}, separators=(",", ":")).encode()
    head += b" " * ((-(8 + len(head))) % 4)
    return b"PXB1" + struct.pack("<I", len(head)) + head + b"".join(blobs)


# -- what the file holds --------------------------------------------------


def catalogue(work) -> dict:
    """Embeddings and colour-by columns of the working file (`work_of`)."""
    import h5py

    path = work["path"]
    out = []
    with h5py.File(str(path), "r") as f:
        obsm = f.get("obsm")
        keys = list(obsm.keys()) if obsm is not None else []
        shapes = {k: tuple(obsm[k].shape) for k in keys if hasattr(obsm[k], "shape")}
    known = dict(KNOWN)
    order = [k for k, _ in KNOWN if k in shapes] + sorted(k for k in shapes if k not in known)
    for key in order:
        shape = shapes[key]
        if len(shape) != 2 or shape[1] < 2 or not is_embedding(key):
            continue
        out.append({"key": key, "label": label_of(key), "dims": int(shape[1])})
    image_column = work.get("image_column") or "imageid"
    columns = work.get("columns") or []
    labels = [c["name"] for c in columns if c.get("kind") == "category" and c["name"] != image_column]
    meta = work.get("metadata") or []
    meta = meta.get("columns") or [] if isinstance(meta, dict) else meta
    meta = {m if isinstance(m, str) else str((m or {}).get("name", "")) for m in meta}
    metadata = [c for c in labels if c in meta]
    return {
        "embeddings": out,
        "n_cells": int(work.get("n_cells") or 0),
        "image_column": image_column,
        "columns": {
            "labels": [c for c in labels if c not in metadata],
            "metadata": metadata + ([image_column] if any(c["name"] == image_column for c in columns) else []),
            "numbers": [c["name"] for c in columns if c.get("kind") == "number"],
            "markers": list(work.get("markers") or []),
        },
    }


#: obsm entries that are not embeddings: positions, gate calls, neighbour
#: counts, graphs.
NOT_EMBEDDING = re.compile(r"spatial|gate|calls|count|connectiv|distance|neighbo", re.I)


def is_embedding(key) -> bool:
    return not NOT_EMBEDDING.search(str(key))


def label_of(key) -> str:
    """UMAP for `umap`, `X_umap` or `umap_2`; the key itself otherwise."""
    known = dict(KNOWN)
    if key in known:
        return known[key]
    low = str(key).lower()
    for word, name in (("umap", "UMAP"), ("tsne", "t-SNE"), ("pca", "PCA"), ("diffmap", "Diffusion map"),
                       ("phate", "PHATE")):
        if word in low:
            base = str(key).removeprefix("X_")
            return name if base.lower() == word else f"{name} ({base})"
    return str(key).removeprefix("X_")


def coords(path, key, dims=(0, 1)) -> tuple[np.ndarray, list]:
    """`(float32 [n*2] interleaved x,y, [xmin, xmax, ymin, ymax])`."""
    import h5py

    with h5py.File(str(path), "r") as f:
        if key not in f["obsm"]:
            raise KeyError(key)
        data = f["obsm"][key]
        a, b = int(dims[0]), int(dims[1])
        if max(a, b) >= data.shape[1] or min(a, b) < 0:
            raise KeyError(f"{key} has {data.shape[1]} dimensions")
        xy = np.asarray(data[:, [a, b]] if a < b else data[:, [b, a]][:, ::-1], dtype=np.float32)
    finite = np.isfinite(xy).all(axis=1)
    if finite.any():
        lo = xy[finite].min(axis=0)
        hi = xy[finite].max(axis=0)
        extent = [float(lo[0]), float(hi[0]), float(lo[1]), float(hi[1])]
    else:
        extent = [0.0, 1.0, 0.0, 1.0]
    xy[~finite] = np.nan
    return xy.reshape(-1), extent


# -- identity: working-file row -> (project, mask label) --------------------


def identity(work, projects, cache_dir) -> tuple[list, np.ndarray, np.ndarray]:
    """`(projects, project code per row (uint16), mask label per row (uint32,
    0 where the row matches no cell of a loaded project))`."""
    path = work["path"]
    stamp = os.stat(path).st_mtime_ns
    tag = f"v2|{stamp}|{'|'.join(projects)}"
    cache = cache_dir / "identity.npz"
    try:
        hit = np.load(cache, allow_pickle=False)
        if str(hit["tag"]) == tag:
            return list(projects), hit["codes"], hit["labels"]
    except (OSError, KeyError, ValueError):
        pass
    codes, labels = _identity(work, projects)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_dir / "identity.part.npz"
        np.savez(tmp, tag=np.asarray(tag), codes=codes, labels=labels)
        os.replace(tmp, cache)
    except OSError:
        pass
    return list(projects), codes, labels


def _identity(work, projects):
    """The mask label of each row, by the viewer's own rule without loading
    anything heavy: a project with a cell-id role is keyed by that column's
    value, which Prepare copied into the working file (`cell_column`); one
    without is keyed by its table's positional id, the row's place among that
    project's rows. Loading the project's geometry instead would open its
    image, which a project on a server or a moved drive may not have."""
    from plexora import api
    from plexora.plugins.scimap.server import results

    path = work["path"]
    images = results.obs_column(path, work.get("image_column") or "imageid").astype(str)
    n = len(images)
    codes = np.full(n, MISSING, dtype=np.uint16)
    labels = np.zeros(n, dtype=np.uint32)
    single = len(projects) == 1
    cell_column = work.get("cell_column")
    ids = None
    if cell_column:
        try:
            raw = np.asarray(results.obs_column(path, cell_column))
            ids = _as_labels(raw)
        except KeyError:
            ids = None
    for index, name in enumerate(projects):
        mine = images == name
        if single and not mine.any():
            # One project whose file names its image something else (an
            # AnnData opened in place): every row is this project's.
            mine = np.ones(n, dtype=bool)
        if not mine.any():
            continue
        try:
            roles = (api.project_info(name) or {}).get("roles") or {}
        except Exception:  # noqa: BLE001 -- a project that cannot be read matches nothing
            continue
        where = np.flatnonzero(mine)
        codes[where] = index
        if roles.get("cell_id") and ids is not None:
            labels[where] = ids[where]
        else:
            labels[where] = np.arange(len(where), dtype=np.uint32)
    return codes, labels


def _as_labels(raw) -> np.ndarray:
    """Ids as uint32 mask labels; 0 (background, never a cell) where a value
    will not survive the trip -- a text id, a negative, a NaN."""
    out = np.zeros(len(raw), dtype=np.uint32)
    try:
        values = raw.astype(np.float64)
    except (TypeError, ValueError):
        values = np.array([_number(v) for v in raw.tolist()], dtype=np.float64)
    keep = np.isfinite(values) & (values >= 0) & (values < 2 ** 32) & (values == np.floor(values))
    out[keep] = values[keep].astype(np.uint32)
    return out


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


# -- colour ---------------------------------------------------------------


def color(work, column) -> tuple[dict, np.ndarray]:
    """`(meta, values)`: uint16 codes and categories for a label column, or
    float32 values and a display range for a number or a marker."""
    from plexora.plugins.scimap.server import palette, results

    path = work["path"]
    kinds = {c["name"]: c.get("kind") for c in work.get("columns") or []}
    if column in kinds and kinds[column] in ("category", "id"):
        cats, codes = categorical_codes(path, column)
        if len(cats) >= MISSING:
            raise ValueError(f"{column} has too many groups to color by")
        missing = codes == MISSING
        counts = np.bincount(codes[~missing], minlength=len(cats)) if len(cats) else np.zeros(0)
        try:
            colors = palette.colors(path, column)
        except Exception:  # noqa: BLE001
            colors = {}
        from plexora.plugins.scimap.server import overlay

        fallback = overlay.colors_for(cats)
        return ({"column": column, "kind": "category", "categories": cats,
                 "colors": [colors.get(c) or fallback[c] for c in cats],
                 "counts": [int(v) for v in counts], "missing": int(missing.sum())}, codes)
    values = _numbers(work, column)
    finite = values[np.isfinite(values)]
    if finite.size:
        lo, hi = np.percentile(finite, [1, 99])
        if hi <= lo:
            lo, hi = float(finite.min()), float(finite.max())
    else:
        lo, hi = 0.0, 1.0
    return ({"column": column, "kind": "number", "range": [float(lo), float(hi)],
             "marker": column in set(work.get("markers") or [])}, values)


NOT_A_VALUE = {"nan", "none", "", "<na>"}


def _read_categorical(obs, column):
    """One obs column as a pandas Categorical (missing values code -1)."""
    import pandas as pd
    from anndata.io import read_elem

    values = read_elem(obs[column])
    if isinstance(values, pd.Categorical):
        return values
    return pd.Categorical(pd.Series(values, dtype=object).where(pd.notna(values), None))


def categorical_codes(path, column) -> tuple[list, np.ndarray]:
    """`(categories in natural order, uint16 code per cell, MISSING where
    none)` -- on the codes, never a string per cell: four million cells
    colour in a fraction of a second."""
    import h5py

    from plexora.plugins.scimap.server import results

    with h5py.File(str(path), "r") as f:
        if column not in f["obs"]:
            raise KeyError(column)
        cat = _read_categorical(f["obs"], column)
    names = [str(c) for c in cat.categories]
    keep = [i for i, c in enumerate(names) if c.strip().lower() not in NOT_A_VALUE]
    ordered = results.natural([names[i] for i in keep])
    where = {c: i for i, c in enumerate(ordered)}
    remap = np.full(len(names) + 1, MISSING, dtype=np.uint16)  # last slot: code -1
    for i in keep:
        remap[i] = where[names[i]]
    codes = remap[np.asarray(cat.codes, dtype=np.int64)]
    return ordered, codes


def _numbers(work, column) -> np.ndarray:
    """A numeric obs column, or one marker's column of X, as float32 (cached
    on the file's modification time: X is read in row blocks)."""
    from plexora.plugins.scimap.server import results

    path = work["path"]
    stamp = os.stat(path).st_mtime_ns
    key = (str(path), column, stamp)
    with _lock:
        if key in _values_cache:
            return _values_cache[key]
    markers = list(work.get("markers") or [])
    if column in markers:
        values = _marker(path, markers.index(column))
    else:
        values = np.asarray(results.obs_column(path, column), dtype=np.float32)
    with _lock:
        if len(_values_cache) > 8:
            _values_cache.clear()
        _values_cache[key] = values
    return values


def _marker(path, j) -> np.ndarray:
    import h5py
    import scipy.sparse as sp
    from anndata.io import read_elem

    with h5py.File(str(path), "r") as f:
        X = f["X"]
        if isinstance(X, h5py.Dataset):
            n = X.shape[0]
            out = np.empty(n, dtype=np.float32)
            step = 500_000
            for start in range(0, n, step):
                out[start:start + step] = X[start:start + step, j]
            return out
        matrix = read_elem(X)
        column = matrix[:, j]
        column = column.toarray() if sp.issparse(column) else np.asarray(column)
        return np.asarray(column, dtype=np.float32).reshape(-1)


# -- labels ---------------------------------------------------------------


class LabelError(ValueError):
    pass


class _write_lock:
    """The working file's lock, refused rather than waited on: a label typed
    while a step runs should say so, not hang the request for minutes."""

    def __init__(self, path):
        from plexora.plugins.scimap.server import working

        self._lock = working._file_lock(path)

    def __enter__(self):
        if not self._lock.acquire(timeout=3):
            raise LabelError("A step is writing the working file; label when it finishes.")
        return self

    def __exit__(self, *exc):
        self._lock.release()


def check_column(name, work, labelled):
    """Refuse a name that is not a column made by labelling or a new one."""
    name = str(name or "").strip()
    if not NAME.match(name):
        raise LabelError("A column name starts with a letter and holds letters, digits and _ only.")
    existing = {c["name"]: c.get("kind") for c in work.get("columns") or []}
    if name == (work.get("image_column") or "imageid"):
        raise LabelError(f"{name} names the images; label into another column.")
    if name in existing and name not in set(labelled or []):
        raise LabelError(f"{name} is a step's result or came with the data. Label into a new column.")
    return name


def rows_from_bitmask(blob: bytes, n: int) -> np.ndarray:
    bits = np.unpackbits(np.frombuffer(blob, dtype=np.uint8), bitorder="little")[:n]
    if bits.size < n:
        raise LabelError("The selection does not match the working file's cells.")
    return bits.astype(bool)


def _column_codes(path, column, n):
    """`(existed, categories, codes)` of one obs column, read as it is."""
    import pandas as pd

    from plexora.plugins.scimap.server import results

    with results._open(path) as f:
        if column not in f["obs"]:
            return False, [], np.full(n, -1, dtype=np.int32)
        values = results._read(f["obs"], column)
    # Missing stays missing (code -1), never the text "nan".
    series = values if isinstance(values, pd.Categorical) else pd.Categorical(pd.Series(values).astype("object"))
    return True, [str(c) for c in series.categories], np.asarray(series.codes, dtype=np.int32)


def _label_with_scimappro(path, column, value, cells, overwrite):
    """One `tl.labelCells` call through the notebook's run path (it opens the
    file the way every step does and writes the column back)."""
    from plexora.plugins.scimap.server import adapter, install

    if not install.supports("labels"):
        raise LabelError(install.why_not("labels"))
    adapter.execute(path, "tl.labelCells",
                    {"cells": np.asarray(cells, dtype=bool), "value": value, "label": column,
                     "overwrite": bool(overwrite)},
                    label=None, writes=[f"obs:{column}"])


def write_label(path, column, label, chosen, *, overwrite, undo_path) -> dict:
    """Label the `chosen` rows (a bool mask) of `column` with Scimap Pro's
    `tl.labelCells`. Returns `{column, label, labelled, kept, counts}`; the
    column before is kept at `undo_path` for one Undo."""
    label = str(label or "").strip()
    if not label:
        raise LabelError("Type a label first.")
    if len(label) > 120:
        raise LabelError("A label is at most 120 characters.")
    n = int(chosen.size)
    with _write_lock(path):
        existed, categories, codes = _column_codes(path, column, n)
        if existed and codes.size != n:
            raise LabelError("The selection does not match the working file's cells; reopen the step.")
        np.savez(undo_path, column=np.asarray(column), existed=np.asarray(existed),
                 codes=codes, categories=np.asarray(categories, dtype=str))
        empty = codes < 0
        target = chosen if overwrite else (chosen & empty)
        _label_with_scimappro(path, column, label, chosen, overwrite)
        _existed, after_cats, after = _column_codes(path, column, n)
    used = np.bincount(after[after >= 0], minlength=len(after_cats))
    return {"column": column, "label": label, "labelled": int(target.sum()),
            "kept": int((chosen & ~empty).sum()) if not overwrite else 0,
            "counts": {c: int(k) for c, k in zip(after_cats, used) if k}}


def undo_label(path, undo_path) -> dict:
    """Put the column back as it was before the last label, one
    `tl.labelCells` call per earlier label (and one clearing the rest)."""
    try:
        saved = np.load(undo_path, allow_pickle=False)
    except OSError as exc:
        raise LabelError("Nothing to undo.") from exc
    column = str(saved["column"])
    codes = saved["codes"]
    categories = [str(c) for c in saved["categories"].tolist()]
    with _write_lock(path):
        _label_with_scimappro(path, column, None, np.ones(codes.size, dtype=bool), True)
        if bool(saved["existed"]):
            for k, category in enumerate(categories):
                cells = codes == k
                if cells.any():
                    _label_with_scimappro(path, column, category, cells, True)
    os.remove(undo_path)
    # A column the label made stays, emptied: Scimap Pro writes columns, it
    # does not delete them.
    return {"column": column, "removed": False, "emptied": not bool(saved["existed"])}
