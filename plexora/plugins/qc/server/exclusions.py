"""The cells QC says must be left out of estimation (`Plugin.cell_exclusions_factory`).

What automatic gating, or anything else that fits or samples a table's values,
reads to stay off QC failures (plexora/agent/cell_exclusions.py). Answers from
the active result's per-cell calls:

- **strict**: every cell called exclude or warn, for every marker; and, for one
  marker, the cells that marker was flagged in (exclude or warn);
- **exclude**: exclude calls, and the per-marker flags with status exclude.

The calls must describe the regions as they are now. A region drawn or reshaped
in the ROI panel since the calls were derived (no `refresh_qc` yet) would
otherwise be invisible to gating, so a stale result is re-derived first -- the
same sync and derivation `refresh_qc` runs, QC's own reversible derived state,
never a source write. Staleness is the ROI document's hash against the one
stamped on the result's cells when they were derived (`calls.write_for_active`),
and the inputs the calls were derived from (`cells/inputs.py`: the mask, the
table, the pixel size, the segmentation transform, Segmentation QC's and DNA
retention's results) against what they are now -- a mask replaced on disk
changes the calls with no region drawn.

A category switched off for exclusions (`set_qc_categories`) is applied when
the calls are derived: its findings are notes there, so nothing here needs to
know about it beyond reporting it (`source["disabled_categories"]`).

On a z-stack whose cells were measured in 3-D, `strict` also leaves out the
cells the stack's first or last plane cuts off (`touches_z_bounds` in the
object table): their values are means over the part of the cell the stack
holds, the way a warn call's are doubtful, and they are counted as reason
`clipped_z`. `exclude` keeps them, and so does a stack where most cells are
cut off (`CLIPPED_MAX_SHARE`): one thinner than its cells cuts every cell
alike, and leaving them out would leave nothing to estimate from.

Nothing here removes a cell from anything: the gate that comes out still
applies to every cell.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from types import SimpleNamespace

import numpy as np

from plexora.plugins.qc.server import results

_log = logging.getLogger(__name__)

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 16


def roi_revision(project) -> str:
    """A hash of the ROI document as stored ("" when there is none)."""
    from plexora import api

    try:
        blob = api.store(project, "roi").get_state()
    except Exception:
        return ""
    return hashlib.sha1(blob).hexdigest()[:16] if blob else ""


def _has_qc_regions(ds) -> bool:
    """Whether the ROI document holds any region in a QC category."""
    from plexora.plugins.qc.server import roi_link

    try:
        state = roi_link._repo(ds).load()
    except Exception:
        return False
    labels = roi_link._labels(state)
    return any(roi_link.class_in(f.get("category_id"), labels,
                                 roi_link.class_token(f.get("notes"))) is not None
               for f in roi_link._features(state))


def _call_for(ds):
    """What `write_for_active` needs of a capability call: a session that
    hands back this handle set."""
    return SimpleNamespace(session=SimpleNamespace(data=lambda _p: ds,
                                                   image_data=lambda _p: ds))


def _rederive(ds):
    from plexora.plugins.qc.server import roi_link
    from plexora.plugins.qc.server.cells import calls

    with results.lock(ds.name):
        document = results.load(ds.name)
        roi_link.sync(ds, document)
        if results.active(document) is not None:
            results.put_result(document, results.active(document))
            results.save(ds.name, document)
    calls.write_for_active(_call_for(ds), ds.name)


def _stale(ds, result, roi_rev, now=None) -> bool:
    from plexora.plugins.qc.server.cells import inputs as cell_inputs

    cells = (result or {}).get("cells") or {}
    if "n" not in cells:
        return True
    if cells.get("roi_revision") != roi_rev:
        return True
    stored = cells.get("inputs")
    if stored is None:  # derived before inputs were stamped: the regions decide
        return False
    now = now if now is not None else _inputs(ds)
    if now is None:
        return False
    return bool(cell_inputs.changed(stored, now)
                & set(cell_inputs.MAPPING + cell_inputs.DIRECT))


def _inputs(ds):
    """The calls' inputs now (stats and record fields), or None."""
    from plexora.plugins.qc.server.cells import inputs as cell_inputs

    try:
        return cell_inputs.fingerprints(ds)
    except Exception:
        return None


