"""The working file: the one AnnData every step of a notebook reads and writes.

Prepare makes it, once, in the results folder the user chose (the table's own
folder by default):

* a **CSV or Parquet** project is read with Scimap Pro's own reader,
  `io.readIntensityTable`, given the project's column roles -- so the cell
  ids, coordinates and markers are the ones Plexora already knows -- and
  written as `<results>/<project>.h5ad`;
* an **AnnData** project is used in place when the results folder is its own
  folder and it already has an image column; otherwise it is copied into the
  results folder with one added (`imageid` = the project's name);
* a **dataset** reads each member the same way and joins them on their shared
  markers (`imageid` = each project's name, so every spatial graph stays inside
  one image), as `<results>/<dataset>.h5ad`.

What Prepare decided is written into the file (`uns["plexora"]`) as well as
into the notebook, so the file explains itself to anyone who opens it in
Jupyter, and an exported script can rely on the same columns.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from plexora.plugins.scimap.server import context as ctx
from plexora.plugins.scimap.server.adapter import RunError

IMAGE_COLUMN = "imageid"
X_COLUMN = "X_centroid"
Y_COLUMN = "Y_centroid"

_meta_cache: dict = {}
_meta_lock = threading.Lock()

# One lock per working file. HDF5 will not open a file for writing while this
# process holds a read handle on it (nor the reverse), so a run holds the lock
# for as long as it may write, and every reader outside the runner thread takes
# it too. The status poll never waits: during a write it answers from its cache.
_file_locks: dict = {}
_file_locks_guard = threading.Lock()


def _file_lock(path) -> threading.RLock:
    key = os.path.realpath(str(path))
    with _file_locks_guard:
        lock = _file_locks.get(key)
        if lock is None:
            lock = _file_locks[key] = threading.RLock()
        return lock


class writing:
    """`with writing(path):` around anything that may write the working file."""

    def __init__(self, path):
        self._lock = _file_lock(path) if path else None

    def __enter__(self):
        if self._lock is not None:
            self._lock.acquire()
        return self

    def __exit__(self, *exc):
        if self._lock is not None:
            self._lock.release()
        return False


class reading:
    """`with reading(path):` around a read from a request thread. Waits for a
    run's write to finish, up to `timeout` seconds, then raises TimeoutError."""

    def __init__(self, path, timeout=30.0):
        self._lock = _file_lock(path)
        self._timeout = timeout

    def __enter__(self):
        if not self._lock.acquire(timeout=self._timeout):
            raise TimeoutError("A step is writing the working file.")
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("._") or "notebook"
    return cleaned[:80]


def check_folder(folder) -> str:
    """The folder as an absolute path, made if missing, refused if unwritable."""
    if not folder or not str(folder).strip():
        raise RunError("invalid_input", "Choose a folder for the results.")
    path = Path(os.path.expanduser(str(folder))).resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RunError("invalid_input", f"That folder cannot be created: {exc.strerror}.") from exc
    if not os.access(path, os.W_OK):
        raise RunError("invalid_input", "That folder cannot be written to. Choose another.")
    return str(path)


