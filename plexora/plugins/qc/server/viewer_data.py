"""What the QC panel draws on the tissue: region outlines and flagged cells.

Read-only. Two answers, both about the ACTIVE result (or a named one, for
the regions):

- `regions`: every QC region that counts now, with the outline the ROI
  document holds (the user's, when they reshaped it) and its bounding box, so
  the panel can draw it and fit the viewer to it without a second request.
- `cells`: the flagged cells in two kinds of group. Whole-cell groups by
  (reason, status) -- "fail" when that reason excluded the cell (the cell's
  own `excluded_by`, exact), "warn" when it only flags it. Marker groups by
  (marker, reason, status) -- "unreliable" or "flagged": that one channel's
  value, the cell kept. Each with its ids (the mask labels the viewer's cell
  layers key on) and the bounding box of their centroids.

Every region and every cell group also names `evidence_channels`: the
channels its call was made on, so that a click on it can show the signal
behind it. For a region that is the detector's channels (a fold found on
the nuclear stain names it even though its scope is every channel). For a
cell group it is what `cells.calls` recorded as the call's evidence: the
module's inputs (the nuclear stain; first and last nuclear cycles), the
tissue-level regions' channels, or the one marker a marker group is about.
`regions` also carries `display`, the calibrated windows and colours those
channels are shown in, which is how the agent's own evidence drew them.

Colours are the user's where they chose one: a region class is drawn in its
`qc_<class>` ROI category's colour (so the ROI panel and this one never
disagree, and the cells inside it follow), a cell reason in the colour kept
in QC's store; everything else in the defaults below.

A result derived before `excluded_by` existed is read the old way (a reason
that only excludes is "fail", one that only warns "warn", else the cell's
action decides) until its cells are derived again.
"""

from __future__ import annotations

from plexora.plugins.qc.server import results, schemas

#: A ceiling on ids in one answer. A LUT is four bytes a cell and JSON is ~8,
#: so this is ~16 MB at worst -- and a flagged set that large is a result
#: that says "this image failed", which the counts already say.
MAX_TOTAL_IDS = 2_000_000

#: Cell reasons in words, for a row label. Region reasons read from the class.
REASON_WORDS = {
    "counterstain_low": "Low counterstain",
    "counterstain_high": "High counterstain",
    "area_small": "Too small",
    "area_large": "Too large",
    "morphology": "Implausible shape",
    "cycle_loss": "Lost across cycles",
    "cycle_gain": "Gained across cycles",
    "extreme_value": "Artifact-bright value",
}

#: Hues for the cell reasons. Region reasons take their class's colour, so a
#: cell inside a fold is drawn in the fold's red; these sit apart from the
#: class palette's alert reds and ambers so a cell reason is not read as a
#: region.
REASON_COLORS = {
    "counterstain_low": "#60a5fa",
    "counterstain_high": "#818cf8",
    "area_small": "#2dd4bf",
    "area_large": "#34d399",
    "morphology": "#a3e635",
    "cycle_loss": "#f472b6",
    "cycle_gain": "#c084fc",
    "extreme_value": "#fb7185",
}


def reason_words(reason):
    if reason.startswith("region:"):
        klass = reason.split(":", 1)[1]
        return f"In {schemas.CLASS_WORDS.get(klass, klass.replace('_', ' '))}"
    return REASON_WORDS.get(reason, reason.replace("_", " ").capitalize())


def reason_color(reason, class_colors=None, reason_colors=None):
    if reason.startswith("region:"):
        klass = reason.split(":", 1)[1]
        return (class_colors or {}).get(klass) or schemas.CLASS_COLORS.get(klass, "#9ca3af")
    return (reason_colors or {}).get(reason) or REASON_COLORS.get(reason, "#9ca3af")


def class_color(klass, class_colors=None):
    return (class_colors or {}).get(klass) or schemas.CLASS_COLORS.get(klass, "#9ca3af")


def _bbox(geometry):
    """[minX, minY, maxX, maxY] of a GeoJSON Polygon/MultiPolygon, or None."""
    if not geometry:
        return None
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    polygons = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    xs, ys = [], []
    for polygon in polygons:
        for ring in polygon or []:
            for point in ring or []:
                if len(point) >= 2:
                    xs.append(float(point[0]))
                    ys.append(float(point[1]))
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


#: How many channels one click puts up besides the nuclear stain: the
#: evidence's marker colour and its two reference colours.
MAX_EVIDENCE_CHANNELS = 3


