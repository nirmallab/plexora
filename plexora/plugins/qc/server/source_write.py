"""QC calls into the user's own file -- only when asked, and only QC's keys.

The columns are `cells/columns.py`'s, with the prefix `plexora_qc_`:

    AnnData / SpatialData table:
        obs["plexora_qc_status"]          category: pass / flagged / excluded
        obs["plexora_qc_pass"]            nullable boolean (other images' rows <NA>)
        obs["plexora_qc_primary_reason"]  category ("" for a passing cell)
        obs["plexora_qc_category"]        category: the primary reason's category
                                          (blur_focus, registration, segmentation,
                                          tissue_acquisition, staining_signal,
                                          review) -- a warned cell's first
                                          reason's; "" for a clean cell, and
                                          for one with notes only
        obs["plexora_qc_categories"]      string, ";"-joined
        obs["plexora_qc_blur_focus"] ...  category, one per category (and
                                          `custom`): excluded / flagged / channel
                                          (a marker of it unreliable; the cell
                                          stands) / noted / ""
        obs["plexora_qc_reasons"], ["_excluded_by"], ["_noted"],
        ["_unreliable_markers"], ["_marker_flags"], ["_roi_ids"]
                                          strings, ";"-joined
        obs["plexora_qc_reason_count"]    Int64
        obs["plexora_qc_background"]      nullable boolean: outside the feathered
                                          tissue (the Background ROI), whatever
                                          it does to the cell (noted unless the
                                          user made it exclude)
        obs["plexora_qc_roi_method"], ["_flag_source"], ["_source"]  category
        obsm["plexora_qc_flags"]          DataFrame, one boolean column per
                                          whole-cell reason (noted ones too)
        obsm["plexora_qc_marker_flags"]   DataFrame, one boolean column per
                                          marker: True where that marker's
                                          value is excluded for the cell
        obsm["plexora_qc_marker_warn"]    the same for markers only warned
        uns["plexora_qc"]                 version "2", definitions, the column
                                          dictionary, and `results` keyed by
                                          image id (each image's result id,
                                          settings, inputs and regions)
    SpatialData, besides the table:
        shapes["plexora_qc_regions"]      the regions behind the calls, mapped
                                          onto the mask's labels element
                                          (`spatialdata_write`)
    CSV / Parquet:
        the same columns, the whole file backed up first

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

Two halves, because the file may be on a data node: `gather` reads QC's own
store (on the primary) into a payload, and `apply` -- the `qc.write_cells`
table operation (`tableops.py`) -- writes it wherever the table is. Rows are
placed by `row` (each call's row in the table as loaded) with an id check, so
a repeated id (a time series) and a corrected id both land right. In a file
shared by several images only this project's rows are touched: a column
"exists" when THIS image's rows hold values in it, `replace` clears and
rewrites those rows alone, and the other images' values stay. `obs` (or the
whole flat file) is backed up before it is rewritten.
"""

from __future__ import annotations

import base64
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from plexora.plugins.qc.server import results, schemas
from plexora.plugins.qc.server.cells import columns as qc_columns

#: The obs / flat columns of the QC calls (every canonical column but the id).
QC_COLUMNS = tuple(f"{qc_columns.PREFIX}{c}" for c in qc_columns.ORDER if c != "cell_id")
OBS_COLUMNS = QC_COLUMNS
FLAT_REASONS = "plexora_qc_reasons"
FLAT_MARKER_FLAGS = "plexora_qc_marker_flags"
OBSM_KEY = qc_columns.OBSM_REASONS
OBSM_MARKERS = qc_columns.OBSM_MARKERS
OBSM_MARKERS_WARN = qc_columns.OBSM_MARKERS_WARN
OBSM_KEYS = (OBSM_KEY, OBSM_MARKERS, OBSM_MARKERS_WARN)
UNS_KEY = "plexora_qc"
SEG_OBS_COLUMNS = ("plexora_seg_qc_status", "plexora_seg_qc_under_score",
                   "plexora_seg_qc_over_score", "plexora_seg_qc_partner_id")
SEG_CATEGORIES = ("under_segmented", "over_segmented", "large", "small", "irregular")
SEG_UNS_KEY = "plexora_seg_qc"
MAX_ROWS = 2_000_000
OPERATION = "qc.write_cells"

