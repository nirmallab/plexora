"""The cell-QC modules: Segmentation QC's scores, read per cell.

Each module joins one Segmentation QC score onto the table's cells, proposes
cutoffs from that check's own flag (and `OUTLIER_Z` robust SDs), and -- once
the agent has looked at the cells beside a cutoff -- turns the score into
per-cell reasons. The agent's say is stored as a relative adjustment per side
(`offset_steps` of `seg_step_score` / `seg_step_z`, `veto`), not as a number.

    seg_under, seg_over     the mask read against the DNA stain: one label
                            holding two nuclei, one nucleus cut across labels.
                            Exclude after a look.
    seg_size                the mask's own size outliers (robust z on log
                            area). Small excludes only where size alone is
                            allowed to (`area.size_alone`); large only where
                            the under-segmentation score says merged.
    seg_shape               the mask's own roundness outliers: only warns.

Everything else a table could say about a cell is said by the image checks'
regions instead: a cell lost or moved between cycles is the Registration
Check's, a dim or empty object Segmentation QC's, an artifact-bright value
the Artifact Detector's and the scan's aggregate and saturation detectors'.
Without Segmentation QC in the session there are no cell modules.

EXCLUSION NEEDS A LOOK. A side excludes only when the agent was shown that
side's cells and judged them artifacts (accept, too_lenient, too_aggressive).
A side it was not shown, could not tell, or called biology only warns; so a
number alone never removes a cell.

A module that cannot run here says why (`available`), and the session skips
it.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.qc.server import schemas

VERSION = "3"

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


# -- proposals ------------------------------------------------------------------------


def _steps(offsets, side):
    return float(((offsets or {}).get(side) or {}).get("offset_steps", 0))


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
    if name in ("seg_under", "seg_over"):
        return ("high",)
    if name == "seg_shape":
        return ("low",)
    return ("low", "high")


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


def _sided(decision, reasons):
    """(exclude {reason: mask}, warn {reason: mask}). A side excludes only
    when the agent looked at it and judged it an artifact; a side it vetoed
    (`not_artifact`), could not tell, or was not shown, warns."""
    decision = decision or {}
    exclude, warn = {}, {}
    for reason, (side, mask) in reasons.items():
        if decision.get("manual_review") or not _judged(decision, side):
            warn[reason] = mask
        else:
            exclude[reason] = mask
    return exclude, warn


class _SegModule:
    """A Segmentation QC score per cell, joined on the table's cell ids."""

    name = ""
    reasons = ()
    key = ""
    sign = 1.0

    def available(self, ds, scan_meta):
        from plexora.plugins.qc.server.segqc import run as segqc

        summary = segqc.current(ds.name)
        if summary is None:
            return False, "Segmentation QC has not run on this mask"
        if segqc._scored(ds.name, summary.get("fingerprint")) is None:
            return False, "this Segmentation QC result has no cell shapes: run it again"
        return True, None

    def measure(self, ds, scan_meta):
        joined = _seg_joined(ds)
        values = joined["arrays"][self.key] * self.sign
        out = {f"m_{self.name}": values, "_column": joined["dna"],
               "_fingerprint": joined["fingerprint"], "_d_nucleus_um": joined["d_nucleus_um"],
               "_flag": joined["flag"]}
        if self.name == "seg_size":
            out["_under"] = joined["arrays"]["under"]
        return out

    def _cut(self, auto, decision, side, kind):
        from plexora.plugins.qc.server.segqc import run as segqc

        step = float(schemas.ENGINE["seg_step_score" if kind == "score" else "seg_step_z"])
        lo, hi = segqc.FLAG_RANGE if kind == "score" else segqc.Z_RANGE
        value = float(np.clip(auto - _steps(decision, side) * step, lo, hi))
        return value, step


class SegUnder(_SegModule):
    name = "seg_under"
    reasons = ("seg_under",)
    key = "under"

    def cutoffs(self, meas, table, decision=None):
        values = meas[f"m_{self.name}"]
        median, mad = _mad(values)
        high, step = self._cut(float(meas["_flag"]), decision, "high", "score")
        return {"low": -np.inf, "high": high, "median": median, "mad": mad,
                "step": {"low": 0.0, "high": step}, "space": "score"}

    def calls(self, meas, cutoffs, decision, table):
        values = meas[f"m_{self.name}"]
        mask = np.isfinite(values) & (values >= cutoffs["high"])
        return _sided(decision, {self.name: ("high", mask)})


class SegOver(SegUnder):
    name = "seg_over"
    reasons = ("seg_over",)
    key = "over"


