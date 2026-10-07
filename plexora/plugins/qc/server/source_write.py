"""QC calls into the user's own file -- only when asked, and only QC's keys.

    AnnData / SpatialData table:
        obs["plexora_qc_pass"]            nullable boolean (other images' rows <NA>)
        obs["plexora_qc_primary_reason"]  category ("" for a passing cell)
        obs["plexora_qc_category"]        category: the primary reason's category
                                          (blur_focus, registration, segmentation,
                                          tissue_acquisition, staining_signal,
                                          review) -- a warned cell's first
                                          reason's; "" for a clean cell, and
                                          for one with notes only
        obs["plexora_qc_reason_count"]    Int64
        obs["plexora_qc_unreliable_markers"]  string, ";"-joined ("" for none)
        obs["plexora_qc_noted"]           string, ";"-joined: the reasons that
                                          only note the cell (kept, recorded)
        obs["plexora_qc_background"]      nullable boolean: outside the feathered
                                          tissue (the Background ROI), whatever
                                          it does to the cell (noted unless the
                                          user made it exclude)
        obsm["plexora_qc_flags"]          DataFrame, one boolean column per
                                          whole-cell reason (noted ones too)
        obsm["plexora_qc_marker_flags"]   DataFrame, one boolean column per
                                          marker: True where that marker's
                                          value is unreliable for the cell
        uns["plexora_qc"]                 version, definitions (cell and
                                          marker reasons), the categories and
                                          which reason is in which, strictness,
                                          ids
    CSV / Parquet:
        the four scalar columns, plexora_qc_background, plexora_qc_reasons,
        plexora_qc_noted and plexora_qc_unreliable_markers (";"-joined), and
        plexora_qc_marker_flags (";"-joined "marker|reason|status")

Every input row is kept: a cell outside the tissue is annotated, never dropped.

and, when Segmentation QC has a result (either block may be written alone):

    obs / columns:
        plexora_seg_qc_status        pass / under_segmented / over_segmented /
                                     ambiguous ("" for a row it did not score)
        plexora_seg_qc_under_score   float32 (NaN for a row it did not score)
        plexora_seg_qc_over_score    float32
        plexora_seg_qc_partner_id    Int64: the cell an over-segmented fragment
                                     was cut from
        plexora_seg_qc_under_segmented / _over_segmented / _large / _small /
        _irregular                   nullable boolean, one per category (the
                                     size three only for a result with shapes)
        uns["plexora_seg_qc"]        version, fingerprint, the thresholds, the
                                     summary (its own key, so writing one block
                                     never rewrites the other's)

The status and the five booleans are at the thresholds the write was given
(the panel's sliders), else the run's own flag and `OUTLIER_Z`; the scores are
always the stored ones. `what` writes the QC calls, Segmentation QC, or both.

`plexora_qc_pass` is about the whole cell. A cell that passes can still have
a marker it should not be read in: filter on both.

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

OBS_COLUMNS = ("plexora_qc_pass", "plexora_qc_primary_reason", "plexora_qc_category",
               "plexora_qc_reason_count", "plexora_qc_unreliable_markers", "plexora_qc_noted",
               "plexora_qc_background")
FLAT_REASONS = "plexora_qc_reasons"
FLAT_MARKER_FLAGS = "plexora_qc_marker_flags"
OBSM_KEY = "plexora_qc_flags"
OBSM_MARKERS = "plexora_qc_marker_flags"
UNS_KEY = "plexora_qc"
SEG_OBS_COLUMNS = ("plexora_seg_qc_status", "plexora_seg_qc_under_score",
                   "plexora_seg_qc_over_score", "plexora_seg_qc_partner_id")
SEG_CATEGORIES = ("under_segmented", "over_segmented", "large", "small", "irregular")
SEG_UNS_KEY = "plexora_seg_qc"
MAX_ROWS = 2_000_000


class Exists(Exception):
    def __init__(self, existing):
        super().__init__("the file already holds QC columns")
        self.existing = existing


def _per_row(ds, cells):
    """(pass, primary, count, reasons, flags {reason: bool[]}, unreliable,
    marker_flags, markers {marker: bool[]}, category, noted, background)
    aligned with the loaded table's rows (None for rows without a call)."""
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
    unreliable = cells["unreliable_markers"].to_list() if "unreliable_markers" in cells.columns \
        else [[] for _ in range(cells.height)]
    per_flag = cells["marker_flags"].to_list() if "marker_flags" in cells.columns \
        else [[] for _ in range(cells.height)]
    out_unreliable = [";".join(unreliable[r] or []) if r is not None else None for r in rows]
    out_marker_flags = [";".join(per_flag[r] or []) if r is not None else None for r in rows]
    markers = {}
    for marker in ds.table.markers:
        markers[marker] = [marker in (unreliable[r] or []) if r is not None else None
                           for r in rows]
    noted = cells["noted_by"].to_list() if "noted_by" in cells.columns \
        else [[] for _ in range(cells.height)]
    out_category = [_category(primary[r], reasons[r], noted[r]) if r is not None else None
                    for r in rows]
    out_noted = [";".join(noted[r] or []) if r is not None else None for r in rows]
    outside = cells["background"].to_list() if "background" in cells.columns \
        else ["background" in (reasons[r] or []) for r in range(cells.height)]
    out_background = [bool(outside[r]) if r is not None else None for r in rows]
    return (out_pass, out_primary, out_count, out_reasons, flags, out_unreliable,
            out_marker_flags, markers, out_category, out_noted, out_background)