def _marker_flags(frame, statuses):
    """{marker: (cell ids, reasons)} of the per-marker flags with a status in
    `statuses` -- one explode over the column, not a Python loop over 200k
    cells."""
    import polars as pl

    if "marker_flags" not in frame.columns or not frame.height:
        return {}
    flat = frame.select(pl.col("cell_id").cast(pl.Int64), pl.col("marker_flags")) \
        .explode("marker_flags", empty_as_null=True).drop_nulls("marker_flags")
    if not flat.height:
        return {}
    parts = flat.with_columns(pl.col("marker_flags").str.split_exact("|", 2).alias("p")) \
        .unnest("p")
    kept = parts.filter(pl.col("field_2").is_in(list(statuses))
                        & pl.col("field_0").is_not_null() & (pl.col("field_0") != ""))
    if not kept.height:
        return {}
    markers = kept["field_0"].to_numpy()
    ids = kept["cell_id"].to_numpy().astype(np.int64)
    reasons = kept["field_1"].fill_null("").to_numpy()
    out = {}
    for marker in sorted(set(markers.tolist())):
        here = markers == marker
        out[str(marker)] = (ids[here], reasons[here])
    return out


def _reason_counts(frame, mask):
    """{reason: n} over the cells `mask` selects (a reason a cell was only
    noted for leaves no cell out, `summary`)."""
    out = {}
    if not mask.any():
        return out
    left = frame.filter(pl_mask(mask))
    noted = left["noted_by"].to_list() if "noted_by" in left.columns else [()] * left.height
    for reasons, notes in zip(left["reasons"].to_list(), noted):
        for reason in reasons or ():
            if reason not in (notes or ()):
                out[reason] = out.get(reason, 0) + 1
    return out


def pl_mask(mask):
    import polars as pl

    return pl.Series(np.asarray(mask, dtype=bool))


def _top(counts, n=2):
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def _record(ds, mode, document, result):
    from plexora.agent import cell_exclusions
    from plexora.plugins.qc.server import digest

    frame = results.cells(ds.name)
    if frame is None or not frame.height:
        return None
    if "result_id" in frame.columns and frame.height and \
            frame["result_id"][0] != result.get("result_id"):
        return None
    action = frame["action"].to_numpy()
    ids = frame["cell_id"].to_numpy().astype(np.int64)
    excluded = action == "exclude"
    warned = action == "warn"
    whole_mask = excluded | warned if mode == "strict" else excluded
    statuses = ("exclude", "warn") if mode == "strict" else ("exclude",)
    flagged = _marker_flags(frame, statuses)
    by_marker = {marker: found[0] for marker, found in flagged.items()}
    regions = []
    for candidate in (result.get("candidates") or {}).values():
        user = candidate.get("user_state") or {}
        if candidate.get("roi_id") and not user.get("deleted") \
                and not user.get("removed_from_qc") \
                and (candidate.get("action") or "exclude") in ("exclude", "warn"):
            regions.append(candidate["roi_id"])
    # What QC says of each marker, for whoever gates it: the channel's verdict
    # and a fixed-order line (`digest`) -- the same stored result, the same
    # bytes.
    whole_reasons = _reason_counts(frame, whole_mask)
    n_whole = int(whole_mask.sum())
    notes, verdict = {}, {}
    for marker in list(getattr(ds.table, "markers", ()) or ()):
        extra_ids, extra_reasons = flagged.get(marker, (np.empty(0, np.int64), np.empty(0)))
        counts = dict(whole_reasons)
        extra = 0
        if extra_ids.size:
            outside = ~np.isin(extra_ids, ids[whole_mask])
            extra = int(np.unique(extra_ids[outside]).size)
            for reason in extra_reasons[outside].tolist():
                counts[reason] = counts.get(reason, 0) + 1
        left_out = {"n": n_whole + extra, "top": _top(counts)} if n_whole + extra else None
        line = digest.marker_digest(result, marker, left_out=left_out)
        if line:
            notes[marker] = line
        verdict[marker] = digest.channel_verdict(result, marker)
    source = {"result_id": result.get("result_id"),
              "origin": result.get("origin") or ("session" if result.get("session_id")
                                                 else "manual"),
              "n_exclude": int(excluded.sum()), "n_warn": int(warned.sum()),
              "n_marker": int(sum(np.unique(v).size for v in by_marker.values())),
              "regions": len(regions),
              "disabled_categories": results.disabled_categories(document)}
    from plexora.plugins.qc.server.cells import inputs as cell_inputs

    # The document's revision carries the strictness and the category switch;
    # the inputs digest a mask or table replaced under unchanged regions.
    stamped = cell_inputs.digest((result.get("cells") or {}).get("inputs") or {})
    fp = hashlib.sha1(f"{result.get('result_id')}|{results.revision(ds.name, document)}"
                      f"|{stamped}".encode()).hexdigest()[:16]
    return cell_exclusions.make_record(ids[whole_mask], by_marker, fingerprint=fp,
                                       mode=mode, source=source, notes=notes,
                                       verdict=verdict)