class SegSize(_SegModule):
    name = "seg_size"
    reasons = ("seg_small", "seg_large")
    key = "area_z"

    def cutoffs(self, meas, table, decision=None):
        from plexora.plugins.qc.server.segqc import run as segqc

        values = meas[f"m_{self.name}"]
        median, mad = _mad(values)
        low, low_step = self._cut(segqc.OUTLIER_Z, decision, "low", "z")
        high, high_step = self._cut(segqc.OUTLIER_Z, decision, "high", "z")
        return {"low": -low, "high": high, "median": median, "mad": mad,
                "step": {"low": low_step, "high": high_step}, "space": "robust_z"}

    def calls(self, meas, cutoffs, decision, table):
        z = meas[f"m_{self.name}"]
        finite = np.isfinite(z)
        small = finite & (z <= cutoffs["low"])
        large = finite & (z >= cutoffs["high"])
        # A small cell is a fragment on its size alone only under a preset
        # that says so; a large one is a merge only where the DNA says two
        # nuclei (the under-segmentation score) -- a big cell is otherwise a
        # big cell (macrophages, tumour cells).
        alone = bool(table.get("area.size_alone"))
        under = np.nan_to_num(meas.get("_under", np.zeros_like(z)), nan=0.0)
        merged = under >= float(meas["_flag"])
        fragments = small if alone else np.zeros_like(small)
        exclude, warn = _sided(decision, {"seg_small": ("low", fragments),
                                          "seg_large": ("high", large & merged)})
        warn["seg_small"] = warn.get("seg_small", np.zeros_like(small)) | (small & ~fragments)
        warn["seg_large"] = warn.get("seg_large", np.zeros_like(large)) | (large & ~merged)
        return exclude, warn


class SegShape(_SegModule):
    name = "seg_shape"
    reasons = ("seg_irregular",)
    key = "circ_z"

    def cutoffs(self, meas, table, decision=None):
        from plexora.plugins.qc.server.segqc import run as segqc

        values = meas[f"m_{self.name}"]
        median, mad = _mad(values)
        low, step = self._cut(segqc.OUTLIER_Z, decision, "low", "z")
        return {"low": -low, "high": np.inf, "median": median, "mad": mad,
                "step": {"low": step, "high": 0.0}, "space": "robust_z"}

    def calls(self, meas, cutoffs, decision, table):
        z = meas[f"m_{self.name}"]
        # Elongated and ragged cells are biology too often: shape only warns.
        return {}, {"seg_irregular": np.isfinite(z) & (z <= cutoffs["low"])}


SEG_MODULES = ("seg_under", "seg_over", "seg_size", "seg_shape")
_SEG_CACHE: dict = {}


def _seg_joined(ds) -> dict:
    """Segmentation QC's per-cell arrays in the table's row order (NaN for a
    row whose cell the mask does not hold), cached per result."""
    from plexora.plugins.qc.server.segqc import run as segqc
    from plexora.server.utils.label_overlay import cell_ids

    summary = segqc.current(ds.name)
    fp = summary.get("fingerprint")
    key = (ds.name, fp)
    if key in _SEG_CACHE:
        return _SEG_CACHE[key]
    scored = segqc._scored(ds.name, fp)
    frame = segqc.frame(ds.name, fp)
    seg_ids = frame["cell_id"].to_numpy().astype(np.int64)
    geometry = ds.table.geometry()
    ids, keep = cell_ids(geometry, ds.schema.cell_id if ds.schema else None)
    rows = np.full(len(keep), -1, dtype=np.int64)
    order = np.argsort(seg_ids, kind="stable")
    wanted = np.asarray(ids, dtype=np.int64)
    at = np.searchsorted(seg_ids[order], wanted)
    at = np.clip(at, 0, max(0, order.size - 1))
    hit = order.size > 0
    found = (seg_ids[order][at] == wanted) if hit else np.zeros(wanted.shape, dtype=bool)
    rows_keep = np.where(found, order[at], -1)
    rows[np.flatnonzero(keep)] = rows_keep

    def spread(values):
        out = np.full(len(keep), np.nan)
        ok = rows >= 0
        out[ok] = np.asarray(values, dtype=np.float64)[rows[ok]]
        return out

    params = {**segqc.PARAMS_DEFAULT, **(summary.get("params") or {})}
    joined = {"fingerprint": fp, "dna": summary.get("dna_channel"),
              "d_nucleus_um": summary.get("d_nucleus_um"), "flag": float(params["flag"]),
              "arrays": {name: spread(scored[name])
                         for name in ("under", "over", "area_z", "circ_z")},
              "matched": int((rows >= 0).sum())}
    _SEG_CACHE.clear()
    _SEG_CACHE[key] = joined
    return joined


#: Modules a session no longer plans. A result saved before they were retired
#: still names them; its cells are derived without them.
RETIRED = ("counterstain_intensity", "segmentation_area", "cycle_stability")


def module(name):
    """The module called `name`, or None for a retired one (see `RETIRED`)."""
    if name in RETIRED or name.startswith("channel_outlier:"):
        return None
    return {"seg_under": SegUnder(), "seg_over": SegOver(), "seg_size": SegSize(),
            "seg_shape": SegShape()}[name]


def planned(session, project, *, segmentation=False) -> list:
    """The modules a session plans for a project with a table: Segmentation
    QC's four when it is in the session (`segmentation`), none otherwise
    (availability is checked in the bulk pass, which says why a module was
    skipped)."""
    record = session.project(project)
    if not record.has_table or not segmentation:
        return []
    return list(SEG_MODULES)