def plan(context: dict, results_dir: str) -> dict:
    """What Prepare will do, before it does it: one row per project, the file
    it will write, and anything that stops it."""
    folder = str(Path(os.path.expanduser(results_dir)).resolve()) if results_dir else ""
    rows, problems = [], []
    for name in context["projects"]:
        info = ctx.table_of(name)
        if not info.get("has_table"):
            problems.append(f"{name} has no cell table.")
            rows.append({"project": name, "ok": False, "why": "No cell table"})
            continue
        if info.get("on_node"):
            problems.append(f"{name}'s table is on a data node; copy it to this computer first.")
            rows.append({"project": name, "ok": False, "why": "Table is on a data node"})
            continue
        if not info.get("supported"):
            problems.append(f"{name}'s table ({info['type']}) cannot be prepared yet.")
            rows.append({"project": name, "ok": False, "why": f"{info['type']} tables are not supported yet"})
            continue
        missing = [r for r in ("cell_id", "x", "y") if not info["roles"].get(r)] \
            if info["type"] in ctx.TABULAR else []
        if missing:
            problems.append(f"{name} has no column marked as the cell {', '.join(missing)}.")
        rows.append({"project": name, "ok": not missing, "type": info["type"], "file": info["file"],
                     "markers": len(info["markers"]) or None, "marker_names": list(info["markers"]),
                     "roles": info["roles"], "pixel_size": info["pixel_size"],
                     "log_transformed": info["log_transformed"],
                     "why": f"No {', '.join(missing)} column" if missing else ""})
    path, mode = target(context, folder)
    if context["kind"] == "dataset":
        logs = {r.get("log_transformed") for r in rows if r.get("ok")}
        if len(logs) > 1:
            problems.append("Some projects' tables are log-transformed and others are not; "
                            "they cannot be joined as they are.")
    return {"rows": rows, "problems": problems, "path": path, "file": Path(path).name if path else "",
            "mode": mode, "results_dir": folder, "exists": bool(path and Path(path).is_file())}


def target(context, folder):
    """`(path, mode)` of the working file for a context and a results folder."""
    if not folder:
        return "", ""
    if context["kind"] == "project":
        info = ctx.table_of(context["projects"][0])
        if info.get("has_table") and info["type"] in ctx.ANNDATA:
            # The project's own AnnData is the working file when the results
            # go beside it: one copy of the data, and the viewer reads the
            # same columns the notebook writes.
            src = Path(info["src"])
            beside = {str(src.parent), str(src.parent / ctx.RESULTS_DIRNAME)}
            if folder in beside and src.suffix == ".h5ad" and os.access(src, os.W_OK):
                return str(src), "in_place"
            return str(Path(folder) / f"{safe_name(context['name'])}.h5ad"), "copy"
        return str(Path(folder) / f"{safe_name(context['name'])}.h5ad"), "convert"
    return str(Path(folder) / f"{safe_name(context['name'])}.h5ad"), "concatenate"


# -- doing it -------------------------------------------------------------


def _read_project(sm, info, sample=None, say=None, options=None):
    """One project's table as an in-memory AnnData with `imageid` set."""
    import anndata as ad
    import numpy as np

    say = say or (lambda m: None)
    name = info["project"]
    if info["type"] in ctx.TABULAR:
        src = info["src"]
        cleanup = None
        if info["type"] == "parquet":
            # Scimap Pro's reader reads CSV; a Parquet table goes through a
            # temporary CSV, which the provenance names.
            import polars as pl

            say(f"Converting {info['file']} to CSV for the reader")
            handle, tmp = tempfile.mkstemp(suffix=".csv", prefix="scimap-")
            os.close(handle)
            pl.read_parquet(src).write_csv(tmp)
            src, cleanup = tmp, tmp
        roles = info["roles"]
        options = options or {}
        log = options.get("log")
        if log is None:
            log = not info["log_transformed"]
        kwargs = dict(cellId=roles["cell_id"], xColumn=roles["x"], yColumn=roles["y"],
                      customImageid=name, removeDna=False, log=bool(log),
                      verbose=False, progress=False)
        markers = [m for m in (options.get("markers") or []) if m in info["markers"]] or info["markers"]
        if markers:
            kwargs["markers"] = list(markers)
        else:
            kwargs["split"] = roles["x"]
        if sample:
            kwargs["randomSample"] = int(sample)
        say(f"Reading {info['file']}")
        try:
            adata = sm.io.readIntensityTable(src, **kwargs)
        finally:
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)
        adata = _keep_subset(sm, adata, info)
        call = {"function": "io.readIntensityTable", "file": info["file"],
                "kwargs": {k: v for k, v in kwargs.items() if k not in ("verbose", "progress")}}
        return adata, roles["cell_id"], call
    # AnnData: read it, and give it the columns every step expects.
    say(f"Reading {info['file']}")
    adata = sm.io.init(info["src"], streamData=False, verbose=False)
    roles = info["roles"]
    image = roles.get("image_id")
    if image and image in adata.obs.columns and image != IMAGE_COLUMN:
        adata.obs[IMAGE_COLUMN] = adata.obs[image].astype(str)
    if IMAGE_COLUMN not in adata.obs.columns:
        adata.obs[IMAGE_COLUMN] = name
    adata = _keep_subset(sm, adata, info)
    for role, column in (("x", X_COLUMN), ("y", Y_COLUMN)):
        source = roles.get(role)
        if column in adata.obs.columns:
            continue
        if source and source in adata.obs.columns:
            adata.obs[column] = adata.obs[source].astype(float)
        elif "spatial" in adata.obsm:
            adata.obs[column] = np.asarray(adata.obsm["spatial"])[:, 0 if role == "x" else 1]
    if sample and adata.n_obs > int(sample):
        adata = sm.pp.subsample(adata, n=int(sample), method="uniform", groupBy=IMAGE_COLUMN,
                                randomState=0, verbose=False, progress=False)
    cell = info.get("obs_id_field") or roles.get("cell_id")
    return adata, cell, {"function": "io.init", "file": info["file"]}


