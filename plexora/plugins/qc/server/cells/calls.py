"""Every cell's QC call: pass or fail, why, which regions it sits in, and
which of its markers cannot be trusted.

`derive` is a pure function of the table's columns, the regions' membership
(`propagate`), the channel-level verdicts and a strictness table -- so the
same result under another preset is one call away, with no packet and no
mask read (the membership fractions are kept).

Two levels (see `schemas`): a CELL reason fails or warns the whole cell; a
MARKER flag says one channel's value is unreliable for that cell and leaves
the cell's call alone. A cell fails when any reason that excludes applies;
warnings are recorded beside it and never fail it.

Regions, by `class_rules.region_level`:

- a region of a class that damages tissue, scoped to every channel, or
  reaching the nuclear stain the cells were segmented from: the cells it
  covers (`cells.roi_overlap_fraction` of their mask) get `region:<class>`;
- a region scoped to some channels: those markers are flagged in the cells
  it covers, and nothing else. For a class that ADDS signal
  (`schemas.SIGNAL_RAISING_CLASSES`) a cell that merely touches the region
  counts, but only when the region is borne out by the cells' own values --
  several times more of its cells stand out above the cells around it than
  chance predicts (`schemas.MARKER_EVIDENCE`, `signal_test`) -- and only the
  cells above the surrounding cells' `cells.marker_quantile` themselves. A
  region the cells do not bear out flags none of them, and says why (a region
  only brighter as a whole is told apart from one no brighter at all).

Segmentation QC's calls (`seg`, `segqc.calls`) are per-cell reasons, never a
region: a merged or split cell is NOTED (kept, recorded in `noted_by`), a
size outlier warns -- or excludes where the preset lets size alone
(`area.size_alone`) -- and an irregular shape only warns.

The Background ROI (`background.py`, class `schemas.BACKGROUND_CLASS`) is a
region too, of action `note`: its cells get `background` (kept, noted, the
`background` column true) -- excluded only when the user renamed or approved
it so.

DNA retention (`dna`, `dna_retention.calls`) gives two more: a label whose
reference-cycle DNA is no brighter than the glass's noise has `no_nucleus`
(a cell reason: warns, excludes under strict, `cells.no_nucleus_exclude`);
a nucleated cell that lost its nucleus before a marker's cycle has that
marker flagged `dna_loss` (exclude for the marker, so it is in
`unreliable_markers` and gating leaves it out).

Three statuses (`schemas.CELL_STATUSES`): exclude fails the cell, warn is
recorded beside a passing cell, note is recorded and changes nothing --
`pass` and `action` ignore notes, as does everything that leaves cells out
(`exclusions`). A noted reason is in `reasons` and in `noted_by`.

Every call keeps what triggered it (`summary["evidence"]`, `["marker_evidence"]`):
the channels, the cutoffs, the test and its numbers. The viewer shows those
channels when the call is clicked.
"""

from __future__ import annotations

import re

import numpy as np
import polars as pl

from plexora.plugins.qc.server import class_rules, results, schemas


def _rows(ds):
    from plexora.server.utils.label_overlay import cell_ids

    frame = ds.table.geometry()
    schema = ds.schema
    ids, keep = cell_ids(frame, schema.cell_id if schema else None)
    return ids.astype(np.int64), keep


def _spread(values):
    """(median, robust spread) of the finite values: the MAD, else the IQR,
    else the standard deviation -- a marker with most cells at zero has no MAD."""
    finite = values[np.isfinite(values)]
    if not finite.size:
        return 0.0, 1e-6
    median = float(np.median(finite))
    spread = float(np.median(np.abs(finite - median))) * 1.4826
    if spread < 1e-6:
        q1, q3 = np.percentile(finite, [25, 75])
        spread = float(q3 - q1) / 1.349
    if spread < 1e-6:
        spread = float(np.std(finite))
    return median, max(spread, 1e-6)


def _rank_greater(inside, reference):
    """(one-sided p that `inside` is stochastically greater than `reference`,
    the probability of superiority: P(a cell inside > a cell around), ties
    halved -- 0.5 is no difference)."""
    if inside.size < 2 or reference.size < 2:
        return 1.0, 0.5
    from scipy.stats import mannwhitneyu

    test = mannwhitneyu(inside, reference, alternative="greater")
    return float(test.pvalue), float(test.statistic) / (inside.size * reference.size)


def _positions(ds, keep):
    frame = ds.table.geometry()
    schema = ds.schema
    try:
        xs = frame[schema.x].cast(pl.Float64, strict=False).to_numpy()[keep]
        ys = frame[schema.y].cast(pl.Float64, strict=False).to_numpy()[keep]
    except Exception:  # no coordinates: every comparison is image-wide
        return None, None
    return xs, ys


