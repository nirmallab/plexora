"""The cell-QC modules: which cells a number flags, and why.

Each module reads a few table columns (never the whole matrix), measures one
thing per cell, proposes cutoffs from robust statistics (median and MAD in the
right space), and -- once the agent has looked at the cells beside a cutoff --
turns the measurement into per-cell reasons. The agent's say is stored as a
relative adjustment per side (`offset_steps`, `veto`), not as a number, so the
cutoff of any preset is that preset's proposal moved the same way: Strict
flags everything Standard does, and Standard everything Lenient does.

    counterstain_intensity  the first nuclear channel. Low: debris, a lost or
                            out-of-plane nucleus (excludes). High: clumped
                            nuclei or saturation -- and dense chromatin, which
                            is biology -- so it only warns.
    segmentation_area       object area, and shape when the table has it:
                            solidity, nucleus-to-cell ratio, segmentation
                            confidence. A small object excludes only with a
                            shape that says it is a fragment (or on its size
                            alone under Strict), a large one only with a shape
                            that says merged; elongation (eccentricity) is
                            never a reason.
    cycle_stability         last vs first nuclear cycle. Loss: the cell was
                            lost or moved during cycling (excludes: its profile
                            is incomplete). Gain only warns.
    channel_outlier:<m>     marker values far above what the marker's own
                            POSITIVE cells reach -- measured against the
                            positives, so a real positive population is never
                            the outlier. A MARKER flag (`extreme_value`), set
                            only on cells the agent looked at and judged an
                            artifact; it never fails the cell.

EXCLUSION NEEDS A LOOK. A side excludes only when the agent was shown that
side's cells and judged them artifacts (accept, too_lenient, too_aggressive).
A side it was not shown, could not tell, or called biology only warns; so a
number alone never removes a cell.

A module that cannot run here says why (`available`), and the session skips
it.
"""

from __future__ import annotations

import re

import numpy as np

from plexora.plugins.qc.server import schemas

VERSION = "2"

AREA = re.compile(r"^(area|cell_?area|cellarea|size)$", re.I)
NUCLEUS_AREA = re.compile(r"^(nucle(us|ar|i)_?area|nuc_?area)$", re.I)
SOLIDITY = re.compile(r"^solidity$", re.I)
SEG_CONF = re.compile(r"^(seg(mentation)?_?(conf(idence)?|score|prob(ability)?))$", re.I)

#: [cal] outlier modules per session, and the extremes a marker needs to get one.
MAX_OUTLIER_MARKERS = 6
MIN_EXTREMES = 10
#: [cal] clustering of extremes: the share of them in the busiest 1/64 tile,
#: over what an even spread puts there. Shown to the agent as context; it is
#: NOT evidence of an artifact (real positive cells cluster too).
CLUSTER_ENRICHMENT = 5.0
#: [cal] a marker's positive cells: above the image's median by this many
#: MADs; fewer than MIN_POSITIVE of them and the marker has no positive
#: population to measure "far brighter" against, so no outlier is proposed.
POSITIVE_K = 3.0
MIN_POSITIVE = 50

#: A side's verdict that came from looking at its cells and judging them
#: artifacts: the only kind that lets the side exclude.
JUDGED_ARTIFACT = ("accept", "too_lenient", "too_aggressive")


def _mad(values):
    finite = values[np.isfinite(values)]
    if finite.size < 3:
        return float(np.nanmedian(values)) if finite.size else 0.0, 0.0
    median = float(np.median(finite))
    mad = float(np.median(np.abs(finite - median))) * 1.4826
    return median, max(mad, 1e-6)


def _to_log(values, log_transformed):
    values = np.asarray(values, dtype=np.float64)
    return values if log_transformed else np.log1p(np.maximum(values, 0))


def _raw(values, log_transformed):
    values = np.asarray(values, dtype=np.float64)
    return np.expm1(values) if log_transformed else values