def display(project) -> dict:
    """{nuclear, windows: {channel: [lo, hi]}, colors: {nuclear, marker,
    references}}: how the panel shows a finding's channels. Windows are the
    calibration's (raw units); a channel it has none for is auto-levelled."""
    from plexora.agent.evidence import calibration

    record = None
    try:
        record = calibration.load(project)
    except Exception:  # a project that was never calibrated reads as empty
        record = None
    channels = (record or {}).get("channels") or {}
    return {"nuclear": (record or {}).get("nuclear"),
            "windows": {name: list(entry["window"]) for name, entry in channels.items()
                        if isinstance(entry, dict) and entry.get("window")},
            "colors": {"nuclear": calibration.NUCLEAR_MUTED_BLUE,
                       "marker": calibration.MARKER_COLOR,
                       "references": list(calibration.REFERENCE_COLORS)}}


def _capped(names):
    return list(dict.fromkeys(n for n in names if n))[:MAX_EVIDENCE_CHANNELS]


def _nuclear_pair(ds, result):
    """(first, last) nuclear columns, as the cell modules read them."""
    from plexora.plugins.qc.server.cells import modules

    try:
        return modules._nuclear_columns(ds, {"cycles": result.get("cycles") or []})
    except Exception:  # no table: nothing to name
        return None, None


def _cell_evidence(ds, result, regions_by_class):
    """{reason: [channel]} for a result derived before calls recorded their
    evidence (`summary["evidence"]`): the modules' inputs, looked up."""
    first, last = _nuclear_pair(ds, result)
    modules = (result.get("cells") or {}).get("modules") or {}
    outliers = [name.split(":", 1)[1] for name, entry in modules.items()
                if name.startswith("channel_outlier:") and entry.get("available")
                and entry.get("state") in ("decided", "manual_review_recommended")]
    out = {reason: _capped([first]) for reason in (
        "counterstain_low", "counterstain_high", "area_small", "area_large", "morphology")}
    out["cycle_loss"] = out["cycle_gain"] = _capped([first, last])
    out["channel_outlier_bright"] = _capped(outliers)
    for klass, channels in regions_by_class.items():
        out[f"region:{klass}"] = _capped(channels)
    return out


def _result(project, result_id=None):
    document = results.load(project)
    if result_id:
        return results.get_result(project, document, result_id)
    return results.active(document)


def regions(ds, project, result_id=None) -> dict:
    """{result_id, display, regions: [{roi_id, candidate_id, class, words,
    category, category_words, custom, color, default_color, action, channels,
    evidence_channels, view_channels, approved, locked, created_by,
    severity, tissue_fraction, geometry, bbox}]}."""
    from plexora.plugins.qc.server import roi_link

    result = _result(project, result_id)
    if result is None:
        return {"result_id": None, "display": display(project), "regions": []}
    candidates = result.get("candidates") or {}
    colors = roi_link.category_colors(ds)
    out = []
    for live in roi_link.live_regions(ds, result):
        candidate = candidates.get(live.get("candidate_id")) or {}
        user = candidate.get("user_state") or {}
        klass = live["class"]
        # What the region is grouped under: its class, or the custom QC
        # category the user named (a technical artifact to everything else).
        key = schemas.category_key(live.get("category_id")) or klass
        custom = schemas.is_custom(key)
        view = [dict(v) for v in candidate.get("view_channels") or [] if v.get("name")]
        out.append({
            "roi_id": live["roi_id"], "candidate_id": live.get("candidate_id"),
            "name": live.get("name"),
            "class": klass, "words": schemas.CLASS_WORDS.get(klass, klass),
            "category": key,
            "category_words": roi_link.custom_words(live.get("category_label")) if custom
            else schemas.CLASS_WORDS.get(key, key),
            "custom": custom,
            "color": colors.get(key) or (schemas.custom_color(key) if custom
                                         else class_color(klass, colors)),
            "default_color": schemas.custom_color(key) if custom
            else schemas.CLASS_COLORS.get(klass, "#9ca3af"),
            "action": live.get("action") or "exclude",
            "channels": list(live.get("channels") or []),
            # A region drawn by hand names the channels that were on screen
            # when it was drawn; `view_channels` puts them back as they were.
            "evidence_channels": [v["name"] for v in view] if view else _capped(
                [*(candidate.get("channels") or []), *(live.get("channels") or [])]),
            "view_channels": view,
            "approved": bool(user.get("approved")), "locked": bool(user.get("locked")),
            "created_by": candidate.get("created_by") or user.get("created_by") or "agent",
            "severity": (candidate.get("ai_decision") or {}).get("severity")
            or candidate.get("severity"),
            "tissue_fraction": (candidate.get("measurement") or {}).get("tissue_fraction"),
            "refined_fraction": (candidate.get("measurement") or {}).get("refined_fraction"),
            "user_edited": bool(user.get("edited")),
            "refinement": {k: (candidate.get("refinement") or {}).get(k) for k in (
                "status", "method", "kept_fraction", "reason")}
            if candidate.get("refinement") else None,
            "geometry": live["geometry"], "bbox": _bbox(live["geometry"])})
    return {"result_id": result.get("result_id"), "display": display(project),
            "regions": out}


