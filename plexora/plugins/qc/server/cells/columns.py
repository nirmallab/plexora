"""The per-cell QC columns every export writes, built once.

`qc_cells` is the one per-cell record (`calls.derive`); this turns it into the
columns a user's file or a download carries, so cells.csv, the flat append,
AnnData obs, the SpatialData table, cells.h5ad and the SCIMAP Pro hand-off
cannot disagree about what a cell's status is. Pure polars over the stored
frame: no per-row Python.

Canonical columns (prefix `plexora_qc_` on the append path, `strip_prefix`
for the standalone CSV):

    cell_id            the table's id (the join key; never prefixed)
    status             pass / flagged / excluded (a warn is flagged; a cell
                       with notes only passes)
    pass               status != excluded
    primary_reason     the reason that excludes it first ("" otherwise)
    category           the primary reason's category, else the first reason's
                       that does more than note the cell
    categories         ";"-joined: every category of such a reason
    blur_focus ...     one per category (the five, review, and `custom` for
                       every custom category together): the cell's strongest
                       status in it -- excluded > flagged > channel (a marker
                       excluded or warned; the cell stands) > noted > ""
    reasons / excluded_by / noted   ";"-joined reasons
    reason_count, background, unreliable_markers, marker_flags
                       as `qc_cells` holds them ("marker|reason|status")
    roi_ids, roi_method   the regions the cell is in, and how it was placed
    flag_source        direct / roi / both
    source             auto / manual / ai / mixed: who made what flags it
"""

from __future__ import annotations

import json

import polars as pl

from plexora.plugins.qc.server import schemas

PREFIX = "plexora_qc_"
COLUMNS_VERSION = "2"
#: The per-category columns, in the panel's order; `custom` is every custom
#: category together.
CATEGORY_COLUMNS = (*schemas.CATEGORY_IDS, schemas.REVIEW["id"], "custom")
STATUS_RANK = ("excluded", "flagged", "channel", "noted")

#: Column order of the canonical frame.
ORDER = ("cell_id", "status", "pass", "primary_reason", "category", "categories",
         *CATEGORY_COLUMNS, "reasons", "excluded_by", "noted", "reason_count", "background",
         "unreliable_markers", "marker_flags", "roi_ids", "roi_method", "flag_source",
         "source")
#: The columns joined with ";" in a flat file (lists in the stored frame).
LIST_COLUMNS = ("reasons", "excluded_by", "noted", "unreliable_markers", "marker_flags",
                "roi_ids")
#: Categorical columns (category dtype in AnnData).
CATEGORICAL = ("status", "primary_reason", "category", *CATEGORY_COLUMNS, "roi_method",
               "flag_source", "source")

OBSM_REASONS = "plexora_qc_flags"
OBSM_MARKERS = "plexora_qc_marker_flags"
OBSM_MARKERS_WARN = "plexora_qc_marker_warn"


def dictionary() -> dict:
    """{column: what it says}, for `uns`, the vocabulary route and docs."""
    words = {
        "cell_id": "The table's cell id (the mask label when that role is set); the join key.",
        "status": "pass, flagged (kept, with a warning) or excluded (left out of analyses).",
        "pass": "False only for an excluded cell.",
        "primary_reason": "The reason that excludes the cell first; empty otherwise.",
        "category": "The primary reason's category, else the first reason's that does more "
                    "than note the cell.",
        "categories": "Every category of a reason that does more than note the cell "
                      "(';'-joined).",
        "reasons": "Every whole-cell reason, noted ones too (';'-joined).",
        "excluded_by": "The reasons that exclude the cell (';'-joined).",
        "noted": "The reasons that only note the cell (';'-joined).",
        "reason_count": "How many whole-cell reasons the cell has.",
        "background": "Outside the tissue (the Background ROI), whatever it does to the cell.",
        "unreliable_markers": "Markers excluded for this cell (';'-joined); the cell stands.",
        "marker_flags": "Every marker-level finding, 'marker|reason|status' (';'-joined).",
        "roi_ids": "The QC regions the cell is in (';'-joined).",
        "roi_method": "How the cell was placed in its regions: mask (overlap of its mask), "
                      "centroid, labels (named by id) or mixed.",
        "flag_source": "direct (measured on the cell), roi (from a region) or both.",
        "source": "auto (a detector), manual (a person drew or edited it), ai (an agent's "
                  "verdict) or mixed.",
    }
    for category in CATEGORY_COLUMNS:
        label = "every custom category" if category == "custom" else \
            schemas.category_words(category)
        words[category] = (f"The cell's strongest status in {label}: excluded, flagged, "
                           "channel (a marker excluded or warned, the cell kept), noted, "
                           "or empty.")
    return words