def _column(ds, pattern):
    names = list(ds.table.metadata_columns) + list(ds.table.markers)
    return next((n for n in names if pattern.match(str(n))), None)


def _nuclear_columns(ds, cycles):
    """(first nuclear column, last nuclear column) the table has."""
    from plexora.agent import presets
    from plexora.plugins.qc.server.cycles import is_nuclear

    markers = list(ds.table.markers)
    ordered = []
    for cycle in (cycles or {}).get("cycles") or []:
        if cycle.get("nuclear") in markers:
            ordered.append(cycle["nuclear"])
    if not ordered:
        ordered = [m for m in markers if is_nuclear(m)]
    if not ordered:
        first = presets.nuclear_channel(markers)
        ordered = [first] if first else []
    if not ordered:
        return None, None
    return ordered[0], ordered[-1] if len(ordered) > 1 else None


def _read(ds, names):
    columns = ds.table.columns([n for n in names if n])
    return {n: np.asarray(columns[n], dtype=np.float64) for n in names if n in columns}


# -- proposals ------------------------------------------------------------------------


def step_size(distance, mad):
    """How far one `offset_steps` moves a cutoff that sits `distance` from the
    median: at least `offset_step_mad` MADs, and at least `offset_step_share`
    of the distance itself. A step is meant to change what the agent sees in
    one round -- a cutoff held out by a floor (cycle stability's
    `cycle.abs_floor`) or far out at a high k (channel outliers) barely moved
    by a MAD fraction, and a correction then cost several looks for nothing."""
    engine = schemas.ENGINE
    return max(float(engine["offset_step_mad"]) * mad,
               float(engine["offset_step_share"]) * abs(float(distance)))


def moved_cutoff(median, distance, mad, steps, side):
    """(cutoff, step) of one side `distance` from the median, moved `steps`
    toward it (positive = stricter: more flagged) -- never closer to the
    median than `offset_min_keep` of the proposed distance. Monotone in
    `distance`, so a stricter preset (smaller distance) stays at least as
    strict after the same moves (`offset_step_share * max steps < 1`)."""
    step = step_size(distance, mad)
    kept = max(float(distance) - float(steps) * step,
               float(schemas.ENGINE["offset_min_keep"]) * float(distance))
    return (median - kept if side == "low" else median + kept), step


def _steps(offsets, side):
    return float(((offsets or {}).get(side) or {}).get("offset_steps", 0))


def side_cutoffs(median, mad, k, *, offsets=None, k_high=None):
    """(low, high, {low: step, high: step}) at `k` MADs (`k_high` for the
    high side when it differs), each moved by its side's offset steps."""
    k_high = k if k_high is None else k_high
    low, low_step = moved_cutoff(median, k * mad, mad, _steps(offsets, "low"), "low")
    high, high_step = moved_cutoff(median, k_high * mad, mad, _steps(offsets, "high"), "high")
    return low, high, {"low": low_step, "high": high_step}


def _k(table, key, kind="k"):
    return float(table[key])


class Counterstain:
    name = "counterstain_intensity"
    reasons = ("counterstain_low", "counterstain_high")

    def available(self, ds, scan_meta):
        first, _last = _nuclear_columns(ds, (scan_meta or {}).get("cycles"))
        if first is None:
            return False, "no nuclear marker column in the table"
        return True, None

    def measure(self, ds, scan_meta):
        first, _last = _nuclear_columns(ds, (scan_meta or {}).get("cycles"))
        values = _read(ds, [first])[first]
        logged = _to_log(values, ds.table.log_transformed)
        return {"m_counterstain_log": logged, "_column": first}

    def cutoffs(self, meas, table, decision=None):
        values = meas["m_counterstain_log"]
        median, mad = _mad(values)
        low, high, step = side_cutoffs(median, mad, _k(table, "counterstain.low_k"),
                                       k_high=_k(table, "counterstain.high_k"),
                                       offsets=decision)
        return {"low": low, "high": high, "median": median, "mad": mad, "step": step,
                "space": "log1p"}

    def calls(self, meas, cutoffs, decision, table):
        values = meas["m_counterstain_log"]
        finite = np.isfinite(values)
        low = finite & (values < cutoffs["low"])
        high = finite & (values > cutoffs["high"])
        return _sided(decision, {"counterstain_low": ("low", low),
                                 "counterstain_high": ("high", high)},
                      warn_only=("counterstain_high",))


