"""The image checks' scores as one shape: a grid of continuous values.

Blur QC scores every 40 um tile, Registration Check the share of nuclear
pixels that disagree every ~6.5 um, Segmentation QC every cell -- and the
share of cells it flags is a map too. Whatever the check, what QC does with
the scores is the same, so it is written once here:

- `ScoreField` -- the values (NaN where nothing could be measured), the
  tissue each grid cell holds (the denominator), the grid in full-resolution
  pixels, the automatic threshold, and what the score is called;
- `distribution` -- the median, the MAD, quantiles and a histogram: what a
  bar is judged against, never one number alone;
- `step_of`, `threshold_at` -- how far one step moves a bar (the larger of a
  MAD and the check's floor) and the bar `offset_steps` from the automatic
  one. A positive step is TIGHTER: the bar comes down and more is flagged;
- `regions` -- the grid cells at or above a bar, 8-connected, largest first,
  each a GeoJSON outline of its own cells (so a region is as snug as its
  check's grid: 40 um for blur, ~6.5 um for registration, a cell and a half
  for segmentation), with the flagged share stated over its denominator;
- `sample_strata` -- a few places from each part of the distribution
  (`schemas.SCORE_STRATA`): clearly fine, just below and just above the bar,
  far above it, and the peaks of the largest flagged regions. Deterministic:
  the same field, bar and seed give the same places. A place is sampled
  only where the field's `content` (Blur QC: the channel's own nuclear
  share of the cell; registration: the nuclear share both cycles hold) is
  at least `sample_floor` -- a nucleus-free place shows nothing to judge.
  A region's peak (where its confirm crop goes) is its strongest place with
  that content too;
- `one_cycle_fields` -- the Registration Check's other half: places where
  one cycle has nuclei and the other (almost) none, a tissue-loss signal
  kept out of the registration score and turned into its own candidates.

Pure numpy / scipy / shapely; no pixel is read here.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

import numpy as np

from plexora.plugins.qc.server import schemas

VERSION = "2"
HISTOGRAM_BINS = 20
MAX_REGIONS = 200
#: The bars a check's threshold may sit between (a score of 0..1).
RANGES = {"blur": (0.05, 0.95), "registration": (0.02, 0.95), "segmentation": (0.05, 0.95)}
#: Segmentation QC's cluster map: its automatic bar is the image's own
#: median + 3 MAD of the flagged share, never under a third of the cells.
SEG_AUTO_FLOOR = 0.3
#: A segmentation map cell is read only where it holds at least this share
#: of a typical cell count.
SEG_MIN_SUPPORT = 0.25
#: The segmentation calls a cluster is made of: the DNA-backed under / over
#: calls and the size outliers; an irregular shape alone is biology too
#: often to make a region.
SEG_CLUSTER_CATEGORIES = ("under", "over", "large", "small")
SCORE_NAMES = {"blur": "Blur Score", "registration": "mismatch share",
               "segmentation": "share of cells flagged"}
#: [cal] A sampled place holds at least this share of the field's median
#: content (nuclei): the strata show places with something to judge.
SAMPLE_CONTENT_OF_MEDIAN = 0.25


@dataclass
class ScoreField:
    check: str
    values: np.ndarray
    weight: np.ndarray
    grid: dict
    fingerprint: str
    auto_threshold: float
    cell_um: float | None = None
    pixel_um: float | None = None
    denominator: str = "evaluable_tissue"
    channel: str | None = None
    reference: str | None = None
    stats: dict = dataclass_field(default_factory=dict)
    #: What counts toward the denominator, when it is not simply where a
    #: value was measured (Blur QC counts every evaluable tile).
    evaluable: np.ndarray | None = None
    #: How much of what the check reads each cell holds (nuclear share):
    #: places under `sample_floor` are never sampled nor a region's peak.
    content: np.ndarray | None = None
    #: The least content a sampled place holds (absolute; the field's own
    #: median content times `SAMPLE_CONTENT_OF_MEDIAN` raises it).
    content_floor: float = 0.0

    @property
    def valid(self):
        return self.evaluable if self.evaluable is not None else np.isfinite(self.values)

    @property
    def range(self):
        return RANGES.get(self.check, (0.0, 1.0))

    @property
    def score_name(self):
        return SCORE_NAMES.get(self.check, "score")

    def sample_floor(self) -> float | None:
        """The content a place needs to be sampled (None: no content map)."""
        if self.content is None:
            return None
        content = np.asarray(self.content, dtype=np.float64)
        held = content[np.asarray(self.valid, dtype=bool) & np.isfinite(content)]
        typical = float(np.median(held)) if held.size else 0.0
        return max(float(self.content_floor), SAMPLE_CONTENT_OF_MEDIAN * typical)

    def sampleable(self) -> np.ndarray:
        """Where a place may be sampled: measured, and holding content."""
        finite = np.isfinite(self.values)
        floor = self.sample_floor()
        if floor is None:
            return finite
        return finite & (np.nan_to_num(np.asarray(self.content, dtype=np.float64)) >= floor)

    def centre(self, iy, ix):
        g = self.grid
        return (float(g["x0"] + (ix + 0.5) * g["step"]), float(g["y0"] + (iy + 0.5) * g["step"]))


# -- from each check ------------------------------------------------------------------------


def from_blur(summary, arrays) -> ScoreField:
    grid = summary["grid"]
    ny, nx = grid["shape"]
    evaluable = np.asarray(arrays["evaluable"], dtype=bool)
    blur = np.asarray(arrays["blur"], dtype=np.float64)
    nuclear = arrays.get("nuclear_fraction") if hasattr(arrays, "get") else None
    params = summary.get("params") or {}
    return ScoreField(
        check="blur", values=np.where(evaluable & np.isfinite(blur), blur, np.nan),
        weight=np.asarray(arrays["tissue_fraction"], dtype=np.float64),
        grid={"x0": 0.0, "y0": 0.0, "step": float(grid["cell_full_px"]), "nx": int(nx),
              "ny": int(ny), "image_size": list(grid["image_size"])},
        fingerprint=summary.get("fingerprint") or "",
        auto_threshold=float(summary.get("auto_threshold") or 0.35),
        cell_um=grid.get("cell_um"), pixel_um=summary.get("pixel_um"),
        denominator="evaluable_tissue", channel=summary.get("channel"),
        stats={"global": summary.get("global_blur") or {}}, evaluable=evaluable,
        content=None if nuclear is None else np.asarray(nuclear, dtype=np.float64),
        content_floor=float(params.get("min_nuclear_fraction") or 0.0))


def from_registration(entry, *, pixel_um, fingerprint, image_size, reference=None,
                      comparison=None, stats=None) -> ScoreField:
    """The mismatch map of one comparison (`registration.field_entry`)."""
    from plexora.plugins.qc.server import registration

    mapped = entry["field"]["map"]
    factor = float(entry["factor"])
    grid = mapped["grid"]
    nucleus = np.asarray(mapped["nucleus"], dtype=np.float64)
    share = np.asarray(mapped["share"], dtype=np.float64)
    valid = nucleus >= registration.MAP_MIN_NUCLEUS
    step = float(grid["step"]) * factor
    return ScoreField(
        check="registration", values=np.where(valid, share, np.nan),
        weight=np.where(valid, nucleus, 0.0),
        grid={"x0": float(grid["x0"]) * factor, "y0": float(grid["y0"]) * factor,
              "step": step, "nx": int(grid["nx"]), "ny": int(grid["ny"]),
              "image_size": list(image_size)},
        fingerprint=fingerprint, auto_threshold=float(registration.DENSE_FRACTION),
        cell_um=step * pixel_um if pixel_um else None, pixel_um=pixel_um,
        denominator="nuclear_area", channel=comparison, reference=reference,
        stats=dict(stats or {}), content=np.where(valid, nucleus, 0.0),
        content_floor=float(registration.MAP_MIN_NUCLEUS))


def one_cycle_fields(entry, *, pixel_um, fingerprint, image_size, reference=None,
                     comparison=None) -> dict:
    """{"comparison": field, "reference": field}: where only one cycle has
    nuclei, named by the cycle that LOST them (the comparison's tissue gone,
    or the reference's). Each field's value is the one-cycle score
    (`registration.one_cycle`) where the other cycle holds the nuclei, its
    automatic bar `1 - ONE_CYCLE_RATIO`; empty without the split maps."""
    from plexora.plugins.qc.server import registration

    mapped = entry["field"]["map"]
    split = registration.one_cycle(mapped)
    if split is None:
        return {}
    factor = float(entry["factor"])
    grid = mapped["grid"]
    step = float(grid["step"]) * factor
    out = {}
    for lost, present in (("comparison", split["reference_only"]),
                          ("reference", ~split["reference_only"])):
        values = np.where(present, split["score"], np.nan)
        out[lost] = ScoreField(
            check="registration", values=values,
            weight=np.where(np.isfinite(values), split["rich"], 0.0),
            grid={"x0": float(grid["x0"]) * factor, "y0": float(grid["y0"]) * factor,
                  "step": step, "nx": int(grid["nx"]), "ny": int(grid["ny"]),
                  "image_size": list(image_size)},
            fingerprint=f"{fingerprint}:one_cycle:{lost}", auto_threshold=split["bar"],
            cell_um=step * pixel_um if pixel_um else None, pixel_um=pixel_um,
            denominator="nuclear_area", channel=comparison, reference=reference,
            stats={"one_cycle": True, "lost_in": lost},
            content=np.where(np.isfinite(values), split["rich"], 0.0),
            content_floor=float(registration.ONE_CYCLE_MIN_NUCLEUS))
    return out


def from_segmentation(project, summary, *, image_size, thresholds=None) -> ScoreField | None:
    """The share of cells Segmentation QC flags, mapped over the whole image
    at about a cell and a half; None for a result that has no positions."""
    from plexora.plugins.qc.server.segqc import run as segqc

    fp = summary.get("fingerprint")
    table = segqc._scored(project, fp)
    if table is None:
        return None
    th = segqc.thresholds(summary, **(thresholds or {}))
    flags = {"under": table["under"] >= th["under"], "over": table["over"] >= th["over"],
             **segqc.size_flags(table, th)}
    flagged = np.zeros(table["x"].shape, dtype=bool)
    for name in SEG_CLUSTER_CATEGORIES:
        flagged |= np.asarray(flags[name], dtype=bool)
    width, height = image_size
    step = max(1.5 * table["spacing"], max(width, height) / 1024.0)
    arrays = segqc._density_arrays(table, {"any": flagged}, (0.0, 0.0, float(width),
                                                             float(height)), step)
    support = arrays["support"]
    share = arrays["shares"]["any"]
    valid = support >= SEG_MIN_SUPPORT
    values = np.where(valid, share, np.nan)
    finite = values[np.isfinite(values)]
    auto = SEG_AUTO_FLOOR
    if finite.size:
        median = float(np.median(finite))
        mad = float(np.median(np.abs(finite - median))) * 1.4826
        auto = max(SEG_AUTO_FLOOR, median + 3.0 * mad)
    lo, hi = RANGES["segmentation"]
    pixel_um = summary.get("pixel_um")
    g = arrays["grid"]
    return ScoreField(
        check="segmentation", values=values, weight=np.where(valid, arrays["total"], 0.0),
        grid={**g, "image_size": [int(width), int(height)]},
        fingerprint=f"{fp}:{_thresholds_key(th)}", auto_threshold=float(np.clip(auto, lo, hi)),
        cell_um=g["step"] * pixel_um if pixel_um else None, pixel_um=pixel_um,
        denominator="cells", channel=summary.get("dna_channel"),
        stats={"thresholds": th, "n_cells": int(table["x"].size),
               "n_flagged": int(flagged.sum()), "d_nucleus_um": summary.get("d_nucleus_um"),
               "d_nucleus_px": summary.get("d_nucleus_px")})


def _thresholds_key(th):
    return ",".join(f"{k}={float(th[k]):.4g}" for k in sorted(th))


# -- the distribution and the bar ----------------------------------------------------------


def distribution(field) -> dict:
    """{n, median, mad, quantiles {p05..p99}, histogram {edges, counts}} of
    the field's evaluable values: what a bar is judged against."""
    values = field.values[np.isfinite(field.values)]
    edges = np.linspace(0.0, 1.0, HISTOGRAM_BINS + 1)
    if not values.size:
        return {"n": 0, "median": None, "mad": None, "quantiles": {},
                "histogram": {"edges": edges.round(3).tolist(),
                              "counts": [0] * HISTOGRAM_BINS}}
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median))) * 1.4826
    qs = np.quantile(values, [0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
    counts, _ = np.histogram(np.clip(values, 0.0, 1.0), bins=edges)
    return {"n": int(values.size), "median": round(median, 4), "mad": round(mad, 4),
            "quantiles": {k: round(float(v), 4) for k, v in zip(
                ("p05", "p25", "p50", "p75", "p95", "p99"), qs)},
            "histogram": {"edges": edges.round(3).tolist(),
                          "counts": counts.astype(int).tolist()}}


def step_of(field, dist=None) -> float:
    """One step of this field's bar: `score_step_mad` MADs, never less than
    the check's floor (`score_step_floor`)."""
    engine = schemas.ENGINE
    dist = dist or distribution(field)
    floor = float((engine["score_step_floor"] or {}).get(field.check, 0.05))
    mad = float(dist.get("mad") or 0.0)
    return round(max(float(engine["score_step_mad"]) * mad, floor), 4)


def threshold_at(auto, step, offset_steps, lo=0.0, hi=1.0) -> float:
    """The bar `offset_steps` from the automatic one; positive is tighter
    (lower: more flagged), clipped to [lo, hi]."""
    return round(float(np.clip(float(auto) - float(offset_steps) * float(step), lo, hi)), 4)


def clamp_steps(offset_steps) -> int:
    bound = int(schemas.ENGINE["adjust_max_steps"])
    return int(max(-bound, min(bound, int(offset_steps))))


def bar(field, offset_steps=0, *, value=None) -> dict:
    """{value, auto, step, offset_steps, lo, hi}: the bar in force."""
    dist = distribution(field)
    step = step_of(field, dist)
    lo, hi = field.range
    auto = float(np.clip(field.auto_threshold, lo, hi))
    if value is not None:
        return {"value": round(float(np.clip(float(value), lo, hi)), 4), "auto": auto,
                "step": step, "offset_steps": None, "lo": lo, "hi": hi}
    steps = clamp_steps(offset_steps)
    return {"value": threshold_at(auto, step, steps, lo, hi), "auto": auto, "step": step,
            "offset_steps": steps, "lo": lo, "hi": hi}


# -- regions ------------------------------------------------------------------------------


def cell_geometry(ys, xs, grid):
    """GeoJSON of the union of these grid cells' squares (full-resolution px)."""
    import shapely

    from plexora.plugins.qc.server import polygons

    s = float(grid["step"])
    x0, y0 = float(grid.get("x0") or 0.0), float(grid.get("y0") or 0.0)
    width, height = grid["image_size"]
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    boxes = shapely.box(x0 + xs * s, y0 + ys * s, np.minimum(x0 + (xs + 1) * s, width),
                        np.minimum(y0 + (ys + 1) * s, height))
    return polygons.to_geojson(shapely.union_all(boxes), simplify_px=0.35 * s)


def regions(field, threshold, *, min_cells=None, geometry=True, max_regions=MAX_REGIONS):
    """The flagged area and regions of `field` at `threshold`. `flagged_pct`
    is the weight (tissue, nuclear area, cells) in flagged cells over the
    weight of every evaluable one; regions are 8-connected groups of at least
    `min_cells` flagged cells, largest first."""
    from scipy import ndimage

    threshold = float(threshold)
    if min_cells is None:
        min_cells = int((schemas.ENGINE["score_min_region_cells"] or {}).get(field.check, 4))
    min_cells = max(1, int(min_cells))
    valid = field.valid
    with np.errstate(invalid="ignore"):
        flagged = valid & (np.nan_to_num(field.values, nan=-1.0) >= threshold)
    labels, n = ndimage.label(flagged, structure=np.ones((3, 3), dtype=bool))
    mask = np.zeros(flagged.shape, dtype=bool)
    kept = []
    if n:
        sizes = ndimage.sum(np.ones_like(labels), labels, index=np.arange(1, n + 1))
        kept = [int(i) + 1 for i in np.argsort(-sizes) if sizes[i] >= min_cells]
        mask = np.isin(labels, kept)
    weight = np.asarray(field.weight, dtype=np.float64)
    denominator = float(weight[valid].sum())
    flagged_pct = round(100.0 * float(weight[mask].sum()) / denominator, 2) \
        if denominator > 0 else 0.0
    grid = field.grid
    s = float(grid["step"])
    x0, y0 = float(grid.get("x0") or 0.0), float(grid.get("y0") or 0.0)
    width, height = grid["image_size"]
    out = []
    sampleable = field.sampleable() if field.content is not None else None
    for k, label in enumerate(kept[:max_regions]):
        ys, xs = np.nonzero(labels == label)
        values = field.values[ys, xs]
        top = int(np.nanargmax(values))
        if sampleable is not None and not sampleable[ys[top], xs[top]]:
            # The crop goes where the score is high AND there is something to
            # see: the strongest place holding nuclei, when the region has one.
            held = sampleable[ys, xs]
            if held.any():
                top = int(np.nanargmax(np.where(held, values, -np.inf)))
        area_px2 = float(ys.size) * s * s
        region = {"id": f"{field.check}_{k + 1}", "cells": int(ys.size),
                  "area_px2": round(area_px2, 1),
                  "area_um2": round(area_px2 * field.pixel_um ** 2, 1)
                  if field.pixel_um else None,
                  "bbox": [round(float(x0 + xs.min() * s), 1), round(float(y0 + ys.min() * s), 1),
                           round(float(min(width, x0 + (xs.max() + 1) * s)), 1),
                           round(float(min(height, y0 + (ys.max() + 1) * s)), 1)],
                  "mean": round(float(np.nanmean(values)), 4),
                  "max": round(float(np.nanmax(values)), 4),
                  "peak_cell": [int(ys[top]), int(xs[top])],
                  "peak": [round(v, 1) for v in field.centre(ys[top], xs[top])],
                  "weight_pct": round(100.0 * float(weight[ys, xs].sum()) / denominator, 3)
                  if denominator > 0 else 0.0}
        if geometry:
            region["geometry"] = cell_geometry(ys, xs, grid)
        out.append(region)
    return {"threshold": threshold, "min_cells": min_cells, "flagged_pct": flagged_pct,
            "denominator": field.denominator, "n_regions": len(kept),
            "residual": max(0, len(kept) - max_regions), "regions": out, "mask": mask,
            "labels": labels, "kept": kept}


def region_mask(found, index):
    """The grid mask of `found["regions"][index]`."""
    return found["labels"] == found["kept"][index]


# -- sampling across the distribution ------------------------------------------------------


def spacing_px(field) -> float:
    """How far apart the places of one row are: `score_spacing_um`, or four
    grid cells without a pixel size -- never under one cell."""
    um = float(schemas.ENGINE["score_spacing_um"])
    if field.pixel_um:
        return max(float(field.grid["step"]), um / float(field.pixel_um))
    return 4.0 * float(field.grid["step"])


def _spaced(field, cells, order, n, spacing, taken):
    picked = []
    for i in order:
        iy, ix = int(cells[0][i]), int(cells[1][i])
        if (iy, ix) in taken:
            continue
        x, y = field.centre(iy, ix)
        if any(abs(x - p["x"]) < spacing and abs(y - p["y"]) < spacing for p in picked):
            continue
        picked.append({"cell": [iy, ix], "x": round(x, 1), "y": round(y, 1),
                       "score": round(float(field.values[iy, ix]), 4)})
        taken.add((iy, ix))
        if len(picked) >= n:
            break
    return picked


def sample_strata(field, threshold, step, found=None, *, per_row=None, spacing=None,
                  strata=None, seed=0) -> dict:
    """{stratum: [{cell, x, y, score}]} -- a few places from each part of the
    distribution (see the module docstring). A row with nothing in it is
    left out; `global` is never sampled here (it is the whole tissue)."""
    per_row = int(per_row or schemas.ENGINE["score_per_stratum"])
    spacing = float(spacing or spacing_px(field))
    wanted = [s for s in (strata or schemas.SCORE_STRATA) if s != "global"]
    values = field.values
    # Only places with something to judge (nuclei): a black tile wastes the
    # look and says nothing about the bar.
    allowed = field.sampleable() if hasattr(field, "sampleable") else np.isfinite(values)
    cells = np.nonzero(allowed)
    v = values[cells]
    if not v.size:
        return {}
    rng = np.random.default_rng(int(seed))
    shuffled = rng.permutation(v.size)
    taken = set()
    out = {}
    threshold, step = float(threshold), float(step)
    if "strongly_abnormal" in wanted:
        idx = np.flatnonzero(v >= threshold + step)
        order = idx[np.lexsort((idx, -v[idx]))]
        out["strongly_abnormal"] = _spaced(field, cells, order, per_row, spacing, taken)
    if "clustered" in wanted and found and found.get("regions"):
        rows = []
        for region in found["regions"][:per_row]:
            iy, ix = region["peak_cell"]
            if (iy, ix) in taken or not allowed[iy, ix]:
                # The peak is already shown far above the bar (or holds no
                # nuclei): the region's next strongest sampleable cell, so
                # the row adds a place.
                mask = region_mask(found, found["regions"].index(region)) & allowed
                ys, xs = np.nonzero(mask)
                order = np.argsort(-values[ys, xs], kind="stable")
                spot = next(((int(ys[j]), int(xs[j])) for j in order
                             if (int(ys[j]), int(xs[j])) not in taken), None)
                if spot is None:
                    continue
                iy, ix = spot
            x, y = field.centre(iy, ix)
            rows.append({"cell": [int(iy), int(ix)], "x": round(x, 1), "y": round(y, 1),
                         "score": round(float(values[iy, ix]), 4), "region": region["id"]})
            taken.add((int(iy), int(ix)))
        out["clustered"] = rows
    if "borderline_above" in wanted:
        idx = np.flatnonzero((v >= threshold) & (v < threshold + step))
        order = idx[np.lexsort((idx, v[idx] - threshold))]
        out["borderline_above"] = _spaced(field, cells, order, per_row, spacing, taken)
    if "borderline_below" in wanted:
        idx = np.flatnonzero((v >= threshold - step) & (v < threshold))
        order = idx[np.lexsort((idx, threshold - v[idx]))]
        out["borderline_below"] = _spaced(field, cells, order, per_row, spacing, taken)
    if "clear_good" in wanted:
        q05, q25 = np.quantile(v, [0.05, 0.25])
        band = (v >= q05) & (v <= q25) & (v < threshold - step)
        if not band.any():
            band = v < threshold - step
        if not band.any():
            band = v < threshold
        order = [i for i in shuffled if band[i]]
        out["clear_good"] = _spaced(field, cells, order, per_row, spacing, taken)
    ordered = {k: out[k] for k in schemas.SCORE_STRATA if out.get(k)}
    return ordered


def global_possible(field) -> dict:
    """{possible, why}: whether the problem may be everywhere, where an
    in-image bar cannot see it (a whole field out of focus, a whole cycle
    shifted, a mask that fails across the tissue)."""
    stats = field.stats or {}
    if field.check == "blur":
        g = stats.get("global") or {}
        if g.get("possible"):
            return {"possible": True,
                    "why": "even the sharpest tiles have little fine detail"
                    if g.get("fine_share") is not None else "too few tiles to compare"}
    elif field.check == "registration":
        if stats.get("pattern") == "widespread":
            return {"possible": True, "why": "most of the evaluated tissue is displaced "
                                             "past the threshold"}
        shift = (stats.get("global_shift_px") or {}).get("magnitude")
        limit = stats.get("effective_threshold_px")
        if shift is not None and limit and shift >= limit:
            return {"possible": True, "why": "the whole cycle is shifted from the reference"}
    elif field.check == "segmentation":
        dist = distribution(field)
        median = dist.get("median")
        if median is not None and median >= field.auto_threshold:
            return {"possible": True, "why": "the typical place is already past the bar"}
    return {"possible": False, "why": None}


# -- Segmentation QC's cells ----------------------------------------------------------------

#: How each segmentation reason reads a cell as worse (higher is worse): the
#: two DNA-backed scores as they are, the size and shape outliers as a robust
#: z on the side that is flagged.
SEG_CELL_SCORES = {"seg_under": ("under", 1.0), "seg_over": ("over", 1.0),
                   "seg_large": ("area_z", 1.0), "seg_small": ("area_z", -1.0),
                   "seg_irregular": ("circ_z", -1.0)}


@dataclass
class CellScores:
    """One segmentation reason's score per cell (higher is worse)."""

    reason: str
    values: np.ndarray
    x: np.ndarray
    y: np.ndarray
    cell_id: np.ndarray
    auto_threshold: float
    kind: str
    fingerprint: str
    image_size: list
    pixel_um: float | None = None
    d_nucleus_px: float | None = None
    channel: str | None = None

    check = "segmentation"
    denominator = "cells"

    @property
    def range(self):
        from plexora.plugins.qc.server.segqc import run as segqc

        return segqc.FLAG_RANGE if self.kind == "score" else segqc.Z_RANGE

    @property
    def score_name(self):
        return {"seg_under": "under-segmentation score", "seg_over": "over-segmentation score",
                "seg_large": "robust z of log area", "seg_small": "robust z of log area, below",
                "seg_irregular": "robust z of circularity, below"}[self.reason]


def cell_scores(project, reason, summary, *, image_size) -> CellScores | None:
    """`reason`'s score for every cell of the current Segmentation QC result
    (None for a result without positions)."""
    from plexora.plugins.qc.server.segqc import run as segqc

    column, sign = SEG_CELL_SCORES[reason]
    table = segqc._scored(project, summary.get("fingerprint"))
    frame = segqc.frame(project, summary.get("fingerprint"))
    if table is None or frame is None:
        return None
    kind = "score" if column in ("under", "over") else "z"
    auto = float({**segqc.PARAMS_DEFAULT, **(summary.get("params") or {})}["flag"]) \
        if kind == "score" else float(segqc.OUTLIER_Z)
    return CellScores(reason=reason, values=sign * np.asarray(table[column], dtype=np.float64),
                      x=np.asarray(table["x"]), y=np.asarray(table["y"]),
                      cell_id=frame["cell_id"].to_numpy(), auto_threshold=auto, kind=kind,
                      fingerprint=summary.get("fingerprint") or "",
                      image_size=list(image_size), pixel_um=summary.get("pixel_um"),
                      d_nucleus_px=summary.get("d_nucleus_px"),
                      channel=summary.get("dna_channel"))


def cell_distribution(scores) -> dict:
    values = scores.values[np.isfinite(scores.values)]
    if not values.size:
        return {"n": 0, "median": None, "mad": None, "quantiles": {}}
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median))) * 1.4826
    qs = np.quantile(values, [0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
    return {"n": int(values.size), "median": round(median, 4), "mad": round(mad, 4),
            "quantiles": {k: round(float(v), 4) for k, v in zip(
                ("p05", "p25", "p50", "p75", "p95", "p99"), qs)}}


def cell_bar(scores, offset_steps=0, *, value=None) -> dict:
    """The bar of a segmentation reason: its automatic cutoff (the run's flag,
    or `OUTLIER_Z`) moved by steps of `seg_step_score` / `seg_step_z`."""
    engine = schemas.ENGINE
    step = float(engine["seg_step_score"] if scores.kind == "score" else engine["seg_step_z"])
    lo, hi = scores.range
    auto = float(np.clip(scores.auto_threshold, lo, hi))
    if value is not None:
        return {"value": round(float(np.clip(float(value), lo, hi)), 4), "auto": auto,
                "step": step, "offset_steps": None, "lo": lo, "hi": hi}
    steps = clamp_steps(offset_steps)
    return {"value": threshold_at(auto, step, steps, lo, hi), "auto": auto, "step": step,
            "offset_steps": steps, "lo": lo, "hi": hi}


def sample_cells(scores, threshold, step, *, per_row=None, spacing=None, strata=None,
                 seed=0) -> dict:
    """{stratum: [{cell_id, x, y, score}]} of one segmentation reason, as
    `sample_strata` samples a field (no `clustered` row: the cluster map is
    the segmentation field's own)."""
    per_row = int(per_row or schemas.ENGINE["score_per_stratum"])
    if spacing is None:
        um = float(schemas.ENGINE["score_spacing_um"])
        spacing = um / scores.pixel_um if scores.pixel_um else \
            4.0 * float(scores.d_nucleus_px or 16.0)
    wanted = [s for s in (strata or schemas.SCORE_STRATA) if s not in ("global", "clustered")]
    v = scores.values
    finite = np.isfinite(v)
    rng = np.random.default_rng(int(seed))
    shuffled = rng.permutation(v.size)
    taken = set()
    threshold, step = float(threshold), float(step)

    def pick(order):
        chosen = []
        for i in order:
            i = int(i)
            if i in taken or not finite[i]:
                continue
            x, y = float(scores.x[i]), float(scores.y[i])
            if any(abs(x - p["x"]) < spacing and abs(y - p["y"]) < spacing for p in chosen):
                continue
            chosen.append({"cell_id": int(scores.cell_id[i]), "x": round(x, 1),
                           "y": round(y, 1), "score": round(float(v[i]), 4)})
            taken.add(i)
            if len(chosen) >= per_row:
                break
        return chosen

    out = {}
    idx = np.arange(v.size)
    if "strongly_abnormal" in wanted:
        sel = idx[finite & (v >= threshold + step)]
        out["strongly_abnormal"] = pick(sel[np.lexsort((sel, -v[sel]))])
    if "borderline_above" in wanted:
        sel = idx[finite & (v >= threshold) & (v < threshold + step)]
        out["borderline_above"] = pick(sel[np.lexsort((sel, v[sel] - threshold))])
    if "borderline_below" in wanted:
        sel = idx[finite & (v >= threshold - step) & (v < threshold)]
        out["borderline_below"] = pick(sel[np.lexsort((sel, threshold - v[sel]))])
    if "clear_good" in wanted and finite.any():
        q25, q50 = np.quantile(v[finite], [0.25, 0.5])
        band = finite & (v >= q25) & (v <= q50) & (v < threshold - step)
        if not band.any():
            band = finite & (v < threshold - step)
        out["clear_good"] = pick([i for i in shuffled if band[i]])
    return {k: out[k] for k in schemas.SCORE_STRATA if out.get(k)}