AREA = re.compile(r"^(area|cell_?area|cellarea|size)$", re.I)


def _area_column(ds):
    names = list(ds.table.metadata_columns) + list(ds.table.markers)
    return next((n for n in names if AREA.match(str(n))), None)


def _to_log(values, log_transformed):
    values = np.asarray(values, dtype=np.float64)
    return values if log_transformed else np.log1p(np.maximum(values, 0))


def _cell_diameter(ds):
    try:
        name = _area_column(ds)
        area = float(np.nanmedian(ds.table.columns([name])[name])) if name else None
    except Exception:
        area = None
    return 2 * np.sqrt(area / np.pi) if area and area > 0 else None


def _ring(geometry, xs, ys, cell_diameter):
    """(ring mask over the rows, ring width px) round a region, or (None, None)."""
    if geometry is None or xs is None:
        return None, None
    import shapely
    from shapely.geometry import shape as to_shape

    try:
        region = to_shape(geometry).buffer(0)
    except Exception:
        return None, None
    if region.is_empty:
        return None, None
    rules = schemas.MARKER_EVIDENCE
    width = max(float(rules["ring_cells"]) * float(cell_diameter or 6.4),
                float(rules["ring_share"]) * float(np.sqrt(region.area / np.pi)))
    outer = region.buffer(width)
    ring = np.asarray(shapely.contains_xy(outer, xs, ys), dtype=bool) \
        & ~np.asarray(shapely.contains_xy(region, xs, ys), dtype=bool)
    return ring, width


def signal_test(in_values, ref_values, marker, quantile):
    """Whether a signal-raising region is borne out by its cells' values, and
    which of them to flag: {borne_out, ... the numbers, why?, _flag: mask
    over `in_values`} (see `schemas.MARKER_EVIDENCE`)."""
    rules = schemas.MARKER_EVIDENCE
    n_in = int(in_values.size)
    ring_median = float(np.median(ref_values)) if ref_values.size else 0.0
    tail_at = float(np.quantile(ref_values, rules["tail_quantile"])) if ref_values.size else 0.0
    n_tail = int((in_values > tail_at).sum())
    p0 = float((ref_values > tail_at).mean()) if ref_values.size else 0.0
    p_tail = _binomial_excess(n_tail, n_in, p0)
    expected_tail = p0 * n_in
    tail_ratio = n_tail / expected_tail if expected_tail > 0 else (
        float("inf") if n_tail else 0.0)
    p_shift, superiority = _rank_greater(in_values, ref_values)
    above_median = int((in_values > ring_median).sum())
    shifted = p_shift <= rules["alpha"] and superiority >= rules["min_superiority"]
    tailed = p_tail <= rules["alpha"] and tail_ratio >= rules["min_tail_ratio"]
    # A flag is a claim about ONE cell, so it needs cells that stand out: the
    # tail, where the ring predicts how many would by chance (a 3x excess
    # keeps at least two flags in three real). A region only brighter as a
    # whole is reported (`shifted`) but flags no cell. Fixed at the tail
    # quantile, not the preset's, so the presets stay nested.
    borne = above_median >= rules["min_cells"] and tailed
    flag_at = float(np.quantile(ref_values, quantile)) if ref_values.size else np.inf
    chosen = (in_values > flag_at) if borne else np.zeros(n_in, dtype=bool)
    out = {"borne_out": bool(borne), "n_inside": n_in, "n_reference": int(ref_values.size),
           "inside_median": float(np.median(in_values)) if n_in else None,
           "reference_median": ring_median, "p_shift": p_shift, "superiority": superiority,
           "shifted": bool(shifted), "p_tail": p_tail, "tail_at": tail_at, "n_tail": n_tail,
           "expected_tail": round(expected_tail, 2),
           "tail_ratio": tail_ratio if np.isfinite(tail_ratio) else None,
           "tailed": bool(tailed), "flag_quantile": quantile,
           "flag_at": flag_at if np.isfinite(flag_at) else None, "_flag": chosen}
    if not borne:
        tail = (f"{n_tail} of its {n_in} cells above the surrounding cells' "
                f"{rules['tail_quantile']:.0%} where {expected_tail:.1f} are expected by chance")
        if tailed:
            out["why"] = (f"fewer than {rules['min_cells']} of its cells are brighter in "
                          f"{marker} than the surrounding cells' median")
        elif tail_ratio >= rules["min_tail_ratio"] and n_tail:
            out["why"] = (f"too few cells to tell: {tail} (p = {p_tail:.2g}; "
                          f"{rules['alpha']:g} needed)")
        elif shifted:
            out["why"] = (f"brighter in {marker} as a whole than the cells around it (a cell "
                          f"inside is brighter than one around {superiority:.0%} of the time)"
                          f", but no cell stands out: {tail}, {rules['min_tail_ratio']:g}x "
                          "needed")
        else:
            out["why"] = (f"the cells inside are not brighter in {marker} than the cells "
                          f"around them by a margin that matters (a cell inside is brighter "
                          f"than one around {superiority:.0%} of the time; {tail})")
    return out