class SegmentationArea:
    name = "segmentation_area"
    reasons = ("area_small", "area_large", "morphology")

    def available(self, ds, scan_meta):
        if _column(ds, AREA) is None:
            return False, "no area column in the table"
        return True, None

    def measure(self, ds, scan_meta):
        names = {"area": _column(ds, AREA), "nucleus": _column(ds, NUCLEUS_AREA),
                 "solidity": _column(ds, SOLIDITY), "seg": _column(ds, SEG_CONF)}
        read = _read(ds, [v for v in names.values() if v])
        area = read[names["area"]]
        out = {"m_area_log": np.log(np.maximum(area, 1e-6)), "_column": names["area"]}
        if names["nucleus"] in read:
            out["m_nuc_cell_ratio"] = read[names["nucleus"]] / np.maximum(area, 1e-6)
        if names["solidity"] in read:
            out["m_solidity"] = read[names["solidity"]]
        if names["seg"] in read:
            out["m_seg_confidence"] = read[names["seg"]]
        return out

    def cutoffs(self, meas, table, decision=None):
        median, mad = _mad(meas["m_area_log"])
        low, high, step = side_cutoffs(median, mad, _k(table, "area.k"), offsets=decision)
        return {"low": low, "high": high, "median": median, "mad": mad, "step": step,
                "space": "log"}

    def calls(self, meas, cutoffs, decision, table):
        area = meas["m_area_log"]
        finite = np.isfinite(area)
        small = finite & (area < cutoffs["low"])
        large = finite & (area > cutoffs["high"])
        shape = np.zeros_like(finite)
        if "m_nuc_cell_ratio" in meas:
            ratio = meas["m_nuc_cell_ratio"]
            shape |= np.isfinite(ratio) & ((ratio < table["area.ratio_low"])
                                           | (ratio > table["area.ratio_high"]))
        if "m_solidity" in meas:
            solidity = meas["m_solidity"]
            shape |= np.isfinite(solidity) & (solidity < table["area.solidity_min"])
        if "m_seg_confidence" in meas:
            seg = meas["m_seg_confidence"]
            shape |= np.isfinite(seg) & (seg < table["area.seg_conf_min"])
        # A small object is only a segmentation error on its size alone under
        # a preset that says so: small cells are also real biology.
        if not table.get("area.size_alone"):
            small = small & shape
        # A large object is a merge when its shape says so; large alone is a
        # big cell until shown otherwise (macrophages, tumour cells), so warns.
        exclude, warn = _sided(decision, {"area_small": ("low", small),
                                          "area_large": ("high", large & shape)})
        warn["area_large"] = warn.get("area_large", np.zeros_like(large)) | (large & ~shape)
        # Shape on its own was never shown to the agent: it warns.
        warn["morphology"] = shape & ~small & ~large
        return exclude, warn


