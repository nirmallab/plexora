"""QC calls into the user's own file -- only when asked, and only QC's keys.

    AnnData / SpatialData table:
        obs["plexora_qc_pass"]            nullable boolean (other images' rows <NA>)
        obs["plexora_qc_primary_reason"]  category ("" for a passing cell)
        obs["plexora_qc_reason_count"]    Int64
        obsm["plexora_qc_flags"]          DataFrame, one boolean column per reason
        uns["plexora_qc"]                 version, definitions, strictness, ids
    CSV / Parquet:
        the three scalar columns plus plexora_qc_reasons (";"-joined)

The row alignment, the per-image mask and the consolidated-zarr handling are
the ROI plugin's (`roi.server.adapters`), which already writes cell columns
safely: rows of another image in a shared file are never touched, an existing
QC column is refused unless `replace`, and `obs` is backed up before it is
rewritten (an h5py rewrite is not atomic).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from plexora.plugins.qc.server import results, schemas

OBS_COLUMNS = ("plexora_qc_pass", "plexora_qc_primary_reason", "plexora_qc_reason_count")
FLAT_REASONS = "plexora_qc_reasons"
OBSM_KEY = "plexora_qc_flags"
UNS_KEY = "plexora_qc"
MAX_ROWS = 2_000_000


class Exists(Exception):
    def __init__(self, existing):
        super().__init__("the file already holds QC columns")
        self.existing = existing


def _per_row(ds, cells):
    """(pass, primary, count, reasons, flags {reason: bool[]}) aligned with the
    loaded table's rows (None for rows without a call)."""
    frame = ds.table.frame()
    cell_id = ds.schema.cell_id if ds.schema else None
    column = cell_id if cell_id and cell_id in frame.columns else "id"
    ids = frame[column].cast(float, strict=False).to_numpy() if column in frame.columns \
        else np.arange(frame.height, dtype=float)
    lookup = {int(cid): i for i, cid in enumerate(cells["cell_id"].to_list())}
    rows = [lookup.get(int(v)) if np.isfinite(v) else None for v in ids]
    passes = cells["pass"].to_list()
    primary = cells["primary_reason"].to_list()
    count = cells["reason_count"].to_list()
    reasons = cells["reasons"].to_list()
    out_pass = [passes[r] if r is not None else None for r in rows]
    out_primary = [primary[r] or "" if r is not None else None for r in rows]
    out_count = [int(count[r]) if r is not None else None for r in rows]
    out_reasons = [";".join(reasons[r]) if r is not None else None for r in rows]
    flags = {}
    for reason in schemas.REASONS:
        flags[reason] = [reason in reasons[r] if r is not None else None for r in rows]
    return out_pass, out_primary, out_count, out_reasons, flags


def _uns_body(result, document):
    return {"version": schemas.RESULT_VERSION,
            "definitions": json.dumps(schemas.REASON_DEFINITIONS),
            "strictness": json.dumps(document.get("strictness") or {}),
            "session_id": str(result.get("session_id") or ""),
            "result_id": str(result["result_id"]),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "software_version": str(result.get("software_version") or ""),
            "summary": json.dumps(results.summary(result), default=str)}


def write(ds, *, replace=False):
    """Write the active result's calls into the project's own table file."""
    from plexora.server.models.adapters import is_flat_table

    document = results.load(ds.name)
    result = results.active(document)
    cells = results.cells(ds.name)
    if result is None or cells is None or not cells.height:
        raise LookupError("this project has no QC cell calls to write (finish a QC session, "
                          "or change the strictness, first)")
    if cells.height > MAX_ROWS:
        raise OverflowError(f"{cells.height} cells is more than a source write handles "
                            f"({MAX_ROWS}); export CSV instead")
    kind = ds.source_kind
    values = _per_row(ds, cells)
    if is_flat_table(kind):
        return _write_flat(ds, kind, values, replace)
    if kind in ("anndata", "spatialdata"):
        return _write_anndata(ds, values, result, document, replace)
    raise LookupError("this project has no cell-level file to write into")