def _keep_subset(sm, adata, info):
    """A project that is part of a table (`subset: {column, value}`) keeps only
    its own cells, dropped by `pp.dropFeatures`."""
    subset = info.get("subset") or {}
    column, value = subset.get("column"), subset.get("value")
    if not column or column not in adata.obs.columns or value is None:
        return adata
    other = adata.obs_names[adata.obs[column].astype(str) != str(value)]
    if not len(other):
        return adata
    out = sm.pp.dropFeatures(adata, dropCells=list(other), verbose=False, progress=False)
    return out if out is not None else adata


def prepare(context: dict, results_dir: str, *, sample=None, say=None, options=None) -> dict:
    """Make the working file. Runs on the runner thread (it imports Scimap Pro
    and its reader is numba-backed). Returns the working record."""
    from plexora.plugins.scimap.server.adapter import licence_refusal, scimappro

    say = say or (lambda m: None)
    folder = check_folder(results_dir)
    planned = plan(context, folder)
    if planned["problems"]:
        raise RunError("invalid_input", " ".join(planned["problems"]))
    path, mode = planned["path"], planned["mode"]
    sm = scimappro()
    from scimappro._license._errors import LicenseError

    infos = [ctx.table_of(name) for name in context["projects"]]
    started = time.time()
    if mode == "in_place":
        info = infos[0]
        import anndata as ad

        backed = ad.read_h5ad(path, backed="r")
        try:
            obs_columns = list(backed.obs.columns)
        finally:
            backed.file.close()
        image = info["roles"].get("image_id")
        if IMAGE_COLUMN not in obs_columns and not (image and image in obs_columns):
            mode = "copy"
            path = str(Path(folder) / f"{safe_name(context['name'])}.h5ad")
        else:
            record = _record(context, mode, path, infos, [{"function": "in place", "file": info["file"]}],
                             None, image if IMAGE_COLUMN not in obs_columns else IMAGE_COLUMN, started)
            metadata = (options or {}).get("metadata")
            if metadata and metadata.get("columns"):
                record.update(_add_metadata(path, metadata, record["image_column"], say))
            return {**record, **describe(path, refresh=True)}
    try:
        per = []
        calls = []
        each = None
        if sample and len(infos) > 1:
            each = max(1, int(sample) // len(infos))
        for info in infos:
            adata, cell_col, call = _read_project(sm, info, sample=each or sample, say=say,
                                                  options=options)
            per.append((info, adata, cell_col))
            calls.append(call)
    except LicenseError as exc:
        raise licence_refusal(exc) from exc
    dropped = []
    # Scimap Pro reads a path by its ending: the temporary file ends in .h5ad.
    tmp = Path(path).with_name(Path(path).stem + ".part.h5ad")
    tmp.unlink(missing_ok=True)
    if len(per) == 1:
        adata = per[0][1]
        cell_col = per[0][2]
        adata.obs[IMAGE_COLUMN] = adata.obs[IMAGE_COLUMN].astype(str).astype("category")
        say("Writing the working file")
        sm.io.writeH5ad(adata, outputDir=str(tmp), overwrite=True, verbose=False, progress=False)
    else:
        common = set(per[0][1].var_names)
        for _, a, _ in per[1:]:
            common &= set(a.var_names)
        everything = set().union(*[set(a.var_names) for _, a, _ in per])
        dropped = sorted(everything - common)
        cell_cols = {c for _, _, c in per}
        cell_col = cell_cols.pop() if len(cell_cols) == 1 else "CellID"
        parts = []
        for _, a, c in per:
            if c != cell_col and c in a.obs.columns:
                a.obs[cell_col] = a.obs[c]
            a.obs[IMAGE_COLUMN] = a.obs[IMAGE_COLUMN].astype(str).astype("category")
            parts.append(a)
        # Scimap Pro joins and writes the file in one go (on disk, sparse
        # where the matrix is), keyed by project so cell names stay unique.
        say("Joining the projects")
        sm.tl.concatenate(parts, join="inner", imageId=IMAGE_COLUMN,
                          keys=[info["project"] for info, _, _ in per],
                          outputDir=str(tmp), verbose=False, progress=False)
    record = _record(context, mode, path, infos, calls, cell_col, IMAGE_COLUMN, started,
                     dropped=dropped, sample=sample)
    metadata = (options or {}).get("metadata")
    if metadata and metadata.get("columns"):
        record.update(_add_metadata(str(tmp), metadata, IMAGE_COLUMN, say))
    write_record(str(tmp), record)
    os.replace(tmp, path)
    return {**record, **describe(path, refresh=True)}


def _add_metadata(path, metadata, image_column, say):
    """The sample metadata, written by Scimap Pro; when the installed release
    cannot, the data is still prepared and the record says why."""
    from plexora.plugins.scimap.server import install, samples

    if not install.supports("metadata"):
        return {"metadata": [], "metadata_note": install.why_not("metadata")}
    say("Adding the sample metadata")
    return {"metadata": samples.write_into(path, metadata, image_column)}


def write_record(path, record):
    """`uns["plexora"]`: what made this file, one JSON string (h5ad cannot hold
    a list of records; it reads the same in Jupyter), written by Scimap Pro's
    store."""
    from scimappro._store import openTable

    table = openTable(str(path), path="", writable=True)
    try:
        table.writeUns("plexora", json.dumps(_uns_safe(record)))
    finally:
        table.close()


def _record(context, mode, path, infos, calls, cell_col, image_col, started, *, dropped=(), sample=None):
    pixel = {i["project"]: i.get("pixel_size") for i in infos}
    return {
        "path": str(path),
        "file": Path(path).name,
        "results_dir": str(Path(path).parent),
        "mode": mode,
        "cell_column": cell_col,
        "image_column": image_col,
        "x_column": X_COLUMN,
        "y_column": Y_COLUMN,
        "coordinates": "px",
        "pixel_size": pixel,
        "log_transformed": any(i.get("log_transformed") for i in infos),
        "markers_dropped": list(dropped),
        "sample": int(sample) if sample else None,
        "sources": [{"project": i["project"], "type": i.get("type"), "file": i.get("file")} for i in infos],
        "calls": calls,
        "prepared_at": time.time(),
        "seconds": round(time.time() - started, 2),
    }


def _uns_safe(record):
    """`uns["plexora"]`: no absolute paths of this computer (the file may be
    shared)."""
    def clean(value):
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items() if v is not None}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value if v is not None]
        return value
    safe = {k: v for k, v in record.items() if k not in ("path", "results_dir")}
    return clean(safe)