def _binomial_excess(k, n, p0):
    """One-sided P(X >= k) for X ~ Binomial(n, p0)."""
    if n <= 0 or k <= 0:
        return 1.0
    if p0 <= 0.0:
        return 0.0
    from scipy.stats import binom

    return float(binom.sf(k - 1, n, min(p0, 1.0)))


#: Segmentation QC's per-cell column behind each segmentation reason.
SEG_COLUMNS = {"seg_under": "under_segmented", "seg_over": "over_segmented",
               "seg_small": "small", "seg_large": "large", "seg_irregular": "irregular"}
#: Which threshold of `segqc.thresholds` each reason was called at.
SEG_CUTOFFS = {"seg_under": "under", "seg_over": "over", "seg_small": "small",
               "seg_large": "large", "seg_irregular": "irregular"}


def seg_status(reason, table):
    """The status a segmentation reason gives a cell under `table`: merged
    and split cells are noted (Segmentation QC's own call, which a person
    reads before removing anything); a size outlier warns, or excludes where
    size alone may (`area.size_alone`); an irregular shape only warns --
    elongated cells are biology."""
    if reason in ("seg_under", "seg_over"):
        return "note"
    if reason in ("seg_small", "seg_large"):
        return "exclude" if float(table.get("area.size_alone") or 0) >= 1 else "warn"
    return "warn"


def _seg_flags(ids, seg, table):
    """{reason: (status, mask over `ids`)} and {reason: evidence} from
    `segqc.calls`'s (summary, frame, thresholds), aligned by cell id; a cell
    Segmentation QC did not score is flagged by nothing."""
    summary, frame, th = seg
    if frame is None or not frame.height or "cell_id" not in frame.columns:
        return {}, {}
    seg_ids = frame["cell_id"].cast(pl.Int64).to_numpy()
    order = np.argsort(seg_ids, kind="stable")
    sorted_ids = seg_ids[order]
    at = np.clip(np.searchsorted(sorted_ids, ids), 0, max(sorted_ids.size - 1, 0))
    found = (sorted_ids[at] == ids) if sorted_ids.size else np.zeros(ids.size, dtype=bool)
    rows = order[at]
    flags, evidence = {}, {}
    for reason, column in SEG_COLUMNS.items():
        if column not in frame.columns:
            continue
        values = frame[column].fill_null(False).cast(pl.Boolean).to_numpy()
        mask = np.zeros(ids.size, dtype=bool)
        mask[found] = values[rows[found]]
        status = seg_status(reason, table)
        flags[reason] = (status, mask)
        evidence[reason] = {"level": "cell", "tool": "segqc",
                            "tool_version": summary.get("version"),
                            "fingerprint": summary.get("fingerprint"),
                            "channels": [c for c in [summary.get("dna_channel")] if c],
                            "column": column, "status": status,
                            "cutoffs": {SEG_CUTOFFS[reason]: (th or {}).get(SEG_CUTOFFS[reason])}}
    return flags, evidence


def _aligned(ids, frame):
    """(found mask over `ids`, row of each in `frame`) by `cell_id`."""
    other = frame["cell_id"].cast(pl.Int64).to_numpy()
    order = np.argsort(other, kind="stable")
    sorted_ids = other[order]
    at = np.clip(np.searchsorted(sorted_ids, ids), 0, max(sorted_ids.size - 1, 0))
    found = (sorted_ids[at] == ids) if sorted_ids.size else np.zeros(ids.size, dtype=bool)
    return found, order[at]


def no_nucleus_status(table):
    """`no_nucleus` warns, and excludes where the preset says
    (`cells.no_nucleus_exclude`: strict)."""
    return "exclude" if float(table.get("cells.no_nucleus_exclude") or 0) >= 1 else "warn"