def strip_prefix(frame):
    """The canonical names, without `PREFIX` (cell_id never had it)."""
    return frame.rename({c: c[len(PREFIX):] for c in frame.columns if c.startswith(PREFIX)})


def with_prefix(frame):
    return frame.rename({c: f"{PREFIX}{c}" for c in frame.columns
                         if c not in ("cell_id", "row")})


def _candidate_source(candidate) -> str:
    user = candidate.get("user_state") or {}
    if candidate.get("created_by") == "user" or user.get("created_by") == "user" \
            or user.get("edited") or user.get("relabelled"):
        return "manual"
    if candidate.get("ai_decision") or str(candidate.get("created_by") or "").startswith(
            ("agent", "bench_")) or candidate.get("origin") == "agent":
        return "ai"
    return "auto"


def region_sources(result, roi_meta=None) -> dict:
    """{roi_id: auto | manual | ai | mixed} of the result's regions: a region
    a person drew or reshaped is manual, one an agent decided is ai, a
    detector's with nobody's judgment auto. A consolidated region is mixed
    when its findings disagree."""
    candidates = (result or {}).get("candidates") or {}
    out = {}
    for candidate in candidates.values():
        roi_id = candidate.get("roi_id")
        if not roi_id:
            continue
        found = {_candidate_source(candidate)}
        for finding in candidate.get("findings") or ():
            source = candidates.get(finding.get("candidate_id"))
            if source is not None:
                found.add(_candidate_source(source))
        prior = out.get(roi_id)
        if prior is not None:
            found.add(prior)
        out[roi_id] = found.pop() if len(found) == 1 else "mixed"
    if roi_meta is not None and getattr(roi_meta, "height", 0):
        for row in roi_meta.select(["roi_id", "created_by", "user_edited"]).to_dicts():
            if row["roi_id"] in out:
                continue
            out[row["roi_id"]] = "manual" if row.get("created_by") == "user" \
                or row.get("user_edited") else "auto"
    return out


def _list(frame, name):
    if name in frame.columns:
        return pl.col(name).cast(pl.List(pl.Utf8))
    return pl.lit([], dtype=pl.List(pl.Utf8))


def _category_status_from_reasons(cells):
    """`category_status` for a frame derived before it was stored: from the
    reasons and the marker flags, by each reason's category."""
    base = cells.select(pl.int_range(pl.len()).alias("_i"),
                        _list(cells, "reasons").alias("reasons"),
                        _list(cells, "excluded_by").alias("excluded_by"),
                        _list(cells, "noted_by").alias("noted_by"),
                        _list(cells, "marker_flags").alias("marker_flags"))
    rows = []
    for name, word in (("excluded_by", "excluded"), ("noted_by", "noted")):
        part = base.select("_i", pl.col(name).alias("r")).explode("r", empty_as_null=True).drop_nulls("r")
        rows.append(part.with_columns(pl.lit(word).alias("s")))
    warned = (base.select("_i", pl.col("reasons").alias("r"),
                          pl.concat_list("excluded_by", "noted_by").alias("x"))
              .explode("r", empty_as_null=True).drop_nulls("r").filter(~pl.col("r").is_in(pl.col("x")))
              .select("_i", "r"))
    rows.append(warned.with_columns(pl.lit("flagged").alias("s")))
    flags = base.select("_i", pl.col("marker_flags").alias("f")).explode("f", empty_as_null=True).drop_nulls("f")
    flags = flags.with_columns(pl.col("f").str.split("|").alias("p")).select(
        "_i", pl.col("p").list.get(1, null_on_oob=True).alias("r"),
        pl.when(pl.col("p").list.get(2, null_on_oob=True) == "note").then(pl.lit("noted"))
        .otherwise(pl.lit("channel")).alias("s"))
    rows.append(flags.drop_nulls("r"))
    long = pl.concat(rows, how="vertical_relaxed").with_columns(
        pl.col("r").replace_strict(schemas.CELL_REASON_CATEGORY, default="tissue_acquisition",
                                   return_dtype=pl.Utf8).alias("c"),
        pl.col("s").replace_strict({w: i for i, w in enumerate(STATUS_RANK)},
                                   return_dtype=pl.Int8).alias("k"))
    best = long.group_by(["_i", "c"]).agg(pl.col("k").min()).with_columns(
        (pl.col("c") + "|" + pl.col("k").replace_strict(
            dict(enumerate(STATUS_RANK)), return_dtype=pl.Utf8)).alias("v"))
    per = best.group_by("_i").agg(pl.col("v"))
    full = base.select("_i").join(per, on="_i", how="left", maintain_order="left")
    return full["v"].fill_null(pl.lit([], dtype=pl.List(pl.Utf8)))