def _positions(ds):
    """A (cell_id, x, y) frame keyed like `qc_cells`, or None without
    coordinates."""
    import numpy as np
    import polars as pl

    from plexora.server.utils.label_overlay import cell_ids

    schema = ds.schema
    if schema is None or not getattr(ds.table, "available", False):
        return None
    frame = ds.table.geometry()
    if frame is None or not schema.x or not schema.y \
            or schema.x not in frame.columns or schema.y not in frame.columns:
        return None
    ids, keep = cell_ids(frame, schema.cell_id)
    return pl.DataFrame({
        "cell_id": ids.astype(np.int64),
        "x": frame[schema.x].cast(pl.Float64, strict=False).to_numpy()[keep],
        "y": frame[schema.y].cast(pl.Float64, strict=False).to_numpy()[keep]})


def _statuses(frame, result):
    """(cell_id, reason, status) for every whole-cell reason of every flagged
    cell: "fail" when that reason excluded it."""
    import polars as pl

    flagged = frame.filter(pl.col("reason_count") > 0)
    if not flagged.height:
        return None
    reasons = (flagged.select(["cell_id", "reasons"]).explode("reasons", empty_as_null=True)
               .drop_nulls("reasons").rename({"reasons": "reason"}))
    if "excluded_by" in frame.columns:
        excluded = (flagged.select(["cell_id", "excluded_by"])
                    .explode("excluded_by", empty_as_null=True).drop_nulls("excluded_by")
                    .rename({"excluded_by": "reason"}).with_columns(pl.lit(True).alias("x")))
        joined = reasons.join(excluded, on=["cell_id", "reason"], how="left")
        return joined.select(["cell_id", "reason", pl.when(pl.col("x").fill_null(False))
                              .then(pl.lit("fail")).otherwise(pl.lit("warn")).alias("status")])
    return _statuses_legacy(flagged, reasons, result)


def _statuses_legacy(flagged, reasons, result):
    import polars as pl

    summary = result.get("cells") or {}
    excludes = set((summary.get("by_reason") or {}).keys())
    warns = set((summary.get("warn_by_reason") or {}).keys())
    only_fail = [r for r in excludes if r not in warns]
    only_warn = [r for r in warns if r not in excludes]
    joined = reasons.join(flagged.select(["cell_id", "action"]), on="cell_id", how="left")
    return joined.select([
        "cell_id", "reason",
        pl.when(pl.col("reason").is_in(only_fail)).then(pl.lit("fail"))
        .when(pl.col("reason").is_in(only_warn)).then(pl.lit("warn"))
        .when(pl.col("action") == "exclude").then(pl.lit("fail"))
        .otherwise(pl.lit("warn")).alias("status")])


def _marker_statuses(frame):
    """(cell_id, marker, reason, status) of every marker flag."""
    import polars as pl

    if "marker_flags" not in frame.columns:
        return None
    rows = (frame.select(["cell_id", "marker_flags"])
            .filter(pl.col("marker_flags").list.len() > 0)
            .explode("marker_flags", empty_as_null=True).drop_nulls("marker_flags"))
    if not rows.height:
        return None
    parts = pl.col("marker_flags").str.split("|")
    return rows.select(["cell_id",
                        parts.list.get(0).alias("marker"),
                        parts.list.get(1).alias("reason"),
                        parts.list.get(2).alias("status")])


#: How a marker flag's status reads in the panel.
MARKER_STATUS = {"exclude": "unreliable", "warn": "flagged"}