_BOOLEAN = {"plexora_qc_pass", "plexora_qc_background"}
_INT = {"plexora_qc_reason_count"}
_CATEGORY = {f"{qc_columns.PREFIX}{c}" for c in qc_columns.CATEGORICAL}


class Exists(Exception):
    def __init__(self, existing):
        super().__init__("the file already holds QC columns")
        self.existing = existing


def _category(primary, reasons, noted=None):
    """A cell's category: its primary reason's, else its first reason's that
    does more than note it ("" for a cell with notes only)."""
    if primary:
        return schemas.category_of_reason(primary)
    noted = set(noted or [])
    left = [r for r in reasons or [] if r not in noted]
    return schemas.category_of_reason(left[0]) if left else ""


# -- frames over the wire -----------------------------------------------------------------


def encode(frame) -> str:
    """A polars frame as base64 Parquet (dtypes kept; the payload is JSON)."""
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def decode(text):
    import polars as pl

    return pl.read_parquet(io.BytesIO(base64.b64decode(text)))


# -- gathering, on the primary ------------------------------------------------------------


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


def _regions_payload(ds, result):
    """The regions behind the calls, with their outlines, for a SpatialData
    shapes element."""
    from plexora.plugins.qc.server import roi_link

    rows = {r["roi_id"]: r for r in qc_columns.region_rows(ds, result)}
    out = []
    for region in roi_link.live_regions(ds, result):
        row = rows.get(region["roi_id"])
        if row is not None and region.get("geometry"):
            out.append({**row, "geometry": region["geometry"]})
    return out


def gather(ds, *, what="both", thresholds=None) -> dict:
    """Everything a write needs from QC's store, as a JSON-safe payload."""
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
    loaded = ds.table.frame()
    payload = {"n_rows": int(loaded.height) if loaded is not None else 0,
               "image_id": qc_columns.image_id(ds), "kind": ds.source_kind}
    if has_calls:
        canon = qc_columns.build(cells, result, results.roi_meta(ds.name))
        obsm = qc_columns.obsm_frames(cells, list(ds.table.markers))
        payload.update(
            cells=encode(canon),
            obsm={key: encode(frame) for key, frame in obsm.items() if frame.width},
            uns=qc_columns.uns_body(ds, result, document),
            result_id=result["result_id"],
            legacy=("row" not in cells.columns))
        if ds.source_kind == "spatialdata":
            payload["regions"] = _regions_payload(ds, result)
            payload["seg_scale"] = int(getattr(ds.project.segmentation, "scale", 1) or 1)
    if seg is not None:
        summary, frame, th = seg
        payload["seg"] = {"frame": encode(frame), "uns": _seg_uns_body(summary, th)}
    return payload


# -- applying, wherever the table is -----------------------------------------------------


def _table_ids(dataset):
    """Each loaded row's cell id (float; NaN where it has none), by the rule
    the calls were derived with (`label_overlay.cell_ids`)."""
    frame = dataset.table.frame()
    cell_id = dataset.schema.cell_id if dataset.schema else None
    column = cell_id if cell_id and cell_id in frame.columns else "id"
    if column not in frame.columns:
        return np.arange(frame.height, dtype=float)
    return frame[column].cast(float, strict=False).fill_null(float("nan")).to_numpy()


def _positions(dataset, canon, legacy):
    """The loaded-table row of each call, checked against the table's ids."""
    ids = _table_ids(dataset)
    called = canon["cell_id"].to_numpy().astype(np.int64)
    if not legacy and "row" in canon.columns:
        rows = canon["row"].to_numpy().astype(np.int64)
        if rows.size and (rows.max() >= ids.size or rows.min() < 0):
            raise ValueError("the table has fewer rows than when QC derived its calls; "
                             "refresh QC and try again")
        if rows.size and not np.array_equal(ids[rows], called.astype(float)):
            raise ValueError("the table's cell ids are not the ones QC derived its calls "
                             "from; refresh QC and try again")
        return rows
    # Derived before rows were stored: the first row of each id.
    order = np.argsort(np.where(np.isfinite(ids), ids, np.inf), kind="stable")
    sorted_ids = ids[order]
    at = np.searchsorted(sorted_ids, called.astype(float))
    at = np.clip(at, 0, max(sorted_ids.size - 1, 0))
    found = sorted_ids[at] == called if sorted_ids.size else np.zeros(called.size, bool)
    rows = np.where(found, order[at], -1)
    return rows