def _file_token(project):
    """(mtime, size) of the project's store file, which every plugin store of
    the project -- QC's and the ROI document alike -- writes to: unchanged
    means nothing QC reads has changed. None when it cannot be read."""
    from plexora.server.models import database_model

    try:
        st = database_model._db_path_for_datasource(project).stat()
    except (OSError, ValueError, AttributeError):
        return None
    return (st.st_mtime_ns, st.st_size)


def _inputs_token(ds):
    """`_file_token` and the digest of the calls' inputs that live outside
    the store (the mask, the table, the pixel size...): either changing
    means the stored record may be out of date."""
    from plexora.plugins.qc.server.cells import inputs as cell_inputs

    token = _file_token(ds.name)
    if token is None:
        return None
    now = _inputs(ds)
    return (*token, cell_inputs.digest(now) if now is not None else "")


_FAST: dict = {}
_ABSENT = object()
_BUSY = threading.local()


def provider(ds, mode):
    """`provider(ds, mode)` for `cell_exclusions`: None when QC was never run
    and no measured 3-D cell is cut off by the stack's ends.

    Asked by every estimate gating makes, so the common answer -- nothing was
    written to the project since the last ask -- is a stat and a lookup."""
    record = _qc_provider(ds, mode)
    if mode != "strict" or getattr(_BUSY, "on", False) \
            or (record is not None and record.source.get("error")):
        return record
    clipped = clipped_z(ds)
    if clipped is None or not clipped[0].size:
        return record
    return _with_clipped(record, *clipped, mode)


def _qc_provider(ds, mode):
    if not getattr(ds.table, "available", False) or getattr(_BUSY, "on", False):
        # Asked from inside QC's own derivation (a render it draws, say): the
        # calls are being made, so there is nothing to leave out yet.
        return None
    token = _inputs_token(ds)
    if token is not None:
        with _CACHE_LOCK:
            found = _FAST.get((ds.name, mode, token), _ABSENT)
        if found is not _ABSENT:
            return found
    _BUSY.on = True
    try:
        record = _provide(ds, mode)
    finally:
        _BUSY.on = False
    after = _inputs_token(ds)
    if after is not None:
        with _CACHE_LOCK:
            if len(_FAST) > _CACHE_MAX:
                _FAST.clear()
            _FAST[(ds.name, mode, after)] = record
    return record


def _provide(ds, mode):
    from plexora.plugins.qc.server.cells import inputs as cell_inputs

    roi_rev = roi_revision(ds.name)
    document = results.load(ds.name)
    result = results.active(document)
    if result is None and not _has_qc_regions(ds):
        return None
    now = _inputs(ds)
    stamp = cell_inputs.digest(now) if now is not None else ""
    key = (ds.name, mode, roi_rev, results.revision(ds.name, document), stamp)
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    try:
        if result is None or _stale(ds, result, roi_rev, now):
            _rederive(ds)
            document = results.load(ds.name)
            result = results.active(document)
            if result is None:
                return None
        record = _record(ds, mode, document, result)
    except Exception as exc:
        # Remembered, so a derivation that fails is not retried by every
        # estimate of the same pass; reported in each result's qc_exclusion.
        from plexora.agent import cell_exclusions

        record = cell_exclusions.make_record([], fingerprint="error", mode=mode,
                                             source={"error": str(exc)})
        with _CACHE_LOCK:
            _CACHE[key] = record
        return record
    # Keyed by what was read before any re-derive as well as after, so the
    # next call with the same inputs is a dictionary lookup.
    after = (ds.name, mode, roi_revision(ds.name), results.revision(ds.name, document), stamp)
    with _CACHE_LOCK:
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = _CACHE[after] = record
    return record


