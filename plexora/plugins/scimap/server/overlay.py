"""The step being looked at in the image viewer, as a colouring of its cells.

A step's "view in the viewer" button records here which result to show -- a
label column, optionally narrowed to a few groups (one cluster, the cells a
lasso caught) -- and for which projects. Cell Explorer then colours by it
through the annotation seam (`Plugin.cell_annotations_factory`): this module's
`provider(ds)` serves it as the categorical annotation `scimap` over the
loaded project's rows. Nothing is written into the user's table.

The record is one small file, `scimap_root()/overlay.json`, deliberately not
per-tab or per-page: the user walks a dataset image by image with the viewer's
own Previous/Next, every step of which is a full page load, and the overlay
must still be there on the next image. It stays until the user dismisses it
in the viewer or views another step. The annotation's name never changes, so
Cell Explorer -- which keeps its colour-by across a dataset walk when the name
exists in every project -- carries it from image to image by itself; the
viewer script re-applies it when it does not.
"""

from __future__ import annotations

import json
import os
import threading
import time

import numpy as np

ANNOTATION = "scimap"
OTHER = "Other"
OTHER_COLOR = "#c9ced8"

#: The notebook's categorical palette, shared with the page (sent with the page
#: data) so a cluster is the same colour in a plot and on the tissue.
PALETTE = ("#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1",
           "#ff9da7", "#9c755f", "#79706e", "#86bcb6", "#d37295", "#8cd17d", "#b6992d",
           "#499894", "#fabfd2", "#a0cbe8", "#ffbe7d", "#f1ce63", "#d4a6c8")

_lock = threading.Lock()
_cache: dict = {}


def _path():
    from plexora.plugins.scimap.server import store

    return store.root() / "overlay.json"


def get() -> dict | None:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def put(record: dict) -> dict:
    record = {**record, "set_at": time.time()}
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, path)
    with _lock:
        _cache.clear()
    return record


def clear() -> None:
    try:
        _path().unlink()
    except FileNotFoundError:
        pass
    with _lock:
        _cache.clear()


def public(record) -> dict | None:
    """What the viewer script needs; no file paths."""
    if not record:
        return None
    return {k: record.get(k) for k in ("notebook_id", "cell_id", "title", "column", "projects",
                                       "context", "focus", "kind", "set_at", "selected")}


def colors_for(categories) -> dict:
    return {c: PALETTE[i % len(PALETTE)] for i, c in enumerate(categories)}


def provider(ds) -> dict:
    """`{scimap: annotation}` for the loaded project, or {} when the overlay is
    not about it. Never raises: an overlay that cannot be drawn colours nothing."""
    record = get()
    if not record or ds is None or getattr(ds, "name", None) not in (record.get("projects") or []):
        return {}
    try:
        stamp = os.stat(record["path"]).st_mtime_ns
    except (OSError, KeyError):
        return {}
    key = (ds.name, record.get("set_at"), stamp)
    with _lock:
        if key in _cache:
            return _cache[key]
    from plexora.plugins.scimap.server import working

    try:
        with working.reading(record["path"], timeout=5.0):
            out = _build(ds, record, stamp)
    except TimeoutError:
        return {}  # a step is writing the file: drawn on the next request, not cached
    except Exception:  # noqa: BLE001 -- the viewer must never break on an overlay
        import logging

        logging.getLogger(__name__).debug("SCIMAP overlay not drawn", exc_info=True)
        out = {}
    with _lock:
        if len(_cache) > 16:
            _cache.clear()
        _cache[key] = out
    return out


def _build(ds, record, stamp):
    from plexora.plugins.scimap.server import results

    path = record["path"]
    image_col = record.get("image_column") or "imageid"
    cell_col = record.get("cell_column")
    images = results.obs_column(path, image_col).astype(str)
    mine = images == ds.name
    if record.get("kind") == "selection":
        chosen = np.zeros(len(images), dtype=bool)
        try:
            chosen[np.load(record["selection"])] = True
        except (OSError, KeyError, IndexError, ValueError):
            return {}
        values = np.where(chosen, "Selected", OTHER)
        categories = ["Selected", OTHER]
        colors = {"Selected": "#0284c7", OTHER: OTHER_COLOR}
    else:
        values = results.obs_column(path, record["column"]).astype(str)
        categories = results.natural(np.unique(values).tolist())
        from plexora.plugins.scimap.server import palette

        colors = {**colors_for(categories), **palette.colors(path, record["column"])}
        focus = [str(f) for f in record.get("focus") or []]
        if focus:
            values = np.where(np.isin(values, focus), values, OTHER)
            categories = [c for c in categories if c in focus] + [OTHER]
            colors = {**{c: colors[c] for c in categories if c != OTHER}, OTHER: OTHER_COLOR}
    rows = _rows(ds, path, cell_col, mine)
    if rows is None:
        return {}
    geometry = ds.table.geometry()
    n = geometry.height if geometry is not None else 0
    out_values = [None] * n
    for row, value in zip(rows.tolist(), values[mine].tolist()):
        if 0 <= row < n:
            out_values[row] = value
    present = [c for c in categories if c in set(v for v in out_values if v is not None)]
    return {ANNOTATION: {
        "label": f"SCIMAP · {record.get('title') or record.get('column')}",
        "values": out_values, "categories": present,
        "colors": {c: colors[c] for c in present},
        "fingerprint": f"{record.get('set_at')}|{stamp}",
        "description": "the result of a SCIMAP notebook step"}}


def _rows(ds, path, cell_col, mine):
    """The loaded table's row of each of this project's working-file cells."""
    import pandas as pd

    from plexora.plugins.scimap.server import results

    geometry = ds.table.geometry()
    if geometry is None:
        return None
    id_role = getattr(getattr(ds, "schema", None), "cell_id", None)
    count = int(mine.sum())
    if cell_col and id_role and id_role in geometry.columns:
        try:
            theirs = results.obs_column(path, cell_col)[mine]
        except KeyError:
            theirs = None
        if theirs is not None:
            loaded = pd.Index(_as_text(geometry[id_role].to_numpy()))
            if loaded.is_unique:
                return loaded.get_indexer(_as_text(np.asarray(theirs)))
            first = pd.Series(np.arange(len(loaded))).groupby(loaded.values).first()
            return first.reindex(_as_text(np.asarray(theirs))).fillna(-1).astype(int).to_numpy()
    if count == geometry.height:
        return np.arange(count)
    return None


def _as_text(values):
    """Ids compared as text, with integral floats written as integers (a CSV
    read as float gives 12.0 where the mask says 12)."""
    out = []
    for v in values.tolist():
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        out.append(str(v))
    return out