class CycleStability:
    name = "cycle_stability"
    reasons = ("cycle_loss", "cycle_gain")

    def available(self, ds, scan_meta):
        first, last = _nuclear_columns(ds, (scan_meta or {}).get("cycles"))
        if first is None or last is None:
            return False, "the table has fewer than two nuclear cycles"
        return True, None

    def measure(self, ds, scan_meta):
        first, last = _nuclear_columns(ds, (scan_meta or {}).get("cycles"))
        read = _read(ds, [first, last])
        a = _raw(read[first], ds.table.log_transformed)
        b = _raw(read[last], ds.table.log_transformed)
        ratio = np.log10((np.maximum(b, 0) + 1) / (np.maximum(a, 0) + 1))
        return {"m_cycle_log10_ratio": ratio, "_column": f"{last}/{first}",
                "_columns": [first, last]}

    def cutoffs(self, meas, table, decision=None):
        median, mad = _mad(meas["m_cycle_log10_ratio"])
        spread = max(float(table["cycle.abs_floor"]), float(table["cycle.k"]) * mad)
        low, low_step = moved_cutoff(median, spread, mad, _steps(decision, "low"), "low")
        high, high_step = moved_cutoff(median, spread, mad, _steps(decision, "high"), "high")
        return {"low": low, "high": high, "median": median, "mad": mad,
                "step": {"low": low_step, "high": high_step}, "space": "log10_ratio"}

    def calls(self, meas, cutoffs, decision, table):
        ratio = meas["m_cycle_log10_ratio"]
        finite = np.isfinite(ratio)
        return _sided(decision, {"cycle_loss": ("low", finite & (ratio < cutoffs["low"])),
                                 "cycle_gain": ("high", finite & (ratio > cutoffs["high"]))},
                      warn_only=("cycle_gain",))


class ChannelOutlier:
    """A marker's values beyond what its own positive cells reach."""

    reasons = ()
    marker_reasons = ("extreme_value",)

    def __init__(self, marker):
        self.marker = marker
        self.name = f"channel_outlier:{marker}"

    def available(self, ds, scan_meta):
        if self.marker not in ds.table.markers:
            return False, f"{self.marker!r} is not a column of the table"
        return True, None

    def measure(self, ds, scan_meta):
        values = _read(ds, [self.marker])[self.marker]
        return {"m_outlier_log": _to_log(values, ds.table.log_transformed),
                "_column": self.marker}

    def cutoffs(self, meas, table, decision=None):
        """The high cutoff `outlier.k` MADs above the POSITIVE cells' median
        (cells `POSITIVE_K` MADs above the image's median). Without a positive
        population there is nothing to be far beyond: no cutoff (inf)."""
        values = meas["m_outlier_log"]
        finite = values[np.isfinite(values)]
        median, mad = _mad(finite)
        positive = finite[finite > median + POSITIVE_K * mad]
        if positive.size < MIN_POSITIVE:
            return {"low": -np.inf, "high": np.inf, "median": median, "mad": mad,
                    "step": {"low": 0.0, "high": 0.0}, "space": "log1p",
                    "reference": "none", "n_positive": int(positive.size),
                    "why": f"fewer than {MIN_POSITIVE} positive cells to measure against"}
        p_median, p_mad = _mad(positive)
        _low, high, step = side_cutoffs(p_median, p_mad, float(table["outlier.k"]),
                                        offsets=decision)
        return {"low": -np.inf, "high": high, "median": p_median, "mad": p_mad, "step": step,
                "space": "log1p", "reference": "positive cells",
                "n_positive": int(positive.size), "image_median": median, "image_mad": mad}

    def extremes(self, meas, cutoffs):
        values = meas["m_outlier_log"]
        return np.isfinite(values) & (values > cutoffs["high"])

    def calls(self, meas, cutoffs, decision, table):
        """No cell reason: an extreme value is the marker's problem."""
        return {}, {}

    def marker_calls(self, meas, cutoffs, decision, table):
        """{reason: mask} of this marker's flags: the extremes, when the agent
        was shown them and judged them an artifact; nothing otherwise."""
        if not _judged(decision, "high") or (decision or {}).get("manual_review"):
            return {}
        return {"extreme_value": self.extremes(meas, cutoffs)}


def public(cutoffs):
    """A cutoff table as JSON can hold it: an absent side (inf) is None."""
    out = {}
    for key, value in (cutoffs or {}).items():
        if isinstance(value, dict):
            out[key] = public(value)
        elif isinstance(value, (float, np.floating)):
            out[key] = float(value) if np.isfinite(value) else None
        elif isinstance(value, np.integer):
            out[key] = int(value)
        else:
            out[key] = value
    return out