def _category(primary, reasons, noted=None):
    """A cell's category: its primary reason's, else its first reason's that
    does more than note it ("" for a cell with notes only)."""
    if primary:
        return schemas.category_of_reason(primary)
    noted = set(noted or [])
    left = [r for r in reasons or [] if r not in noted]
    return schemas.category_of_reason(left[0]) if left else ""


def _table_ids(ds):
    frame = ds.table.frame()
    cell_id = ds.schema.cell_id if ds.schema else None
    column = cell_id if cell_id and cell_id in frame.columns else "id"
    return frame[column].cast(float, strict=False).to_numpy() if column in frame.columns \
        else np.arange(frame.height, dtype=float)


def _seg_per_row(ds, seg):
    """(status, under, over, partner, {category: bool[]}) aligned with the
    loaded table's rows; `seg` is `segqc.calls`'s frame."""
    lookup = {int(cid): i for i, cid in enumerate(seg["cell_id"].to_list())}
    rows = [lookup.get(int(v)) if np.isfinite(v) else None for v in _table_ids(ds)]
    words = seg["status"].to_list()
    under = seg["under_score"].to_list()
    over = seg["over_score"].to_list()
    partner = seg["partner_id"].to_list()
    flags = {}
    for name in SEG_CATEGORIES:
        if name in seg.columns:
            column = seg[name].to_list()
            flags[name] = [bool(column[r]) if r is not None else None for r in rows]
    return ([words[r] if r is not None else "" for r in rows],
            [float(under[r]) if r is not None else None for r in rows],
            [float(over[r]) if r is not None else None for r in rows],
            [int(partner[r]) if r is not None and partner[r] else None for r in rows],
            flags)


def _seg_columns(flags):
    return list(SEG_OBS_COLUMNS) + [f"plexora_seg_qc_{name}" for name in flags]