def _dna_flags(ids, dna, table, markers):
    """From DNA retention's (summary, frame): (no_nucleus (status, mask),
    evidence, [(marker, mask, record)] of `dna_loss`). A cell it did not
    score is flagged by nothing."""
    summary, frame = dna
    if frame is None or not frame.height or "cell_id" not in frame.columns:
        return None, None, []
    found, rows = _aligned(ids, frame)

    def column(name, fill, dtype):
        out = np.full(ids.size, fill, dtype=dtype)
        if name in frame.columns:
            values = frame[name].fill_null(fill).to_numpy().astype(dtype)
            out[found] = values[rows[found]]
        return out

    no_nucleus = column("no_nucleus", False, bool)
    has_nucleus = column("has_nucleus", False, bool)
    last_good = column("last_good_cycle", 0, np.int64)
    status = no_nucleus_status(table)
    common = {"tool": "dna_retention", "tool_version": summary.get("version"),
              "fingerprint": summary.get("fingerprint")}
    evidence = {"level": "cell", **common, "channels": [summary.get("reference")],
                "column": "no_nucleus", "status": status,
                "cutoffs": {"no_nucleus_at": summary.get("no_nucleus_at"),
                            "present_floor": (summary.get("params") or {}).get("present_floor")}}
    lost = []
    for cycle in summary.get("cycles") or []:
        index = int(cycle.get("index") or 0)
        mask = found & has_nucleus & (last_good < index)
        for channel in cycle.get("markers") or []:
            marker = class_rules.table_column(channel, markers)
            if marker is None:
                continue
            lost.append((marker, mask, {
                "class": None, "reason": "dna_loss", "channel": channel, "marker": marker,
                "status": "exclude", "channels": [marker, cycle.get("channel")],
                "test": "dna_retention", "cycle": index, "dna_channel": cycle.get("channel"),
                "reference": summary.get("reference"), **common,
                "retained_ratio": (summary.get("params") or {}).get("retained_ratio"),
                "borne_out": bool(mask.any()), "n_flagged": int(mask.sum())}))
    return (status, no_nucleus), evidence, lost