def value_key(meas):
    """The measurement a module's cutoffs are drawn on."""
    return next(k for k in meas if k.startswith("m_"))


def sides_of(name):
    return ("high",) if name.startswith("channel_outlier:") else ("low", "high")


def beyond_and_near(values, cutoffs, side):
    """(cells beyond the cutoff, cells within half a step inside it) of one
    side: what a look at the cells beside that cutoff would show."""
    finite = np.isfinite(values)
    cut = float(cutoffs[side])
    half = 0.5 * float((cutoffs.get("step") or {}).get(side) or 0.0)
    if side == "low":
        beyond = finite & (values < cut)
        near = finite & (values >= cut) & (values < cut + half)
    else:
        beyond = finite & (values > cut)
        near = finite & (values <= cut) & (values > cut - half)
    return int(beyond.sum()), int(near.sum())


def _judged(decision, side):
    """Whether `side` was shown to the agent and judged an artifact."""
    entry = (decision or {}).get(side) or {}
    return entry.get("verdict") in JUDGED_ARTIFACT and not entry.get("veto")


def _sided(decision, reasons, *, warn_only=()):
    """(exclude {reason: mask}, warn {reason: mask}). A side excludes only
    when the agent looked at it and judged it an artifact; a side it vetoed
    (`not_artifact`), could not tell, was not shown, or a reason in
    `warn_only`, warns."""
    decision = decision or {}
    exclude, warn = {}, {}
    for reason, (side, mask) in reasons.items():
        if reason in warn_only or decision.get("manual_review") or not _judged(decision, side):
            warn[reason] = mask
        else:
            exclude[reason] = mask
    return exclude, warn


def module(name):
    if name.startswith("channel_outlier:"):
        return ChannelOutlier(name.split(":", 1)[1])
    return {"counterstain_intensity": Counterstain(), "segmentation_area": SegmentationArea(),
            "cycle_stability": CycleStability()}[name]


def clustered(xs, ys, mask, *, tiles=8):
    """(clustered, enrichment, box) of the flagged cells' positions."""
    count = int(mask.sum())
    if count < MIN_EXTREMES:
        return False, 0.0, None
    x, y = xs[mask], ys[mask]
    x0, x1 = float(np.nanmin(xs)), float(np.nanmax(xs)) + 1e-6
    y0, y1 = float(np.nanmin(ys)), float(np.nanmax(ys)) + 1e-6
    ix = np.clip(((x - x0) / (x1 - x0) * tiles).astype(int), 0, tiles - 1)
    iy = np.clip(((y - y0) / (y1 - y0) * tiles).astype(int), 0, tiles - 1)
    counts = np.bincount(iy * tiles + ix, minlength=tiles * tiles)
    top = int(np.argmax(counts))
    enrichment = counts[top] / max(1e-9, count / (tiles * tiles))
    ty, tx = divmod(top, tiles)
    box = [x0 + tx * (x1 - x0) / tiles, y0 + ty * (y1 - y0) / tiles,
           x0 + (tx + 1) * (x1 - x0) / tiles, y0 + (ty + 1) * (y1 - y0) / tiles]
    return enrichment >= CLUSTER_ENRICHMENT and counts[top] >= MIN_EXTREMES, float(enrichment), box


def planned(session, project) -> list:
    """The modules a session plans for a project with a table (availability
    is checked in the bulk pass, which says why a module was skipped)."""
    record = session.project(project)
    if not record.has_table:
        return []
    names = ["counterstain_intensity", "segmentation_area", "cycle_stability"]
    try:
        ds = session.data(project)
        from plexora.plugins.qc.server.cycles import is_nuclear

        markers = [m for m in ds.table.markers if not is_nuclear(m)]
    except Exception:
        markers = []
    names += [f"channel_outlier:{m}" for m in markers]
    return names