def _aligned(frame, rows, n):
    """`frame`'s columns spread over `n` rows (None where no call)."""
    import polars as pl

    keep = rows >= 0
    placed = frame.filter(pl.Series(keep)).with_columns(
        pl.Series("__row", rows[keep], dtype=pl.Int64))
    full = pl.DataFrame({"__row": pl.Series(np.arange(n, dtype=np.int64))})
    return full.join(placed, on="__row", how="left").sort("__row").drop("__row")


def _seg_aligned(dataset, seg_frame):
    """Segmentation QC's columns over the loaded rows, by cell id."""
    import polars as pl

    ids = _table_ids(dataset)
    table = pl.DataFrame({"__row": np.arange(ids.size, dtype=np.int64),
                          "cell_id": pl.Series(ids).cast(pl.Int64, strict=False)})
    seg = seg_frame.with_columns(pl.col("cell_id").cast(pl.Int64)).unique(
        subset=["cell_id"], keep="first")
    joined = table.join(seg, on="cell_id", how="left", maintain_order="left").sort("__row")
    status = "status_word" if "status_word" in joined.columns else "status"
    picked = [pl.col(status).cast(pl.Utf8).fill_null("").alias("plexora_seg_qc_status"),
              pl.col("under_score").cast(pl.Float32).alias("plexora_seg_qc_under_score"),
              pl.col("over_score").cast(pl.Float32).alias("plexora_seg_qc_over_score"),
              pl.when(pl.col("partner_id") > 0).then(pl.col("partner_id")).otherwise(None)
              .cast(pl.Int64).alias("plexora_seg_qc_partner_id")]
    picked += [pl.col(name).cast(pl.Boolean).alias(f"plexora_seg_qc_{name}")
               for name in SEG_CATEGORIES if name in joined.columns]
    return joined.select(picked)


def _qc_frame(dataset, payload):
    """(canonical columns prefixed, {obsm key: frame}) over the loaded rows."""
    canon = decode(payload["cells"])
    rows = _positions(dataset, canon, payload.get("legacy"))
    n = int(payload["n_rows"])
    body = qc_columns.with_prefix(canon.drop([c for c in ("row", "cell_id")
                                              if c in canon.columns]))
    aligned = _aligned(body, rows, n)
    obsm = {key: _aligned(decode(text), rows, n)
            for key, text in (payload.get("obsm") or {}).items()}
    return aligned, obsm


def apply(dataset, payload, *, replace=False):
    """Write a `gather`ed payload into this dataset's own file."""
    from plexora.server.models.adapters import is_flat_table

    kind = dataset.source_kind
    loaded = dataset.table.frame()
    n = int(payload.get("n_rows") or 0)
    if loaded is None or loaded.height != n:
        raise ValueError(f"this project's table had {n} cells when QC was read but has "
                         f"{0 if loaded is None else loaded.height} here; reopen the project "
                         "and try again")
    qc = _qc_frame(dataset, payload) if payload.get("cells") else None
    seg = None
    if payload.get("seg"):
        seg = (_seg_aligned(dataset, decode(payload["seg"]["frame"])), payload["seg"]["uns"])
    if is_flat_table(kind):
        return _write_flat(dataset, kind, qc, seg, replace)
    if kind in ("anndata", "spatialdata"):
        return _write_anndata(dataset, payload, qc, seg, replace)
    raise LookupError("this project has no cell-level file to write into")


def write(ds, *, replace=False, what="both", thresholds=None):
    """Write the active result's calls (`what` "qc"), Segmentation QC's at
    `thresholds` ("segmentation"), or both, into the project's own table file
    -- through `qc.write_cells`, so a table on a data node is written there."""
    from plexora.plugins.qc.server import tableops

    payload = gather(ds, what=what, thresholds=thresholds)
    answer = tableops.run_write(ds, {**payload, "replace": bool(replace)})
    if payload.get("cells"):
        with results.lock(ds.name):
            document = results.load(ds.name)
            results.record_source_write(document, {
                "path": answer.get("path"), "kind": ds.source_kind,
                "image_id": payload["image_id"], "result_id": payload.get("result_id"),
                "columns_version": qc_columns.COLUMNS_VERSION,
                "columns": [c for c in answer.get("columns") or ()
                            if c.startswith(qc_columns.PREFIX)],
                "status_column": f"{qc_columns.PREFIX}status",
                "pass_column": f"{qc_columns.PREFIX}pass",
                "obsm": [k for k in OBSM_KEYS if k in (answer.get("obsm_keys") or ())],
                "backup": answer.get("backup"), "on_node": answer.get("on_node", False)})
            results.save(ds.name, document)
    return answer


