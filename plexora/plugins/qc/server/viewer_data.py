"""What the QC panel draws on the tissue: region outlines and flagged cells.

Read-only. Two answers, both about the ACTIVE result (or a named one, for
the regions):

- `regions`: every QC region that counts now, with the outline the ROI
  document holds (the user's, when they reshaped it) and its bounding box, so
  the panel can draw it and fit the viewer to it without a second request.
- `cells`: the flagged cells in two kinds of group. Whole-cell groups by
  (reason, status) -- "fail" when that reason excluded the cell (the cell's
  own `excluded_by`, exact), "note" when it only noted it (`noted_by`: the
  cell kept and recorded), "warn" when it flags it. Marker groups by
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

Every region and cell group names its `category` -- one of the five
(`schemas.CATEGORIES`), "review", or a custom category -- and its subtype
(`class`, `words`); the panel groups by the first and notes the second.

Colours are the user's where they chose one: a region is drawn in its
`qc_<category>` ROI category's colour (so the ROI panel and this one never
disagree, and the cells inside it follow), a cell reason in the colour kept
in QC's store; everything else in the defaults below.

A result derived before `excluded_by` existed is read the old way (a reason
that only excludes is "fail", one that only warns "warn", else the cell's
action decides) until its cells are derived again.
"""

from __future__ import annotations

import math
import threading

from plexora.plugins.qc.server import results, schemas

#: A ceiling on ids in one answer. A LUT is four bytes a cell and JSON is ~8,
#: so this is ~16 MB at worst -- and a flagged set that large is a result
#: that says "this image failed", which the counts already say.
MAX_TOTAL_IDS = 2_000_000

#: Cell reasons in words, for a row label (the words live in `schemas`).
REASON_WORDS = schemas.REASON_WORDS

#: Hues for the cell reasons. Region reasons take their class's colour, so a
#: cell inside a fold is drawn in the fold's red; these sit apart from the
#: class palette's alert reds and ambers so a cell reason is not read as a
#: region.
REASON_COLORS = {
    "seg_under": "#d946ef",
    "seg_over": "#8b5cf6",
    "seg_small": "#67e8f9",
    "seg_large": "#5eead4",
    "seg_irregular": "#bef264",
    "no_nucleus": "#fda4af",
    "dna_loss": "#fca5a5",
    # The cells outside the tissue: the Background ROI's own slate, so the
    # cells match its outline (a `background|note` group, faint).
    "background": schemas.BACKGROUND["color"],
}


def reason_words(reason):
    if reason.startswith("region:"):
        klass = reason.split(":", 1)[1]
        return f"In {schemas.CLASS_WORDS.get(klass, klass.replace('_', ' '))}"
    return REASON_WORDS.get(reason, reason.replace("_", " ").capitalize())


def reason_color(reason, category_colors=None, reason_colors=None):
    """A region reason is drawn in its category's colour, so the cells in a
    region match its outline; a cell reason in its own."""
    if reason.startswith("region:"):
        category = schemas.category_of_class(reason.split(":", 1)[1])
        return (category_colors or {}).get(category) or schemas.category_color(category)
    if reason == "background":
        return (category_colors or {}).get(schemas.BACKGROUND["id"]) \
            or (reason_colors or {}).get(reason) or REASON_COLORS[reason]
    return (reason_colors or {}).get(reason) or REASON_COLORS.get(reason, "#9ca3af")


def category_color(category, category_colors=None):
    return (category_colors or {}).get(category) or schemas.category_color(category)


#: The order categories are listed in: the five, then review, then custom.
CATEGORY_ORDER = {k: i for i, k in enumerate(schemas.category_order())}


def category_rank(category):
    return CATEGORY_ORDER.get(category, len(CATEGORY_ORDER))


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


def _cell_evidence(regions_by_class):
    """{reason: [channel]} for a result derived before calls recorded their
    evidence (`summary["evidence"]`): each region class's channels."""
    return {f"region:{klass}": _capped(channels) for klass, channels in regions_by_class.items()}


def _result(project, result_id=None):
    document = results.load(project)
    if result_id:
        return results.get_result(project, document, result_id)
    return results.active(document)