# -- cells cut off by a z-stack's ends -----------------------------------------

_CLIPPED: dict = {}

#: Above this share of the measured cells cut off by the stack's ends, none is
#: left out: the stack is thinner than its cells, so all are cut alike.
CLIPPED_MAX_SHARE = 0.5


def clipped_z(ds):
    """`(ids, fingerprint)` of the cells a z-stack's first or last plane cuts
    off, from the project's measured 3-D cells -- no id when more than
    `CLIPPED_MAX_SHARE` of them are; None when there are none to read -- a
    flat image, a 2-D mask, cells never measured, or a table measured from a
    mask the project no longer uses.

    By id: in a time series whose frames reuse ids, a cell cut off in one
    frame is left out in every frame."""
    import json

    from plexora import paths
    from plexora.server.volume import objects

    folder = paths.project_state_dir(ds.name)
    table, manifest = folder / objects.TABLE_NAME, folder / objects.MANIFEST_NAME
    try:
        st = table.stat()
    except OSError:
        return None
    seg = getattr(ds.project, "segmentation", None)
    derived = str(getattr(seg, "derived", "") or "")
    key = (ds.name, st.st_mtime_ns, st.st_size, derived)
    with _CACHE_LOCK:
        if key in _CLIPPED:
            return _CLIPPED[key]
    found = None
    try:
        measured = json.loads(manifest.read_text(encoding="utf-8")).get("labels")
        if derived and measured == derived:
            import polars as pl

            frame = pl.read_parquet(table, columns=["label", "touches_z_bounds"])
            ids = np.unique(frame.filter(pl.col("touches_z_bounds"))["label"]
                            .to_numpy().astype(np.int64))
            cells = np.unique(frame["label"].to_numpy()).size
            if ids.size > CLIPPED_MAX_SHARE * cells:
                ids = np.empty(0, np.int64)
            found = (ids, f"{st.st_mtime_ns}:{st.st_size}")
    except (OSError, ValueError, KeyError) as exc:
        # An object table that cannot be read leaves nothing out; the QC
        # half of the record is unaffected.
        _log.warning("QC could not read %s for clipped cells: %s", table, exc)
    with _CACHE_LOCK:
        if len(_CLIPPED) > _CACHE_MAX:
            _CLIPPED.clear()
        _CLIPPED[key] = found
    return found


def _with_clipped(record, ids, stamp, mode):
    from plexora.agent import cell_exclusions

    if record is None:
        whole, by_marker, base, source = ids, {}, "", {"origin": "object_table"}
    else:
        whole, by_marker = np.union1d(record.whole, ids), record.by_marker
        base, source = record.fingerprint, dict(record.source)
    source["n_clipped_z"] = int(ids.size)
    fp = hashlib.sha1(f"{base}|clipped:{stamp}".encode()).hexdigest()[:16]
    return cell_exclusions.make_record(whole, by_marker, fingerprint=fp, mode=mode,
                                       source=source)