# -- flat files -----------------------------------------------------------------------------


def _backup_folder(dataset):
    """Where this end keeps backups: QC's store folder, or None when there is
    none (then the write says so)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    try:
        folder = results._store(dataset.name).directory() / "backups" / stamp
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    except Exception:
        return None


def _flat_series(name, column):
    import polars as pl

    if name in _BOOLEAN:
        return column.cast(pl.Boolean).alias(name)
    if name in _INT:
        return column.cast(pl.Int64).alias(name)
    return column.cast(pl.Utf8).alias(name)


def _write_flat(dataset, kind, qc, seg, replace):
    import contextlib
    import os
    import shutil
    import tempfile

    from plexora.server.models.adapters import read_flat_table, write_flat_table

    source = dataset.table.source
    frame = read_flat_table(source.path, kind)
    added = []
    if qc is not None:
        added += [_flat_series(name, qc[0][name]) for name in qc[0].columns]
    if seg is not None:
        added += [seg[0][name] for name in seg[0].columns]
    written = [s.name for s in added]
    taken = [c for c in written if c in frame.columns]
    if taken and not replace:
        raise Exists(taken)
    if frame.height != (added[0].len() if added else frame.height):
        raise ValueError(f"this project's table has {added[0].len()} cells but the file now "
                         f"has {frame.height} rows; reopen the project and try again")
    folder = _backup_folder(dataset)
    backup = None
    if folder is not None:
        backup = str(folder / Path(source.path).name)
        shutil.copy2(source.path, backup)
    frame = frame.with_columns(added)
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
    passes = qc[0]["plexora_qc_pass"].to_list() if qc is not None else []
    return {"path": source.path, "columns": written, "n_cells": frame.height,
            "n_fail": sum(1 for v in passes if v is False), "backup": backup,
            "segmentation_qc": seg is not None, "source_kind": kind}


# -- AnnData / SpatialData tables -----------------------------------------------------------


def _held(obs, mask, column):
    """Whether this project's rows hold values in `column`."""
    if column not in obs.columns:
        return False
    values = obs[column].iloc[np.flatnonzero(mask)]
    try:
        values = values.astype("object")
        return bool(values.map(lambda v: not (v is None or v is _NA() or v != v
                                              or v == "")).any())
    except Exception:
        return bool(values.notna().any())


def _NA():
    import pandas as pd

    return pd.NA


def _obs_positions(dataset, obs, mask):
    """The obs row of each loaded table row (-1 for none). By identifier
    where the project has one (`obs_id_field`, matched against the table's
    own ids -- before any correction, `table_correction.raw_cell_id_column`),
    never by position: a project is routinely a subset of its file's obs. In
    file order otherwise, which the row-count check has confirmed."""
    import pandas as pd

    from plexora.server.models import table_correction

    positions = np.flatnonzero(np.asarray(mask))
    spec = dataset.project.dataset
    id_field = getattr(spec, "obs_id_field", None)
    frame = dataset.table.frame()
    key = dataset.schema.cell_id if dataset.schema else None
    roles = getattr(spec, "roles", None)
    raw = table_correction.raw_cell_id_column(roles, getattr(spec, "correction", None)) \
        if roles is not None else None
    if raw and frame is not None and raw in frame.columns:
        key = raw
    if not (id_field and id_field in obs.columns and frame is not None and key
            and key in frame.columns):
        return positions
    theirs = pd.Series(positions, index=obs[id_field].astype(str).to_numpy()[positions])
    theirs = theirs[~theirs.index.duplicated(keep="first")]
    mine = pd.Index([str(v) for v in frame[key].to_list()])
    found = theirs.reindex(mine)
    return found.fillna(-1).astype(np.int64).to_numpy()