def _category_expr(name, status_col):
    if name == "custom":
        def has(word):
            return status_col.list.eval(
                pl.element().str.contains(rf"^{schemas.CUSTOM_PREFIX}[^|]*\|{word}$")).list.any()
    else:
        def has(word):
            return status_col.list.contains(f"{name}|{word}")
    expr = pl.lit("")
    for word in reversed(STATUS_RANK):
        expr = pl.when(has(word)).then(pl.lit(word)).otherwise(expr)
    return expr.alias(name)


def build(cells, result=None, roi_meta=None):
    """The canonical frame of `qc_cells` (one row per stored cell, `row` kept
    for alignment when the frame has it)."""
    if cells is None:
        return None
    statuses = cells["category_status"].cast(pl.List(pl.Utf8)) \
        if "category_status" in cells.columns else _category_status_from_reasons(cells)
    work = cells.with_columns(statuses.alias("_cs"), pl.int_range(pl.len()).alias("_i"))
    reasons = _list(work, "reasons")
    noted = _list(work, "noted_by")
    # Reasons that do more than note: each with its category, in list order.
    long = (work.select("_i", reasons.alias("r"), noted.alias("n"))
            .explode("r", empty_as_null=True).drop_nulls("r")
            .filter(~pl.col("r").is_in(pl.col("n")))
            .with_columns(pl.col("r").replace_strict(
                schemas.CELL_REASON_CATEGORY, default="tissue_acquisition",
                return_dtype=pl.Utf8).alias("c")))
    rank = {c: i for i, c in enumerate(schemas.category_order())}
    agg = long.group_by("_i", maintain_order=True).agg(
        pl.col("c").first().alias("_first"),
        pl.col("c").unique().sort_by(pl.col("c").unique().replace_strict(
            rank, default=99, return_dtype=pl.Int32)).alias("_cats"),
        pl.col("r").str.starts_with("region:").any().alias("_region"),
        (~pl.col("r").str.starts_with("region:")).any().alias("_direct"))
    work = work.join(agg, on="_i", how="left", maintain_order="left")
    primary = pl.col("primary_reason").fill_null("") if "primary_reason" in work.columns \
        else pl.lit("")
    action = pl.col("action") if "action" in work.columns else pl.when(pl.col("pass")) \
        .then(pl.lit("pass")).otherwise(pl.lit("exclude"))
    # Who made what flags each cell.
    sources = region_sources(result, roi_meta)
    rois = (work.select("_i", _list(work, "roi_ids").alias("roi")).explode("roi", empty_as_null=True)
            .drop_nulls("roi")
            .with_columns(pl.col("roi").replace_strict(sources, default="auto",
                                                       return_dtype=pl.Utf8).alias("src")))
    direct_flag = (work.select("_i", _list(work, "marker_flags").alias("f")).explode("f", empty_as_null=True)
                   .drop_nulls("f").filter(~pl.col("f").str.split("|").list.get(
                       1, null_on_oob=True).str.starts_with("region:"))
                   .select("_i", pl.lit("auto").alias("src")))
    direct_reason = work.filter(pl.col("_direct").fill_null(False)).select(
        "_i", pl.lit("auto").alias("src"))
    who = (pl.concat([rois.select("_i", "src"), direct_flag, direct_reason],
                     how="vertical_relaxed")
           .group_by("_i").agg(pl.col("src").unique()))
    work = work.join(who, on="_i", how="left", maintain_order="left")
    out = work.select(
        *(["row"] if "row" in work.columns else []),
        pl.col("cell_id").cast(pl.Int64),
        action.replace_strict({"exclude": "excluded", "warn": "flagged", "pass": "pass"},
                              default="pass", return_dtype=pl.Utf8).alias("status"),
        pl.col("pass").cast(pl.Boolean),
        primary.alias("primary_reason"),
        pl.when(primary != "").then(primary.replace_strict(
            schemas.CELL_REASON_CATEGORY, default="tissue_acquisition", return_dtype=pl.Utf8))
        .otherwise(pl.col("_first").fill_null("")).alias("category"),
        pl.col("_cats").fill_null(pl.lit([], dtype=pl.List(pl.Utf8))).list.join(";")
        .alias("categories"),
        *[_category_expr(c, pl.col("_cs")) for c in CATEGORY_COLUMNS],
        reasons.list.join(";").alias("reasons"),
        _list(work, "excluded_by").list.join(";").alias("excluded_by"),
        noted.list.join(";").alias("noted"),
        (pl.col("reason_count").cast(pl.Int16) if "reason_count" in work.columns
         else reasons.list.len().cast(pl.Int16)).alias("reason_count"),
        (pl.col("background").cast(pl.Boolean) if "background" in work.columns
         else reasons.list.contains("background")).alias("background"),
        _list(work, "unreliable_markers").list.join(";").alias("unreliable_markers"),
        _list(work, "marker_flags").list.join(";").alias("marker_flags"),
        _list(work, "roi_ids").list.join(";").alias("roi_ids"),
        (pl.col("roi_method").fill_null("") if "roi_method" in work.columns
         else pl.lit("")).alias("roi_method"),
        pl.when(pl.col("_region") & pl.col("_direct")).then(pl.lit("both"))
        .when(pl.col("_region")).then(pl.lit("roi"))
        .when(pl.col("_direct")).then(pl.lit("direct")).otherwise(pl.lit(""))
        .alias("flag_source"),
        pl.when(pl.col("src").list.len() > 1).then(pl.lit("mixed"))
        .when(pl.col("src").list.len() == 1).then(pl.col("src").list.first())
        .otherwise(pl.lit("")).alias("source"))
    return out