# -- reading it back ------------------------------------------------------


def describe(path, refresh=False) -> dict:
    """`{n_cells, markers, images, columns: [{name, kind, categories, n}]}` for
    the working file, cached on its modification time."""
    path = str(path)
    try:
        stamp = os.stat(path).st_mtime_ns
    except FileNotFoundError:
        return {"exists": False}
    with _meta_lock:
        hit = _meta_cache.get(path)
        if hit and hit[0] == stamp and not refresh:
            return hit[1]
    lock = _file_lock(path)
    if hit is None:
        lock.acquire()
    elif not lock.acquire(blocking=False):
        return hit[1]  # a step is writing: the last description stands
    try:
        return _describe(path, stamp)
    finally:
        lock.release()


def _describe(path, stamp) -> dict:
    import anndata as ad
    import pandas as pd

    backed = ad.read_h5ad(path, backed="r")
    try:
        obs = backed.obs
        markers = [str(v) for v in backed.var_names]
        columns = []
        for name in obs.columns:
            series = obs[name]
            if isinstance(series.dtype, pd.CategoricalDtype) or series.dtype == object or \
                    pd.api.types.is_bool_dtype(series):
                # Missing values are not a group: a label column leaves the
                # cells nobody labelled empty, and "nan" is not a label.
                cats = series.dropna().astype(str)
                counts = cats.value_counts()
                if len(counts) <= 200:
                    from plexora.plugins.scimap.server import palette

                    stored = backed.uns.get(f"{name}_colors") if f"{name}_colors" in backed.uns else None
                    columns.append({"name": str(name), "kind": "category", "n": int(len(counts)),
                                    "categories": [str(c) for c in _natural(counts.index)],
                                    "colors": palette.assign(series, None if stored is None
                                                             else [str(v) for v in list(stored)])})
                else:
                    columns.append({"name": str(name), "kind": "id", "n": int(len(counts))})
            else:
                columns.append({"name": str(name), "kind": "number"})
        images = sorted(obs[IMAGE_COLUMN].astype(str).unique().tolist()) if IMAGE_COLUMN in obs.columns else []
        info = {"exists": True, "n_cells": int(backed.n_obs), "markers": markers,
                "images": images, "columns": columns, "obsm": [str(k) for k in backed.obsm.keys()],
                "uns": [str(k) for k in backed.uns.keys()], "mtime": stamp,
                "size": os.path.getsize(path), **_matrix_facts(backed)}
    finally:
        backed.file.close()
    with _meta_lock:
        _meta_cache[path] = (stamp, info)
    return info