def _place(obs, mask, column, values, dtype, at=None):
    """`values` (one per loaded row; None for none) onto this project's rows
    of `column` -- `at` the obs row of each (`_obs_positions`), default the
    masked rows in order -- keeping every other row's value; cast to a
    nullable dtype."""
    import pandas as pd

    series = obs[column].astype("object") if column in obs.columns else pd.Series(
        [None] * len(obs), index=obs.index, dtype="object")
    series = series.copy()
    # This project's rows start empty: a row this write has no call for must
    # not keep a previous run's value.
    series.iloc[np.flatnonzero(mask)] = None
    at = np.flatnonzero(mask) if at is None else np.asarray(at)
    clean = [None if v is None or (isinstance(v, float) and v != v) else v for v in values]
    hit = at >= 0
    series.iloc[at[hit]] = [v for v, h in zip(clean, hit) if h]
    if dtype == "boolean":
        series = series.map(lambda v: pd.NA if v is None or v is pd.NA or v != v
                            else bool(v) if isinstance(v, (bool, np.bool_))
                            else str(v) == "True").astype("boolean")
    elif dtype == "Int64":
        series = pd.to_numeric(series, errors="coerce").astype("Int64")
    elif dtype == "float32":
        # Plain float32, NaN where unscored: anndata cannot write pandas'
        # nullable Float32.
        series = pd.to_numeric(series, errors="coerce").astype("float32")
    elif dtype == "category":
        series = series.astype("string").astype("category")
    else:
        series = series.astype("string")
    obs[column] = series
    return obs


def _dtype_of(name):
    if name in _BOOLEAN:
        return "boolean"
    if name in _INT:
        return "Int64"
    if name in _CATEGORY:
        return "category"
    return "string"


def _obsm_overlay(existing, obs, mask, frame, index, at):
    """An obsm frame over every obs row: `existing`'s values kept for rows
    outside `mask`, `frame`'s (one per loaded row, placed at `at`) on it;
    columns the union."""
    import pandas as pd

    names = list(dict.fromkeys([*(existing.columns if existing is not None else []),
                                *frame.columns]))
    out = pd.DataFrame(index=index)
    hit = np.asarray(at) >= 0
    for name in names:
        column = (existing[name].astype("object").to_numpy().copy()
                  if existing is not None and name in existing.columns
                  else np.full(len(index), None, dtype=object))
        if existing is not None and len(column) != len(index):
            column = np.full(len(index), None, dtype=object)
        column[np.flatnonzero(mask)] = None
        values = np.asarray(frame[name].to_list() if name in frame.columns
                            else [None] * hit.size, dtype=object)
        column[np.asarray(at)[hit]] = values[hit]
        out[name] = pd.Series(column, index=index).map(
            lambda v: pd.NA if v is None or v is pd.NA or v != v else bool(v)).astype("boolean")
    return out


def _uns_merged(existing, body):
    """`body` with the `results` of every other image `existing` holds."""
    if not existing or str(existing.get("version")) != qc_columns.COLUMNS_VERSION:
        return body
    try:
        mine = json.loads(body.get("results") or "{}")
        theirs = json.loads(existing.get("results") or "{}")
    except (TypeError, ValueError):
        return body
    theirs.update(mine)
    return {**body, "results": json.dumps(theirs, default=str)}