def summary(ds, mode="strict") -> dict:
    """What `get_qc_exclusions` reports: counts by reason and marker, never ids."""
    from plexora.agent import cell_exclusions

    with cell_exclusions.mode(mode):
        record = provider(ds, mode) if mode != "off" else None
        if record is None:
            return {"mode": mode, "applied": False,
                    "reason": "qc='off'" if mode == "off" else "no QC result for this image"}
        block = cell_exclusions.describe(ds, record)
    frame = results.cells(ds.name)
    by_reason: dict = {}
    if frame is not None and frame.height:
        left = frame.filter(frame["action"].is_in(
            ["exclude", "warn"] if mode == "strict" else ["exclude"]))
        noted = left["noted_by"].to_list() if "noted_by" in left.columns \
            else [()] * left.height
        for reasons, notes in zip(left["reasons"].to_list(), noted):
            for reason in reasons or ():
                if reason not in (notes or ()):  # a note leaves no cell out
                    by_reason[reason] = by_reason.get(reason, 0) + 1
    if record.source.get("n_clipped_z"):
        by_reason["clipped_z"] = record.source["n_clipped_z"]
    block["by_reason"] = dict(sorted(by_reason.items(), key=lambda kv: -kv[1]))
    block["by_marker"] = {m: int(v.size) for m, v in sorted(record.by_marker.items())}
    return block


# -- what one cell's QC says (`explain_cell`) --------------------------------------


def explain(ds, cell_id) -> dict | None:
    """One cell's QC, compactly: {status, why, reasons [{reason, category,
    status, channels, value, cutoff, regions}], markers [{marker, reason,
    status}], regions, roi_method}; None when QC says nothing of it."""
    from plexora.plugins.qc.server import viewer_data

    record = viewer_data.cell_by_id(ds, ds.name, int(cell_id))
    if record is None:
        return None
    if not record.get("calls", True):
        return {"status": None, "why": record.get("note"), "reasons": [], "markers": []}
    reasons = [{"reason": r["reason"], "words": r.get("words"), "category": r.get("category"),
                "status": r.get("status"), "channels": r.get("channels") or [],
                "value": r.get("value"), "cutoff": r.get("cutoff"),
                "category_enabled": r.get("category_enabled", True),
                "regions": [v.get("roi_id") for v in r.get("via_regions") or []]}
               for r in record.get("reasons") or []]
    markers = [{"marker": m["marker"], "reason": m["reason"], "status": m.get("status"),
                "category": m.get("category")} for m in record.get("markers") or []]
    action = record.get("action") or "pass"
    status = {"exclude": "excluded", "warn": "flagged"}.get(action) or (
        "marker_only" if any(m["status"] in ("unreliable", "flagged") for m in markers)
        else "noted" if reasons or markers else "pass")
    first = next((r for r in reasons if r["status"] == "fail"), None) \
        or next((r for r in reasons if r["status"] == "warn"), None)
    if first is not None:
        why = (f"{'excluded' if first['status'] == 'fail' else 'flagged'}: "
               f"{first.get('words') or first['reason']}"
               + (f" (in {', '.join(first['regions'][:3])})" if first["regions"] else ""))
    elif status == "marker_only":
        names = sorted({m["marker"] for m in markers
                        if m["status"] in ("unreliable", "flagged")})
        why = f"kept; not to be read in {', '.join(names[:6])}"
    elif reasons:
        why = "kept; noted: " + ", ".join(r.get("words") or r["reason"] for r in reasons[:3])
    else:
        why = "nothing flagged"
    return {"status": status, "why": why, "primary_reason": record.get("primary_reason"),
            "reasons": reasons, "markers": markers,
            "regions": [r.get("roi_id") for r in record.get("regions") or []],
            "roi_method": record.get("roi_method"),
            "unreliable_markers": record.get("unreliable_markers") or []}


class _Provider:
    """`provider(ds, mode)` for `cell_exclusions`, and `explain(ds, cell_id)`
    for `explain_cell` -- one object, so core asks one thing of a plugin."""

    def __call__(self, ds, mode):
        return provider(ds, mode)

    @staticmethod
    def explain(ds, cell_id):
        return explain(ds, cell_id)


def factory():
    return _Provider()


# -- per-cell annotations (`Plugin.cell_annotations_factory`) ------------------------

#: The statuses `qc_status` takes, strongest first, and their colours.
ANNOTATION_STATUSES = ("excluded", "flagged", "marker_only", "noted", "pass")
STATUS_COLORS = {"excluded": "#ef4444", "flagged": "#f59e0b", "marker_only": "#a78bfa",
                 "noted": "#94a3b8", "pass": "#d1d5db"}