def obsm_frames(cells, markers) -> dict:
    """{obsm key: polars frame} of boolean columns per stored cell: every
    whole-cell reason, the markers excluded, the markers warned."""
    reasons = _list(cells, "reasons")
    unreliable = _list(cells, "unreliable_markers")
    flags = _list(cells, "marker_flags")
    markers = list(markers or ())
    return {
        OBSM_REASONS: cells.select([reasons.list.contains(r).alias(r)
                                    for r in schemas.REASONS]),
        OBSM_MARKERS: cells.select([unreliable.list.contains(m).alias(m) for m in markers])
        if markers else pl.DataFrame(),
        OBSM_MARKERS_WARN: cells.select([
            flags.list.eval(pl.element().str.starts_with(f"{m}|")
                            & pl.element().str.ends_with("|warn")).list.any().alias(m)
            for m in markers]) if markers else pl.DataFrame(),
    }


def image_id(ds) -> str:
    """Which image of a shared file the project is: its subset value, else
    the project's name."""
    record = getattr(ds, "project", None)
    spec = getattr(record, "dataset", None) if record is not None else None
    subset = (getattr(spec, "subset", None) or {}) if spec is not None else {}
    value = subset.get("value") if isinstance(subset, dict) else None
    return str(value) if value is not None else str(ds.name)


