"""Tracing an artifact's own pixels inside the envelope the agent judged.

The agent says what a region is and roughly where -- an outline on the 50 µm
map grid, or grid squares -- and never types a coordinate. That outline is an
*envelope*: where to look. This module reads the envelope's pixels at about a
micron a pixel and traces the artifact itself (an aggregate's specks, a fold's
bright band, the blurred patch, the saturated plateau), so what is written
keeps the normal tissue the envelope takes in.

What comes back is always inside the envelope and never a stray trace: a
result passes its guards (the trace covers the candidate's strongest cells)
or falls back to the envelope itself, which is what QC wrote before tracing
existed. Classes whose region is the envelope (a seam, shading, a failed
channel) are `not_applicable` and read no pixels.

A pure function of the scan, the pixels and the parameters: no session state,
no randomness, float32 throughout, so the same envelope traces to the same
outline and a memo's replayed answers stay valid. cv2, scipy and shapely are
imported where they are used, as everywhere in the QC server.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from plexora.plugins.qc.server import scan as scanmod

VERSION = "1"

#: [cal] every number the tracer uses. Lengths are microns (µm² for areas);
#: pixel budgets are per read.
REFINE = {
    # which pyramid level: the coarsest at or finer than target_um * level_slack,
    # coarser while the box is over budget, never coarser than max_um
    "target_um": 1.0, "level_slack": 1.4, "max_um": 20.0,
    "max_pixels": 4_000_000, "max_total_pixels": 16_000_000, "max_channels": 4,
    # read around the envelope so neighbourhood filters are exact at its edge
    "halo_um": 64.0,
    # blur is judged against the tissue this many map cells round the envelope
    "blur_context_cells": 2,
    # diffuse brightness / darkness: a robust z against the scan's own surround
    "bright_z": 3.0, "dark_z": -2.5, "diffuse_sigma_cells": 0.4,
    "diffuse_sigma_min_um": 8.0, "diffuse_sigma_max_um": 20.0,
    "diffuse_floor": scanmod.DIFFUSE_FLOOR, "agree": 0.5, "half_strength": 0.5,
    # compact bright objects: the scan's top-hat, plus a plateau far above the
    # brightest real cells (a large aggregate survives the top-hat's opening)
    "tophat_um": scanmod.TOPHAT_UM, "compact_of_range": 0.25,
    "plateau_ratio": 2.0, "plateau_sigma_um": 1.0, "bright_cells_percentile": 99.0,
    "saturation_of_ceiling": scanmod.SATURATION_OF_CEILING,
    # focus: local Laplacian energy over local spread, against a clean ring
    "focus_window_um": 20.0, "focus_log2": -1.0, "focus_min_contrast": 0.35,
    "blur_min_ref_px": 20_000,
    # cycle loss: as the scan's cross-cycle rule, per pixel
    "loss_ratio": 4.0, "loss_sigma_um": 5.0,
    "edge_band_um": 50.0, "tissue_pad_um": 10.0,
    # guards: the peak cell and most of the strongest cells are touched
    "top_cells_share": 0.25, "top_cells_max": 16, "top_cells_min_covered": 0.6,
    "full_fraction": 0.95, "simplify_px": 0.7,
    "max_parts": 150, "parts_merge_max_um": 50.0,
}

#: [cal] what is done to each method's evidence, in microns: closing radius,
#: the largest hole filled (a bigger one is normal tissue inside a fold),
#: the smallest part kept, and the margin grown round what is kept.
POST = {
    "bright_compact": {"close_um": 0.0, "hole_max_um2": 0.0, "min_area_um2": 3.0,
                       "margin_um": 3.0},
    "bright_multi": {"close_um": 5.0, "hole_max_um2": 400.0, "min_area_um2": 25.0,
                     "margin_um": 5.0},
    "saturation": {"close_um": 3.0, "hole_max_um2": math.inf, "min_area_um2": 4.0,
                   "margin_um": 3.0},
    "diffuse_bright": {"close_um": 10.0, "hole_max_um2": 2500.0, "min_area_um2": 400.0,
                       "margin_um": 5.0},
    "diffuse_abs": {"close_um": 10.0, "hole_max_um2": math.inf, "min_area_um2": 400.0,
                    "margin_um": 10.0},
    "dark": {"close_um": 10.0, "hole_max_um2": 2500.0, "min_area_um2": 400.0,
             "margin_um": 10.0},
    "blur": {"close_um": 15.0, "hole_max_um2": 2500.0, "min_area_um2": 2500.0,
             "margin_um": 10.0},
    "cycle_loss": {"close_um": 10.0, "hole_max_um2": 2500.0, "min_area_um2": 400.0,
                   "margin_um": 5.0},
    "edge_band": {"close_um": 0.0, "hole_max_um2": 0.0, "min_area_um2": 400.0,
                  "margin_um": 0.0},
}

#: How each class is traced. A class not here keeps its envelope: a seam, a
#: shading gradient, a failed channel or a registration error is the region
#: the envelope names, not a set of pixels inside it.
METHODS = {
    "antibody_aggregate": "bright_compact",
    "debris_or_foreign_object": "bright_multi",
    "saturation_or_clipping": "saturation",
    "tissue_fold": "diffuse_bright",
    "autofluorescence": "diffuse_bright",
    "excessive_background": "diffuse_bright",
    "air_bubble_or_coverslip": "diffuse_abs",
    "tissue_damage_or_detachment": "dark",
    "cycle_specific_tissue_loss": "cycle_loss",
    "out_of_focus": "blur",
    "slide_or_tissue_edge": "edge_band",
}

#: The map a method's guard ranks the envelope's cells by, and which way is
#: worse (+1 higher, -1 lower).
GUARD_METRIC = {"bright_compact": ("bright_compact", 1), "bright_multi": ("bright_compact", 1),
                "saturation": ("saturation", 1), "diffuse_bright": ("bright_diffuse", 1),
                "diffuse_abs": ("bright_diffuse", 1), "dark": ("median", -1),
                "blur": ("focus_rel", -1)}

WORDS = {"bright_compact": "bright specks", "bright_multi": "bright specks in several channels",
         "saturation": "saturated pixels", "diffuse_bright": "diffuse brightness",
         "diffuse_abs": "a brightness change", "dark": "dark tissue",
         "blur": "blurred tissue", "cycle_loss": "tissue lost in a cycle",
         "edge_band": "the tissue edge"}


def method_for(artifact_class) -> str | None:
    return METHODS.get(artifact_class or "")


@dataclass
class RefinementResult:
    """One trace. `geometry` is level-0 pixels (holes kept); `fallback` and
    `not_applicable` carry the envelope as their geometry."""

    status: str
    reason: str
    method: str | None = None
    geometry: dict | None = None
    level: int | None = None
    factor: float | None = None
    refine_um: float | None = None
    box_level: list | None = None
    mask: np.ndarray | None = field(default=None, repr=False)
    area_px2: float | None = None
    area_um2: float | None = None
    envelope_area_px2: float | None = None
    envelope_area_um2: float | None = None
    kept_fraction: float | None = None
    parts: int = 0
    dropped_parts: int = 0
    channels: list = field(default_factory=list)
    params: dict = field(default_factory=dict)
    guards: dict = field(default_factory=dict)
    timing: dict = field(default_factory=dict)
    pixels_read: int = 0

    @property
    def refined(self) -> bool:
        return self.status == "refined"

    def to_record(self, *, geometry=False) -> dict:
        """JSON of the result (never the mask; the geometry only if asked)."""
        out = {k: getattr(self, k) for k in (
            "status", "reason", "method", "level", "factor", "refine_um", "box_level",
            "area_px2", "area_um2", "envelope_area_px2", "envelope_area_um2",
            "kept_fraction", "parts", "dropped_parts", "channels", "params", "guards",
            "timing", "pixels_read")}
        out["version"] = VERSION
        if geometry:
            out["geometry"] = self.geometry
        return _jsonable(out)


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return None if not math.isfinite(value) else round(value, 6)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


# -- what is being traced -------------------------------------------------------------


def describe(candidate, scan=None) -> dict:
    """{class, channels, primary_metric, peak, cycles} of a detector Candidate,
    a session unit, or a stored candidate record."""
    if isinstance(candidate, dict):
        if "decision" in candidate:
            # A session unit: the agent's latest judgment leads.
            klass = (candidate.get("decision") or {}).get("artifact_class") \
                or candidate.get("class") or candidate.get("class_hint")
        else:
            # A stored record: its class follows the user's relabelling.
            klass = candidate.get("class") \
                or (candidate.get("ai_decision") or {}).get("artifact_class") \
                or candidate.get("class_hint")
        return {"class": klass, "channels": list(candidate.get("channels") or []),
                "primary_metric": candidate.get("primary_metric") or "",
                "peak": candidate.get("peak"),
                "cycles": [int(c) for c in candidate.get("cycles") or [] if c]}
    peak = None
    if scan is not None:
        from plexora.plugins.qc.server import candidates as cand

        peak = cand.peak_of(candidate, scan)
    return {"class": candidate.class_hint, "channels": list(candidate.channels),
            "primary_metric": candidate.primary_metric or "", "peak": peak,
            "cycles": [int(c) for c in candidate.cycles if c]}


def envelope_geometry(envelope_mask, grid):
    """The envelope as GeoJSON: the union of its map cells."""
    from plexora.plugins.qc.server import polygons

    return polygons.mask_to_geometry(np.asarray(envelope_mask, dtype=bool), grid)


def _usable(scan):
    if scan.meta.get("brightfield"):
        return [scan.channels[0]["name"]] if scan.channels else []
    return [c["name"] for c in scan.channels
            if not ({"empty_channel", "near_zero_plane"} & set(c.get("flags") or []))]


def _primary(about, usable):
    channel = (about["primary_metric"] or "").partition("::")[0]
    if channel in usable:
        return channel
    for name in about["channels"]:
        if name in usable:
            return name
    return usable[0] if usable else None


def _nuclear(scan, usable):
    from plexora.plugins.qc.server.cycles import is_nuclear

    first = scan.meta.get("nuclear")
    if first in usable:
        return first
    return next((n for n in usable if is_nuclear(n)), None)


def _ranked(scan, names, metric, where, *, lowest=False):
    """`names` by their map's mean over `where`, strongest first (the input
    order breaks ties, so the choice is deterministic)."""
    scored = []
    for order, name in enumerate(names):
        values = scan.map(name, metric)
        if values is None:
            continue
        if metric == "median":
            values = np.log1p(np.maximum(values, 0))
        data = values[where & np.isfinite(values)]
        score = float(data.mean()) if data.size else -math.inf
        scored.append((score if not lowest else -score, -order, name))
    scored.sort(reverse=True)
    return [name for _s, _o, name in scored]


def _loss_entry(about, scan, where):
    """(reference channel, this cycle's nuclear channel, intensity ratio) of a
    cycle-loss trace, or None."""
    cross = scan.meta.get("cross_cycle") or {}
    entries = [e for e in cross.get("cycles") or [] if e.get("channel")]
    if not cross.get("available") or not entries or not cross.get("reference"):
        return None
    wanted = None
    metric = about["primary_metric"] or ""
    if metric.startswith("tissue_loss:c"):
        try:
            wanted = int(metric.rsplit("c", 1)[1])
        except ValueError:
            wanted = None
    if wanted is None and about["cycles"]:
        wanted = min(about["cycles"])
    entry = next((e for e in entries if e["cycle"] == wanted), None)
    if entry is None:
        # The cycle whose loss map is strongest inside the envelope.
        best = (-1.0, None)
        for candidate in entries:
            loss = scan.shared(f"tissue_loss:c{candidate['cycle']}")
            if loss is None:
                continue
            data = np.nan_to_num(loss)[where]
            value = float(data.mean()) if data.size else 0.0
            if value > best[0]:
                best = (value, candidate)
        entry = best[1]
    if entry is None:
        return None
    return cross["reference"], entry["channel"], float(entry.get("intensity_ratio") or 1.0), \
        int(entry["cycle"])


def _channels(method, about, scan, envelope_mask):
    """(channels to read, a note) for one method; channels None when the
    method cannot run on this image."""
    usable = _usable(scan)
    cap = int(REFINE["max_channels"])
    tissue = scan.tissue()
    where = envelope_mask & tissue
    if not where.any():
        where = envelope_mask
    own = [c for c in about["channels"] if c in usable] or usable
    primary = _primary(about, usable)
    nuclear = _nuclear(scan, usable) if not scan.meta.get("brightfield") else None
    if method == "edge_band":
        return [], {}
    if not usable or primary is None:
        return None, {"why": "no usable channel"}
    if method in ("bright_compact", "saturation", "blur"):
        return [primary], {}
    if method == "bright_multi":
        return _ranked(scan, own, "bright_compact", where)[:cap], {}
    if method in ("diffuse_bright", "diffuse_abs"):
        klass = about["class"]
        if klass == "excessive_background":
            return [primary], {}
        if klass == "autofluorescence":
            return _ranked(scan, own, "bright_diffuse", where)[:cap], {}
        others = [c for c in usable if c != nuclear]
        ranked = _ranked(scan, others, "bright_diffuse", where)
        if nuclear is None:
            return ranked[:cap], {}
        return [nuclear, *ranked[:cap - 1]], {"required": nuclear}
    if method == "dark":
        others = [c for c in usable if c != nuclear]
        ranked = _ranked(scan, others, "median", where, lowest=True)
        if nuclear is None:
            return ranked[:cap], {}
        return [nuclear, *ranked[:cap - 1]], {"required": nuclear}
    if method == "cycle_loss":
        entry = _loss_entry(about, scan, where)
        if entry is None:
            return None, {"why": "no cycle structure to compare against"}
        reference, this, ratio, cycle = entry
        if reference not in usable and not scan.meta.get("brightfield"):
            return None, {"why": "the reference nuclear stain is unusable"}
        return [reference, this], {"ratio": ratio, "cycle": cycle}
    return None, {"why": f"no method {method!r}"}


# -- where and at what resolution ---------------------------------------------------------


def choose_level(source, bounds, pixel_um, n_channels, *, pad_full=0.0, options=None):
    """(level, factor, µm a pixel, haloed box in level pixels) for a box of
    full-resolution `bounds` (x0, y0, x1, y1), or (None, reason)."""
    params = {**REFINE, **(options or {})}
    base_um = float(pixel_um) if pixel_um else scanmod.CELL_UM / scanmod.CELL_PX
    full_h, full_w = source.level_shape(0)
    rows = []
    for level in range(max(1, int(source.levels))):
        lh, lw = source.level_shape(level)
        factor = full_w / max(1, lw)
        rows.append((factor, level, int(lh), int(lw)))
    rows.sort()
    fine = [i for i, r in enumerate(rows) if base_um * r[0] <= params["target_um"]
            * params["level_slack"]]
    start = fine[-1] if fine else 0
    budget = min(float(params["max_pixels"]),
                 float(params["max_total_pixels"]) / max(1, int(n_channels)))
    x0, y0, x1, y1 = bounds
    for factor, level, lh, lw in rows[start:]:
        um = base_um * factor
        if um > params["max_um"]:
            return None, (f"the envelope is too large to trace at {params['max_um']:g} µm "
                          "a pixel or finer")
        halo = int(math.ceil(params["halo_um"] / um))
        bx0 = max(0, int(math.floor((x0 - pad_full) / factor)) - halo)
        by0 = max(0, int(math.floor((y0 - pad_full) / factor)) - halo)
        bx1 = min(lw, int(math.ceil((x1 + pad_full) / factor)) + halo)
        by1 = min(lh, int(math.ceil((y1 + pad_full) / factor)) + halo)
        if bx1 <= bx0 or by1 <= by0:
            return None, "the envelope lies outside the image"
        if (bx1 - bx0) * (by1 - by0) <= budget:
            return (level, float(factor), float(um), [bx0, by0, bx1, by1]), None
    return None, "the envelope is too large to trace within the pixel budget"


class _Crop:
    """One traced box: its pixels' full-resolution centres and the lookups
    from them to the scan's grid and overview."""

    def __init__(self, scan, box, factor, um):
        self.scan = scan
        self.box = box
        self.factor = factor
        self.um = um
        x0, y0, x1, y1 = box
        self.shape = (y1 - y0, x1 - x0)
        self.rows = (np.arange(y0, y1, dtype=np.float64) + 0.5) * factor
        self.cols = (np.arange(x0, x1, dtype=np.float64) + 0.5) * factor
        grid = scan.grid
        self.cell = float(grid["cell_full_px"])
        ny, nx = grid["shape"]
        self.iy = np.clip((self.rows // self.cell).astype(np.int64), 0, ny - 1)
        self.ix = np.clip((self.cols // self.cell).astype(np.int64), 0, nx - 1)
        self._cache = {}

    def px(self, um):
        return um / self.um

    def nearest(self, grid_values):
        return grid_values[np.ix_(self.iy, self.ix)]

    def bilinear(self, grid_values):
        """A grid map at every pixel, bilinearly: a smooth baseline, never the
        staircase of its cells."""
        ny, nx = grid_values.shape
        gy = np.clip(self.rows / self.cell - 0.5, 0, ny - 1)
        gx = np.clip(self.cols / self.cell - 0.5, 0, nx - 1)
        y_lo, y_hi = int(np.floor(gy.min())), min(ny - 1, int(np.ceil(gy.max())))
        x_lo, x_hi = int(np.floor(gx.min())), min(nx - 1, int(np.ceil(gx.max())))
        sub = np.asarray(grid_values[y_lo:y_hi + 1, x_lo:x_hi + 1], dtype=np.float64)
        return (_weights(gy - y_lo, sub.shape[0]) @ sub
                @ _weights(gx - x_lo, sub.shape[1]).T).astype(np.float32)

    def cell_counts(self, mask):
        """Traced pixels per map cell, and pixels per map cell, over the crop."""
        ny, nx = self.scan.grid["shape"]
        ys, xs = np.nonzero(mask)
        traced = np.bincount(self.iy[ys] * nx + self.ix[xs], minlength=ny * nx)
        total_y = np.bincount(self.iy, minlength=ny)
        total_x = np.bincount(self.ix, minlength=nx)
        return traced.reshape(ny, nx), np.outer(total_y, total_x)


def _weights(coords, n):
    lo = np.clip(np.floor(coords).astype(np.int64), 0, n - 1)
    hi = np.minimum(lo + 1, n - 1)
    frac = coords - lo
    out = np.zeros((coords.size, n), dtype=np.float64)
    rows = np.arange(coords.size)
    np.add.at(out, (rows, lo), 1.0 - frac)
    np.add.at(out, (rows, hi), frac)
    return out


def _tissue(crop, *, filled=False):
    """Tissue at every pixel of the crop, from the scan's overview mask
    (grown by `tissue_pad_um` so a trace is not cut at a ragged rim)."""
    from scipy import ndimage

    scan = crop.scan
    overview = scan.shared("tissue_overview")
    if overview is not None and overview.size > 1:
        mask = overview.astype(bool)
        if filled:
            mask = ndimage.binary_fill_holes(mask)
        factor = float(scan.meta.get("overview_factor") or 1.0)
        oh, ow = mask.shape
        oy = np.clip((crop.rows / factor).astype(np.int64), 0, oh - 1)
        ox = np.clip((crop.cols / factor).astype(np.int64), 0, ow - 1)
        tissue = mask[np.ix_(oy, ox)]
    else:
        fraction = scan.shared("tissue_fraction")
        if fraction is None:
            return np.ones(crop.shape, dtype=bool)
        grid_mask = fraction >= 0.25
        if filled:
            grid_mask = ndimage.binary_fill_holes(grid_mask)
        tissue = crop.nearest(grid_mask)
    pad = int(round(crop.px(REFINE["tissue_pad_um"])))
    return _dilate(tissue, pad) if pad >= 1 else tissue


def _rasterise(geometry, crop):
    """A GeoJSON polygon (full-resolution pixels) as a mask of the crop."""
    import cv2
    from shapely.geometry import MultiPolygon, shape

    canvas = np.zeros(crop.shape, dtype=np.uint8)
    x0, y0 = crop.box[0], crop.box[1]
    found = shape(geometry)

    def points(coords):
        ring = np.asarray(coords, dtype=np.float64) / crop.factor - [x0, y0] - 0.5
        return np.round(ring * 16).astype(np.int32).reshape(-1, 1, 2)

    for polygon in (found.geoms if isinstance(found, MultiPolygon) else [found]):
        cv2.fillPoly(canvas, [points(polygon.exterior.coords)], 1, lineType=cv2.LINE_8,
                     shift=4)
        for hole in polygon.interiors:
            cv2.fillPoly(canvas, [points(hole.coords)], 0, lineType=cv2.LINE_8, shift=4)
    return canvas.astype(bool)


# -- morphology (cv2: a 20 px disk on a 4 MP mask is milliseconds) ---------------------


def _kernel(radius):
    import cv2

    r = max(1, int(round(radius)))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def _dilate(mask, radius):
    import cv2

    if radius < 1 or not mask.any():
        return mask
    return cv2.dilate(mask.astype(np.uint8), _kernel(radius)).astype(bool)


def _erode(mask, radius):
    import cv2

    if radius < 1:
        return mask
    return cv2.erode(mask.astype(np.uint8), _kernel(radius)).astype(bool)


def _closing(mask, radius):
    import cv2

    if radius < 1 or not mask.any():
        return mask
    r = max(1, int(round(radius)))
    # Padded so a closing is not cut short at the crop's border.
    padded = np.pad(mask.astype(np.uint8), r + 1)
    closed = cv2.morphologyEx(padded, cv2.MORPH_CLOSE, _kernel(r))
    return closed[r + 1:-(r + 1), r + 1:-(r + 1)].astype(bool) | mask


def _fill_small_holes(mask, max_px):
    from scipy import ndimage

    if max_px <= 0 or not mask.any():
        return mask
    filled = ndimage.binary_fill_holes(mask)
    if math.isinf(max_px):
        return filled
    holes = filled & ~mask
    if not holes.any():
        return mask
    count, labels, stats, _ = _components(holes)
    small = np.zeros(count, dtype=bool)
    small[1:] = stats[1:, 4] <= max_px
    return mask | small[labels]


def _components(mask):
    import cv2

    return cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)


def _drop_small(mask, min_px):
    if min_px <= 1 or not mask.any():
        return mask
    count, labels, stats, _ = _components(mask)
    keep = np.zeros(count, dtype=bool)
    keep[1:] = stats[1:, 4] >= min_px
    return keep[labels]


def _gaussian(plane, sigma):
    import cv2

    if sigma < 0.3:
        return plane
    return cv2.GaussianBlur(plane, (0, 0), sigmaX=float(sigma), sigmaY=float(sigma),
                            borderType=cv2.BORDER_REFLECT)


# -- per-pixel evidence ---------------------------------------------------------------


class _Evidence:
    def __init__(self, crop, planes, about, note, tissue, envelope_px, envelope_mask):
        self.crop = crop
        self.scan = crop.scan
        self.planes = planes
        self.about = about
        self.note = note
        self.tissue = tissue
        self.envelope_px = envelope_px
        self.envelope_mask = envelope_mask

    # bright specks and plateaus

    def _bright_level(self, name):
        """How bright the brightest real cells are: a high percentile of the
        per-cell 90th percentiles (specks cover too little of a cell to move
        its p90)."""
        values = self.scan.map(name, "p90")
        if values is None:
            return 0.0
        data = values[self.scan.tissue() & np.isfinite(values)]
        return float(np.percentile(data, REFINE["bright_cells_percentile"])) if data.size \
            else 0.0

    def bright(self, name):
        import cv2

        plane = self.planes[name]
        summary = self.scan.channel(name).get("summary") or {}
        p50 = float(summary.get("p50_tissue") or 0.0)
        p999 = float(summary.get("p999_tissue") or 0.0)
        threshold = max(1.0, REFINE["compact_of_range"] * (p999 - p50))
        radius = max(1, int(round(self.crop.px(REFINE["tophat_um"]))))
        opened = cv2.morphologyEx(plane, cv2.MORPH_OPEN, _kernel(radius))
        speck = (plane - opened) > threshold
        floor = max(REFINE["plateau_ratio"] * self._bright_level(name), p50 + threshold)
        plateau = _gaussian(plane, self.crop.px(REFINE["plateau_sigma_um"])) >= floor
        return speck | plateau

    # diffuse brightness and darkness

    def _sigma_um(self):
        cell_um = self.scan.grid.get("cell_um") or \
            self.crop.cell * (scanmod.CELL_UM / scanmod.CELL_PX)
        return float(np.clip(REFINE["diffuse_sigma_cells"] * cell_um,
                             REFINE["diffuse_sigma_min_um"], REFINE["diffuse_sigma_max_um"]))

    def _baseline(self, name):
        """(baseline at every pixel, spread below, spread above): the scan's
        `derive` surround of the channel's mean map -- a linear mean,
        comparable with a linearly smoothed plane -- and how far cells
        normally sit below and above it, apart. One spread would be a
        marker's positive cells' (large), and would hide dark tissue."""
        from scipy import ndimage

        key = ("baseline", name)
        if key in self.crop._cache:
            return self.crop._cache[key]
        values = self.scan.map(name, "mean")
        logged = np.log1p(np.maximum(np.nan_to_num(values, nan=0.0), 0)).astype(np.float32)
        on = self.scan.tissue() & np.isfinite(values)
        fill = float(np.median(logged[on])) if on.any() else float(np.median(logged))
        filled = np.where(on, logged, fill).astype(np.float32)
        surround = ndimage.median_filter(filled, size=9, mode="nearest")
        residual = (filled - surround)[on]
        centre = float(np.median(residual)) if residual.size else 0.0

        def spread(side):
            data = np.abs(side - centre)
            return max(REFINE["diffuse_floor"], float(np.median(data)) * 1.4826) \
                if data.size else REFINE["diffuse_floor"]

        out = (self.crop.bilinear(surround) + centre,
               spread(residual[residual <= centre]), spread(residual[residual >= centre]))
        self.crop._cache[key] = out
        return out

    def z(self, name):
        """The smoothed plane's distance from its surround, in spreads of the
        side it lies on."""
        key = ("z", name)
        if key in self.crop._cache:
            return self.crop._cache[key]
        smoothed = _gaussian(self.planes[name], self.crop.px(self._sigma_um()))
        s = np.log1p(np.maximum(smoothed, 0)).astype(np.float32)
        baseline, below, above = self._baseline(name)
        delta = s - baseline
        out = np.where(delta < 0, delta / np.float32(below), delta / np.float32(above))
        self.crop._cache[key] = out.astype(np.float32)
        return self.crop._cache[key]

    def vote(self, masks):
        """Where at least `agree` of the channels say so (one mask per plane,
        in reading order), and always the required one (the nuclear stain)."""
        count = np.sum([m.astype(np.uint8) for m in masks], axis=0)
        need = max(1, int(math.ceil(REFINE["agree"] * len(masks))))
        out = count >= need
        names = list(self.planes)
        required = self.note.get("required")
        if required in names:
            out &= masks[names.index(required)]
        return out

    # the methods

    def bright_compact(self):
        return self.bright(next(iter(self.planes)))

    def bright_multi(self):
        masks = [self.bright(name) for name in self.planes]
        count = np.sum([m.astype(np.uint8) for m in masks], axis=0)
        return count >= min(2, len(masks))

    def saturation(self):
        """Clipped pixels, and the bright plateau they sit in: a region the
        agent calls saturated often clips in only a few pixels (or only at
        its display window), while the bright bodies around them are the
        artifact all the same."""
        name = next(iter(self.planes))
        ceiling = (self.scan.channel(name).get("summary") or {}).get("ceiling")
        clipped = self.planes[name] >= REFINE["saturation_of_ceiling"] * float(ceiling)
        return clipped | self.bright(name)

    def strong(self, name, cut, sign=1):
        """Where a channel's z passes `cut` and half its typical strength
        there: the smoothing spreads a patch's edge outwards, and a blurred
        step's edge is where it is half-way up."""
        z = sign * self.z(name)
        cut = sign * cut
        over = z >= cut
        inside = over & self.envelope_px
        if inside.any():
            cut = max(cut, REFINE["half_strength"] * float(np.median(z[inside])))
        return z >= cut

    def diffuse_bright(self):
        return self.vote([self.strong(name, REFINE["bright_z"]) for name in self.planes])

    def diffuse_abs(self):
        return self.vote([self.strong(name, REFINE["bright_z"])
                          | self.strong(name, -REFINE["bright_z"], -1) for name in self.planes])

    def dark(self):
        return self.vote([self.strong(name, REFINE["dark_z"], -1) for name in self.planes])

    def cycle_loss(self):
        from plexora.plugins.qc.server.scan import _smoothed_log

        reference, this = list(self.planes)
        sigma = self.crop.px(REFINE["loss_sigma_um"])
        smooth_ref = _smoothed_log(self.planes[reference], sigma)
        smooth_this = _smoothed_log(self.planes[this], sigma)
        return smooth_this < smooth_ref + np.log(max(self.note["ratio"], 1e-6)) \
            - np.log(REFINE["loss_ratio"])

    def blur(self):
        import cv2

        plane = self.planes[next(iter(self.planes))]
        logged = np.log1p(np.maximum(plane, 0)).astype(np.float32)
        lap = cv2.Laplacian(logged, cv2.CV_32F, ksize=1)
        window = max(3, int(round(self.crop.px(REFINE["focus_window_um"]))) | 1)
        energy = cv2.blur(lap * lap, (window, window))
        mean = cv2.blur(logged, (window, window))
        variance = np.maximum(cv2.blur(logged * logged, (window, window)) - mean * mean, 0)
        ratio = energy / (variance + scanmod.FOCUS_EPS)
        structured = self.tissue & (np.sqrt(variance) >= REFINE["focus_min_contrast"])
        # The reference is the tissue round the envelope (a map cell out),
        # else whatever of the crop lies outside it, else its sharper quarter.
        from scipy import ndimage

        grown = ndimage.binary_dilation(self.envelope_mask, iterations=1)
        ring = structured & ~self.crop.nearest(grown)
        outside = structured & ~self.envelope_px
        if ring.sum() >= REFINE["blur_min_ref_px"] or (
                ring.sum() >= 0.25 * REFINE["blur_min_ref_px"] and ring.sum() >= outside.sum()):
            reference, source = float(np.median(ratio[ring])), "ring"
        elif outside.sum() >= 0.25 * REFINE["blur_min_ref_px"]:
            reference, source = float(np.median(ratio[outside])), "outside"
        elif structured.any():
            reference, source = float(np.percentile(ratio[structured], 75)), "sharpest_quarter"
        else:
            raise _Fallback("no structured tissue to judge focus by")
        self.note["focus_reference"] = source
        if reference <= 0:
            raise _Fallback("the focus reference is flat")
        with np.errstate(divide="ignore", invalid="ignore"):
            log2 = np.log2(np.maximum(ratio, 1e-12) / reference)
        return structured & (log2 <= REFINE["focus_log2"])

    def edge_band(self):
        band = int(round(self.crop.px(REFINE["edge_band_um"])))
        filled = self.tissue
        return filled & ~_erode(filled, band)


class _Fallback(Exception):
    pass


# -- the trace ------------------------------------------------------------------------


def _post(mask, method, crop, tissue, envelope_px, margin_um=None):
    p = POST[method]
    area = crop.um * crop.um
    mask = mask & tissue
    mask = _closing(mask, crop.px(p["close_um"])) if p["close_um"] else mask
    mask = _fill_small_holes(mask, p["hole_max_um2"] / area)
    mask = _drop_small(mask, p["min_area_um2"] / area)
    margin = p["margin_um"] if margin_um is None else float(margin_um)
    if margin > 0:
        mask = _dilate(mask, max(1.0, crop.px(margin)))
    return mask & envelope_px


def _cap_parts(mask, crop, envelope_px):
    """(mask, parts, dropped): at most `max_parts` parts -- merged by a growing
    closing first (which keeps every speck), only then the smallest dropped."""
    limit = int(REFINE["max_parts"])
    count = _components(mask)[0] - 1
    radius = 2.0
    while count > limit and radius * crop.um <= REFINE["parts_merge_max_um"]:
        mask = _closing(mask, radius) & envelope_px
        count = _components(mask)[0] - 1
        radius *= 2
    dropped = 0
    if count > limit:
        n, labels, stats, _ = _components(mask)
        order = np.argsort(-stats[1:, 4], kind="stable")[:limit] + 1
        keep = np.zeros(n, dtype=bool)
        keep[order] = True
        mask = keep[labels]
        dropped = count - limit
        count = limit
    return mask, int(count), int(dropped)


def _guards(mask, crop, method, about, envelope_mask, primary):
    """{name: {ok, ...}} and the first failure's reason (or None)."""
    ny, nx = crop.scan.grid["shape"]
    traced, total = crop.cell_counts(mask)
    in_crop = total > 0
    out = {}
    failure = None
    peak = about.get("peak")
    if peak:
        iy = int(np.clip(peak[1] // crop.cell, 0, ny - 1))
        ix = int(np.clip(peak[0] // crop.cell, 0, nx - 1))
        if envelope_mask[iy, ix] and in_crop[iy, ix]:
            ok = bool(traced[iy, ix] >= 1)
            out["peak"] = {"ok": ok, "cell": [iy, ix], "traced_px": int(traced[iy, ix])}
            if not ok:
                failure = "the trace misses the candidate's strongest cell"
    metric, sign = GUARD_METRIC.get(method, (None, 1))
    values = None
    if metric is not None:
        name = (about["primary_metric"] or "").partition("::")
        if name[1] and name[2] == metric and crop.scan.map(name[0], metric) is not None:
            values = crop.scan.map(name[0], metric)
        elif primary:
            values = crop.scan.map(primary, metric)
    if values is not None:
        if metric in ("focus_rel",):
            values = np.log(np.maximum(values, 1e-6))
        elif metric == "median":
            values = np.log1p(np.maximum(values, 0))
        where = envelope_mask & in_crop & np.isfinite(values) & crop.scan.tissue()
        centre = float(np.nanmedian(values[crop.scan.tissue() & np.isfinite(values)])) \
            if where.any() else 0.0
        # Only cells worse than the tissue's middle are "strongest": where the
        # map is flat (a saturation map at zero), ties would name any cell.
        where &= np.nan_to_num(sign * (values - centre)) > 0
        if where.any():
            score = np.where(where, sign * (values - centre), -np.inf)
            n = int(where.sum())
            k = int(np.clip(math.ceil(REFINE["top_cells_share"] * n), 1,
                            REFINE["top_cells_max"]))
            flat = np.argsort(-score, axis=None, kind="stable")[:k]
            cells = [divmod(int(i), nx) for i in flat]
            touched = [c for c in cells if traced[c] >= 1]
            share = len(touched) / len(cells)
            ok = share >= REFINE["top_cells_min_covered"]
            out["top_cells"] = {"ok": ok, "covered": len(touched), "of": len(cells),
                                "metric": metric}
            if not ok and failure is None:
                missed = [list(c) for c in cells if traced[c] < 1][:5]
                out["top_cells"]["missed"] = missed
                failure = (f"the trace misses {len(cells) - len(touched)} of the "
                           f"{len(cells)} strongest cells ({metric})")
    return out, failure


def refine(candidate, envelope_mask, scan, source, *, pixel_um, envelope=None,
           options=None) -> RefinementResult:
    """Trace `candidate` inside its envelope: the grid `envelope_mask`, and
    `envelope` (GeoJSON, full resolution) when it is not exactly those cells.

    `options`: `margin_um` overrides the method's margin; any `REFINE` key
    overrides that number. `source` is an open `SourceImage`."""
    from plexora.plugins.qc.server import polygons

    started = time.perf_counter()
    options = dict(options or {})
    margin_um = options.pop("margin_um", None)
    overrides = {k: v for k, v in options.items() if k in REFINE}
    about = describe(candidate, scan)
    method = method_for(about["class"])
    envelope_mask = np.asarray(envelope_mask, dtype=bool)
    if envelope is None:
        envelope = envelope_geometry(envelope_mask, scan.grid) if envelope_mask.any() else None
    envelope_area = polygons.area_of(envelope)
    px_area = (pixel_um or 0) ** 2 or None
    params = {"version": VERSION, "method": method, "margin_um": margin_um,
              "assumed_pixel": not pixel_um, **overrides}
    result = RefinementResult(status="not_applicable", reason="", method=method,
                              geometry=envelope, envelope_area_px2=envelope_area,
                              envelope_area_um2=envelope_area * px_area if px_area else None,
                              kept_fraction=1.0, params=params)

    def finish(status, reason):
        result.status = status
        result.reason = reason
        if status != "refined":
            result.geometry = envelope
            result.mask = None
            result.area_px2 = envelope_area
            result.area_um2 = result.envelope_area_um2
            result.kept_fraction = 1.0
        result.timing["total_s"] = round(time.perf_counter() - started, 4)
        return result

    if envelope is None or not envelope_mask.any():
        return finish("not_applicable", "the envelope is empty")
    if method is None:
        from plexora.plugins.qc.server import schemas

        words = schemas.CLASS_WORDS.get(about["class"], about["class"] or "region")
        return finish("not_applicable", f"a {words} is the region its outline names")
    if method == "saturation" and (scan.meta.get("brightfield") or not (
            scan.channel(_primary(about, _usable(scan)) or scan.channels[0]["name"])
            .get("summary") or {}).get("ceiling")):
        return finish("not_applicable", "no saturation ceiling to trace against")
    channels, note = _channels(method, about, scan, envelope_mask)
    if channels is None:
        return finish("not_applicable", note.get("why") or "no channel to trace")
    result.channels = list(channels)
    from shapely.geometry import shape

    bounds = shape(envelope).bounds
    pad = (REFINE["blur_context_cells"] + 1) * float(scan.grid["cell_full_px"]) \
        if method == "blur" else 0.0
    # Clipping is a full-resolution fact: a pyramid level averages it away.
    level_options = {**overrides, "target_um": 0.0} if method == "saturation" else overrides
    chosen, why = choose_level(source, bounds, pixel_um, len(channels), pad_full=pad,
                               options=level_options)
    if chosen is None:
        return finish("not_applicable", why)
    level, factor, um, box = chosen
    result.level, result.factor, result.refine_um, result.box_level = level, factor, um, box
    crop = _Crop(scan, box, factor, um)
    read_started = time.perf_counter()
    planes = {}
    try:
        for name in channels:
            planes[name] = _read_plane(source, scan, name, level, box, crop.shape)
            result.pixels_read += int(planes[name].size)
    except Exception as exc:
        return finish("fallback", f"could not read the pixels: {exc}")
    result.timing["read_s"] = round(time.perf_counter() - read_started, 4)
    trace_started = time.perf_counter()
    tissue = _tissue(crop, filled=method in ("dark", "edge_band"))
    envelope_px = _rasterise(envelope, crop)
    evidence = _Evidence(crop, planes, about, note, tissue, envelope_px, envelope_mask)
    try:
        raw = getattr(evidence, method)()
    except _Fallback as exc:
        return finish("fallback", str(exc))
    mask = _post(raw, method, crop, tissue, envelope_px, margin_um)
    mask, parts, dropped = _cap_parts(mask, crop, envelope_px)
    result.parts, result.dropped_parts = parts, dropped
    if note.get("focus_reference"):
        result.params["focus_reference"] = note["focus_reference"]
    result.timing["trace_s"] = round(time.perf_counter() - trace_started, 4)
    if not mask.any():
        return finish("fallback", f"nothing traced inside the envelope ({WORDS[method]})")
    guards, failure = _guards(mask, crop, method, about, envelope_mask,
                              channels[0] if channels else None)
    result.guards = guards
    if failure is not None:
        return finish("fallback", failure)
    try:
        traced = mask_to_polygon(mask, (box[0], box[1]), factor,
                                 simplify_px=REFINE["simplify_px"] * factor)
        traced = polygons.clip_to(traced, envelope)
    except Exception as exc:
        return finish("fallback", f"the trace is not a valid outline: {exc}")
    if traced is None:
        return finish("fallback", "the trace is empty inside the envelope")
    result.geometry = traced
    result.mask = mask
    result.area_px2 = polygons.area_of(traced)
    result.area_um2 = result.area_px2 * px_area if px_area else None
    result.kept_fraction = result.area_px2 / envelope_area if envelope_area else 1.0
    reason = f"traced {WORDS[method]}"
    if result.kept_fraction > REFINE["full_fraction"]:
        reason += "; the artifact fills its envelope"
    if dropped:
        reason += f"; {dropped} smallest parts left out"
    return finish("refined", reason)


def _read_plane(source, scan, name, level, box, shape):
    from plexora.plugins.qc.server.scan import _read

    meta = scan.channel(name)
    brightfield = bool(scan.meta.get("brightfield"))
    index = 0 if brightfield else source.channel_index(meta.get("key"))
    if index is None:
        index = int(meta.get("index") or 0)
    plane, _clipped = _read(source, index, level, tuple(box), brightfield)
    plane = np.asarray(plane, dtype=np.float32)
    if plane.shape != shape:
        out = np.zeros(shape, dtype=np.float32)
        h, w = min(shape[0], plane.shape[0]), min(shape[1], plane.shape[1])
        out[:h, :w] = plane[:h, :w]
        plane = out
    return plane


# -- mask to polygon ----------------------------------------------------------------------


def mask_to_polygon(mask, origin, factor, *, simplify_px, min_area_px=0.0,
                    max_vertices=None):
    """GeoJSON (full-resolution pixels, holes kept) of a crop's mask whose
    pixel (0, 0) is level pixel `origin`.

    `cv2.findContours` with RETR_CCOMP gives every outer ring and its holes
    through the boundary pixels' centres; half a pixel of mitred buffer puts
    the edges back on the pixels' outer sides (and a hole's on its inner)."""
    import cv2
    import shapely
    from shapely.geometry import LineString, Point, Polygon

    from plexora.plugins.qc.server import polygons

    mask = np.ascontiguousarray(mask.astype(np.uint8))
    if not mask.any():
        return None
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    hierarchy = hierarchy[0]
    solid, thin = [], []

    def ring(index):
        return contours[index].reshape(-1, 2).astype(np.float64) + 0.5

    for index in range(len(contours)):
        if hierarchy[index][3] != -1:
            continue
        outer = ring(index)
        holes = []
        child = hierarchy[index][2]
        while child != -1:
            hole = ring(child)
            if len(hole) >= 3:
                holes.append(hole)
            child = hierarchy[child][0]
        polygon = Polygon(outer, holes) if len(outer) >= 3 else None
        if polygon is not None and polygon.area > 0:
            if not polygon.is_valid:
                polygon = shapely.make_valid(polygon)
            solid.append(polygon)
        elif len(outer) >= 2:
            thin.append(LineString(outer).buffer(0.5, cap_style="square", join_style="mitre"))
        else:
            thin.append(Point(outer[0]).buffer(0.5, cap_style="square"))
    parts = []
    if solid:
        merged = polygons._polygonal(shapely.union_all(solid))
        if merged is not None:
            parts.append(merged.buffer(0.5, join_style="mitre", mitre_limit=2.0))
    parts.extend(thin)
    if not parts:
        return None
    shape_ = shapely.union_all(parts)
    ox, oy = float(origin[0]), float(origin[1])
    shape_ = shapely.transform(shape_, lambda xy: (xy + [ox, oy]) * float(factor))
    shape_ = polygons._polygonal(shape_)
    return polygons.to_geojson(shape_, simplify_px=simplify_px,
                               max_vertices=max_vertices or polygons.MAX_VERTICES,
                               min_area_px=min_area_px)