def derive(ds, result, table, *, pairs=None, geometries=None, seg=None, dna=None):
    """(cells DataFrame, pairs DataFrame, summary). `geometries` {roi_id:
    GeoJSON} are the regions as the ROI document holds them now (the user's
    edits, the traced outline); a region missing there is read from its
    candidate. `seg` is Segmentation QC's `calls` (summary, frame,
    thresholds), `dna` DNA retention's (summary, frame), or None."""
    ids, keep = _rows(ds)
    n = int(ids.size)
    cycles = result.get("cycles") or []
    segmentation = class_rules.segmentation_channel(ds, cycles)
    markers = list(ds.table.markers)
    exclude = {r: np.zeros(n, dtype=bool) for r in schemas.REASONS}
    warn = {r: np.zeros(n, dtype=bool) for r in schemas.REASONS}
    note = {r: np.zeros(n, dtype=bool) for r in schemas.REASONS}
    by_status = {"exclude": exclude, "warn": warn, "note": note}
    #: (marker, reason) -> {"exclude": mask, "warn": mask}
    flags = {}
    evidence = {}
    marker_evidence = []

    def flag(marker, reason, status, mask):
        entry = flags.setdefault((marker, reason), {"exclude": np.zeros(n, dtype=bool),
                                                    "warn": np.zeros(n, dtype=bool)})
        entry[status] |= mask

    # -- Segmentation QC's calls ------------------------------------------------
    if seg is not None:
        seg_flags, seg_evidence = _seg_flags(ids, seg, table)
        for reason, (status, mask) in seg_flags.items():
            by_status[status][reason] |= mask
            if mask.any():
                evidence[reason] = seg_evidence[reason]

    # -- DNA retention: no nucleus (cell), nucleus lost by a cycle (marker) ---------
    if dna is not None:
        no_nucleus, dna_evidence, lost = _dna_flags(ids, dna, table, markers)
        if no_nucleus is not None:
            status, mask = no_nucleus
            by_status[status]["no_nucleus"] |= mask
            if mask.any():
                evidence["no_nucleus"] = dna_evidence
        for marker, mask, record in lost:
            if mask.any():
                flag(marker, "dna_loss", "exclude", mask)
            marker_evidence.append(record)

    # -- the regions ----------------------------------------------------------
    roi_ids = {}             # row -> [roi id], only the rows in a region
    roi_method = np.full(n, None, dtype=object)
    if pairs is None:
        pairs = results.cell_rois(ds.name)
    from plexora.plugins.qc.server import roi_link

    # Every finding the cells follow: a QC ROI, or one finding of a
    # consolidated ROI (keyed `<roi id>#<n>`, `roi_link.membership_meta`).
    region_meta = roi_link.membership_meta(result)
    threshold = float(table["cells.roi_overlap_fraction"])
    index = {int(cid): i for i, cid in enumerate(ids.tolist())}
    members = {}             # roi_id -> [(row, fraction, method)]
    if pairs is not None and pairs.height:
        for cid, roi_id, fraction, method in pairs.select(
                ["cell_id", "roi_id", "fraction", "method"]).iter_rows():
            row = index.get(int(cid))
            if roi_id in region_meta and row is not None:
                members.setdefault(roi_id, []).append((row, float(fraction), method))

    def note_region(key, rows, method_of):
        roi_id = roi_link.parent_of(key)
        for row in rows:
            listed = roi_ids.setdefault(row, [])
            if roi_id not in listed:
                listed.append(roi_id)
            method = method_of[row]
            roi_method[row] = method if roi_method[row] in (None, method) else "mixed"

    levels = {}
    for roi_id, candidate in region_meta.items():
        klass = candidate.get("class") or "other_technical"
        levels[roi_id] = class_rules.region_level(klass, candidate.get("scope"),
                                                  candidate.get("channels") or [],
                                                  segmentation=segmentation)
    # Every (marker, region) of the marker level, for each marker's reference.
    marker_regions = {}
    for roi_id, (level, channels) in levels.items():
        if level != "marker":
            continue
        for channel in channels:
            column = class_rules.table_column(channel, markers)
            marker_regions.setdefault(column or channel, []).append(roi_id)
    needed = [m for m in marker_regions if m in markers]
    values_of = {}
    if needed:
        read = ds.table.columns(needed)
        for m in needed:
            if m in read:
                raw = np.asarray(read[m], dtype=np.float64)
                values_of[m] = _to_log(raw, ds.table.log_transformed)[keep]
    in_marker_region = {}
    for marker, rois in marker_regions.items():
        mask = np.zeros(n, dtype=bool)
        for roi_id in rois:
            for row, _f, _m in members.get(roi_id, ()):
                mask[row] = True
        in_marker_region[marker] = mask

    rules = schemas.MARKER_EVIDENCE
    xs, ys = _positions(ds, keep) if needed else (None, None)
    diameter = _cell_diameter(ds) if needed else None
    for roi_id, candidate in region_meta.items():
        action = candidate.get("action") or "exclude"
        if action not in schemas.CELL_STATUSES:
            continue
        klass = candidate.get("class") or "other_technical"
        reason = schemas.region_reason(klass)
        level, channels = levels[roi_id]
        entries = members.get(roi_id, [])
        method_of = {row: method for row, _f, method in entries}
        if level != "cell" and action == "note":
            continue  # a note is about the whole cell; no marker is noted
        if level == "cell":
            rows = [row for row, fraction, method in entries
                    if not (method == "mask" and fraction < threshold)]
            by_status[action][reason][rows] = True
            note_region(roi_id, rows, method_of)
            ev = evidence.setdefault(reason, {"level": "cell", "rois": [], "channels": []})
            if roi_link.parent_of(roi_id) not in ev["rois"]:
                ev["rois"].append(roi_link.parent_of(roi_id))
            ev["channels"] = list(dict.fromkeys([*ev["channels"],
                                                 *(candidate.get("channels") or [])]))
            continue
        raising = klass in schemas.SIGNAL_RAISING_CLASSES
        for channel in channels:
            marker = class_rules.table_column(channel, markers)
            record = {"roi_id": roi_link.parent_of(roi_id), "class": klass, "reason": reason,
                      "channel": channel,
                      "marker": marker, "status": action, "channels": [marker or channel],
                      "test": "signal" if raising else "overlap"}
            if marker is None:
                marker_evidence.append({**record, "borne_out": False, "n_flagged": 0,
                                        "why": "the table does not measure this channel"})
                continue
            if raising:
                # Any overlap: a speck on a tenth of a cell is most of its mean.
                rows = np.array([row for row, fraction, _m in entries if fraction > 0],
                                dtype=np.int64)
            else:
                rows = np.array([row for row, fraction, method in entries
                                 if not (method == "mask" and fraction < threshold)],
                                dtype=np.int64)
            if not raising:
                mask = np.zeros(n, dtype=bool)
                mask[rows] = True
                flag(marker, reason, action, mask)
                note_region(roi_id, rows.tolist(), method_of)
                marker_evidence.append({**record, "borne_out": True,
                                        "n_inside": int(rows.size),
                                        "n_flagged": int(rows.size)})
                continue
            values = values_of.get(marker)
            finite = np.isfinite(values)
            outside = finite & ~in_marker_region[marker]
            geometry = (geometries or {}).get(roi_id) or candidate.get("geometry")
            ring, ring_px = _ring(geometry, xs, ys, diameter)
            basis = "the cells around the region"
            reference = outside & ring if ring is not None else None
            if reference is None or int(reference.sum()) < rules["min_reference"]:
                reference = outside
                basis = "every cell outside this marker's regions"
            if int(reference.sum()) < rules["min_reference"]:
                reference, basis = finite, "every cell"
            inside = rows[finite[rows]] if rows.size else rows
            test = signal_test(values[inside], values[reference], marker,
                               float(table["cells.marker_quantile"]))
            chosen = inside[test.pop("_flag")]
            mask = np.zeros(n, dtype=bool)
            mask[chosen] = True
            if test["borne_out"]:
                flag(marker, reason, action, mask)
                note_region(roi_id, chosen.tolist(), method_of)
            marker_evidence.append({**record, **test, "reference": basis, "ring_px": ring_px,
                                    "n_flagged": int(mask.sum())})

    # -- channel-level verdicts ----------------------------------------------
    # A cycle out of register everywhere has no region: its channels'
    # markers are unreliable in every cell.
    for candidate in (result.get("candidates") or {}).values():
        user = candidate.get("user_state") or {}
        action = candidate.get("action") or "exclude"
        if not candidate.get("channel_level") or action not in ("exclude", "warn") \
                or user.get("deleted") or user.get("removed_from_qc"):
            continue
        klass = candidate.get("class") or "other_technical"
        reason = f"region:{klass}"
        for channel in candidate.get("channels") or []:
            marker = class_rules.table_column(channel, markers)
            record = {"candidate_id": candidate.get("id"), "class": klass, "reason": reason,
                      "channel": channel, "marker": marker, "status": action,
                      "channels": [marker or channel], "test": "channel_level",
                      "channel_level": True}
            if marker is None:
                marker_evidence.append({**record, "borne_out": False, "n_flagged": 0,
                                        "why": "the table does not measure this channel"})
                continue
            flag(marker, reason, action, np.ones(n, dtype=bool))
            marker_evidence.append({**record, "borne_out": True, "n_flagged": n})

    # -- the user's dismissals ------------------------------------------------
    # A finding the user judged wrong flags nothing; its evidence stays, marked.
    dismissed = [d for d in result.get("user_dismissed") or []
                 if d.get("finding") in ("cell_reason", "marker")]
    for entry in dismissed:
        reason = entry.get("reason")
        if entry["finding"] == "cell_reason" and reason in exclude:
            exclude[reason][:] = False
            warn[reason][:] = False
            note[reason][:] = False
            if reason in evidence:
                evidence[reason]["dismissed"] = {"by": entry.get("by"), "at": entry.get("at")}
        elif entry["finding"] == "marker":
            flags.pop((entry.get("marker"), reason), None)
            for ev in marker_evidence:
                if ev.get("marker") == entry.get("marker") and ev.get("reason") == reason:
                    ev["dismissed"] = {"by": entry.get("by"), "at": entry.get("at")}

    # -- the calls ------------------------------------------------------------
    no_nucleus = exclude["no_nucleus"] | warn["no_nucleus"] | note["no_nucleus"]
    # Outside the tissue, whatever the user made the background do.
    background = exclude["background"] | warn["background"] | note["background"]
    frame, failing, warned, by_marker, noted_any, flagged_any, unreliable_any = _cells_frame(
        ids, result["result_id"], exclude, warn, note, flags, roi_ids, roi_method, background)
    by_reason = {r: int(exclude[r].sum()) for r in schemas.REASONS if exclude[r].any()}
    warn_by_reason = {r: int(warn[r].sum()) for r in schemas.REASONS if warn[r].any()}
    only_noted = {r: note[r] & ~exclude[r] & ~warn[r] for r in schemas.REASONS}
    note_by_reason = {r: int(m.sum()) for r, m in only_noted.items() if m.any()}
    summary = {"n": n, "n_fail": int(failing.sum()), "n_warn": int((~failing & warned).sum()),
               "n_noted": int(noted_any.sum()),
               "n_background": int(background.sum()),
               "n_no_nucleus": int(no_nucleus.sum()),
               # Both reasons are listed on such a cell, not de-duplicated.
               "n_no_nucleus_in_background": int((no_nucleus & background).sum()),
               "by_reason": by_reason, "warn_by_reason": warn_by_reason,
               "note_by_reason": note_by_reason,
               "n_marker_flagged": int(flagged_any.sum()),
               "n_marker_unreliable": int(unreliable_any.sum()),
               "marker_flags": by_marker,
               "evidence": evidence, "marker_evidence": marker_evidence,
               "not_borne_out": [e for e in marker_evidence if e.get("borne_out") is False],
               "segmentation_channel": segmentation,
               "roi_overlap_fraction": threshold,
               "marker_quantile": float(table["cells.marker_quantile"]),
               "roi_method": sorted({m for m in roi_method.tolist() if m}),
               "dismissed": [{k: v for k, v in d.items()} for d in dismissed],
               "cells_version": schemas.CELLS_VERSION}
    return frame, pairs, summary