def cells(ds, project, result_id=None, *, max_ids=MAX_TOTAL_IDS) -> dict:
    """{available, result_id, n, n_fail, n_warn, n_marker_flagged,
    has_positions, groups: [{key, level, reason, status, marker, label,
    definition, color, default_color, count, ids, bbox, evidence_channels,
    derived_from, truncated}], truncated, note}. `derived_from` names the
    regions drawn by hand behind a region reason ([{roi_id, name}]). Whole-cell groups come first (every fail
    before every warn, then the order a cell's primary reason is chosen in),
    then the marker groups (unreliable before flagged, by marker)."""
    import polars as pl

    result = _result(project, result_id)
    empty = {"available": False, "result_id": (result or {}).get("result_id"), "n": 0,
             "n_fail": 0, "n_warn": 0, "n_marker_flagged": 0, "has_positions": False,
             "groups": [], "truncated": False}
    if result is None:
        return {**empty, "note": "no QC result yet"}
    frame = results.cells(project)
    if frame is None or not frame.height:
        return {**empty, "note": "no cell calls: the project has no cell table, or QC "
                                 "has not flagged its cells yet"}
    stored = frame["result_id"][0] if "result_id" in frame.columns else None
    if stored and stored != result.get("result_id"):
        return {**empty, "note": "cell calls are kept for the active result only"}
    summary = result.get("cells") or {}
    base = {"available": True, "result_id": result.get("result_id"),
            "n": int(summary.get("n") or frame.height),
            "n_fail": int(summary.get("n_fail") or 0),
            "n_warn": int(summary.get("n_warn") or 0),
            "n_marker_flagged": int(summary.get("n_marker_flagged") or 0)}
    statuses = _statuses(frame, result)
    marker_statuses = _marker_statuses(frame)
    if (statuses is None or not statuses.height) and marker_statuses is None:
        return {**base, "has_positions": False, "groups": [], "truncated": False}
    positions = None
    try:
        positions = _positions(ds)
    except Exception:  # a table on a node that is asleep: ids without boxes
        positions = None
    aggregations = [pl.len().alias("count"), pl.col("cell_id").alias("ids")]
    if positions is not None:
        aggregations += [pl.col("x").min().alias("x0"), pl.col("x").max().alias("x1"),
                         pl.col("y").min().alias("y0"), pl.col("y").max().alias("y1")]

    def grouped(frame_, keys):
        if frame_ is None or not frame_.height:
            return []
        if positions is not None:
            frame_ = frame_.join(positions, on="cell_id", how="left")
        return frame_.group_by(keys).agg(aggregations).to_dicts()

    order = {r: i for i, r in enumerate(schemas.PRIMARY_ORDER)}
    cell_rows = sorted(grouped(statuses, ["reason", "status"]), key=lambda g: (
        0 if g["status"] == "fail" else 1, order.get(g["reason"], 999), g["reason"]))
    marker_rows = sorted(grouped(marker_statuses, ["marker", "reason", "status"]),
                         key=lambda g: (0 if g["status"] == "exclude" else 1,
                                        str(g["marker"]), g["reason"]))
    recorded = summary.get("evidence")
    legacy = None
    if recorded is None:
        by_class = {}
        for candidate in (result.get("candidates") or {}).values():
            if candidate.get("roi_id"):
                by_class.setdefault(candidate.get("class") or "other_technical", []).extend(
                    candidate.get("channels") or [])
        legacy = _cell_evidence(ds, result, by_class)
    from plexora.plugins.qc.server import roi_link

    class_colors = roi_link.category_colors(ds)
    reason_colors = results.reason_colors(project)
    # The regions drawn by hand behind each region reason, by name: a cell
    # inside one is there because of that outline, not because QC found
    # anything in the cell itself, and the panel says so ("Derived from").
    drawn = {}
    for live in roi_link.live_regions(ds, result):
        if live.get("created_by") == "user":
            drawn.setdefault(f"region:{live['class']}", []).append(
                {"roi_id": live["roi_id"], "name": live.get("name") or ""})
    budget = int(max_ids)
    groups = []
    truncated = False

    def ids_and_box(row):
        nonlocal budget, truncated
        ids = [int(i) for i in row["ids"]]
        cut = len(ids) > budget
        if cut:
            ids = ids[:max(budget, 0)]
            truncated = True
        budget -= len(ids)
        box = None
        if positions is not None and row.get("x0") is not None:
            box = [row["x0"], row["y0"], row["x1"], row["y1"]]
        return ids, box, cut

    for row in cell_rows:
        ids, box, cut = ids_and_box(row)
        reason = row["reason"]
        channels = _capped(((recorded or {}).get(reason) or {}).get("channels") or []) \
            if recorded is not None else legacy.get(reason, [])
        groups.append({"key": f"{reason}|{row['status']}", "level": "cell", "reason": reason,
                       "status": row["status"], "marker": None, "label": reason_words(reason),
                       "definition": schemas.REASON_DEFINITIONS.get(reason, ""),
                       "color": reason_color(reason, class_colors, reason_colors),
                       "default_color": reason_color(reason), "count": int(row["count"]),
                       "ids": ids, "bbox": box, "evidence_channels": channels,
                       "derived_from": drawn.get(reason, []), "truncated": cut})
    for row in marker_rows:
        ids, box, cut = ids_and_box(row)
        marker, reason, status = row["marker"], row["reason"], row["status"]
        words = reason_words(reason)
        groups.append({"key": f"m:{marker}|{reason}|{status}", "level": "marker",
                       "reason": reason, "status": MARKER_STATUS.get(status, status),
                       "marker": marker, "label": f"{marker} · {words[:1].lower()}{words[1:]}",
                       "definition": schemas.MARKER_REASON_DEFINITIONS.get(reason, ""),
                       "color": reason_color(reason, class_colors, reason_colors),
                       "default_color": reason_color(reason), "count": int(row["count"]),
                       "ids": ids, "bbox": box, "evidence_channels": [marker],
                       "truncated": cut})
    return {**base, "has_positions": positions is not None, "groups": groups,
            "truncated": truncated}