def _matrix_facts(backed) -> dict:
    """Which expression matrix the analysis functions should read, and whether
    it still needs logging.

    `tl.cluster` and `tl.umap` default to `layer='raw', log=True`: the raw
    intensities, logged on the fly. That is what a CSV prepared here holds
    (`readIntensityTable(log=True)` keeps them in `.raw`). An AnnData made
    anywhere else may carry no raw layer, and the default then refuses with
    "This dataset has no raw layer" -- so the steps pass `layer` and `log`
    explicitly from these facts.
    """
    has_raw = backed.raw is not None or "raw" in (backed.layers.keys() if backed.layers is not None else [])
    logged = None
    try:
        record = backed.uns.get("plexora")
        if isinstance(record, str):
            import json

            record = json.loads(record)
        if isinstance(record, dict):
            logged = record.get("log_transformed")
    except Exception:  # noqa: BLE001
        logged = None
    if logged is None:
        try:
            import numpy as np

            sample = backed.X[: min(int(backed.n_obs), 2000)]
            sample = sample.toarray() if hasattr(sample, "toarray") else np.asarray(sample)
            finite = sample[np.isfinite(sample)]
            # log1p of 16-bit intensities stays under ~11; raw intensities do not.
            logged = bool(finite.size) and float(finite.min()) >= 0 and float(finite.max()) <= 30
        except Exception:  # noqa: BLE001
            logged = False
    named = [str(k) for k in (backed.layers.keys() if backed.layers is not None else []) if str(k) != "raw"]
    layers = (["raw"] if has_raw else []) + ["X"] + named
    return {"raw": bool(has_raw), "x_logged": bool(logged), "layers": layers}


def _natural(values):
    """The order every plot, legend and the viewer give a column's groups, so a
    group keeps its colour everywhere (results.natural, shared)."""
    from plexora.plugins.scimap.server.results import natural

    return natural(values)