def _seg_uns_body(summary, th):
    return {"version": str(summary.get("version") or ""),
            "fingerprint": str(summary.get("fingerprint") or ""),
            "dna_channel": str(summary.get("dna_channel") or ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "thresholds": json.dumps(th),
            "summary": json.dumps({k: v for k, v in summary.items() if k != "mask"},
                                  default=str)}


def _segmentation(ds, thresholds=None):
    """(summary, per-cell calls, thresholds) of Segmentation QC's current
    result at `thresholds` (`segqc.thresholds`'s keywords), or None."""
    from plexora.plugins.qc.server.segqc import run as segqc

    found = segqc.calls(ds.name, **(thresholds or {}))
    if found is None or not found[1].height:
        return None
    return found


def _uns_body(result, document):
    from plexora.plugins.qc.server import provenance

    return {"version": schemas.RESULT_VERSION,
            "definitions": json.dumps(schemas.REASON_DEFINITIONS),
            "marker_definitions": json.dumps(schemas.MARKER_REASON_DEFINITIONS),
            "categories": json.dumps(provenance.vocabulary()),
            "strictness": json.dumps(document.get("strictness") or {}),
            "session_id": str(result.get("session_id") or ""),
            "result_id": str(result["result_id"]),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "software_version": str(result.get("software_version") or ""),
            "summary": json.dumps(results.summary(result), default=str)}


def write(ds, *, replace=False, what="both", thresholds=None):
    """Write the active result's calls (`what` "qc"), Segmentation QC's at
    `thresholds` ("segmentation"), or both, into the project's own table file."""
    from plexora.server.models.adapters import is_flat_table

    document = results.load(ds.name)
    result = results.active(document)
    cells = results.cells(ds.name) if what != "segmentation" else None
    has_calls = result is not None and cells is not None and bool(cells.height)
    seg = _segmentation(ds, thresholds) if what != "qc" else None
    if what == "segmentation" and seg is None:
        raise LookupError("Segmentation QC has no result to write: run it first")
    if not has_calls and seg is None:
        raise LookupError("this project has no QC cell calls to write (finish a QC session, "
                          "change the strictness, or run Segmentation QC, first)")
    for frame in (cells if has_calls else None, seg[1] if seg else None):
        if frame is not None and frame.height > MAX_ROWS:
            raise OverflowError(f"{frame.height} cells is more than a source write handles "
                                f"({MAX_ROWS}); export CSV instead")
    kind = ds.source_kind
    values = _per_row(ds, cells) if has_calls else None
    seg_values = (_seg_per_row(ds, seg[1]), seg[0], seg[2]) if seg else None
    if is_flat_table(kind):
        return _write_flat(ds, kind, values, replace, seg_values)
    if kind in ("anndata", "spatialdata"):
        return _write_anndata(ds, values, result, document, replace, seg_values)
    raise LookupError("this project has no cell-level file to write into")


def _write_flat(ds, kind, values, replace, seg_values=None):
    import contextlib
    import os
    import tempfile

    import polars as pl

    from plexora.server.models.adapters import read_flat_table, write_flat_table

    source = ds.table.source
    frame = read_flat_table(source.path, kind)
    written = ([*OBS_COLUMNS, FLAT_REASONS, FLAT_MARKER_FLAGS] if values else []) \
        + (_seg_columns(seg_values[0][4]) if seg_values else [])
    taken = [c for c in written if c in frame.columns]
    if taken and not replace:
        raise Exists(taken)
    n_rows = len(values[0]) if values else len(seg_values[0][0])
    if frame.height != n_rows:
        raise ValueError(f"this project's table has {n_rows} cells but the file now "
                         f"has {frame.height} rows; reopen the project and try again")
    columns = []
    passes = []
    if values:
        passes, primary, count, reasons, _flags, unreliable, marker_flags, _markers, \
            category, noted, background = values
        columns += [
            pl.Series("plexora_qc_pass", passes, dtype=pl.Boolean),
            pl.Series("plexora_qc_primary_reason", primary, dtype=pl.Utf8),
            pl.Series("plexora_qc_category", category, dtype=pl.Utf8),
            pl.Series("plexora_qc_reason_count", count, dtype=pl.Int64),
            pl.Series(FLAT_REASONS, reasons, dtype=pl.Utf8),
            pl.Series("plexora_qc_unreliable_markers", unreliable, dtype=pl.Utf8),
            pl.Series(FLAT_MARKER_FLAGS, marker_flags, dtype=pl.Utf8),
            pl.Series("plexora_qc_noted", noted, dtype=pl.Utf8),
            pl.Series("plexora_qc_background", background, dtype=pl.Boolean)]
    if seg_values:
        (status, under, over, partner, flags), _summary, _th = seg_values
        columns += [
            pl.Series("plexora_seg_qc_status", status, dtype=pl.Utf8),
            pl.Series("plexora_seg_qc_under_score", under, dtype=pl.Float32),
            pl.Series("plexora_seg_qc_over_score", over, dtype=pl.Float32),
            pl.Series("plexora_seg_qc_partner_id", partner, dtype=pl.Int64)]
        columns += [pl.Series(f"plexora_seg_qc_{name}", per, dtype=pl.Boolean)
                    for name, per in flags.items()]
    frame = frame.with_columns(columns)
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
    return {"path": source.path, "columns": written, "n_cells": n_rows,
            "n_fail": sum(1 for v in passes if v is False),
            "segmentation_qc": bool(seg_values)}


def _write_anndata(ds, values, result, document, replace, seg_values=None):
    import pandas as pd

    try:
        from anndata.io import read_elem, write_elem
    except ImportError:  # pragma: no cover
        from anndata._io.specs import read_elem, write_elem

    from plexora.plugins.roi.server import adapters

    path = adapters._table_path(ds)
    source = ds.table.source
    obs_written = (list(OBS_COLUMNS) if values else []) \
        + (_seg_columns(seg_values[0][4]) if seg_values else [])
    with adapters._open_group(path) as handle:
        obs = read_elem(handle["obs"])
        existing = [c for c in obs_written if c in obs.columns]
        if values:
            for key in (OBSM_KEY, OBSM_MARKERS):
                if "obsm" in handle and key in handle["obsm"]:
                    existing.append(f"obsm/{key}")
            if "uns" in handle and UNS_KEY in handle["uns"]:
                existing.append(f"uns/{UNS_KEY}")
        if seg_values and "uns" in handle and SEG_UNS_KEY in handle["uns"]:
            existing.append(f"uns/{SEG_UNS_KEY}")
    if existing and not replace:
        raise Exists(existing)
    n_rows = len(values[0]) if values else len(seg_values[0][0])
    mask = adapters._project_rows(ds, obs)
    if int(mask.sum()) != n_rows:
        raise ValueError(f"this project's table has {n_rows} cells but the file now has "
                         f"{int(mask.sum())} matching rows; reopen the project and try again")
    backup = _backup(ds, obs)
    # A replace starts from nothing: a row this write has no call for must
    # not keep the previous run's value.
    obs = obs.drop(columns=[c for c in obs_written if c in obs.columns])
    if seg_values:
        (status, under, over, partner, flags), seg_summary, seg_th = seg_values
        obs = _assign(obs, mask, "plexora_seg_qc_status", status, ds, "category")
        obs = _assign(obs, mask, "plexora_seg_qc_under_score", under, ds, "float32")
        obs = _assign(obs, mask, "plexora_seg_qc_over_score", over, ds, "float32")
        obs = _assign(obs, mask, "plexora_seg_qc_partner_id", partner, ds, "Int64")
        for name, per in flags.items():
            obs = _assign(obs, mask, f"plexora_seg_qc_{name}", per, ds, "boolean")
    if not values:
        with adapters._open_group(path, writable=True) as handle:
            del handle["obs"]
            write_elem(handle, "obs", obs)
            if "uns" not in handle:
                handle.create_group("uns")
            if SEG_UNS_KEY in handle["uns"]:
                del handle["uns"][SEG_UNS_KEY]
            write_elem(handle["uns"], SEG_UNS_KEY, _seg_uns_body(seg_summary, seg_th))
        return {"path": str(path), "columns": obs_written, "uns": SEG_UNS_KEY,
                "n_cells": n_rows, "n_fail": 0, "backup": backup,
                "source_kind": ds.source_kind, "table": getattr(source, "table", None),
                "segmentation_qc": True}
    passes, primary, count, _reasons, flags, unreliable, _marker_flags, markers, category, \
        noted, background = values
    obs = _assign(obs, mask, "plexora_qc_pass", passes, ds, "boolean")
    obs = _assign(obs, mask, "plexora_qc_primary_reason", primary, ds, "category")
    obs = _assign(obs, mask, "plexora_qc_category", category, ds, "category")
    obs = _assign(obs, mask, "plexora_qc_reason_count", count, ds, "Int64")
    obs = _assign(obs, mask, "plexora_qc_unreliable_markers", unreliable, ds, "string")
    obs = _assign(obs, mask, "plexora_qc_noted", noted, ds, "string")
    obs = _assign(obs, mask, "plexora_qc_background", background, ds, "boolean")
    flag_frame = pd.DataFrame(index=obs.index)
    for reason, per in flags.items():
        column = _assign(pd.DataFrame(index=obs.index), mask, reason, per, ds, "boolean")
        flag_frame[reason] = column[reason]
    flag_frame.index = flag_frame.index.astype(str)
    marker_frame = pd.DataFrame(index=obs.index)
    for marker, per in markers.items():
        column = _assign(pd.DataFrame(index=obs.index), mask, marker, per, ds, "boolean")
        marker_frame[marker] = column[marker]
    marker_frame.index = marker_frame.index.astype(str)
    with adapters._open_group(path, writable=True) as handle:
        del handle["obs"]
        write_elem(handle, "obs", obs)
        if "obsm" not in handle:
            handle.create_group("obsm")
        for key, body in ((OBSM_KEY, flag_frame), (OBSM_MARKERS, marker_frame)):
            if key in handle["obsm"]:
                del handle["obsm"][key]
            write_elem(handle["obsm"], key, body)
        if "uns" not in handle:
            handle.create_group("uns")
        if UNS_KEY in handle["uns"]:
            del handle["uns"][UNS_KEY]
        write_elem(handle["uns"], UNS_KEY, _uns_body(result, document))
        if seg_values:
            if SEG_UNS_KEY in handle["uns"]:
                del handle["uns"][SEG_UNS_KEY]
            write_elem(handle["uns"], SEG_UNS_KEY, _seg_uns_body(seg_summary, seg_th))
    return {"path": str(path), "columns": obs_written, "obsm": OBSM_KEY,
            "obsm_markers": OBSM_MARKERS, "uns": UNS_KEY,
            "n_cells": len(passes), "n_fail": sum(1 for v in passes if v is False),
            "backup": backup, "source_kind": ds.source_kind,
            "table": getattr(source, "table", None), "segmentation_qc": bool(seg_values)}


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
    elif dtype == "float32":
        # Plain float32, NaN where unscored: anndata cannot write pandas'
        # nullable Float32.
        series = pd.to_numeric(series, errors="coerce").astype("float32")
    elif dtype == "category":
        series = series.astype("string").astype("category")
    elif dtype == "string":
        series = series.astype("string")
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