def _write_flat(ds, kind, values, replace):
    import contextlib
    import os
    import tempfile

    import polars as pl

    from plexora.server.models.adapters import read_flat_table, write_flat_table

    source = ds.table.source
    frame = read_flat_table(source.path, kind)
    taken = [c for c in (*OBS_COLUMNS, FLAT_REASONS) if c in frame.columns]
    if taken and not replace:
        raise Exists(taken)
    passes, primary, count, reasons, _flags = values
    if frame.height != len(passes):
        raise ValueError(f"this project's table has {len(passes)} cells but the file now "
                         f"has {frame.height} rows; reopen the project and try again")
    frame = frame.with_columns([
        pl.Series("plexora_qc_pass", passes, dtype=pl.Boolean),
        pl.Series("plexora_qc_primary_reason", primary, dtype=pl.Utf8),
        pl.Series("plexora_qc_reason_count", count, dtype=pl.Int64),
        pl.Series(FLAT_REASONS, reasons, dtype=pl.Utf8)])
    directory = os.path.dirname(os.path.abspath(source.path)) or "."
    handle, temporary = tempfile.mkstemp(suffix=Path(source.path).suffix, dir=directory)
    os.close(handle)
    try:
        write_flat_table(frame, temporary, kind)
        os.replace(temporary, source.path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return {"path": source.path, "columns": [*OBS_COLUMNS, FLAT_REASONS],
            "n_cells": len(passes), "n_fail": sum(1 for v in passes if v is False)}


def _write_anndata(ds, values, result, document, replace):
    import pandas as pd

    try:
        from anndata.io import read_elem, write_elem
    except ImportError:  # pragma: no cover
        from anndata._io.specs import read_elem, write_elem

    from plexora.plugins.roi.server import adapters

    path = adapters._table_path(ds)
    source = ds.table.source
    with adapters._open_group(path) as handle:
        obs = read_elem(handle["obs"])
        existing = [c for c in OBS_COLUMNS if c in obs.columns]
        if "obsm" in handle and OBSM_KEY in handle["obsm"]:
            existing.append(f"obsm/{OBSM_KEY}")
        if "uns" in handle and UNS_KEY in handle["uns"]:
            existing.append(f"uns/{UNS_KEY}")
    if existing and not replace:
        raise Exists(existing)
    passes, primary, count, _reasons, flags = values
    mask = adapters._project_rows(ds, obs)
    if int(mask.sum()) != len(passes):
        raise ValueError(f"this project's table has {len(passes)} cells but the file now has "
                         f"{int(mask.sum())} matching rows; reopen the project and try again")
    backup = _backup(ds, obs)
    obs = _assign(obs, mask, "plexora_qc_pass", passes, ds, "boolean")
    obs = _assign(obs, mask, "plexora_qc_primary_reason", primary, ds, "category")
    obs = _assign(obs, mask, "plexora_qc_reason_count", count, ds, "Int64")
    flag_frame = pd.DataFrame(index=obs.index)
    for reason, per in flags.items():
        column = _assign(pd.DataFrame(index=obs.index), mask, reason, per, ds, "boolean")
        flag_frame[reason] = column[reason]
    flag_frame.index = flag_frame.index.astype(str)
    with adapters._open_group(path, writable=True) as handle:
        del handle["obs"]
        write_elem(handle, "obs", obs)
        if "obsm" not in handle:
            handle.create_group("obsm")
        if OBSM_KEY in handle["obsm"]:
            del handle["obsm"][OBSM_KEY]
        write_elem(handle["obsm"], OBSM_KEY, flag_frame)
        if "uns" not in handle:
            handle.create_group("uns")
        if UNS_KEY in handle["uns"]:
            del handle["uns"][UNS_KEY]
        write_elem(handle["uns"], UNS_KEY, _uns_body(result, document))
    return {"path": str(path), "columns": list(OBS_COLUMNS), "obsm": OBSM_KEY, "uns": UNS_KEY,
            "n_cells": len(passes), "n_fail": sum(1 for v in passes if v is False),
            "backup": backup, "source_kind": ds.source_kind,
            "table": getattr(source, "table", None)}


def _assign(frame, mask, column, values, ds, dtype):
    """One column onto the masked rows, aligned by cell id (the ROI adapter's
    rule), then cast to a nullable dtype."""
    import pandas as pd

    from plexora.plugins.roi.server import adapters

    frame = adapters._assign_column(frame, mask, column, [None if v is None else v
                                                          for v in values],
                                    ds, ds.table.source, categorical=False)
    series = frame[column]
    if dtype == "boolean":
        series = series.map(lambda v: pd.NA if v is None or v is pd.NA or v != v
                            else str(v) == "True").astype("boolean")
    elif dtype == "Int64":
        series = pd.to_numeric(series, errors="coerce").astype("Int64")
    elif dtype == "category":
        series = series.astype("string").astype("category")
    frame[column] = series
    return frame


def _backup(ds, obs):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    folder = results._store(ds.name).directory() / "backups" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "obs.parquet"
    try:
        obs.to_parquet(target)
    except Exception:
        obs.astype(str).to_parquet(target)
    return str(target)
