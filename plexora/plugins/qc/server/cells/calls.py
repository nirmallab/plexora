"""Every cell's QC call: pass or fail, why, and which regions it sits in.

`derive` is a pure function of the table's columns, the regions' membership
(`propagate`), the modules' stored decisions and a strictness table -- so the
same result under another preset is one call away, with no packet and no
mask read (the membership fractions are kept). A cell fails when any reason
that excludes applies; warnings are recorded beside it and never fail it.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from plexora.plugins.qc.server import results, schemas
from plexora.plugins.qc.server.cells import modules as cell_modules


def _rows(ds):
    from plexora.server.utils.label_overlay import cell_ids

    frame = ds.table.geometry()
    schema = ds.schema
    ids, keep = cell_ids(frame, schema.cell_id if schema else None)
    return ids.astype(np.int64), keep


def module_measurements(ds, scan_meta, names):
    out = {}
    for name in names:
        module = cell_modules.module(name)
        ok, _why = module.available(ds, scan_meta)
        if ok:
            out[name] = module.measure(ds, scan_meta)
    return out


def derive(ds, result, table, *, pairs=None, measurements=None, scan_meta=None):
    """(cells DataFrame, pairs DataFrame, summary)."""
    ids, keep = _rows(ds)
    n = int(ids.size)
    modules = (result.get("cells") or {}).get("modules") or {}
    names = [m for m, entry in modules.items() if entry.get("available") and
             entry.get("state") in ("decided", "manual_review_recommended")]
    if measurements is None:
        measurements = module_measurements(ds, scan_meta or {"cycles": {"cycles": result.get(
            "cycles") or []}}, names)
    exclude = {r: np.zeros(n, dtype=bool) for r in schemas.REASONS}
    warn = {r: np.zeros(n, dtype=bool) for r in schemas.REASONS}
    measures = {}
    for name in names:
        meas = measurements.get(name)
        if meas is None:
            continue
        module = cell_modules.module(name)
        decision = modules[name].get("decision") or {}
        if modules[name].get("state") == "manual_review_recommended":
            decision = {**decision, "manual_review": True}
        cutoffs = module.cutoffs(meas, table, decision)
        ex, wa = module.calls(meas, cutoffs, decision, table)
        for reason, mask in ex.items():
            exclude[reason] |= np.asarray(mask, dtype=bool)[keep]
        for reason, mask in wa.items():
            warn[reason] |= np.asarray(mask, dtype=bool)[keep]
        for key, values in meas.items():
            if key.startswith("m_"):
                column = key if not name.startswith("channel_outlier:") else \
                    f"m_outlier_{name.split(':', 1)[1]}"
                measures[column] = np.asarray(values, dtype=np.float32)[keep]
        modules[name]["cutoffs"] = {k: float(v) if isinstance(v, (int, float, np.floating))
                                    else v for k, v in cutoffs.items()}
    # Regions.
    roi_ids = [[] for _ in range(n)]
    roi_method = np.full(n, None, dtype=object)
    if pairs is None:
        pairs = results.cell_rois(ds.name)
    region_meta = {}
    for candidate in (result.get("candidates") or {}).values():
        user = candidate.get("user_state") or {}
        if candidate.get("roi_id") and not user.get("deleted") and not user.get("removed_from_qc"):
            region_meta[candidate["roi_id"]] = candidate
    threshold = float(table["cells.roi_overlap_fraction"])
    if pairs is not None and pairs.height:
        index = {int(cid): i for i, cid in enumerate(ids.tolist())}
        for cid, roi_id, fraction, method in pairs.select(
                ["cell_id", "roi_id", "fraction", "method"]).iter_rows():
            candidate = region_meta.get(roi_id)
            row = index.get(int(cid))
            if candidate is None or row is None:
                continue
            if method == "mask" and float(fraction) < threshold:
                continue
            reason = f"region:{candidate.get('class') or 'other_technical'}"
            action = candidate.get("action") or "exclude"
            if action == "exclude":
                exclude[reason][row] = True
            elif action == "warn":
                warn[reason][row] = True
            else:
                continue
            roi_ids[row].append(roi_id)
            roi_method[row] = method if roi_method[row] in (None, method) else "mixed"
    failing = np.zeros(n, dtype=bool)
    warned = np.zeros(n, dtype=bool)
    for reason in schemas.REASONS:
        failing |= exclude[reason]
        warned |= warn[reason]
    reasons_list = [[] for _ in range(n)]
    primary = np.full(n, "", dtype=object)
    order = {r: i for i, r in enumerate(schemas.PRIMARY_ORDER)}
    for reason in sorted(schemas.REASONS, key=lambda r: order.get(r, 999)):
        hits = np.flatnonzero(exclude[reason] | warn[reason])
        for row in hits:
            reasons_list[row].append(reason)
        for row in np.flatnonzero(exclude[reason]):
            if not primary[row]:
                primary[row] = reason
    for row in np.flatnonzero(~failing & warned):
        primary[row] = reasons_list[row][0] if reasons_list[row] else ""
    action = np.where(failing, "exclude", np.where(warned, "warn", "pass"))
    frame = pl.DataFrame({
        "cell_id": pl.Series(ids, dtype=pl.Int64),
        "pass": pl.Series(~failing, dtype=pl.Boolean),
        "action": pl.Series(action.tolist(), dtype=pl.Utf8),
        "primary_reason": pl.Series(primary.tolist(), dtype=pl.Utf8),
        "reasons": pl.Series(reasons_list, dtype=pl.List(pl.Utf8)),
        "reason_count": pl.Series([len(r) for r in reasons_list], dtype=pl.Int16),
        "roi_ids": pl.Series(roi_ids, dtype=pl.List(pl.Utf8)),
        "roi_method": pl.Series(roi_method.tolist(), dtype=pl.Utf8),
        "result_id": pl.Series([result["result_id"]] * n, dtype=pl.Utf8),
        **{k: pl.Series(v, dtype=pl.Float32) for k, v in sorted(measures.items())},
    })
    by_reason = {r: int(exclude[r].sum()) for r in schemas.REASONS if exclude[r].any()}
    warn_by_reason = {r: int(warn[r].sum()) for r in schemas.REASONS if warn[r].any()}
    summary = {"n": n, "n_fail": int(failing.sum()), "n_warn": int((~failing & warned).sum()),
               "by_reason": by_reason, "warn_by_reason": warn_by_reason,
               "roi_overlap_fraction": threshold,
               "roi_method": sorted({m for m in roi_method.tolist() if m})}
    return frame, pairs, summary


def _regions_and_pairs(ds, result):
    from plexora.plugins.qc.server import propagate, roi_link

    regions = roi_link.live_regions(ds, result)
    area = None
    try:
        area_name = cell_modules._column(ds, cell_modules.AREA)
        if area_name:
            values = ds.table.columns([area_name])[area_name]
            area = float(np.nanmedian(values))
    except Exception:
        area = None
    diameter = 2 * np.sqrt(area / np.pi) if area else None
    pairs, per_roi = propagate.propagate(ds, regions, median_diameter_px=diameter)
    return regions, pairs, per_roi


def write_for_active(call, project, *, session_id=None, refresh_regions=True):
    """Derive and store the active result's cells (after the regions are
    propagated again, unless `refresh_regions=False`)."""
    from plexora.plugins.qc.server import strictness

    session = call.session
    ds = session.data(project)
    if not ds.table.available:
        return None
    with results.lock(project):
        document = results.load(project)
        result = results.active(document)
        if result is None:
            return None
        if session_id:
            _copy_module_decisions(result, session_id)
        preset = (document.get("strictness") or {}).get("preset") or "standard"
        custom = (document.get("strictness") or {}).get("thresholds")
        table = strictness.thresholds(preset, custom if preset == "custom" else None)
        pairs = None
        per_roi = None
        if refresh_regions:
            _regions, pairs, per_roi = _regions_and_pairs(ds, result)
        frame, pairs, summary = derive(ds, result, table, pairs=pairs)
        results.put_cells(project, frame, pairs)
        result.setdefault("cells", {}).update(summary)
        if per_roi is not None:
            result["cells"]["propagation"] = per_roi
        result["cells"]["table_name"] = ds.table.source_kind
        results.put_result(document, result)
        results.save(project, document)
    return summary


def _copy_module_decisions(result, session_id):
    from plexora.plugins.qc.server.engine import store

    try:
        record = store().load(session_id)
    except Exception:
        return
    modules = result.setdefault("cells", {}).setdefault("modules", {})
    for unit in record["units"].values():
        if unit.get("type") != "cells":
            continue
        modules[unit["module"]] = {
            "available": unit["state"] not in ("skipped_not_applicable", "skipped_no_table"),
            "state": unit["state"], "reason": unit.get("reason"),
            "decision": unit.get("decision") or {}, "summary": unit.get("summary"),
            "proposals": unit.get("proposals"), "evidence_artifacts": unit.get("artifacts"),
            "version": cell_modules.VERSION}