def _list_column(n, rows, values):
    """A List(Utf8) Series of `n` rows from flat pairs: `rows` ascending (a
    row's values in their order), `values` a Utf8 Series beside them. A row
    with no value is [] -- built in polars, not from n Python lists."""
    empty = pl.lit([], dtype=pl.List(pl.Utf8))
    flat = pl.DataFrame({"row": pl.Series(np.asarray(rows, dtype=np.int64), dtype=pl.Int64),
                         "v": values.cast(pl.Utf8)})
    grouped = flat.group_by("row", maintain_order=True).agg(pl.col("v"))
    full = pl.DataFrame({"row": pl.Series(np.arange(n, dtype=np.int64), dtype=pl.Int64)}).join(
        grouped, on="row", how="left", maintain_order="left")
    return full.select(pl.col("v").fill_null(empty))["v"]


def _listed(n, masks, labels):
    """(List(Utf8) Series, rows with any) where row i lists `labels[k]` for
    every mask k true at i, in the masks' order."""
    hits = [np.flatnonzero(mask) for mask in masks]
    if hits:
        rows = np.concatenate(hits)
        codes = np.concatenate([np.full(h.size, k, dtype=np.int64) for k, h in enumerate(hits)])
    else:
        rows = codes = np.zeros(0, dtype=np.int64)
    order = np.argsort(rows, kind="stable")  # by row; each row's labels in mask order
    rows, codes = rows[order], codes[order]
    values = (pl.Series(list(labels), dtype=pl.Utf8).gather(codes) if codes.size
              else pl.Series([], dtype=pl.Utf8))
    return _list_column(n, rows, values), rows


