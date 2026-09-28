"""The cell-QC modules: which cells a number flags, and why.

Each module reads a few table columns (never the whole matrix), measures one
thing per cell, proposes cutoffs from robust statistics (median and MAD in the
right space), and -- once the agent has looked at the cells beside a cutoff --
turns the measurement into per-cell reasons. The agent's say is stored as a
relative adjustment per side (`offset_steps`, `veto`), not as a number, so the
cutoff of any preset is that preset's proposal moved the same way: Strict
flags everything Standard does, and Standard everything Lenient does.

    counterstain_intensity  the first nuclear channel: debris / lost nuclei
                            (low), clumps and over-segmented nuclei (high)
    segmentation_area       object area (+ shape, when the table has it)
    cycle_stability         last vs first nuclear cycle: cells lost or moved
    channel_outlier:<m>     a marker far outside every other cell, and only
                            excluded when the extremes cluster in space AND the
                            agent calls them an artifact

A module that cannot run here says why (`available`), and the session skips
it.
"""

from __future__ import annotations

import re

import numpy as np

from plexora.plugins.qc.server import schemas

VERSION = "1"

AREA = re.compile(r"^(area|cell_?area|cellarea|size)$", re.I)
NUCLEUS_AREA = re.compile(r"^(nucle(us|ar|i)_?area|nuc_?area)$", re.I)
ECC = re.compile(r"^eccentricity$", re.I)
SOLIDITY = re.compile(r"^solidity$", re.I)
SEG_CONF = re.compile(r"^(seg(mentation)?_?(conf(idence)?|score|prob(ability)?))$", re.I)

#: [cal] outlier modules per session, and the extremes a marker needs to get one.
MAX_OUTLIER_MARKERS = 6
MIN_EXTREMES = 10
#: [cal] clustering of extremes: the share of them in the busiest 1/64 tile,
#: over what an even spread puts there.
CLUSTER_ENRICHMENT = 5.0


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


def side_cutoffs(median, mad, k, *, offsets=None, step=None):
    """(low, high) at `k` MADs, each moved by its side's offset steps
    (positive = stricter: more flagged)."""
    step = schemas.ENGINE["offset_step_mad"] if step is None else step
    offsets = offsets or {}
    low = median - k * mad + float((offsets.get("low") or {}).get("offset_steps", 0)) * step * mad
    high = median + k * mad - float((offsets.get("high") or {}).get("offset_steps", 0)) \
        * step * mad
    return low, high


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
        low, _h = side_cutoffs(median, mad, _k(table, "counterstain.low_k"), offsets=decision)
        _l, high = side_cutoffs(median, mad, _k(table, "counterstain.high_k"),
                                offsets=decision)
        return {"low": low, "high": high, "median": median, "mad": mad, "space": "log1p"}

    def calls(self, meas, cutoffs, decision, table):
        values = meas["m_counterstain_log"]
        finite = np.isfinite(values)
        low = finite & (values < cutoffs["low"])
        high = finite & (values > cutoffs["high"])
        return _sided(decision, {"counterstain_low": ("low", low),
                                 "counterstain_high": ("high", high)})


class SegmentationArea:
    name = "segmentation_area"
    reasons = ("area_small", "area_large", "morphology")

    def available(self, ds, scan_meta):
        if _column(ds, AREA) is None:
            return False, "no area column in the table"
        return True, None

    def measure(self, ds, scan_meta):
        names = {"area": _column(ds, AREA), "nucleus": _column(ds, NUCLEUS_AREA),
                 "ecc": _column(ds, ECC), "solidity": _column(ds, SOLIDITY),
                 "seg": _column(ds, SEG_CONF)}
        read = _read(ds, [v for v in names.values() if v])
        area = read[names["area"]]
        out = {"m_area_log": np.log(np.maximum(area, 1e-6)), "_column": names["area"]}
        if names["nucleus"] in read:
            out["m_nuc_cell_ratio"] = read[names["nucleus"]] / np.maximum(area, 1e-6)
        if names["ecc"] in read:
            out["m_eccentricity"] = read[names["ecc"]]
        if names["solidity"] in read:
            out["m_solidity"] = read[names["solidity"]]
        if names["seg"] in read:
            out["m_seg_confidence"] = read[names["seg"]]
        return out

    def cutoffs(self, meas, table, decision=None):
        median, mad = _mad(meas["m_area_log"])
        low, high = side_cutoffs(median, mad, _k(table, "area.k"), offsets=decision)
        return {"low": low, "high": high, "median": median, "mad": mad, "space": "log"}

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
        if "m_eccentricity" in meas:
            shape |= np.nan_to_num(meas["m_eccentricity"]) > table["area.ecc_max"]
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
        return _sided(decision, {"area_small": ("low", small), "area_large": ("high", large),
                                 "morphology": (None, shape)})


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
        step = schemas.ENGINE["offset_step_mad"] * mad
        offsets = decision or {}
        low = median - spread + float((offsets.get("low") or {}).get("offset_steps", 0)) * step
        high = median + spread - float((offsets.get("high") or {}).get("offset_steps", 0)) \
            * step
        return {"low": low, "high": high, "median": median, "mad": mad, "space": "log10_ratio"}

    def calls(self, meas, cutoffs, decision, table):
        ratio = meas["m_cycle_log10_ratio"]
        finite = np.isfinite(ratio)
        return _sided(decision, {"cycle_loss": ("low", finite & (ratio < cutoffs["low"])),
                                 "cycle_gain": ("high", finite & (ratio > cutoffs["high"]))})


class ChannelOutlier:
    reasons = ("channel_outlier_bright", "channel_outlier_dim")

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
        median, mad = _mad(meas["m_outlier_log"])
        low, high = side_cutoffs(median, mad, float(table["outlier.k"]), offsets=decision)
        return {"low": low, "high": high, "median": median, "mad": mad, "space": "log1p"}

    def calls(self, meas, cutoffs, decision, table):
        values = meas["m_outlier_log"]
        finite = np.isfinite(values)
        bright = finite & (values > cutoffs["high"])
        dim = finite & (values < cutoffs["low"])
        clustered = bool((decision or {}).get("clustered"))
        artifact = bool((decision or {}).get("artifact"))
        exclude = {}
        warn = {"channel_outlier_bright": bright, "channel_outlier_dim": dim}
        # Scattered extremes are rare biology until shown otherwise: warn.
        if clustered and artifact:
            exclude["channel_outlier_bright"] = bright
            if table.get("outlier.dim_clustered_exclude"):
                exclude["channel_outlier_dim"] = dim
        return exclude, warn


def _sided(decision, reasons):
    """(exclude {reason: mask}, warn {reason: mask}): a side the agent vetoed
    (`not_artifact`) or could not judge warns only."""
    decision = decision or {}
    exclude, warn = {}, {}
    for reason, (side, mask) in reasons.items():
        vetoed = side is not None and (decision.get(side) or {}).get("veto")
        if vetoed or decision.get("manual_review"):
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