_ANNOTATIONS: dict = {}


def annotations(ds) -> dict:
    """{qc_status, qc_category} over the loaded table's rows, from the active
    result's calls (None for a row QC has no call for); {} without calls."""
    import polars as pl

    from plexora.plugins.qc.server import schemas
    from plexora.plugins.qc.server.cells import columns

    token = _inputs_token(ds)
    key = (ds.name, token)
    with _CACHE_LOCK:
        if token is not None and key in _ANNOTATIONS:
            return _ANNOTATIONS[key]
    # Calls made stale by a region drawn since are derived again first, as
    # for gating: what is coloured is what is left out.
    try:
        _qc_provider(ds, "strict")
    except Exception:  # the stored calls stand
        pass
    token = _inputs_token(ds)
    key = (ds.name, token)
    document = results.load(ds.name)
    result = results.active(document)
    frame = results.cells(ds.name) if result is not None else None
    out = {}
    if frame is not None and frame.height and (
            "result_id" not in frame.columns
            or frame["result_id"][0] == result.get("result_id")):
        geometry = ds.table.geometry()
        n = geometry.height if geometry is not None else 0
        flags = frame["marker_flags"] if "marker_flags" in frame.columns else \
            pl.Series([[]] * frame.height, dtype=pl.List(pl.Utf8))
        kept = flags.list.eval(pl.element().str.ends_with("|exclude")
                               | pl.element().str.ends_with("|warn")).list.any()
        noted = (frame["noted_by"].list.len() > 0) if "noted_by" in frame.columns else \
            pl.Series([False] * frame.height)
        noted = noted | flags.list.eval(pl.element().str.ends_with("|note")).list.any()
        status = (pl.DataFrame({"a": frame["action"], "k": kept.fill_null(False),
                                "n": noted.fill_null(False)})
                  .select(pl.when(pl.col("a") == "exclude").then(pl.lit("excluded"))
                          .when(pl.col("a") == "warn").then(pl.lit("flagged"))
                          .when(pl.col("k")).then(pl.lit("marker_only"))
                          .when(pl.col("n")).then(pl.lit("noted"))
                          .otherwise(pl.lit("pass")))
                  .to_series().to_list())
        category = columns.build(frame)["category"].to_list()
        if "row" in frame.columns:
            rows = frame["row"].to_numpy()
        else:  # derived before rows were stored: the first row of each id
            from plexora.server.utils.label_overlay import cell_ids

            ids, keep = cell_ids(geometry, ds.schema.cell_id if ds.schema else None)
            where = np.flatnonzero(keep)
            first = {}
            for position, cid in zip(where.tolist(), ids.tolist()):
                first.setdefault(int(cid), position)
            rows = np.asarray([first.get(int(c), -1) for c in frame["cell_id"].to_list()])
        status_values = [None] * n
        category_values = [None] * n
        for row, word, cat in zip(rows.tolist(), status, category):
            if 0 <= row < n:
                status_values[row] = word
                category_values[row] = cat or None
        stamp = f"{result.get('result_id')}|{results.revision(ds.name, document)}"
        present = sorted({c for c in category_values if c},
                         key=lambda c: list(schemas.category_order()).index(c)
                         if c in schemas.category_order() else 99)
        out = {
            "qc_status": {"label": "QC status", "values": status_values,
                          "categories": list(ANNOTATION_STATUSES), "colors": dict(STATUS_COLORS),
                          "fingerprint": stamp,
                          "description": "excluded / flagged (kept, warned) / marker_only "
                                         "(a marker unreliable, the cell kept) / noted / pass"},
            "qc_category": {"label": "QC category", "values": category_values,
                            "categories": present,
                            "colors": {c: schemas.category_color(c) for c in present},
                            "fingerprint": stamp,
                            "description": "the category of the reason that flags the cell"},
        }
    with _CACHE_LOCK:
        if len(_ANNOTATIONS) > _CACHE_MAX:
            _ANNOTATIONS.clear()
        if token is not None:
            _ANNOTATIONS[key] = out
    return out


def annotations_factory():
    return annotations