def uns_result(ds, result, document, *, regions=None) -> dict:
    """One image's entry of `uns["plexora_qc"]["results"]`: what the calls were
    made from and under which settings, and the regions behind them."""
    from plexora.plugins.qc.server import strictness

    cells = (result or {}).get("cells") or {}
    preset = (document.get("strictness") or {}).get("preset") or "standard"
    custom = (document.get("strictness") or {}).get("thresholds")
    try:
        table = strictness.thresholds(preset, custom if preset == "custom" else None)
    except Exception:
        table = {}
    return {"result_id": result.get("result_id"), "session_id": result.get("session_id"),
            "origin": result.get("origin"), "created_at": result.get("created_at"),
            "config": {"strictness": document.get("strictness") or {},
                       "roi_overlap_fraction": table.get("cells.roi_overlap_fraction"),
                       "marker_quantile": table.get("cells.marker_quantile"),
                       "disabled_categories": list(
                           (document.get("exclusion_filter") or {})
                           .get("disabled_categories") or []),
                       "cycles_override": document.get("cycles_override")},
            "provenance": {"roi_revision": cells.get("roi_revision"),
                           "inputs": cells.get("inputs"),
                           "dismissed": cells.get("dismissed") or []},
            "regions": regions or [],
            "summary": {k: cells.get(k) for k in ("n", "n_fail", "n_warn", "n_noted",
                                                  "n_flagged_any", "by_category")}}


def region_rows(ds, result) -> list:
    """The regions behind the calls, compactly: id, category, class, action,
    level, channels, cycles, score, threshold, confidence, source, cells."""
    from plexora.plugins.qc.server import class_rules, provenance, roi_link

    sources = region_sources(result)
    try:
        counts = provenance.cells_per_region(ds.name, result) or {}
    except Exception:
        counts = {}
    try:
        segmentation = class_rules.segmentation_channel(ds, result.get("cycles") or [])
    except Exception:
        segmentation = None
    out = []
    for region in roi_link.live_regions(ds, result):
        candidate = (result.get("candidates") or {}).get(region["candidate_id"]) or {}
        level, _channels = class_rules.region_level(
            region["class"], candidate.get("scope"), candidate.get("channels") or [],
            segmentation=segmentation)
        metrics = candidate.get("metrics") or {}
        decision = candidate.get("ai_decision") or {}
        out.append({"roi_id": region["roi_id"], "candidate_id": region["candidate_id"],
                    "category": region.get("category")
                    or schemas.category_of_class(region["class"]),
                    "class": region["class"], "action": region["action"], "level": level,
                    "channels": list(candidate.get("channels") or []),
                    "cycles": list(candidate.get("cycles") or []),
                    "score": candidate.get("score"), "threshold": metrics.get("threshold"),
                    "threshold_source": metrics.get("threshold_source"),
                    "severity": decision.get("severity"),
                    "confidence": decision.get("confidence"),
                    "source": sources.get(region["roi_id"], "auto"),
                    "created_by": candidate.get("created_by"),
                    "n_cells": counts.get(region["roi_id"])})
    return out


def uns_body(ds, result, document, *, existing=None) -> dict:
    """`uns["plexora_qc"]` (version "2"): definitions, the column dictionary,
    and `results` keyed by image id -- merged into `existing` (the file's
    current body), so writing one image never drops another's."""
    from plexora.plugins.qc import VERSION as PLUGIN_VERSION
    from plexora.plugins.qc.server import provenance
    from plexora.plugins.qc.server.results import _software_version

    merged = {}
    if existing and str(existing.get("version")) == COLUMNS_VERSION:
        try:
            merged = json.loads(existing.get("results") or "{}")
        except (TypeError, ValueError):
            merged = {}
    merged[image_id(ds)] = uns_result(ds, result, document,
                                      regions=region_rows(ds, result))
    from datetime import datetime, timezone

    return {"version": COLUMNS_VERSION,
            "result_id": str(result["result_id"]),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "plexora_version": str(_software_version() or ""),
            "plugin_version": str(PLUGIN_VERSION),
            "definitions": json.dumps(schemas.REASON_DEFINITIONS),
            "marker_definitions": json.dumps(schemas.MARKER_REASON_DEFINITIONS),
            "categories": json.dumps(provenance.vocabulary()),
            "columns": json.dumps(dictionary()),
            "results": json.dumps(merged, default=str)}