def _cells_frame(ids, result_id, exclude, warn, note, flags, roi_ids, roi_method, background):
    """The per-cell frame from the reasons' masks ({reason: bool[n]} by
    status), the marker flags ({(marker, reason): {status: bool[n]}}), the
    regions (`roi_ids` {row: [roi id]}, `roi_method`) and `background`.
    Returns (frame, failing, warned, by_marker, noted_any, flagged_any,
    unreliable_any). Column by column from flat arrays: lists of lists for
    200k cells were most of `derive`'s time."""
    n = int(ids.size)
    failing = np.zeros(n, dtype=bool)
    warned = np.zeros(n, dtype=bool)
    for reason in schemas.REASONS:
        failing |= exclude[reason]
        warned |= warn[reason]
    order = {r: i for i, r in enumerate(schemas.PRIMARY_ORDER)}
    ordered = sorted(schemas.REASONS, key=lambda r: order.get(r, 999))
    reasons, reason_rows = _listed(n, [exclude[r] | warn[r] | note[r] for r in ordered], ordered)
    # Noted only where nothing stronger flagged the cell for this reason.
    noted_by, noted_rows = _listed(n, [note[r] & ~exclude[r] & ~warn[r] for r in ordered],
                                   ordered)
    excluded_by, _rows = _listed(n, [exclude[r] for r in ordered], ordered)
    # A passing cell's primary reason stays "": its warnings are in `reasons`.
    primary = np.full(n, "", dtype=object)
    unset = np.ones(n, dtype=bool)
    for reason in ordered:
        hit = unset & exclude[reason]
        primary[hit] = reason
        unset &= ~hit
    by_marker = {}
    flag_labels, flag_masks = [], []
    excluded_markers = {}    # marker -> its cells with any excluding flag
    for (marker, reason), masks in sorted(flags.items()):
        for status in ("exclude", "warn"):
            mask = masks[status]
            count = int(np.count_nonzero(mask))
            if not count:
                continue
            by_marker.setdefault(marker, {}).setdefault(reason, {})[status] = count
            flag_labels.append(f"{marker}|{reason}|{status}")
            flag_masks.append(mask)
            if status == "exclude":
                prior = excluded_markers.get(marker)
                excluded_markers[marker] = mask.copy() if prior is None else prior | mask
    marker_flags, flag_rows = _listed(n, flag_masks, flag_labels)
    unreliable_names = sorted(excluded_markers)
    unreliable, unreliable_rows = _listed(n, [excluded_markers[m] for m in unreliable_names],
                                          unreliable_names)
    region_rows = sorted(roi_ids)
    region_values = [roi for row in region_rows for roi in roi_ids[row]]
    regions = _list_column(n, np.repeat(np.asarray(region_rows, dtype=np.int64),
                                        [len(roi_ids[row]) for row in region_rows]),
                           pl.Series(region_values, dtype=pl.Utf8))
    action = np.where(failing, "exclude", np.where(warned, "warn", "pass"))
    frame = pl.DataFrame({
        "cell_id": pl.Series(ids, dtype=pl.Int64),
        "pass": pl.Series(~failing, dtype=pl.Boolean),
        "action": pl.Series(action.tolist(), dtype=pl.Utf8),
        "primary_reason": pl.Series(primary.tolist(), dtype=pl.Utf8),
        "reasons": reasons,
        "reason_count": pl.Series(np.bincount(reason_rows, minlength=n).astype(np.int16),
                                  dtype=pl.Int16),
        "excluded_by": excluded_by,
        "noted_by": noted_by,
        "background": pl.Series(background, dtype=pl.Boolean),
        "unreliable_markers": unreliable,
        "marker_flags": marker_flags,
        "roi_ids": regions,
        "roi_method": pl.Series(roi_method.tolist(), dtype=pl.Utf8),
        "result_id": pl.repeat(result_id, n, dtype=pl.Utf8, eager=True),
    })

    def any_of(rows):
        out = np.zeros(n, dtype=bool)
        out[rows] = True
        return out

    return (frame, failing, warned, by_marker, any_of(noted_rows), any_of(flag_rows),
            any_of(unreliable_rows))