def regions(ds, project, result_id=None) -> dict:
    """{result_id, display, regions: [{roi_id, candidate_id, class, words,
    category, category_words, custom, color, default_color, action, channels,
    evidence_channels, view_channels, approved, locked, created_by,
    severity, tissue_fraction, score, score_kind, threshold,
    threshold_source, tool, ai, n_cells, geometry, bbox}]} -- in category
    order."""
    from plexora.plugins.qc.server import provenance, roi_link

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
        # What the region is grouped under: one of the five, "review", or the
        # custom QC category the user named (a technical artifact to
        # everything else); its class is the subtype.
        key = live.get("category") or schemas.category_of_class(klass)
        custom = schemas.is_custom(key)
        view = [dict(v) for v in candidate.get("view_channels") or [] if v.get("name")]
        record = provenance.region_summary(candidate, checks=result.get("checks"))
        out.append({
            "roi_id": live["roi_id"], "candidate_id": live.get("candidate_id"),
            "name": live.get("name"),
            "class": klass, "words": schemas.CLASS_WORDS.get(klass, klass),
            "category": key,
            "category_words": roi_link.custom_words(live.get("category_label")) if custom
            else schemas.category_words(key),
            "custom": custom,
            "color": category_color(key, colors),
            "default_color": schemas.category_color(key),
            **record,
            "action": live.get("action") or "exclude",
            "channels": list(live.get("channels") or []),
            # A region drawn by hand names the channels that were on screen
            # when it was drawn; `view_channels` puts them back as they were.
            # The channel it was found on first: a fold scoped to every channel
            # is shown on the stain it was seen in, not the first three names.
            "evidence_channels": [v["name"] for v in view] if view else _capped(
                # A consolidated ROI opens every finding's own channel first.
                [*(((f.get("channels") or [None])[0]) for f in candidate.get("findings") or []),
                 candidate.get("reference"), candidate.get("channel"),
                 candidate.get("audit_channel"), *(candidate.get("channels") or []),
                 *(live.get("channels") or [])]),
            "findings": [{"class": f["class"], "words": schemas.CLASS_WORDS.get(f["class"],
                                                                                f["class"]),
                          "channels": list(f.get("channels") or []),
                          "share": f.get("share"), "primary": bool(f.get("primary"))}
                         for f in candidate.get("findings") or []],
            "view_channels": view,
            # The whole view it was drawn under (viewport, zoom, HD mode,
            # channels), when the panel that drew it sent one: clicking the
            # region puts it back.
            "view": candidate.get("view"),
            "method": record["tool"].get("method"),
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
    counts = provenance.cells_per_region(project, result)
    for region in out:
        region["n_cells"] = counts.get(region["roi_id"])
    out.sort(key=lambda r: category_rank(r["category"]))
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
    cell: "fail" when that reason excluded it, "note" when it only noted it
    (the cell's `noted_by`), else "warn"."""
    import polars as pl

    flagged = frame.filter(pl.col("reason_count") > 0)
    if not flagged.height:
        return None
    reasons = (flagged.select(["cell_id", "reasons"]).explode("reasons", empty_as_null=True)
               .drop_nulls("reasons").rename({"reasons": "reason"}))
    if "excluded_by" in frame.columns:
        def marked(column, flag):
            return (flagged.select(["cell_id", column])
                    .explode(column, empty_as_null=True).drop_nulls(column)
                    .rename({column: "reason"}).with_columns(pl.lit(True).alias(flag)))

        joined = reasons.join(marked("excluded_by", "x"), on=["cell_id", "reason"], how="left")
        if "noted_by" in frame.columns:
            joined = joined.join(marked("noted_by", "nb"), on=["cell_id", "reason"], how="left")
        else:
            joined = joined.with_columns(pl.lit(None, dtype=pl.Boolean).alias("nb"))
        return joined.select(["cell_id", "reason",
                              pl.when(pl.col("x").fill_null(False)).then(pl.lit("fail"))
                              .when(pl.col("nb").fill_null(False)).then(pl.lit("note"))
                              .otherwise(pl.lit("warn")).alias("status")])
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


def _dismissed(result):
    """The findings the user set aside (dismiss_qc_finding), worded for the
    panel's "Set aside" rows: [{finding, reason, marker, channel, label, at}]."""
    out = []
    for entry in result.get("user_dismissed") or []:
        if entry.get("finding") == "channel":
            label = f"{entry.get('channel')} · audit verdict"
        elif entry.get("finding") == "marker":
            words = reason_words(entry.get("reason") or "")
            label = f"{entry.get('marker')} · {words[:1].lower()}{words[1:]}"
        else:
            label = reason_words(entry.get("reason") or "")
        out.append({k: entry.get(k) for k in ("finding", "reason", "marker", "channel", "at")}
                   | {"label": label})
    return out


def cells(ds, project, result_id=None, *, max_ids=MAX_TOTAL_IDS) -> dict:
    """{available, result_id, n, n_fail, n_warn, n_marker_flagged,
    n_noted, has_positions, groups: [{key, level, reason, status, marker, label,
    definition, color, default_color, count, ids, bbox, evidence_channels,
    derived_from, truncated}], truncated, note}. `derived_from` names the
    regions drawn by hand behind a region reason ([{roi_id, name}]). Whole-cell groups come first (every fail
    before every warn before every note, then the order a cell's primary
    reason is chosen in),
    then the marker groups (unreliable before flagged, by marker)."""
    import polars as pl

    result = _result(project, result_id)
    empty = {"available": False, "result_id": (result or {}).get("result_id"), "n": 0,
             "n_fail": 0, "n_warn": 0, "n_noted": 0, "n_marker_flagged": 0,
             "has_positions": False,
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
            "n_noted": int(summary.get("n_noted") or 0),
            "n_marker_flagged": int(summary.get("n_marker_flagged") or 0),
            "dismissed": _dismissed(result)}
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
    rank_status = {"fail": 0, "warn": 1, "note": 2}
    cell_rows = sorted(grouped(statuses, ["reason", "status"]), key=lambda g: (
        category_rank(schemas.category_of_reason(g["reason"])),
        rank_status.get(g["status"], 1), order.get(g["reason"], 999), g["reason"]))
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
        legacy = _cell_evidence(by_class)
    from plexora.plugins.qc.server import roi_link

    class_colors = roi_link.category_colors(ds)
    reason_colors = results.reason_colors(project)
    # The regions drawn by hand behind each region reason, by name: a cell
    # inside one is there because of that outline, not because QC found
    # anything in the cell itself, and the panel says so ("Derived from").
    drawn = {}
    for live in roi_link.live_regions(ds, result):
        if live.get("created_by") == "user":
            drawn.setdefault(schemas.region_reason(live["class"]), []).append(
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
        category = schemas.category_of_reason(reason)
        groups.append({"key": f"{reason}|{row['status']}", "level": "cell", "reason": reason,
                       "category": category,
                       "category_words": schemas.category_words(category),
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
        category = schemas.category_of_reason(reason)
        groups.append({"key": f"m:{marker}|{reason}|{status}", "level": "marker",
                       "reason": reason, "category": category,
                       "category_words": schemas.category_words(category),
                       "status": MARKER_STATUS.get(status, status),
                       "marker": marker, "label": f"{marker} · {words[:1].lower()}{words[1:]}",
                       "definition": schemas.MARKER_REASON_DEFINITIONS.get(reason, ""),
                       "color": reason_color(reason, class_colors, reason_colors),
                       "default_color": reason_color(reason), "count": int(row["count"]),
                       "ids": ids, "bbox": box, "evidence_channels": [marker],
                       "truncated": cut})
    return {**base, "has_positions": positions is not None, "groups": groups,
            "truncated": truncated}


# -- the cell under the pointer ---------------------------------------------------
#
# The viewer's hover card asks what QC says of the cell under the pointer. The
# mask answers which cell it is (the label at that pixel, or the nearest one
# within `radius`); without a mask, or when it cannot be read, the nearest
# centroid within about a cell's radius does. The answer is the cell's record
# (`provenance.cell_record`), or None for glass and for a cell the table does
# not hold. Everything a hover reads is held between hovers and dropped when
# the project's store file changes (`exclusions._file_token`).

#: The furthest from the pointer (full-resolution pixels) a cell is looked for.
MAX_HOVER_RADIUS_PX = 64
#: How long one mask read for a hover may take on a node before the centroids
#: answer instead (a node that is asleep must not hold a request thread).
HOVER_READ_TIMEOUT_S = 5.0
#: A centroid counts as under the pointer within this much of the typical
#: cell spacing (about a cell's radius), never further than the cap.
CENTROID_SHARE = 0.5
MAX_CENTROID_PX = 32.0

_HOVER_LOCK = threading.Lock()
_HOVER: dict = {}       # project -> held context
_CENTROIDS: dict = {}   # (project, identity) -> (ids, tree, reach)
_SEGQC_HELD: dict = {}  # (project, fp) -> (ids sorted, order, frame)


def _held_put(store, key, value, cap=4):
    with _HOVER_LOCK:
        store.pop(key, None)
        store[key] = value
        while len(store) > cap:
            store.pop(next(iter(store)))


def _index(ids):
    """(sorted ids, the order that sorts them) for a searchsorted lookup."""
    import numpy as np

    ids = np.asarray(ids, dtype=np.int64)
    order = np.argsort(ids, kind="stable")
    return ids[order], order


def _find(index, cell_id):
    import numpy as np

    ids, order = index
    if not ids.size:
        return None
    at = int(np.searchsorted(ids, cell_id))
    if at < ids.size and int(ids[at]) == int(cell_id):
        return int(order[at])
    return None


def _hover_context(ds, project):
    """The active result, its cell calls indexed by id, the live regions in
    words and the cell/region overlaps: what one hover reads."""
    from plexora.plugins.qc.server import roi_link
    from plexora.plugins.qc.server.exclusions import _file_token

    token = _file_token(project)
    with _HOVER_LOCK:
        held = _HOVER.get(project)
    if held is not None and token is not None and held["token"] == token:
        return held
    result = _result(project)
    frame = results.cells(project) if result is not None else None
    note = None
    if result is None:
        note = "no QC result yet"
    elif frame is None or not frame.height:
        frame, note = None, "QC has not flagged this project's cells"
    elif "result_id" in frame.columns and frame["result_id"][0] != result.get("result_id"):
        frame, note = None, "cell calls are kept for the active result only"
    colors = roi_link.category_colors(ds) if result is not None else {}
    regions = {}
    for live in (roi_link.live_regions(ds, result) if result is not None else []):
        key = live.get("category") or schemas.category_of_class(live["class"])
        custom = schemas.is_custom(key)
        regions[live["roi_id"]] = {
            "roi_id": live["roi_id"], "name": live.get("name") or "",
            "class": live["class"],
            "class_words": schemas.CLASS_WORDS.get(live["class"], live["class"]),
            "category": key,
            "category_words": roi_link.custom_words(live.get("category_label")) if custom
            else schemas.category_words(key),
            "color": category_color(key, colors), "action": live.get("action"),
            "created_by": live.get("created_by")}
    pairs = results.cell_rois(project) if frame is not None else None
    held = {"token": token, "result": result, "frame": frame, "note": note,
            "index": _index(frame["cell_id"].to_numpy()) if frame is not None else None,
            "regions": regions, "class_colors": colors,
            "reason_colors": results.reason_colors(project) if result is not None else {},
            "pairs": pairs if pairs is not None and pairs.height else None,
            "pair_index": _index(pairs["cell_id"].to_numpy())
            if pairs is not None and pairs.height else None}
    _held_put(_HOVER, project, held)
    return held


def _fractions(held, cell_id):
    """{roi_id: (fraction, method)} of the regions that hold the cell."""
    import numpy as np

    pairs = held.get("pairs")
    if pairs is None:
        return {}
    ids, order = held["pair_index"]
    lo, hi = np.searchsorted(ids, cell_id, side="left"), np.searchsorted(ids, cell_id,
                                                                        side="right")
    out = {}
    for row in order[lo:hi].tolist():
        values = pairs.row(int(row), named=True)
        # A consolidated ROI's findings are members of the ROI: the largest share.
        roi_id = str(values["roi_id"]).split("#", 1)[0]
        fraction = _float(values.get("fraction"))
        held_fraction = (out.get(roi_id) or (None,))[0]
        if roi_id not in out or (fraction or 0) > (held_fraction or 0):
            out[roi_id] = (fraction, values.get("method"))
    return out


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _segqc_row(project, cell_id):
    """The cell's Segmentation QC call, or None (no run, or not in it)."""
    from plexora.plugins.qc.server.segqc import run as segqc

    try:
        summary = segqc.current(project)
    except Exception:
        return None
    if not summary:
        return None
    fp = summary.get("fingerprint")
    with _HOVER_LOCK:
        held = _SEGQC_HELD.get((project, fp))
    if held is None:
        frame = segqc.frame(project, fp)
        if frame is None or not frame.height:
            return None
        held = (_index(frame["cell_id"].to_numpy()), frame)
        _held_put(_SEGQC_HELD, (project, fp), held, cap=2)
    index, frame = held
    at = _find(index, cell_id)
    if at is None:
        return None
    row = frame.row(at, named=True)
    flag = float({**segqc.PARAMS_DEFAULT, **(summary.get("params") or {})}["flag"])
    partner = row.get("partner_id")
    return {"status": row.get("status_word"), "reason": row.get("reason") or None,
            "under_score": _float(row.get("under_score")),
            "over_score": _float(row.get("over_score")), "flag": flag,
            "partner_id": int(partner) if partner not in (None, 0, -1) else None}


#: How far around the pointer the mask is read (full-resolution pixels), so
#: that the cell found comes back whole -- its shape lets the browser answer
#: every later move over it without asking again.
SHAPE_READ_PX = 40


def _shape(labels, label, x0, y0):
    """The pixels of `label` within the read window, packed for the browser:
    {box: [x, y, w, h] (full-resolution pixels), bits: base64 of the row-major
    bit mask, MSB first}. A cell cut by the window's edge comes back cut: the
    browser asks again for the part it was not sent."""
    import base64

    import numpy as np

    rows, cols = np.nonzero(labels == label)
    if not rows.size:
        return None
    r0, r1, c0, c1 = int(rows.min()), int(rows.max()) + 1, int(cols.min()), int(cols.max()) + 1
    patch = labels[r0:r1, c0:c1] == label
    return {"box": [x0 + c0, y0 + r0, c1 - c0, r1 - r0],
            "bits": base64.b64encode(np.packbits(patch.ravel())).decode("ascii")}


def _label_at(ds, x, y, radius):
    """(label or None, "mask", shape or None) from the mask, or (None, None,
    None) when there is no mask to read or it could not be read."""
    import inspect

    import numpy as np

    from plexora.plugins.qc.server import propagate

    try:
        provider, _why = propagate._mask_provider(ds)
    except Exception:
        provider = None
    if provider is None:
        return None, None, None
    record = ds.project
    extra = int(getattr(record.segmentation, "extra_levels", 0) or 0)
    width, height = int(record.image.width or 0), int(record.image.height or 0)
    r = int(math.ceil(radius))
    reach = max(r, SHAPE_READ_PX)
    cx, cy = int(math.floor(x)), int(math.floor(y))
    x0, y0 = max(0, cx - reach), max(0, cy - reach)
    x1 = min(width or cx + reach + 1, cx + reach + 1)
    y1 = min(height or cy + reach + 1, cy + reach + 1)
    if x1 <= x0 or y1 <= y0:
        return None, "mask", None
    kwargs = {"max_pixels": (x1 - x0) * (y1 - y0)}
    try:
        if "timeout" in inspect.signature(provider.read_region).parameters:
            kwargs["timeout"] = HOVER_READ_TIMEOUT_S
    except (TypeError, ValueError):
        pass
    try:
        labels = np.asarray(provider.read_region(extra, (x0, y0, x1, y1), **kwargs))
    except Exception:
        return None, None, None
    if labels.ndim > 2:
        labels = labels.reshape(labels.shape[-2:])
    if not labels.size:
        return None, "mask", None
    h, w = labels.shape
    py, px = min(max(cy - y0, 0), h - 1), min(max(cx - x0, 0), w - 1)
    centre = int(labels[py, px])
    if centre:
        return centre, "mask", _shape(labels, centre, x0, y0)
    if r <= 0:
        return None, "mask", None
    near = labels[max(0, py - r):py + r + 1, max(0, px - r):px + r + 1]
    rows, cols = np.nonzero(near)
    if not rows.size:
        return None, "mask", None
    rows = rows + max(0, py - r)
    cols = cols + max(0, px - r)
    d2 = (rows - py) ** 2 + (cols - px) ** 2
    best = int(np.argmin(d2))
    if d2[best] > radius * radius:
        return None, "mask", None
    found = int(labels[rows[best], cols[best]])
    return found, "mask", _shape(labels, found, x0, y0)


def _centroids(ds, project):
    """(ids, KD-tree, reach) of the table's centroids, held per project and
    table; None without coordinates."""
    import numpy as np

    try:
        from plexora.agent.session import _identity

        # The identity holds the spec's fingerprint (a dict): its repr is the key.
        key = (project, repr(_identity(ds.project)))
    except Exception:
        key = (project, id(ds.project))
    with _HOVER_LOCK:
        held = _CENTROIDS.get(key)
    if held is not None:
        return held
    positions = _positions(ds)
    if positions is None or not positions.height:
        return None
    from scipy.spatial import cKDTree

    xs, ys = positions["x"].to_numpy(), positions["y"].to_numpy()
    finite = np.isfinite(xs) & np.isfinite(ys)
    ids = positions["cell_id"].to_numpy()[finite]
    points = np.column_stack([xs[finite], ys[finite]])
    if not ids.size:
        return None
    extent = max(1.0, float(np.ptp(points[:, 0]) * np.ptp(points[:, 1])))
    spacing = math.sqrt(extent / ids.size)
    reach = min(MAX_CENTROID_PX, CENTROID_SHARE * spacing)
    held = (ids, cKDTree(points), reach)
    _held_put(_CENTROIDS, key, held, cap=4)
    return held


def _nearest_centroid(ds, project, x, y, radius):
    held = _centroids(ds, project)
    if held is None:
        return None
    ids, tree, reach = held
    distance, at = tree.query([x, y], k=1)
    if not distance <= max(radius, reach, 1.0):
        return None
    return int(ids[int(at)])


def cell_at(ds, project, x, y, *, radius=0.0) -> dict:
    """{cell, method, result_id}: QC's record of the cell at full-resolution
    pixel (x, y) (`provenance.cell_record`), or None. `method` is "mask" (the
    label there, or the nearest within `radius`), "centroid" (no mask, or it
    could not be read: the nearest centroid within about a cell's radius),
    "none" (no way to tell) or "out_of_bounds". A cell QC has no current calls
    for reads `calls: False` with a `note`. With the mask, `shape` is the
    cell's pixels (`_shape`), so the browser knows where it ends."""
    from plexora.plugins.qc.server import provenance

    record = ds.project
    width, height = int(record.image.width or 0), int(record.image.height or 0)
    if x < 0 or y < 0 or (width and x >= width) or (height and y >= height):
        return {"cell": None, "method": "out_of_bounds", "result_id": None}
    radius = max(0.0, min(float(radius or 0.0), float(MAX_HOVER_RADIUS_PX)))
    cell_id, method, shape = _label_at(ds, x, y, radius)
    if method is None:
        try:
            cell_id = _nearest_centroid(ds, project, x, y, radius)
        except Exception:  # a table on a node that is asleep
            cell_id = None
            method = "none"
        else:
            method = "centroid"
    held = _hover_context(ds, project)
    result_id = (held["result"] or {}).get("result_id")
    if cell_id is None:
        return {"cell": None, "method": method, "result_id": result_id}
    shaped = {"shape": shape} if shape else {}
    if held["frame"] is None:
        return {"cell": {"cell_id": int(cell_id), "calls": False, "note": held["note"],
                         "segqc": _segqc_row(project, cell_id)},
                "method": method, "result_id": result_id, **shaped}
    at = _find(held["index"], cell_id)
    if at is None:
        # A label the table does not hold: no cell of QC's.
        return {"cell": None, "method": method, "result_id": result_id, **shaped}
    row = held["frame"].row(at, named=True)
    cell = provenance.cell_record(
        held["result"], row, regions=held["regions"], fractions=_fractions(held, cell_id),
        segqc=_segqc_row(project, cell_id), class_colors=held["class_colors"],
        reason_colors=held["reason_colors"])
    return {"cell": cell, "method": method, "result_id": result_id, **shaped}