def _write_anndata(dataset, payload, qc, seg, replace):
    try:
        from anndata.io import read_elem, write_elem
    except ImportError:  # pragma: no cover
        from anndata._io.specs import read_elem, write_elem

    from plexora.plugins.qc.server import spatialdata_write
    from plexora.plugins.roi.server import adapters

    path = adapters._table_path(dataset)
    source = dataset.table.source
    qc_frame, obsm = qc if qc is not None else (None, {})
    obs_written = (list(qc_frame.columns) if qc_frame is not None else []) \
        + (list(seg[0].columns) if seg is not None else [])
    with adapters._open_group(path) as handle:
        obs = read_elem(handle["obs"])
        attrs = spatialdata_write.table_attrs(handle, read_elem)
        held_obsm = {}
        for key in obsm:
            if "obsm" in handle and key in handle["obsm"]:
                try:
                    held_obsm[key] = read_elem(handle["obsm"][key])
                except Exception:
                    held_obsm[key] = None
        uns_now = {}
        for key in (UNS_KEY, SEG_UNS_KEY):
            if "uns" in handle and key in handle["uns"]:
                try:
                    uns_now[key] = read_elem(handle["uns"][key])
                except Exception:
                    uns_now[key] = {}
    mask = adapters._project_rows(dataset, obs)
    n = int(payload["n_rows"])
    if int(mask.sum()) != n:
        raise ValueError(f"this project's table has {n} cells but the file now has "
                         f"{int(mask.sum())} matching rows; reopen the project and try again")
    notes = []
    if qc_frame is not None and attrs:
        notes += spatialdata_write.check_table(attrs, obs, mask, dataset)
    # "Exists" is about this image's rows: another image's columns in a
    # shared file are not in the way.
    existing = [c for c in obs_written if _held(obs, mask, c)]
    for key, held in held_obsm.items():
        if held is not None and len(held) == len(obs) and any(
                _held(held, mask, c) for c in held.columns):
            existing.append(f"obsm/{key}")
    image = str(payload.get("image_id") or "")
    if qc_frame is not None and UNS_KEY in uns_now:
        body = uns_now[UNS_KEY] or {}
        try:
            mine = image in json.loads(body.get("results") or "{}")
        except (TypeError, ValueError, AttributeError):
            mine = False
        if mine or (str(body.get("version")) != qc_columns.COLUMNS_VERSION and existing):
            existing.append(f"uns/{UNS_KEY}")
    if seg is not None and SEG_UNS_KEY in uns_now and any(
            c.startswith("plexora_seg_qc_") for c in existing):
        existing.append(f"uns/{SEG_UNS_KEY}")
    if existing and not replace:
        raise Exists(existing)
    backup = _backup(dataset, obs)
    at = _obs_positions(dataset, obs, mask)
    if seg is not None:
        for name in seg[0].columns:
            dtype = "category" if name == "plexora_seg_qc_status" else "float32" \
                if name.endswith("_score") else "Int64" if name.endswith("_partner_id") \
                else "boolean"
            obs = _place(obs, mask, name, seg[0][name].to_list(), dtype, at)
    if qc_frame is not None:
        for name in qc_frame.columns:
            obs = _place(obs, mask, name, qc_frame[name].to_list(), _dtype_of(name), at)
    index = obs.index.astype(str)
    frames = {key: _obsm_overlay(held_obsm.get(key) if held_obsm.get(key) is not None
                                 and len(held_obsm[key]) == len(obs) else None,
                                 obs, mask, body.to_pandas() if hasattr(body, "to_pandas")
                                 else body, index, at)
              for key, body in obsm.items()}
    with adapters._open_group(path, writable=True) as handle:
        del handle["obs"]
        write_elem(handle, "obs", obs)
        if frames:
            if "obsm" not in handle:
                handle.create_group("obsm")
            for key, body in frames.items():
                if key in handle["obsm"]:
                    del handle["obsm"][key]
                write_elem(handle["obsm"], key, body)
        if "uns" not in handle:
            handle.create_group("uns")
        if qc_frame is not None:
            if UNS_KEY in handle["uns"]:
                del handle["uns"][UNS_KEY]
            write_elem(handle["uns"], UNS_KEY, _uns_merged(uns_now.get(UNS_KEY),
                                                             payload["uns"]))
        if seg is not None:
            if SEG_UNS_KEY in handle["uns"]:
                del handle["uns"][SEG_UNS_KEY]
            write_elem(handle["uns"], SEG_UNS_KEY, seg[1])
    out = {"path": str(path), "columns": obs_written, "n_cells": n, "backup": backup,
           "source_kind": dataset.source_kind, "table": getattr(source, "table", None),
           "segmentation_qc": seg is not None, "image_id": image}
    if qc_frame is not None:
        passes = qc_frame["plexora_qc_pass"].to_list()
        out.update(obsm=OBSM_KEY, obsm_markers=OBSM_MARKERS, obsm_markers_warn=OBSM_MARKERS_WARN,
                   obsm_keys=list(frames), uns=UNS_KEY,
                   n_fail=sum(1 for v in passes if v is False))
    else:
        out.update(uns=SEG_UNS_KEY, n_fail=0)
    if qc_frame is not None and dataset.source_kind == "spatialdata":
        shapes = spatialdata_write.write_regions(dataset, payload.get("regions") or [],
                                                 image_id=image,
                                                 seg_scale=payload.get("seg_scale") or 1,
                                                 replace=replace)
        out["shapes"] = shapes
        notes += shapes.pop("notes", [])
    if notes:
        out["notes"] = notes
    return out


def _backup(dataset, obs):
    folder = _backup_folder(dataset)
    if folder is None:
        return None
    target = folder / "obs.parquet"
    try:
        obs.to_parquet(target)
    except Exception:
        obs.astype(str).to_parquet(target)
    return str(target)