def _regions_and_pairs(ds, result):
    from plexora.plugins.qc.server import propagate, roi_link

    regions = roi_link.membership_regions(ds, result)
    area = None
    try:
        area_name = _area_column(ds)
        if area_name:
            values = ds.table.columns([area_name])[area_name]
            area = float(np.nanmedian(values))
    except Exception:
        area = None
    diameter = 2 * np.sqrt(area / np.pi) if area else None
    pairs, per_roi = propagate.propagate(ds, regions, median_diameter_px=diameter)
    return regions, pairs, per_roi


def segmentation_calls(session, project):
    """(Segmentation QC's `calls` or None, {state, fingerprint?}): "none"
    without a result, "stale" when the result is not the one the project's
    mask and DNA channel would give now (its calls are left out), else
    "current"."""
    from plexora.plugins.qc.server.segqc import run as segqc

    try:
        found = segqc.calls(project)
    except Exception:  # an unreadable store: no calls
        found = None
    if found is None:
        return None, {"state": "none"}
    fingerprint = found[0].get("fingerprint")
    try:
        stale = bool(segqc.public_status(session, project).get("stale"))
    except Exception:  # no mask to compare with: the stored calls stand
        stale = False
    if stale:
        return None, {"state": "stale", "fingerprint": fingerprint}
    return found, {"state": "current", "fingerprint": fingerprint}


def dna_calls(session, project):
    """(DNA retention's `calls` (summary, frame) or None, {state,
    fingerprint?}): "none", "stale" (measured on another mask or image: left
    out) or "current" -- as `segmentation_calls`."""
    from plexora.plugins.qc.server import dna_retention

    try:
        found = dna_retention.calls(project)
    except Exception:  # an unreadable store: no calls
        found = None
    if found is None:
        return None, {"state": "none"}
    fingerprint = found[0].get("fingerprint")
    try:
        stale = bool(dna_retention.public_status(session, project).get("stale"))
    except Exception:  # nothing to compare with: the stored calls stand
        stale = False
    if stale:
        return None, {"state": "stale", "fingerprint": fingerprint}
    return found, {"state": "current", "fingerprint": fingerprint}


def write_for_active(call, project, *, refresh_regions=True):
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
        preset = (document.get("strictness") or {}).get("preset") or "standard"
        custom = (document.get("strictness") or {}).get("thresholds")
        table = strictness.thresholds(preset, custom if preset == "custom" else None)
        pairs = None
        per_roi = None
        geometries = None
        if refresh_regions:
            regions, pairs, per_roi = _regions_and_pairs(ds, result)
        else:
            from plexora.plugins.qc.server import roi_link

            regions = roi_link.membership_regions(ds, result)
        geometries = {r["roi_id"]: r.get("geometry") for r in regions}
        seg, seg_state = segmentation_calls(session, project)
        dna, dna_state = dna_calls(session, project)
        frame, pairs, summary = derive(ds, result, table, pairs=pairs, geometries=geometries,
                                       seg=seg, dna=dna)
        summary["seg_flags"] = seg_state
        summary["dna_flags"] = dna_state
        summary["warnings"] = [
            "seg_flags_stale: Segmentation QC's result was made from another mask or DNA "
            "channel; its calls are left out until it is run again"] \
            if seg_state.get("state") == "stale" else []
        if dna_state.get("state") == "stale":
            summary["warnings"].append(
                "dna_flags_stale: DNA retention was measured on another mask or image; its "
                "calls are left out until it is run again")
        results.put_cells(project, frame, pairs)
        result.setdefault("cells", {}).update(summary)
        if per_roi is not None:
            result["cells"]["propagation"] = per_roi
        result["cells"]["table_name"] = ds.table.source_kind
        # What the calls were derived from: a region drawn since makes them
        # stale for anything that leaves QC failures out (server/exclusions.py).
        from plexora.plugins.qc.server.exclusions import roi_revision

        result["cells"]["roi_revision"] = roi_revision(project)
        results.put_result(document, result)
        results.save(project, document)
    return summary
