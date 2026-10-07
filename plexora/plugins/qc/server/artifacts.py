"""Artifact Detector: folds, tears, debris and saturation as snug regions.

Coarse to fine, across every channel, without a model (after the standalone
`artifact_scan.py`, made multi-channel):

1. **Stage 1, one pass over every channel** at the finest pyramid level whose
   longer side is at most `stage1_max_side` (4096 px). Each channel is read
   whole once; its smoothed log intensity becomes a robust z against the
   tissue (`agree_bright` / `agree_dark` count, per pixel, how many channels
   are raised or lowered there), its saturated pixels are kept as seeds, and
   a subset of channels (`pan_channels`: one nuclear per cycle, then markers
   evenly spaced) is averaged into the *pan* image, each scaled by its own
   tissue p99 so no one marker dominates.
2. **Seeds** on the pan: the tissue (Otsu, closed, holes filled), the
   analysis region (the tissue opened by `tissue_min_width_um`, so peeled
   ribbons fall away, then grown by `feather_um`), and per category:
   `fold` -- broad, bright, inside the tissue and raised in most channels
   (and in the nuclear one); `tear` -- inside the tissue body at least
   `tear_inset_um` from its border, holding under `tear_level` of the
   tissue's signal over the glass; `debris` -- off the tissue only (on it,
   collagen and bright cells look the same): long thin `fiber`s by a white
   top-hat, hysteresis and a directional close, and `compact` objects by
   their contrast to the glass; `saturation` -- per channel, pixels at a
   share of the channel's own ceiling (12-bit data in a uint16 file clips at
   4095, not 65535).
3. **Attribution**: one more read per channel takes the mean z of every
   seed (`ndimage.mean` over one label image), so each object says which
   channels show it and which shows it most (`source_channel`).
4. **Stage 2, per merged box** (seeds padded and merged by rasterising,
   never pairwise): the finest level the box fits at within a pixel budget is
   read, and each seed is regrown by hysteresis on that level's evidence --
   a fold over the share of channels raised against their own neighbourhood,
   a tear over the empty level, debris over its contrast to the glass ring
   round it, saturation confirmed at `sat_confirm` of the ceiling at full
   resolution. A seed whose fine evidence is empty, or that the budget
   cannot afford, keeps its coarse outline (`refined: false`).

Every object carries a continuous `strength` (a fold's robust z, a tear's
depth, debris's share of the tissue's signal over the glass, saturation's log
area) and a 0..1 `score` from one saturating map (`soft`): 0 at the
detector's liberal universe gate, 0.5 at the script's published default.
The score field paints each place's own strength inside its object
(`_cell_score_maps`), so a review's strata differ within one object.
A category's threshold keeps objects at or above it, so a slider never
changes a geometry and never reads a pixel (`evaluate`).

Stored per project (`<QC store>/artifacts/<fp>.json|.npz`); `<fp>.npz`
holds the score fields on a 25 µm grid that the Auto QC session check reads.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import threading
import time
from time import perf_counter

import numpy as np

from plexora.agent.errors import AgentError

VERSION = "2"
CATEGORIES = ("fold", "tear", "debris", "saturation")
CATEGORY_CLASS = {"fold": "tissue_fold", "tear": "tissue_damage_or_detachment",
                  "debris": "debris_or_foreign_object", "saturation": "saturation_or_clipping"}
CLASS_CATEGORY = {v: k for k, v in CATEGORY_CLASS.items()}
CATEGORY_LABEL = {"fold": "Fold", "tear": "Tear", "debris": "Debris",
                  "saturation": "Saturation"}
CATEGORY_WORDS = {"fold": "tissue folds", "tear": "tears and detached tissue",
                  "debris": "debris and fibers off the tissue",
                  "saturation": "saturated (clipped) pixels"}
#: Default colours. Core's class colours make a fold and a tear nearly the
#: same red, so a tear gets a distinct blue.
PALETTE = {"fold": "#ef4444", "tear": "#0ea5e9", "debris": "#a855f7",
           "saturation": "#f97316"}
PARAMS_DEFAULT = {
    # µm per pixel at full resolution; None reads the image's own.
    "pixel_um": None,
    "categories": list(CATEGORIES),
    # Channels averaged into the pan image (the same subset at both stages:
    # every fine threshold is relative to the coarse statistics).
    "pan_channels": 8,
    "stage1_max_side": 4096, "stage1_max_pixels": 2.0e7,
    # Stage 2: a box's pixels times the channels it reads, per read.
    "stage2_max_pixels": 2.4e7, "stage2_min_um": 0.5, "stage2_max_um": 8.0,
    "max_total_fine_pixels": 2.0e9,
    # The tissue and the analysis region.
    "tissue_close_um": 60.0, "tissue_min_width_um": 200.0, "tissue_margin_um": 40.0,
    "feather_um": 100.0,
    # How far stage 2 may grow past a seed, and the context round it.
    "grow_um": 150.0, "halo_um": 50.0,
    # Per-channel agreement: a channel is raised (lowered) where its smoothed
    # log intensity is this many robust SDs above (below) the tissue's.
    "agree_sigma_um": 10.0, "agree_z": 1.5,
    # fold (universe gates; the score says how strong)
    "fold_scale_um": 40.0, "fold_z": 2.5, "fold_min_area_um2": 1.0e4, "fold_agree": 0.5,
    "fold_nuclear_z": 1.0, "fold_grow_frac": 0.28, "fold_smooth_um": 40.0,
    "fold_window_um": 400.0,
    # A fold seed lying in a band along the tissue's edge (at least
    # `fold_edge_share` of it within `fold_edge_band_um` of the border, thin
    # and long) is an epidermis or a crushed rim -- bright in the stained
    # markers -- unless the autofluorescence channel is raised there too by
    # `fold_af_z` (a real fold glows in the blank channel).
    "fold_edge_band_um": 100.0, "fold_edge_share": 0.7, "fold_edge_minor_um": 150.0,
    "fold_edge_aspect": 3.0, "fold_af_z": 1.0,
    # tear: the share of the tissue's signal (over the glass) a place holds
    "tear_level": 0.35, "tear_agree": 0.6, "tear_min_area_um2": 2.0e4,
    "tear_inset_um": 60.0,
    # Stage 2 grows a tear at most `tear_grow_um` past its seed, over places
    # at least `tear_grow_depth_z` deep and `tear_grow_seed_share` of the
    # seed's depth; a grown part whose mean depth is under
    # `tear_grown_mean_share` of the seed's has grown into tissue.
    "tear_grow_um": 75.0, "tear_grow_depth_z": 2.5, "tear_grow_seed_share": 0.7,
    "tear_grown_mean_share": 0.6,
    # debris (off the tissue)
    "fiber_max_width_um": 40.0, "fiber_min_length_um": 250.0, "fiber_min_aspect": 5.0,
    "fiber_min_elongation": 4.0, "fiber_max_solidity": 0.6, "fiber_weak_k": 2.5,
    "fiber_z": 3.0, "fiber_gap_um": 80.0,
    "debris_z": 3.0, "debris_min_area_um2": 150.0, "debris_max_area_um2": 5.0e4,
    "debris_min_solidity": 0.35,
    # The glass's spread is at least this share of the tissue's (and at
    # least the camera noise): a flat, quantised glass has a spread near
    # zero, and against it every speck was a z of hundreds.
    "debris_glass_spread_floor": 0.05,
    # saturation: seeded at a share of the ceiling, confirmed at another
    "sat_coarse": 0.35, "sat_confirm": 0.98, "sat_min_area_um2": 25.0,
    "max_seeds": 2000, "max_objects": 500, "max_vertices": 400,
}
#: The parameters an agent or the panel may set (the rest are engineering).
TUNABLE = ("pixel_um", "categories", "pan_channels", "feather_um", "tissue_min_width_um",
           "grow_um", "fold_z", "tear_level", "debris_z")
#: soft(strength, floor, half): 0 at the universe floor, 0.5 at the
#: published default. Saturation: 1000 µm² confirmed is 0.5. Debris: its
#: brightness as a share of the tissue's signal over the glass (the z is
#: only its gate), so a flat slide's speck is never 1.0 by a tiny spread.
SCORE_SCALE = {"fold": (2.0, 4.0), "tear": (1.5, 3.0), "debris": (0.1, 0.5),
               "saturation": (0.0, math.log2(1000.0 / 25.0))}
#: A fold seed's agreement at which its score is not discounted.
FOLD_FULL_AGREEMENT = 0.7
AUTO_THRESHOLD = 0.5
AUTO_RANGE = (0.05, 0.95)
#: One relative step (`adjust` tighter / looser) moves a bar this much.
STEP = 0.1
#: Refined at fine resolution, strongest first; the rest keep the coarse outline.
MAX_CANDIDATES = {"fold": 50, "tear": 50, "debris": 300, "saturation": 200}
#: The Auto QC score field's cell.
FIELD_CELL_UM = 25.0
HISTOGRAM_BINS = 20
#: A read is tiled above this side (the source refuses huge reads).
READ_TILE = 2048
#: The pan holds each channel's log intensity over its tissue p99, up to this.
PAN_CLIP = 1.5
#: Standard full-scale values a channel may clip at.
CEILINGS = (255.0, 1023.0, 4095.0, 16383.0, 65535.0)
EPS = 1e-6
PHASES = ("Finding the tissue", "Reading channels", "Finding candidates",
          "Attributing channels", "Refining objects", "Scoring")

_GUARD = threading.Lock()
_MEMORY: dict = {}
_EVALUATIONS: dict = {}


# -- where it lives ----------------------------------------------------------------------


def _folder(project):
    from plexora import api

    path = api.store(project, "qc").directory() / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path, data: bytes):
    """Write then replace; retried briefly, since a synced folder (Dropbox)
    may hold the target for a moment on Windows."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.05)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def settings(project) -> dict:
    """{per: {category: {threshold | offset_steps, color}}, channels: [...]}."""
    return _read_json(_folder(project) / "settings.json") or {}


def save_settings(project, **values):
    """Merge `values` in; a None removes the key (its default again)."""
    merged = {k: v for k, v in {**settings(project), **values}.items() if v is not None}
    _atomic_write(_folder(project) / "settings.json",
                  json.dumps(merged, sort_keys=True).encode("utf-8"))
    return merged


def category_settings(project, category) -> dict:
    return dict((settings(project).get("per") or {}).get(category) or {})


def save_category_settings(project, category, **values):
    """Merge `values` into one category's settings; a None removes the key."""
    stored = settings(project)
    per = dict(stored.get("per") or {})
    mine = {**(per.get(category) or {}), **values}
    per[category] = {k: v for k, v in mine.items() if v is not None}
    return save_settings(project, per=per)


# -- inputs and their fingerprint --------------------------------------------------------


def _stamp(path):
    if not path:
        return None
    try:
        stat = os.stat(path)
        return f"{stat.st_size}-{stat.st_mtime_ns}"
    except OSError:
        return None


def channel_names(record) -> list:
    return [c.get("fullname") or c.get("name") for c in record.image.real_channels]


def nuclear_names(names) -> list:
    from plexora.agent import presets

    return presets.nuclear_channels(names)


def pan_subset(names, limit) -> list:
    """The channels averaged into the pan: one nuclear channel per cycle (at
    most three), then the other channels evenly spaced, `limit` in all."""
    names = list(names)
    limit = max(1, int(limit))
    if len(names) <= limit:
        return names
    nuclear = nuclear_names(names)
    try:
        from plexora.plugins.qc.server import cycles

        per_cycle = [c["nuclear"] for c in cycles.infer(names)["cycles"] if c.get("nuclear")]
    except Exception:
        per_cycle = []
    chosen = list(dict.fromkeys(per_cycle or nuclear))[:min(3, limit)]
    others = [n for n in names if n not in chosen and n not in nuclear]
    room = limit - len(chosen)
    if room > 0 and others:
        picks = np.unique(np.round(np.linspace(0, len(others) - 1, min(room, len(others))))
                          .astype(int))
        chosen += [others[i] for i in picks]
    return [n for n in names if n in chosen]


def _level_shapes(source) -> list:
    return [tuple(int(v) for v in source.level_shape(lv))
            for lv in range(max(1, int(source.levels)))]


def _stage1_level(shapes, params) -> int:
    """The finest level whose longer side and size are within budget."""
    for lv, (h, w) in enumerate(shapes):
        if max(h, w) <= params["stage1_max_side"] and h * w <= params["stage1_max_pixels"]:
            return lv
    return len(shapes) - 1


def availability(session, project) -> dict:
    """{available, reason, hint}: whether this image can be checked at all,
    without reading a pixel."""
    from plexora.server.utils import pixel_scale, source_image

    record = session.project(project)
    if record.image.is_blank:
        return {"available": False, "reason": "no_image",
                "message": "this sample has no image to check"}
    try:
        with source_image.SHELF.reader(session.image_data(project)) as source:
            if source.is_brightfield:
                return {"available": False, "reason": "brightfield",
                        "message": "brightfield slides are not supported yet"}
    except Exception as exc:
        return {"available": False, "reason": "unreadable", "message": str(exc)[:200]}
    if pixel_scale.pixel_size(record) is None and not settings(project).get("pixel_um"):
        return {"available": False, "reason": "no_pixel_size",
                "message": "needs the image's pixel size (set it in the project, or pass "
                           "params.pixel_um)", "hint": "params.pixel_um"}
    return {"available": True, "reason": None, "message": None}


def _clean_params(params) -> dict:
    merged = {**PARAMS_DEFAULT, **{k: v for k, v in (params or {}).items() if v is not None}}
    unknown = sorted(set(params or {}) - set(PARAMS_DEFAULT))
    if unknown:
        raise AgentError("invalid_input", f"unknown artifact parameters: {unknown}",
                         detail={"parameters": sorted(PARAMS_DEFAULT)})
    cats = [c for c in CATEGORIES if c in (merged.get("categories") or CATEGORIES)]
    bad = sorted(set(merged.get("categories") or ()) - set(CATEGORIES))
    if bad or not cats:
        raise AgentError("invalid_input", f"categories must be some of {list(CATEGORIES)}",
                         detail={"categories": list(CATEGORIES)})
    merged["categories"] = cats
    return merged


def plan(session, project, *, params=None) -> tuple:
    """(fingerprint, context) of a run, without reading a pixel."""
    from plexora.server.utils import pixel_scale, source_image

    record = session.project(project)
    if record.image.is_blank:
        raise AgentError("unsupported_modality", "this sample has no image to check")
    params = _clean_params(params)
    names = channel_names(record)
    if not names:
        raise AgentError("precondition_missing", "the image has no channel to check")
    pixel_source = "params"
    pixel_um = params.get("pixel_um")
    if not pixel_um:
        stored = settings(project).get("pixel_um")
        if stored:
            pixel_um, pixel_source = float(stored), "settings"
        else:
            found = pixel_scale.pixel_size(record)
            if found is None:
                raise AgentError(
                    "precondition_missing",
                    "the Artifact Detector measures in microns and this image has no pixel "
                    "size; set it in the project or pass params.pixel_um",
                    detail={"hint": "params.pixel_um"})
            pixel_um, pixel_source = float(found["value"]), found.get("source") or "metadata"
    pixel_um = float(pixel_um)
    if not (0.01 <= pixel_um <= 100.0):
        raise AgentError("invalid_input", f"pixel_um {pixel_um} is not a plausible pixel size")
    image_data = session.image_data(project)
    with source_image.SHELF.reader(image_data) as source:
        if source.is_brightfield:
            raise AgentError("unsupported_modality",
                             "the Artifact Detector reads fluorescence channels; brightfield "
                             "slides are not supported yet")
        shapes = _level_shapes(source)
    keys = [source_image.channel_key(c) for c in record.image.real_channels]
    level1 = _stage1_level(shapes, params)
    pan = pan_subset(names, params["pan_channels"])
    identity = source_image.ReaderShelf.identity_of(image_data)
    image_stamp = None if str(identity or "").startswith("node://") else _stamp(identity)
    blob = json.dumps({"v": VERSION, "image": identity, "image_stamp": image_stamp,
                       "keys": keys, "shapes": shapes, "level1": level1, "pan": pan,
                       "params": {k: v for k, v in params.items() if k != "pixel_um"},
                       "pixel_um": pixel_um}, sort_keys=True, default=str)
    fp = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]
    return fp, {"record": record, "image_data": image_data, "identity": identity,
                "image_stamp": image_stamp, "names": names, "keys": keys, "shapes": shapes,
                "level1": level1, "pan": pan, "nuclear": nuclear_names(names),
                "params": params, "pixel_um": pixel_um, "pixel_um_source": pixel_source}


# -- small vectorised helpers ------------------------------------------------------------


def soft(strength, floor, half):
    """0 at or under `floor`, 0.5 at `half`, 0.75 one more (half - floor)
    above it: never saturating, so strong objects still rank."""
    s = np.asarray(strength, dtype=np.float64)
    span = max(float(half) - float(floor), 1e-9)
    with np.errstate(over="ignore", invalid="ignore"):
        out = np.where(s <= floor, 0.0, 1.0 - np.power(2.0, -(s - floor) / span))
    return np.nan_to_num(out, nan=0.0)


def _robust(values, limit=1_000_000):
    """(median, 1.4826 MAD) of a sample, subsampled to `limit`, spread floored."""
    v = np.asarray(values, dtype=np.float32).ravel()
    v = v[np.isfinite(v)]
    if not v.size:
        return 0.0, 1.0
    if v.size > limit:
        v = v[::int(math.ceil(v.size / limit))]
    med = float(np.median(v))
    mad = float(np.median(np.abs(v - med))) * 1.4826
    return med, max(mad, EPS)


def _disk(radius):
    import cv2

    r = max(int(round(radius)), 1)
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def _morph(mask, op, radius):
    import cv2

    if radius < 0.5:
        return mask.astype(bool)
    kernel = _disk(radius)
    if op == cv2.MORPH_CLOSE:
        return _closed(mask, kernel)
    return cv2.morphologyEx(mask.astype(np.uint8), op, kernel).astype(bool)


def _closed(mask, kernel):
    """A closing with nothing outside the image: cv2's default border counts
    as foreground for the erosion, which fills from an object to the edge."""
    import cv2

    pad = max(kernel.shape) // 2 + 1
    padded = np.pad(mask.astype(np.uint8), pad)
    out = cv2.morphologyEx(padded, cv2.MORPH_CLOSE, kernel)
    return out[pad:-pad, pad:-pad].astype(bool)


def _blur(plane, sigma):
    import cv2

    return cv2.GaussianBlur(plane, (0, 0), max(float(sigma), 0.3))


def _within(mask, distance_px):
    """Pixels within `distance_px` of `mask` (a dilation by an exact disc)."""
    import cv2

    if not mask.any():
        return np.zeros(mask.shape, dtype=bool)
    dist = cv2.distanceTransform((~mask).astype(np.uint8), cv2.DIST_L2, 5)
    return dist <= float(distance_px)


def _inside(mask):
    """Distance (px) of every pixel inside `mask` to its border (0 outside)."""
    import cv2

    padded = np.pad(mask.astype(np.uint8), 1)
    return cv2.distanceTransform(padded, cv2.DIST_L2, 5)[1:-1, 1:-1]


def _components(mask):
    """(n, labels int32, stats) with 8-connectivity; label 0 is background."""
    import cv2

    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8),
                                                           connectivity=8)
    return n, labels, stats


def _hysteresis(weak, strong):
    """Every weak component holding at least one strong pixel."""
    n, labels, _ = _components(weak)
    if n <= 1:
        return np.zeros(weak.shape, dtype=bool)
    hit = np.zeros(n, dtype=bool)
    hit[np.unique(labels[strong & weak])] = True
    hit[0] = False
    return hit[labels]


def _drop_small(mask, min_px):
    """Components of at least `min_px` pixels."""
    n, labels, stats = _components(mask)
    if n <= 1:
        return mask.astype(bool)
    keep = np.concatenate([[False], stats[1:, 4] >= max(1.0, float(min_px))])
    return keep[labels]


def _keep_largest_share(mask, share=0.01):
    """Components at least `share` of the largest one."""
    n, labels, stats = _components(mask)
    if n <= 2:
        return mask.astype(bool)
    areas = stats[1:, 4]
    keep = np.concatenate([[False], areas >= share * areas.max()])
    return keep[labels]


def _line_se(length, angle_deg):
    import cv2

    size = max(int(length) | 1, 3)
    c = size // 2
    kernel = np.zeros((size, size), np.uint8)
    dx, dy = np.cos(np.deg2rad(angle_deg)), -np.sin(np.deg2rad(angle_deg))
    cv2.line(kernel, (int(c - dx * c), int(c - dy * c)), (int(c + dx * c), int(c + dy * c)),
             1, 1)
    return kernel


def _close_lines(mask, length, angles=(0, 30, 60, 90, 120, 150)):
    """Close along each direction and union: joins collinear breaks in a
    fiber without fattening it across its width."""
    out = np.zeros(mask.shape, dtype=bool)
    for angle in angles:
        out |= _closed(mask, _line_se(length, angle))
    return out


def _label_quantile(values, labels, n, q):
    """The q-quantile of `values` within each label 1..n (NaN where empty),
    by one sort: no loop over labels."""
    flat = labels.ravel()
    inside = flat > 0
    lab = flat[inside]
    out = np.full(n, np.nan)
    if not lab.size:
        return out
    vals = np.asarray(values, dtype=np.float64).ravel()[inside]
    order = np.lexsort((vals, lab))
    lab, vals = lab[order], vals[order]
    counts = np.bincount(lab, minlength=n + 1)[1:n + 1]
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    has = counts > 0
    pos = starts + np.floor(q * np.maximum(counts - 1, 0)).astype(np.int64)
    out[has] = vals[pos[has]]
    return out


def _label_mean(values, labels, n):
    """Mean of `values` per label 1..n (NaN where empty), by bincount."""
    flat = labels.ravel()
    counts = np.bincount(flat, minlength=n + 1)[1:n + 1].astype(np.float64)
    sums = np.bincount(flat, weights=np.asarray(values, dtype=np.float64).ravel(),
                       minlength=n + 1)[1:n + 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(counts > 0, sums / counts, np.nan)


def _read_box(source, index, level, box):
    """(plane float32 of exactly the box's shape, dtype, pixels read): one
    channel's rectangle (x0, y0, x1, y1 at `level`), tiled so no single read
    is large; outside the image reads 0."""
    x0, y0, x1, y1 = (int(v) for v in box)
    h, w = max(0, y1 - y0), max(0, x1 - x0)
    out = np.zeros((h, w), dtype=np.float32)
    dtype = None
    for ty in range(y0, y1, READ_TILE):
        for tx in range(x0, x1, READ_TILE):
            bx1, by1 = min(x1, tx + READ_TILE), min(y1, ty + READ_TILE)
            plane, clipped = source.read(index, level, (tx, ty, bx1, by1))
            plane = np.asarray(plane)
            dtype = dtype or plane.dtype
            cx0, cy0, cx1, cy1 = (int(v) for v in clipped)
            ph, pw = min(plane.shape[0], cy1 - cy0), min(plane.shape[1], cx1 - cx0)
            if ph > 0 and pw > 0:
                out[cy0 - y0:cy0 - y0 + ph, cx0 - x0:cx0 - x0 + pw] = plane[:ph, :pw]
    return out, dtype, h * w


def _effective_ceiling(plane, dtype):
    """The value this channel clips at, or None. The dtype's maximum unless
    the data reach a lower standard full scale exactly (12-bit data in
    uint16 clip at 4095: a clipped pixel holds the full-scale value itself,
    not a bright value near it); a float channel has none."""
    if dtype is None or np.dtype(dtype).kind not in "ui":
        return None
    top = float(np.iinfo(np.dtype(dtype)).max)
    observed = float(plane.max()) if plane.size else 0.0
    for c in CEILINGS:
        if c >= top:
            break
        if observed == c and int((plane == observed).sum()) >= 4:
            return c
    return top


# -- the run -----------------------------------------------------------------------------


class _Job:
    """Progress, cancellation and accounting for one run."""

    def __init__(self, progress, check_cancelled, total):
        self.progress, self.check_cancelled = progress, check_cancelled
        self.total, self.done = int(total), 0
        self.pixels = 0

    def say(self, message, advance=1):
        self.done = min(self.total, self.done + advance)
        if self.progress is not None:
            self.progress(done=self.done, total=self.total, message=message)

    def stop_if_asked(self):
        if self.check_cancelled is not None:
            self.check_cancelled()


def _indices(source, context):
    out = []
    for position, key in enumerate(context["keys"]):
        index = source.channel_index(key)
        out.append(position if index is None else index)
    return out


def _stage1(source, context, job):
    """One read per channel at level 1: the pan, the agreement counters, the
    per-channel statistics and saturation seeds."""
    from plexora.plugins.qc.server import scan

    params, names = context["params"], context["names"]
    level1 = context["level1"]
    h, w = context["shapes"][level1]
    um1 = context["um1"]
    indices = context["indices"]
    sigma = params["agree_sigma_um"] / um1
    # The provisional tissue: the first nuclear channel's (else the first's).
    tissue_name = (context["nuclear"] or names)[0]
    plane, _dtype, n = _read_box(source, indices[names.index(tissue_name)], level1,
                                 (0, 0, w, h))
    job.pixels += n
    tissue0 = scan.tissue_estimate({tissue_name: plane}, tissue_name, False,
                                   sigma_px=scan.TISSUE_SMOOTH_UM / um1)["mask"]
    if tissue0.shape != (h, w) or not tissue0.any():
        tissue0 = np.ones((h, w), dtype=bool)
    del plane
    pan_set = set(context["pan"])
    pan_sum = np.zeros((h, w), dtype=np.float32)
    agree_bright = np.zeros((h, w), dtype=np.uint8)
    agree_dark = np.zeros((h, w), dtype=np.uint8)
    channel_stats = []
    saturation = {}
    sample = np.flatnonzero(tissue0.ravel())
    if sample.size > 1_000_000:
        sample = sample[::int(math.ceil(sample.size / 1_000_000))]
    for k, name in enumerate(names):
        job.stop_if_asked()
        job.say(f"{PHASES[1]} ({k + 1}/{len(names)})")
        a, dtype, n = _read_box(source, indices[k], level1, (0, 0, w, h))
        job.pixels += n
        ceiling = _effective_ceiling(a, dtype)
        if ceiling is not None and params["sat_coarse"] * ceiling <= float(a.max()):
            seed = a >= params["sat_coarse"] * ceiling
            sure = a >= params["sat_confirm"] * ceiling
            saturation[name] = {"seed": np.packbits(seed), "sure": np.packbits(sure)}
        la = np.log1p(np.maximum(a, 0.0))
        del a
        top = float(np.percentile(la.ravel()[sample], 99)) if sample.size else 1.0
        top = top if top > EPS else 1.0
        g = _blur(la, sigma)
        med, mad = _robust(g.ravel()[sample])
        z = (g - med) / mad
        agree_bright += (z > params["agree_z"]).astype(np.uint8)
        agree_dark += (z < -params["agree_z"]).astype(np.uint8)
        if name in pan_set:
            pan_sum += np.minimum(la / top, PAN_CLIP)
        channel_stats.append({"name": name, "p99_log": round(top, 5), "z_median": med,
                              "z_mad": mad, "ceiling": ceiling})
        del la, g, z
    pan = pan_sum / max(1, len(pan_set))
    del pan_sum
    return {"pan": pan, "agree_bright": agree_bright, "agree_dark": agree_dark,
            "tissue0": tissue0, "channels": channel_stats, "saturation": saturation,
            "shape": (h, w)}


def _regions(pan, context):
    """The tissue, its filled body, the analysis region and the glass."""
    import cv2

    from plexora.plugins.qc.server import scan

    params, um1 = context["params"], context["um1"]
    close_px = params["tissue_close_um"] / um1
    smoothed = _blur(pan, max(close_px / 6.0, 1.0))
    raw = scan._otsu_plane(smoothed)
    if raw is None or raw.mean() < 0.005:
        raw = smoothed > np.percentile(smoothed, 97.0)
    tissue = _morph(raw, cv2.MORPH_CLOSE, close_px / 2.0)
    tissue = _keep_largest_share(tissue)
    from scipy import ndimage

    filled = ndimage.binary_fill_holes(tissue)
    body = _morph(filled, cv2.MORPH_OPEN, params["tissue_min_width_um"] / um1 / 2.0)
    body = _keep_largest_share(body)
    if not body.any():
        body = filled
    roi = _within(body, params["feather_um"] / um1)
    din = _inside(filled)
    margin = params["tissue_margin_um"] / um1
    dout = _within(filled, margin)  # within the margin of the tissue
    glass = roi & ~dout
    core = din > margin
    if core.sum() < 64:
        core = filled
    return {"tissue": tissue, "filled": filled, "body": body, "roi": roi, "din": din,
            "glass": glass, "core": core}


def _shape_table(labels, n):
    from skimage import measure

    if n <= 0:
        return None
    return measure.regionprops_table(
        labels, properties=("label", "area", "perimeter", "solidity",
                            "axis_major_length", "axis_minor_length"))


def _cap_labels(labels, stats, keep_ids):
    """Relabel to `keep_ids` (1..k in that order); everything else 0."""
    lut = np.zeros(stats.shape[0], dtype=np.int32)
    lut[np.asarray(keep_ids, dtype=np.int64)] = np.arange(1, len(keep_ids) + 1)
    return lut[labels]


def _seeds(stage, regions, context):
    """{category: [seed dicts]} with one shared label image of physical
    seeds (`labels`) and per-channel saturation seeds."""
    import cv2

    params, um1 = context["params"], context["um1"]
    area_px = um1 * um1
    cats = set(params["categories"])
    pan = stage["pan"]
    n_channels = len(context["names"])
    filled, roi, core, glass, din = (regions[k] for k in ("filled", "roi", "core", "glass",
                                                          "din"))
    tis_med, tis_mad = _robust(_blur(pan, 10.0 / um1)[core])
    bg_med, bg_mad = _robust(pan[glass]) if glass.sum() >= 64 else \
        _robust(pan[~filled]) if (~filled).sum() >= 64 else (float(pan.min()), 1.0)
    floor = glass_spread_floor(pan, glass, tis_mad, params)
    bg_mad = max(bg_mad, floor)
    stats = {"tissue_median": tis_med, "tissue_mad": tis_mad, "glass_median": bg_med,
             "glass_mad": bg_mad, "glass_spread_floor": floor}
    labels = np.zeros(pan.shape, dtype=np.int32)
    seeds = []
    # Per-place strength maps on the score field's grid (`_cell_score_maps`).
    maps = {}
    grid = context.get("field")

    def add(category, comp_labels, comp_stats, ids, strength, extra):
        base = len(seeds)
        ids = np.asarray(ids, dtype=np.int64)
        if not ids.size:
            return
        lut = np.zeros(comp_stats.shape[0], dtype=np.int32)
        lut[ids] = base + 1 + np.arange(ids.size)
        painted = lut[comp_labels]
        labels[painted > 0] = painted[painted > 0]
        for j, cid in enumerate(ids):
            x, y, bw, bh, area = (int(v) for v in comp_stats[cid])
            seeds.append({"id": base + 1 + j, "category": category,
                          "bbox": [y, x, y + bh, x + bw], "area_px": area,
                          "strength": float(strength[j]),
                          **{k: (v[j] if isinstance(v, np.ndarray) else v)
                             for k, v in extra.items()}})

    def strongest(ids, strength, cap):
        order = np.argsort(-np.nan_to_num(strength, nan=-np.inf))[:cap]
        return ids[order], strength[order], order

    # fold: broad, bright, inside the tissue, raised in most channels
    if "fold" in cats:
        smooth = _blur(pan, params["fold_scale_um"] / um1 / 2.0)
        f_med, f_mad = _robust(smooth[core])
        z_fold = (smooth - f_med) / f_mad
        cand = filled & roi & (z_fold > params["fold_z"])
        cand = _morph(cand, cv2.MORPH_OPEN, params["fold_scale_um"] / um1 / 8.0)
        n, comp, cstats = _components(cand)
        if n > 1:
            ids = np.flatnonzero(cstats[:, 4] * area_px >= params["fold_min_area_um2"])
            ids = ids[ids > 0]
            agree = _label_mean(stage["agree_bright"].astype(np.float32) / n_channels,
                                comp, n - 1)
            strength = _label_quantile(z_fold, comp, n - 1, 0.5)
            ok = ids[agree[ids - 1] >= params["fold_agree"]]
            ok, s, order = strongest(ok, strength[ok - 1], params["max_seeds"])
            band = edge_band(comp, cstats, ok, din, um1, params)
            add("fold", comp, cstats, ok, s, {"agreement": agree[ok - 1], **band})
        stats["fold_median"], stats["fold_mad"] = f_med, f_mad
        maps["fold"] = _to_grid(z_fold, grid)
    # tear: emptied, well inside the tissue body -- near the glass's level,
    # or lowered in most channels at once -- and well under the tissue
    if "tear" in cats:
        smooth = _blur(pan, 10.0 / um1)
        span = max(tis_med - bg_med, EPS)
        level = (smooth - bg_med) / span
        depth_z = (tis_med - smooth) / tis_mad
        dark = stage["agree_dark"].astype(np.float32) / n_channels
        cand = ((level <= params["tear_level"]) | (dark >= params["tear_agree"])) & \
            (depth_z >= SCORE_SCALE["tear"][0]) & (din > params["tear_inset_um"] / um1) & roi
        cand = _morph(_morph(cand, cv2.MORPH_OPEN, 2), cv2.MORPH_CLOSE, 2)
        n, comp, cstats = _components(cand)
        if n > 1:
            ids = np.flatnonzero(cstats[:, 4] * area_px >= params["tear_min_area_um2"])
            ids = ids[ids > 0]
            depth = _label_quantile(depth_z, comp, n - 1, 0.5)
            held = _label_quantile(level, comp, n - 1, 0.5)
            agree = _label_mean(dark, comp, n - 1)
            ids, s, _ = strongest(ids, depth[ids - 1], params["max_seeds"])
            add("tear", comp, cstats, ids, s, {"agreement": agree[ids - 1],
                                                  "level": held[ids - 1]})
        maps["tear"] = _to_grid(depth_z, grid)
    # debris: off the tissue only
    if "debris" in cats and glass.any():
        z_bg = (pan - bg_med) / bg_mad
        # The strength: brightness as a share of the tissue's signal over
        # the glass (z only gates).
        level_bg = (pan - bg_med) / max(tis_med - bg_med, EPS)
        width_px = params["fiber_max_width_um"] / um1
        tophat = pan - cv2.morphologyEx(pan, cv2.MORPH_OPEN, _disk(max(width_px / 2.0, 1)))
        # Against the top-hat's own spread on the glass: on noise alone a
        # top-hat sits a couple of SDs up everywhere (the opening takes local
        # minima), so the raw glass spread would join all the glass into one.
        th_med, th_mad = _robust(tophat[glass])
        th_z = (tophat - th_med) / max(th_mad, floor)
        weak = glass & (th_z > params["fiber_weak_k"])
        strong = glass & (th_z > params["fiber_z"])
        # Speckles (noise that cleared the gate) go before the close, which
        # would otherwise bridge them into the fiber's outline.
        fiber = _drop_small(_hysteresis(weak, strong),
                            params["fiber_min_length_um"] / um1 / 8.0)
        fiber = _close_lines(fiber, params["fiber_gap_um"] / um1) & glass & (fiber | (th_z > 0))
        n, comp, cstats = _components(fiber)
        fiber_keep = np.zeros(0, dtype=np.int64)
        if n > 1:
            ids = np.arange(1, n)
            ids = ids[cstats[ids, 4] >= 3]
            if ids.size > params["max_seeds"]:
                ids = ids[np.argsort(-cstats[ids, 4])[:params["max_seeds"]]]
            comp = _cap_labels(comp, cstats, ids)
            cstats = np.concatenate([cstats[:1], cstats[ids]])
            n = ids.size + 1
            table = _shape_table(comp, n - 1)
            if table is not None and len(table["label"]):
                major = table["axis_major_length"]
                minor = np.maximum(table["axis_minor_length"], 1.0)
                elong = table["perimeter"] ** 2 / (4 * np.pi * np.maximum(table["area"], 1))
                # Long, and either straight and thin (a high axis ratio) or
                # branched (perimeter-elongated, not filling its hull).
                ok = ((major * um1 >= params["fiber_min_length_um"])
                      & ((major / minor >= params["fiber_min_aspect"])
                         | ((elong >= params["fiber_min_elongation"])
                            & (table["solidity"] <= params["fiber_max_solidity"]))))
                fiber_keep = np.asarray(table["label"][ok], dtype=np.int64)
                strength = _label_quantile(level_bg, comp, n - 1, 0.9)
                add("debris", comp, cstats, fiber_keep, strength[fiber_keep - 1],
                    {"shape": "fiber",
                     "length_um": np.asarray(major[ok] * um1, dtype=np.float64),
                     "aspect": np.asarray(major[ok] / minor[ok], dtype=np.float64),
                     "solidity": np.asarray(table["solidity"][ok], dtype=np.float64)})
        taken = labels > 0
        cand = glass & (z_bg > params["debris_z"])
        cand = _morph(cand, cv2.MORPH_CLOSE, 1) & ~_within(taken, 2)
        n, comp, cstats = _components(cand)
        if n > 1:
            ids = np.arange(1, n)
            area = cstats[ids, 4] * area_px
            ids = ids[(area >= params["debris_min_area_um2"])
                      & (area <= params["debris_max_area_um2"])]
            if ids.size > params["max_seeds"]:
                ids = ids[np.argsort(-cstats[ids, 4])[:params["max_seeds"]]]
            comp = _cap_labels(comp, cstats, ids)
            cstats = np.concatenate([cstats[:1], cstats[ids]])
            n = ids.size + 1
            table = _shape_table(comp, n - 1)
            if table is not None and len(table["label"]):
                ok = table["solidity"] >= params["debris_min_solidity"]
                keep = np.asarray(table["label"][ok], dtype=np.int64)
                strength = _label_quantile(level_bg, comp, n - 1, 0.9)
                add("debris", comp, cstats, keep, strength[keep - 1],
                    {"shape": "compact",
                     "solidity": np.asarray(table["solidity"][ok], dtype=np.float64)})
        maps["debris"] = _to_grid(level_bg, grid, how="max")
    # saturation: per channel, seeds that reach the analysis region
    saturated = []
    if "saturation" in cats:
        h, w = pan.shape
        for name, packed in stage["saturation"].items():
            seed = np.unpackbits(packed["seed"])[:h * w].reshape(h, w).astype(bool)
            sure = np.unpackbits(packed["sure"])[:h * w].reshape(h, w).astype(bool)
            seed = _morph(seed, cv2.MORPH_DILATE, 1)
            n, comp, cstats = _components(seed)
            if n <= 1:
                continue
            ids = np.arange(1, n)
            reach = _label_mean(roi.astype(np.float32), comp, n - 1) > 0
            ids = ids[reach]
            if not ids.size:
                continue
            sure_px = np.bincount(comp.ravel(), weights=sure.ravel().astype(np.float64),
                                  minlength=n)
            order = np.lexsort((-cstats[ids, 4], -sure_px[ids]))
            ids = ids[order][:params["max_seeds"]]
            for cid in ids:
                x, y, bw, bh, area = (int(v) for v in cstats[cid])
                saturated.append({"category": "saturation", "channel": name,
                                  "bbox": [y, x, y + bh, x + bw], "area_px": area,
                                  "sure_px": int(sure_px[cid]),
                                  "mask": comp[y:y + bh, x:x + bw] == cid})
    return {"labels": labels, "seeds": seeds, "saturation": saturated, "stats": stats,
            "maps": maps}


def glass_spread_floor(pan, glass, tissue_mad, params) -> float:
    """The least spread the glass is allowed (pan units): the larger of
    `debris_glass_spread_floor` of the tissue's spread and the camera noise
    on the glass (the pan less its own 2 px blur)."""
    floor = float(params["debris_glass_spread_floor"]) * float(tissue_mad)
    if glass.sum() >= 64:
        noise = pan - _blur(pan, 2.0)
        floor = max(floor, _robust(noise[glass])[1])
    return max(floor, EPS)


def edge_band(comp, cstats, ids, din, um, params) -> dict:
    """{edge_share, minor_um, aspect, edge_band} per component `ids` of
    `comp` (arrays in `ids` order): the share of it within
    `fold_edge_band_um` of the tissue's border (`din`, px inside), its minor
    axis and its axis ratio; `edge_band` when it is a thin, long band along
    the edge (an epidermis, a crushed rim) rather than a fold."""
    ids = np.asarray(ids, dtype=np.int64)
    out = {"edge_share": np.zeros(ids.size), "minor_um": np.zeros(ids.size),
           "aspect": np.ones(ids.size), "edge_band": np.zeros(ids.size, dtype=bool)}
    if not ids.size:
        return out
    capped = _cap_labels(comp, cstats, ids)
    near = (din <= params["fold_edge_band_um"] / um).astype(np.float32)
    share = np.nan_to_num(_label_mean(near, capped, ids.size))
    table = _shape_table(capped, ids.size)
    minor = np.zeros(ids.size)
    major = np.zeros(ids.size)
    if table is not None and len(table["label"]):
        at = np.asarray(table["label"], dtype=np.int64) - 1
        minor[at] = table["axis_minor_length"]
        major[at] = table["axis_major_length"]
    aspect = major / np.maximum(minor, 1.0)
    out.update(edge_share=share, minor_um=minor * um, aspect=aspect,
               edge_band=(share >= params["fold_edge_share"])
               & (minor * um <= params["fold_edge_minor_um"])
               & (aspect >= params["fold_edge_aspect"]))
    return out


def _to_grid(values, grid, how="mean"):
    """A level-1 map on the score field's grid (`grid`: nx, ny): the mean
    of each cell's pixels, or (`how="max"`, for objects smaller than a
    cell) the largest. None without a grid."""
    import cv2

    if grid is None:
        return None
    nx, ny = int(grid["nx"]), int(grid["ny"])
    plane = np.nan_to_num(np.asarray(values, dtype=np.float32), nan=0.0, posinf=0.0,
                          neginf=0.0)
    if how == "max":
        k = max(1, int(math.ceil(max(plane.shape[0] / ny, plane.shape[1] / nx))))
        plane = cv2.dilate(plane, np.ones((k, k), np.uint8))
        return cv2.resize(plane, (nx, ny), interpolation=cv2.INTER_NEAREST)
    return cv2.resize(plane, (nx, ny), interpolation=cv2.INTER_AREA)


def _attribute(source, context, stage, seeds, job):
    """{seed id: [z per channel]}: each seed's mean z in every channel (one
    read per channel), against the tissue for folds and tears and against
    the glass for debris."""
    labels = seeds["labels"]
    n = len(seeds["seeds"])
    names = context["names"]
    evidence = np.zeros((n, len(names)), dtype=np.float32)
    if not n:
        return evidence
    params, um1 = context["params"], context["um1"]
    sigma = params["agree_sigma_um"] / um1
    level1 = context["level1"]
    h, w = stage["shape"]
    debris = np.array([s["category"] == "debris" for s in seeds["seeds"]])
    glass = context["regions"]["glass"]
    glass_sample = np.flatnonzero(glass.ravel())
    for k, name in enumerate(names):
        job.stop_if_asked()
        job.say(f"{PHASES[3]} ({k + 1}/{len(names)})")
        a, _dtype, px = _read_box(source, context["indices"][k], level1, (0, 0, w, h))
        job.pixels += px
        g = _blur(np.log1p(np.maximum(a, 0.0)), sigma)
        del a
        cs = stage["channels"][k]
        means = _label_mean(g, labels, n)
        z = (means - cs["z_median"]) / cs["z_mad"]
        if debris.any() and glass_sample.size >= 64:
            g_med, g_mad = _robust(g.ravel()[glass_sample])
            # Floored as the pan's glass spread is (`glass_spread_floor`).
            g_mad = max(g_mad, params["debris_glass_spread_floor"] * cs["z_mad"])
            z = np.where(debris, (means - g_med) / g_mad, z)
        evidence[:, k] = np.nan_to_num(z)
        del g
    return evidence


def _merge_groups(rects, pad, shape):
    """Group rectangles (y0, x0, y1, x1) whose padded extents touch, by
    painting them onto a coarse canvas (a summed-area paint, then one
    labelling) instead of comparing pairs. Returns a group index per rect."""
    if not len(rects):
        return np.zeros(0, dtype=np.int64)
    h, w = shape
    ds = max(1, int(math.ceil(max(h, w) / 1024)))
    ch, cw = int(math.ceil(h / ds)), int(math.ceil(w / ds))
    r = np.asarray(rects, dtype=np.float64)
    y0 = np.clip(np.floor((r[:, 0] - pad) / ds), 0, ch).astype(np.int64)
    x0 = np.clip(np.floor((r[:, 1] - pad) / ds), 0, cw).astype(np.int64)
    y1 = np.clip(np.ceil((r[:, 2] + pad) / ds), 0, ch).astype(np.int64)
    x1 = np.clip(np.ceil((r[:, 3] + pad) / ds), 0, cw).astype(np.int64)
    diff = np.zeros((ch + 1, cw + 1), dtype=np.int32)
    np.add.at(diff, (y0, x0), 1)
    np.add.at(diff, (y0, x1), -1)
    np.add.at(diff, (y1, x0), -1)
    np.add.at(diff, (y1, x1), 1)
    cover = diff.cumsum(axis=0).cumsum(axis=1)[:ch, :cw] > 0
    _n, labels, _stats = _components(cover)
    cy = np.clip(((r[:, 0] + r[:, 2]) / 2.0 / ds).astype(np.int64), 0, ch - 1)
    cx = np.clip(((r[:, 1] + r[:, 3]) / 2.0 / ds).astype(np.int64), 0, cw - 1)
    return labels[cy, cx].astype(np.int64)


def _union_box(rects, pad, shape):
    r = np.asarray(rects, dtype=np.float64)
    h, w = shape
    return [int(max(0, math.floor(r[:, 0].min() - pad))),
            int(max(0, math.floor(r[:, 1].min() - pad))),
            int(min(h, math.ceil(r[:, 2].max() + pad))),
            int(min(w, math.ceil(r[:, 3].max() + pad)))]


def _fine_level(box1, context, n_reads, *, finest_um=None):
    """The finest level at which a level-1 box fits the read budget, or None
    when only level 1 itself does (or it is already the finest)."""
    params = context["params"]
    level1 = context["level1"]
    y0, x0, y1, x1 = box1
    f1 = context["factors"][level1]
    area0 = (y1 - y0) * (x1 - x0) * f1 * f1
    min_um = params["stage2_min_um"] if finest_um is None else finest_um
    for lv in range(level1):
        f = context["factors"][lv]
        um = context["pixel_um"] * f
        if um < min_um and lv + 1 < level1:
            continue
        if um > params["stage2_max_um"]:
            return None
        if area0 / (f * f) * max(1, n_reads) <= params["stage2_max_pixels"]:
            return lv
    return None


def _split_groups(members, rects, pad, shape, context, n_reads):
    """Split a group whose box no finer level affords along its long axis
    (by seed centre) until each part fits or is one seed."""
    out, todo = [], [np.asarray(members)]
    while todo:
        group = todo.pop()
        box = _union_box(rects[group], pad, shape)
        if len(group) == 1 or _fine_level(box, context, n_reads) is not None:
            out.append((group, box))
            continue
        centres = (rects[group][:, [0, 1]] + rects[group][:, [2, 3]]) / 2.0
        axis = 0 if (box[2] - box[0]) >= (box[3] - box[1]) else 1
        order = np.argsort(centres[:, axis], kind="stable")
        half = len(group) // 2
        todo.extend([group[order[:half]], group[order[half:]]])
    return out


def _upsample(crop, shape):
    import cv2

    return cv2.resize(crop.astype(np.uint8) if crop.dtype == bool else crop,
                      (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)


def _shape_of(mask, offset_xy, scale):
    """A shapely (Multi)Polygon of a boolean mask, in level-0 pixels: the
    contours (holes kept) through pixel centres, grown half a pixel back to
    the pixel edges (so a one-pixel fiber keeps its width)."""
    import cv2
    import shapely
    from shapely.geometry import LineString, Point, Polygon

    contours, hierarchy = cv2.findContours(mask.astype(np.uint8), cv2.RETR_CCOMP,
                                           cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    ox, oy = offset_xy
    rings = [(c.reshape(-1, 2).astype(np.float64) + 0.5 + (ox, oy)) * scale for c in contours]
    hier = hierarchy[0]
    parts = []
    for i, ring in enumerate(rings):
        if hier[i][3] != -1:
            continue
        holes = [rings[j] for j in range(len(rings)) if hier[j][3] == i and len(rings[j]) >= 3]
        if len(ring) >= 3:
            parts.append(Polygon(ring, holes))
        elif len(ring) == 2:
            parts.append(LineString(ring))
        else:
            parts.append(Point(ring[0]))
    shape = shapely.make_valid(shapely.union_all(parts))
    return shape.buffer(0.5 * scale, join_style=2)


def _objects_in(lab, n, seed_lab, *, category, offset_xy, scale, level, um_px, context,
                refined, field, channel=None, extra=None):
    """One object per component of `lab` (1..n): its outline, area, bbox,
    seeds and the field cells it covers."""
    params = context["params"]
    if n <= 0:
        return []
    # Component -> seeds it holds: unique (component, seed) pairs at once.
    both = (lab > 0) & (seed_lab > 0)
    pairs = np.unique(lab[both].astype(np.int64) * (1 << 31) + seed_lab[both]) \
        if both.any() else np.zeros(0, dtype=np.int64)
    comp_of, seed_of = pairs >> 31, pairs & ((1 << 31) - 1)
    # Component -> the field cells it covers.
    ys, xs = np.nonzero(lab)
    cell = field["cell_full_px"]
    fy = np.clip(((ys + 0.5 + offset_xy[1]) * scale / cell).astype(np.int64), 0, field["ny"] - 1)
    fx = np.clip(((xs + 0.5 + offset_xy[0]) * scale / cell).astype(np.int64), 0, field["nx"] - 1)
    keyed = np.unique(lab[ys, xs].astype(np.int64) * (field["ny"] * field["nx"])
                      + fy * field["nx"] + fx)
    cells_comp, cells_flat = keyed // (field["ny"] * field["nx"]), keyed % (field["ny"]
                                                                          * field["nx"])
    found = []
    slices = _bboxes(lab, n)
    filled = extra.get("filled") if extra else None
    for k in range(1, n + 1):
        sl = slices[k - 1]
        if sl is None:
            continue
        y0, y1, x0, x1 = sl
        mask = lab[y0:y1, x0:x1] == k
        area_px = int(mask.sum())
        if not area_px:
            continue
        shape = _shape_of(mask, (offset_xy[0] + x0, offset_xy[1] + y0), scale)
        geometry = _geojson(shape, scale, int(params["max_vertices"]))
        if geometry is None:
            continue
        cyx = np.argwhere(mask).mean(axis=0)
        on_tissue = float(filled[y0:y1, x0:x1][mask].mean()) if filled is not None else None
        found.append({
            "category": category, "class": CATEGORY_CLASS[category], "geometry": geometry,
            "bbox": [round((offset_xy[0] + x0) * scale, 1), round((offset_xy[1] + y0) * scale, 1),
                     round((offset_xy[0] + x1) * scale, 1), round((offset_xy[1] + y1) * scale, 1)],
            "centroid": [round((offset_xy[0] + x0 + cyx[1] + 0.5) * scale, 1),
                         round((offset_xy[1] + y0 + cyx[0] + 0.5) * scale, 1)],
            "area_px2": round(area_px * scale * scale, 1),
            "area_um2": round(area_px * um_px * um_px, 2),
            "seeds": seed_of[comp_of == k].astype(int).tolist(),
            "cells": cells_flat[cells_comp == k],
            "refined": bool(refined), "level": int(level),
            "tissue_fraction": None if on_tissue is None else round(on_tissue, 3),
            "channel": channel,
        })
    return found


def _geojson(shape, scale, max_vertices):
    """The ROI plugin's GeoJSON of a shape, simplified harder until it fits
    the vertex budget (a ragged outline can need it); None if it never does."""
    from plexora.plugins.qc.server import polygons

    if shape is None or shape.is_empty:
        return None
    for tolerance in (0.7, 2.0, 6.0, 18.0):
        try:
            return polygons.to_geojson(shape, simplify_px=tolerance * scale,
                                       max_vertices=max_vertices)
        except ValueError:
            continue
    return None


def _bboxes(lab, n):
    """(y0, y1, x0, x1) per label 1..n (None where absent), by the compiled
    label kernel."""
    from plexora.server.utils.label_kernels import label_bboxes

    found = label_bboxes(np.ascontiguousarray(lab, dtype=np.int32),
                         np.arange(1, n + 1, dtype=np.int32))
    out = []
    for k in range(n):
        if found["count"][k] <= 0:
            out.append(None)
        else:
            out.append((int(found["y0"][k]), int(found["y1"][k]) + 1,
                        int(found["x0"][k]), int(found["x1"][k]) + 1))
    return out


def _refine_box(source, context, stage, seeds, group, box1, level, job, field):
    """The fine masks of one box's seeds, category by category: objects."""
    import cv2

    params = context["params"]
    regions = context["regions"]
    stats = seeds["stats"]
    level1 = context["level1"]
    f1, fl = context["factors"][level1], context["factors"][level]
    um = context["pixel_um"] * fl
    y0, x0, y1, x1 = box1
    s = f1 / fl
    bx0, by0 = int(math.floor(x0 * s)), int(math.floor(y0 * s))
    bx1, by1 = int(math.ceil(x1 * s)), int(math.ceil(y1 * s))
    lh, lw = context["shapes"][level]
    bx1, by1 = min(bx1, lw), min(by1, lh)
    shape = (by1 - by0, bx1 - bx0)
    if shape[0] < 4 or shape[1] < 4:
        return None
    pan_names = context["pan"]
    names = context["names"]
    members = [seeds["seeds"][i] for i in group]
    cats = {m["category"] for m in members}
    ids = np.array([m["id"] for m in members], dtype=np.int32)
    seed_crop = seeds["labels"][y0:y1, x0:x1]
    seed_crop = np.where(np.isin(seed_crop, ids), seed_crop, 0).astype(np.int32)
    seed_lab = _upsample(seed_crop, shape).astype(np.int32)
    up = {k: _upsample(regions[k][y0:y1, x0:x1], shape).astype(bool)
          for k in ("filled", "roi", "glass")}
    din = cv2.resize(regions["din"][y0:y1, x0:x1].astype(np.float32), (shape[1], shape[0]),
                     interpolation=cv2.INTER_LINEAR) * s
    # One read per pan channel at this level: the pan, and the fold's share
    # of channels raised against their own neighbourhood.
    pan_box = np.zeros(shape, dtype=np.float32)
    raised = np.zeros(shape, dtype=np.float32) if "fold" in cats else None
    window = max(int(params["fold_window_um"] / um) | 1, 5)
    for name in pan_names:
        job.stop_if_asked()
        k = names.index(name)
        a, _dtype, px = _read_box(source, context["indices"][k], level, (bx0, by0, bx1, by1))
        job.pixels += px
        la = np.log1p(np.maximum(a, 0.0))
        del a
        pan_box += np.minimum(la / stage["channels"][k]["p99_log"], PAN_CLIP)
        if raised is not None:
            smooth = _blur(la, params["fold_smooth_um"] / um / 2.0)
            local = cv2.boxFilter(smooth, -1, (window, window), borderType=cv2.BORDER_REFLECT)
            dev = smooth - local
            spread = cv2.boxFilter(np.abs(dev), -1, (window, window),
                                   borderType=cv2.BORDER_REFLECT) * 1.4826 + EPS
            raised += (dev / spread > 1.0)
            del smooth, local, dev, spread
        del la
    pan_box /= max(1, len(pan_names))
    grow_px = params["grow_um"] / um
    out = []
    for category in ("fold", "tear", "debris"):
        mine = [m for m in members if m["category"] == category]
        if not mine:
            continue
        cat_ids = np.array([m["id"] for m in mine], dtype=np.int32)
        seed = np.isin(seed_lab, cat_ids)
        if not seed.any():
            continue
        search = _within(seed, grow_px)
        overgrown = set()
        if category == "fold":
            frac = raised / max(1, len(pan_names))
            bright = _blur(pan_box, 10.0 / um) > stats["tissue_median"] + stats["tissue_mad"]
            # The seed (smoothed at the coarse level, so wider than the fold)
            # only says where to start: the outline is the fine evidence's.
            weak = (frac >= params["fold_grow_frac"]) & bright & search & up["filled"] & \
                up["roi"]
            mask = _hysteresis(weak, seed)
            mask = _morph(mask, cv2.MORPH_CLOSE, min(25.0 / um, 16))
            mask = _morph(mask, cv2.MORPH_OPEN, min(10.0 / um, 8))
            min_px = params["fold_min_area_um2"] / (um * um) * 0.25
        elif category == "tear":
            depth = (stats["tissue_median"] - _blur(pan_box, 10.0 / um)) / stats["tissue_mad"]
            allowed = up["filled"] & (din > params["tear_inset_um"] / um * 0.5)
            mask, overgrown = _grow_tear(depth, seed_lab, mine, allowed, um, params)
            min_px = params["tear_min_area_um2"] / (um * um) * 0.25
        else:
            ring = search & up["glass"] & ~_within(seed, 10.0 / um)
            if ring.sum() > 200:
                lm, ls = _robust(pan_box[ring])
                ls = max(ls, stats["glass_mad"] * 0.5)
            else:
                lm, ls = stats["glass_median"], stats["glass_mad"]
            fibers = np.isin(seed_lab, [m["id"] for m in mine if m.get("shape") == "fiber"])
            compact = seed & ~fibers
            mask = np.zeros(shape, dtype=bool)
            if fibers.any():
                width = params["fiber_max_width_um"] / um
                th = pan_box - cv2.morphologyEx(pan_box, cv2.MORPH_OPEN,
                                                _disk(min(max(width / 2.0, 1), 32)))
                # Against the top-hat's own spread on the ring of glass round
                # the seeds (see the coarse pass).
                t_med, t_mad = _robust(th[ring]) if ring.sum() > 200 else _robust(th[search])
                th_z = (th - t_med) / max(t_mad, stats.get("glass_spread_floor", EPS))
                weak = search & up["glass"] & (th_z > params["fiber_weak_k"])
                grown = _drop_small(_hysteresis(weak, fibers & (th_z > params["fiber_z"])),
                                    params["fiber_min_length_um"] / um / 8.0)
                gap = min(params["fiber_gap_um"] / um, 41)
                mask |= _close_lines(grown, gap) & search & up["glass"] & (grown | (th_z > 0))
            if compact.any():
                z = (pan_box - lm) / ls
                weak = (search & up["glass"] & (z > params["debris_z"] * 0.7)) | compact
                grown = _morph(_hysteresis(weak, compact), cv2.MORPH_CLOSE, min(5.0 / um, 12))
                mask |= grown & search
            min_px = params["debris_min_area_um2"] / (um * um) * 0.25
        n, lab, cstats = _components(mask)
        if n > 1:
            small = np.concatenate([[True], cstats[1:, 4] < max(min_px, 4)])
            lab = np.where(small[lab], 0, lab)
            hit = np.unique(lab[seed & (lab > 0)])
            keep = np.zeros(n, dtype=bool)
            keep[hit] = True
            keep[0] = False
            lab = np.where(keep[lab], lab, 0)
        else:
            lab = np.zeros(shape, dtype=np.int32)
        # A seed the fine evidence lost keeps its own (upsampled) outline.
        covered = np.unique(seed_lab[(lab > 0) & seed])
        lost = np.setdiff1d(cat_ids, covered)
        nref = int(lab.max())
        lab, nref = _relabel(lab, nref)
        objs = _objects_in(lab, nref, seed_lab, category=category, offset_xy=(bx0, by0),
                           scale=fl, level=level, um_px=um, context=context, refined=True,
                           field=field, extra={"filled": up["filled"]})
        if lost.size:
            fallback = np.where(np.isin(seed_lab, lost), seed_lab, 0)
            fl_lab, fl_n = _relabel_by_value(fallback)
            more = _objects_in(fl_lab, fl_n, seed_lab, category=category,
                               offset_xy=(bx0, by0), scale=fl, level=level, um_px=um,
                               context=context, refined=False, field=field,
                               extra={"filled": up["filled"]})
            for o in more:
                o["refine_reason"] = "grew_into_tissue" \
                    if overgrown & set(o.get("seeds") or ()) else "no_fine_support"
            objs += more
        out += objs
    return out


def _grow_tear(depth, seed_lab, seeds, allowed, um, params):
    """(mask, overgrown seed ids): each tear seed regrown on the fine
    `depth` (robust SDs under the tissue), at most `tear_grow_um` past it,
    over places at least `tear_grow_depth_z` deep and `tear_grow_seed_share`
    of the seed's own depth (its `strength`), within `allowed`. Holes stay
    holes -- a tear is a gap, the tissue it encloses is tissue. A grown part
    whose mean depth is under `tear_grown_mean_share` of the seed's has grown
    into tissue: it is dropped (the seed keeps its own outline)."""
    import cv2

    mask = np.zeros(depth.shape, dtype=bool)
    overgrown = set()
    for s in seeds:
        seed = seed_lab == s["id"]
        if not seed.any():
            continue
        seed_depth = max(float(s.get("strength") or 0.0), 0.0)
        floor = max(params["tear_grow_depth_z"], params["tear_grow_seed_share"] * seed_depth)
        weak = (depth >= floor) & _within(seed, params["tear_grow_um"] / um) & allowed
        grown = _hysteresis(weak, seed)
        grown = _morph(grown, cv2.MORPH_OPEN, min(10.0 / um, 16))
        if not grown.any():
            continue
        if float(depth[grown].mean()) < params["tear_grown_mean_share"] * seed_depth:
            overgrown.add(int(s["id"]))
            continue
        mask |= grown
    return mask, overgrown


def _relabel(lab, n):
    """Consecutive labels 1..k of a label image."""
    present = np.unique(lab)
    present = present[present > 0]
    if not present.size:
        return np.zeros(lab.shape, dtype=np.int32), 0
    lut = np.zeros(int(present.max()) + 1, dtype=np.int32)
    lut[present] = np.arange(1, present.size + 1)
    return lut[lab], int(present.size)


def _relabel_by_value(lab):
    return _relabel(lab, int(lab.max()))


def _coarse_objects(seeds, group, context, field, reason):
    """Objects of seeds kept at their level-1 outline."""
    level1 = context["level1"]
    f1 = context["factors"][level1]
    regions = context["regions"]
    out = []
    members = [seeds["seeds"][i] for i in group]
    rects = np.array([m["bbox"] for m in members])
    y0, x0 = rects[:, 0].min(), rects[:, 1].min()
    y1, x1 = rects[:, 2].max(), rects[:, 3].max()
    crop = seeds["labels"][y0:y1, x0:x1]
    for category in ("fold", "tear", "debris"):
        ids = [m["id"] for m in members if m["category"] == category]
        if not ids:
            continue
        lab = np.where(np.isin(crop, ids), crop, 0).astype(np.int32)
        rl, n = _relabel_by_value(lab)
        objs = _objects_in(rl, n, lab, category=category, offset_xy=(x0, y0), scale=f1,
                           level=level1, um_px=context["um1"], context=context, refined=False,
                           field=field, extra={"filled": regions["filled"][y0:y1, x0:x1]})
        for o in objs:
            o["refine_reason"] = reason
        out += objs
    return out


def _saturation_objects(source, context, seeds, job, field, budget):
    """Saturation seeds confirmed at full resolution, channel by channel."""
    import cv2

    params = context["params"]
    level1 = context["level1"]
    f1 = context["factors"][level1]
    regions = context["regions"]
    out, residual = [], 0
    by_channel = {}
    for s in seeds["saturation"]:
        by_channel.setdefault(s["channel"], []).append(s)
    names = context["names"]
    refined_count = 0
    pad = 4
    for position, (name, items) in enumerate(by_channel.items()):
        job.stop_if_asked()
        job.say(f"{PHASES[4]}: saturation in {name} ({position + 1}/{len(by_channel)})")
        k = names.index(name)
        ceiling = context["stage_channels"][k]["ceiling"]
        rects = np.array([s["bbox"] for s in items], dtype=np.float64)
        groups = _merge_groups(rects, pad, context["shapes"][level1])
        for g in np.unique(groups):
            job.stop_if_asked()
            members = [items[i] for i in np.flatnonzero(groups == g)]
            box1 = _union_box(np.array([m["bbox"] for m in members]), pad,
                              context["shapes"][level1])
            level = _fine_level(box1, context, 1, finest_um=0.0)
            can = refined_count < MAX_CANDIDATES["saturation"] and \
                budget["used"] < params["max_total_fine_pixels"]
            if level is None:
                level = level1
            fl = context["factors"][level]
            s = f1 / fl
            y0, x0, y1, x1 = box1
            seed1 = np.zeros((y1 - y0, x1 - x0), dtype=bool)
            for m in members:
                my0, mx0, my1, mx1 = m["bbox"]
                seed1[my0 - y0:my1 - y0, mx0 - x0:mx1 - x0] |= m["mask"]
            roi1 = regions["roi"][y0:y1, x0:x1]
            if not can and level != level1:
                # Out of budget: the coarse pixels that are sure (>= confirm).
                level, fl, s = level1, f1, 1.0
            lh, lw = context["shapes"][level]
            bx0, by0 = int(math.floor(x0 * s)), int(math.floor(y0 * s))
            bx1, by1 = min(lw, int(math.ceil(x1 * s))), min(lh, int(math.ceil(y1 * s)))
            shape = (by1 - by0, bx1 - bx0)
            if shape[0] < 1 or shape[1] < 1:
                continue
            a, _dtype, px = _read_box(source, context["indices"][k], level,
                                      (bx0, by0, bx1, by1))
            job.pixels += px
            budget["used"] += px
            seed = _upsample(seed1, shape).astype(bool)
            roi = _upsample(roi1, shape).astype(bool)
            um = context["pixel_um"] * fl
            sure = (a >= params["sat_confirm"] * ceiling) & _within(seed, max(4.0, s)) & roi
            sure = _morph(sure, cv2.MORPH_CLOSE, min(3.0 / um, 4))
            n, lab, cstats = _components(sure)
            if n <= 1:
                continue
            min_px = params["sat_min_area_um2"] / (um * um)
            keep = np.concatenate([[False], cstats[1:, 4] >= max(min_px, 1)])
            lab = np.where(keep[lab], lab, 0)
            lab, n = _relabel(lab, n)
            refined = level != level1 or level == 0
            objs = _objects_in(lab, n, np.zeros(lab.shape, dtype=np.int32),
                               category="saturation", offset_xy=(bx0, by0), scale=fl,
                               level=level, um_px=um, context=context, refined=refined,
                               field=field, channel=name)
            confirmed_frac = float(sure.sum()) / max(1.0, float(seed.sum()))
            for o in objs:
                o["strength"] = float(math.log2(max(o["area_um2"], 1e-3) / 25.0))
                o["channels"] = [name]
                o["source_channel"] = name
                o["channel_evidence"] = {name: 1.0}
                o["metrics"] = {"ceiling": ceiling, "confirmed_fraction": round(
                    min(1.0, confirmed_frac), 3)}
                if not refined:
                    o["refine_reason"] = "budget" if can is False else "coarse_only"
            refined_count += 1
            out += objs
    return out, residual


def run(session, project, fp, context, *, progress=None, check_cancelled=None) -> dict:
    from plexora.server.utils import source_image

    started = perf_counter()
    timing = {}
    params = context["params"]
    names = context["names"]
    shapes = context["shapes"]
    level1 = context["level1"]
    width0 = shapes[0][1]
    context["factors"] = [width0 / max(1, shape[1]) for shape in shapes]
    context["um1"] = context["pixel_um"] * context["factors"][level1]
    # Total: tissue, a read per channel, seeds, attribution per channel, the
    # boxes (known later: counted as they are found), scoring.
    job = _Job(progress, check_cancelled, total=2 * len(names) + 4)
    job.say(PHASES[0], advance=0)
    with source_image.SHELF.reader(context["image_data"]) as source:
        context["indices"] = _indices(source, context)
        stage = _stage1(source, context, job)
    timing["stage1_s"] = round(perf_counter() - started, 3)
    job.stop_if_asked()
    job.say(PHASES[2])
    t0 = perf_counter()
    # Field grid for the Auto QC score fields.
    h0, w0 = shapes[0]
    cell_px = FIELD_CELL_UM / context["pixel_um"]
    field = {"x0": 0.0, "y0": 0.0, "step": float(cell_px), "cell_full_px": float(cell_px),
             "nx": int(math.ceil(w0 / cell_px)), "ny": int(math.ceil(h0 / cell_px)),
             "image_size": [int(w0), int(h0)], "cell_um": FIELD_CELL_UM}
    context["field"] = field
    regions = _regions(stage["pan"], context)
    context["regions"] = regions
    context["stage_channels"] = stage["channels"]
    seeds = _seeds(stage, regions, context)
    timing["seeds_s"] = round(perf_counter() - t0, 3)
    t0 = perf_counter()
    with source_image.SHELF.reader(context["image_data"]) as source:
        evidence = _attribute(source, context, stage, seeds, job)
    timing["attribution_s"] = round(perf_counter() - t0, 3)
    keep, dropped = fold_gate(seeds["seeds"], evidence, names, context["nuclear"], params)
    if not keep.all():
        gone = np.array([s["id"] for s, k in zip(seeds["seeds"], keep) if not k])
        seeds["labels"][np.isin(seeds["labels"], gone)] = 0
    alive = np.flatnonzero(keep)
    # Stage 2: merged boxes, strongest seeds first, within budget.
    t0 = perf_counter()
    rects = np.array([seeds["seeds"][i]["bbox"] for i in alive], dtype=np.float64) \
        if alive.size else np.zeros((0, 4))
    pad = (params["grow_um"] + params["halo_um"]) / context["um1"]
    n_reads = max(1, len(context["pan"]))
    groups = _merge_groups(rects, pad, shapes[level1])
    work = []
    for g in np.unique(groups):
        members = alive[np.flatnonzero(groups == g)]
        local = np.flatnonzero(np.isin(alive, members))
        for part, box in _split_groups(local, rects, pad, shapes[level1], context, n_reads):
            work.append((alive[part], box))
    strength_of = lambda grp: max(seeds["seeds"][i]["strength"] for i in grp)  # noqa: E731
    work.sort(key=lambda item: -strength_of(item[0]))
    sat_boxes = len({s["channel"] for s in seeds["saturation"]})
    job.total += len(work) + sat_boxes
    budget = {"used": 0}
    refined_seeds = {c: 0 for c in CATEGORIES}
    raw_objects = []
    with source_image.SHELF.reader(context["image_data"]) as source:
        for k, (group, box) in enumerate(work):
            job.stop_if_asked()
            job.say(f"{PHASES[4]} ({k + 1}/{len(work)})")
            cats = [seeds["seeds"][i]["category"] for i in group]
            over_cap = any(refined_seeds[c] >= MAX_CANDIDATES[c] for c in set(cats))
            level = _fine_level(box, context, n_reads)
            if level is None and level1 == 0:
                level = 0
            reason = None
            if level is None:
                reason = "too_large"
            elif over_cap:
                reason = "cap"
            elif budget["used"] >= params["max_total_fine_pixels"]:
                reason = "budget"
            if reason is None:
                before = job.pixels
                objs = _refine_box(source, context, stage, seeds, group, box, level, job,
                                   field)
                budget["used"] += job.pixels - before
                if objs is None:
                    objs = _coarse_objects(seeds, group, context, field, "too_small")
                for c in cats:
                    refined_seeds[c] += 1
            else:
                objs = _coarse_objects(seeds, group, context, field, reason)
            raw_objects += objs
        sat_objects, _ = _saturation_objects(source, context, seeds, job, field, budget)
    timing["refine_s"] = round(perf_counter() - t0, 3)
    job.stop_if_asked()
    job.say(PHASES[5])
    t0 = perf_counter()
    objects = _score(raw_objects, sat_objects, seeds, evidence, context)
    summary, arrays = _finish(objects, seeds, regions, stage, context, field, evidence)
    timing["score_s"] = round(perf_counter() - t0, 3)
    if context["image_stamp"] is not None and _stamp(context["identity"]) != \
            context["image_stamp"]:
        raise AgentError("conflict",
                         "the image changed while the Artifact Detector ran; run it again",
                         retryable=True)
    timing["total_s"] = round(perf_counter() - started, 3)
    summary.update(fingerprint=fp, project=project, timing=timing,
                   pixels_read=int(job.pixels), dropped=dropped)
    job.say("Done", advance=job.total)
    return {"summary": summary, "arrays": arrays}


def fold_gate(seeds, evidence, names, nuclear, params):
    """(keep, dropped counts) over `seeds`: a fold holds nuclei, folded over
    (its nuclear z at least `fold_nuclear_z`), and a fold seed in a band
    along the tissue's edge (`edge_band`) is kept only when an
    autofluorescence / blank channel is raised there too (`fold_af_z`): a
    real fold glows in the blank channel, an epidermis only in the stained
    markers. Other categories pass."""
    from plexora.plugins.qc.server.class_rules import is_af_channel

    keep = np.ones(len(seeds), dtype=bool)
    dropped = {"fold_no_nuclei": 0, "fold_edge_band": 0}
    if not len(seeds):
        return keep, dropped
    is_fold = np.array([s["category"] == "fold" for s in seeds])
    nuclear_idx = [names.index(n) for n in nuclear or () if n in names]
    if nuclear_idx:
        nuc_z = evidence[:, nuclear_idx].max(axis=1)
        no_nuclei = is_fold & (nuc_z < params["fold_nuclear_z"])
        keep &= ~no_nuclei
        dropped["fold_no_nuclei"] = int(no_nuclei.sum())
    band = is_fold & np.array([bool(s.get("edge_band")) for s in seeds]) & keep
    if band.any():
        af_idx = [k for k, n in enumerate(names) if is_af_channel(n)]
        glows = evidence[:, af_idx].max(axis=1) >= params["fold_af_z"] if af_idx \
            else np.zeros(len(seeds), dtype=bool)
        edge = band & ~glows
        keep &= ~edge
        dropped["fold_edge_band"] = int(edge.sum())
    return keep, dropped


def _score(raw_objects, sat_objects, seeds, evidence, context):
    """Strength, score, channels and an id for every object."""
    names = context["names"]
    params = context["params"]
    by_id = {s["id"]: (i, s) for i, s in enumerate(seeds["seeds"])}
    out = []
    for o in raw_objects:
        members = [by_id[s] for s in o.pop("seeds") if s in by_id]
        if not members:
            continue
        idx = np.array([i for i, _ in members])
        weight = np.array([s["area_px"] for _, s in members], dtype=np.float64)
        weight /= weight.sum()
        strength = float(np.dot(weight, [s["strength"] for _, s in members]))
        z = (weight[:, None] * evidence[idx]).sum(axis=0)
        category = o["category"]
        if category == "tear":
            on = [names[k] for k in np.flatnonzero(z <= -1.0)]
            source = names[int(np.argmin(z))]
        elif category == "debris":
            on = [names[k] for k in np.flatnonzero(z >= 3.0)]
            source = names[int(np.argmax(z))]
        else:
            on = [names[k] for k in np.flatnonzero(z >= 1.0)]
            source = names[int(np.argmax(z))]
        order = np.argsort(-np.abs(z))
        o["strength"] = strength
        o["channels"] = sorted(on, key=lambda n: -abs(float(z[names.index(n)]))) or [source]
        o["source_channel"] = source
        o["channel_evidence"] = {names[k]: round(float(z[k]), 3) for k in order[:12]}
        first = members[0][1]
        metrics = {"n_channels_elevated": len(on), "seeds": len(members),
                   "coarse_area_um2": round(float(sum(s["area_px"] for _, s in members))
                                            * context["um1"] ** 2, 1)}
        if "agreement" in first:
            metrics["agreement"] = round(float(np.dot(
                weight, [s.get("agreement", 0.0) for _, s in members])), 3)
        if category == "debris":
            metrics["shape"] = first.get("shape")
            for key in ("length_um", "aspect", "solidity"):
                if first.get(key) is not None:
                    metrics[key] = round(float(first[key]), 3)
        if category == "tear" and first.get("level") is not None:
            metrics["depth_z"] = round(strength, 2)
            metrics["signal_left"] = round(float(first["level"]), 3)
        o["metrics"] = metrics
        out.append(o)
    for o in sat_objects:
        out.append(o)
    for o in out:
        floor, half = SCORE_SCALE[o["category"]]
        score = float(soft(o["strength"], floor, half))
        if o["category"] == "fold":
            score *= min(1.0, o["metrics"].get("agreement", 1.0) / FOLD_FULL_AGREEMENT)
        o["score"] = round(score, 4)
        o["strength"] = round(float(o["strength"]), 4)
        o["on_tissue"] = None if o.get("tissue_fraction") is None else \
            bool(o["tissue_fraction"] >= 0.5)
        o["sub_scores"] = {"strength": o["strength"], "floor": floor, "half": half}
    final = []
    for category in CATEGORIES:
        mine = sorted((o for o in out if o["category"] == category),
                      key=lambda o: (-o["score"], -o["area_um2"]))
        mine = mine[:int(params["max_objects"])]
        for rank, o in enumerate(mine, start=1):
            o["id"] = f"art_{category}_{rank:04d}"
            final.append(o)
    return final


def _cell_score_maps(objects, maps, field) -> dict:
    """{category: (ny, nx) float32} score fields: NaN off every object;
    inside one, each cell's own strength (`maps`: the smoothed fold z, the
    tear depth, the debris level) through the category's `soft`, discounted
    as the object's score is and never above it, with the object's centroid
    cell forced to the object's score -- so a field's peak in an object is
    the object's score (`regions_from_objects` agrees) while its strata
    still differ inside it. A category without a map (saturation) paints
    the object's score flat."""
    ny, nx = int(field["ny"]), int(field["nx"])
    step = float(field["cell_full_px"])
    out = {}
    for category in CATEGORIES:
        values = np.full(ny * nx, np.nan, dtype=np.float32)
        plane = maps.get(category)
        flat = None if plane is None or plane.shape != (ny, nx) else plane.ravel()
        floor, half = SCORE_SCALE[category]
        for o in objects:
            if o["category"] != category:
                continue
            cells = np.asarray(o["cells"], dtype=np.int64)
            if not cells.size:
                continue
            score = float(o["score"])
            if flat is None:
                painted = np.full(cells.size, score, dtype=np.float32)
            else:
                discount = 1.0
                if category == "fold":
                    discount = min(1.0, (o.get("metrics") or {}).get("agreement", 1.0)
                                   / FOLD_FULL_AGREEMENT)
                painted = np.minimum(soft(flat[cells], floor, half) * discount,
                                     score).astype(np.float32)
                cx, cy = o.get("centroid") or (None, None)
                at = -1
                if cx is not None:
                    centre = int(min(ny - 1, max(0, cy // step))) * nx + \
                        int(min(nx - 1, max(0, cx // step)))
                    hit = np.flatnonzero(cells == centre)
                    at = int(hit[0]) if hit.size else -1
                painted[at if at >= 0 else int(np.argmax(painted))] = score
            current = values[cells]
            values[cells] = np.where(np.isnan(current), painted, np.maximum(current, painted))
        out[category] = values.reshape(ny, nx)
    return out


def _finish(objects, seeds, regions, stage, context, field, evidence):
    """(summary, arrays) of a run."""
    import cv2

    params = context["params"]
    um1 = context["um1"]
    ny, nx = field["ny"], field["nx"]
    arrays = {f"field_{category}": values.astype(np.float16) for category, values in
              _cell_score_maps(objects, seeds.get("maps") or {}, field).items()}
    weight = cv2.resize(regions["filled"].astype(np.float32), (nx, ny),
                        interpolation=cv2.INTER_AREA)
    roi_w = cv2.resize(regions["roi"].astype(np.float32), (nx, ny),
                       interpolation=cv2.INTER_AREA)
    arrays["tissue_fraction"] = np.clip(weight, 0, 1).astype(np.float32)
    arrays["roi_fraction"] = np.clip(roi_w, 0, 1).astype(np.float32)
    h, w = regions["filled"].shape
    small = max(1, int(math.ceil(max(h, w) / 1024)))
    sh, sw = int(math.ceil(h / small)), int(math.ceil(w / small))
    arrays["tissue"] = cv2.resize(regions["filled"].astype(np.uint8), (sw, sh),
                                  interpolation=cv2.INTER_NEAREST)
    arrays["roi"] = cv2.resize(regions["roi"].astype(np.uint8), (sw, sh),
                               interpolation=cv2.INTER_NEAREST)
    for o in objects:
        o.pop("cells", None)
        o.pop("channel", None)
    counts = {c: sum(1 for o in objects if o["category"] == c) for c in CATEGORIES}
    histogram = {}
    edges = np.linspace(0, 1, HISTOGRAM_BINS + 1)
    for c in CATEGORIES:
        scores = [o["score"] for o in objects if o["category"] == c]
        histogram[c] = np.histogram(scores, bins=edges)[0].astype(int).tolist()
    residual = {c: sum(1 for o in objects if o["category"] == c and not o["refined"])
                for c in CATEGORIES}
    warnings = []
    if not regions["tissue"].any():
        warnings.append("no tissue was found; nothing was checked")
    summary = {
        "version": VERSION, "status": "ok" if regions["tissue"].any() else "insufficient",
        "pixel_um": context["pixel_um"], "pixel_um_source": context["pixel_um_source"],
        "channels": context["names"], "pan_channels": context["pan"],
        "nuclear": context["nuclear"],
        "levels": {"stage1": context["level1"], "stage1_um_px": round(um1, 4),
                   "shapes": [list(s) for s in context["shapes"]]},
        "field": field, "ceilings": {c["name"]: c["ceiling"] for c in stage["channels"]
                                     if c["ceiling"] is not None},
        "saturated_channels": sorted({o["source_channel"] for o in objects
                                      if o["category"] == "saturation"}),
        "tissue": {"method": "pan_otsu",
                   "area_um2": round(float(regions["filled"].sum()) * um1 * um1, 1),
                   "roi_area_um2": round(float(regions["roi"].sum()) * um1 * um1, 1)},
        "pan_stats": {k: round(float(v), 5) for k, v in seeds["stats"].items()},
        "params": params, "categories": params["categories"], "counts": counts,
        "residual": residual, "auto_threshold": {c: AUTO_THRESHOLD for c in CATEGORIES},
        "histogram": {"edges": edges.round(3).tolist(), "counts": histogram},
        "warnings": warnings, "objects": objects,
    }
    return summary, arrays


# -- thresholds and objects: never a pixel --------------------------------------------------


def auto_threshold(category, summary=None) -> float:
    found = (summary or {}).get("auto_threshold") or {}
    return float(found.get(category, AUTO_THRESHOLD))


def threshold_of(project, category, summary=None) -> dict:
    """{value, auto, source, offset_steps}: the user's threshold if one is
    set (`user`); else the automatic one moved `offset_steps` (tighter
    positive: the bar comes down by `STEP` a step, `user_relative`); else the
    automatic one (`auto`)."""
    summary = summary if summary is not None else current(project)
    auto = auto_threshold(category, summary)
    mine = category_settings(project, category)
    if mine.get("threshold") is not None:
        return {"value": float(mine["threshold"]), "auto": auto, "source": "user",
                "offset_steps": None}
    steps = int(mine.get("offset_steps") or 0)
    if steps:
        value = float(np.clip(auto - steps * STEP, *AUTO_RANGE))
        return {"value": round(value, 4), "auto": auto, "source": "user_relative",
                "offset_steps": steps, "step": STEP}
    return {"value": auto, "auto": auto, "source": "auto", "offset_steps": 0}


def thresholds_of(project, summary=None) -> dict:
    summary = summary if summary is not None else current(project)
    return {c: threshold_of(project, c, summary)["value"] for c in CATEGORIES}


def _visible_channels(o):
    return o.get("channels") or ([o["source_channel"]] if o.get("source_channel") else [])


def objects_at(summary, thresholds, *, categories=None, channels=None, geometry=True,
               limit=None) -> list:
    """The objects retained at `thresholds` ({category: bar}), strongest
    first; `channels` keeps those any of whose channels is listed."""
    wanted = set(categories or CATEGORIES)
    listed = set(channels or ())
    out = []
    for o in (summary or {}).get("objects") or []:
        if o["category"] not in wanted:
            continue
        if o["score"] < float(thresholds.get(o["category"], AUTO_THRESHOLD)):
            continue
        if listed and not listed.intersection(_visible_channels(o)):
            continue
        out.append(o if geometry else {k: v for k, v in o.items() if k != "geometry"})
    out.sort(key=lambda o: -o["score"])
    return out[:limit] if limit else out


def evaluate(summary, thresholds, *, categories=None, channels=None, geometry=False) -> dict:
    """Per category: the bar, the objects kept out of all found, their area
    and its share of the analysis region. O(objects); reads nothing."""
    roi_area = float(((summary or {}).get("tissue") or {}).get("roi_area_um2") or 0.0)
    per = {}
    total = 0
    for c in CATEGORIES:
        bar = float(thresholds.get(c, AUTO_THRESHOLD))
        mine = [o for o in (summary or {}).get("objects") or [] if o["category"] == c]
        kept = objects_at(summary, {c: bar}, categories=[c], channels=channels,
                          geometry=geometry)
        area = float(sum(o["area_um2"] for o in kept))
        entry = {"threshold": round(bar, 4), "n_objects": len(kept), "n_total": len(mine),
                 "area_um2": round(area, 1),
                 "flagged_pct": round(100.0 * area / roi_area, 3) if roi_area else 0.0}
        if geometry:
            entry["objects"] = kept
        per[c] = entry
        total += len(kept)
    return {"per_category": per, "n_objects": total, "denominator": "analysis_roi"}


def evaluation(project, *, thresholds=None, categories=None, channels=None, geometry=False):
    """`evaluate` of the current result at the effective (or given)
    thresholds, cached; None without a result."""
    summary = current(project)
    if summary is None:
        return None
    bars = {**thresholds_of(project, summary), **(thresholds or {})}
    key = (project, summary["fingerprint"], tuple(sorted((k, round(float(v), 6))
                                                         for k, v in bars.items())),
           tuple(sorted(categories or ())), tuple(sorted(channels or ())), bool(geometry))
    with _GUARD:
        held = _EVALUATIONS.get(key)
    if held is not None:
        return held
    out = evaluate(summary, bars, categories=categories, channels=channels, geometry=geometry)
    with _GUARD:
        _EVALUATIONS[key] = out
        while len(_EVALUATIONS) > 32:
            _EVALUATIONS.pop(next(iter(_EVALUATIONS)))
    return out


def public_evaluation(result, *, objects=False):
    if result is None:
        return None
    per = {}
    for c, entry in result["per_category"].items():
        per[c] = {k: v for k, v in entry.items() if objects or k != "objects"}
    return {"per_category": per, "n_objects": result["n_objects"],
            "denominator": result["denominator"]}


# -- cache and store -----------------------------------------------------------------------


def _pointer(project) -> dict:
    return _read_json(_folder(project) / "current.json") or {}


def _point(project, fp):
    data = {"fingerprint": fp} if fp else {}
    _atomic_write(_folder(project) / "current.json",
                  json.dumps(data, sort_keys=True).encode("utf-8"))


def _remember(project, fp, summary, arrays):
    with _GUARD:
        _MEMORY[(project, fp)] = (summary, arrays)
        while len(_MEMORY) > 6:
            _MEMORY.pop(next(iter(_MEMORY)))


def _save(project, fp, result):
    from plexora.plugins.qc.server.results import now_iso

    folder = _folder(project)
    summary = {**result["summary"], "computed_at": now_iso()}
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **result["arrays"])
    _atomic_write(folder / f"{fp}.npz", buffer.getvalue())
    _atomic_write(folder / f"{fp}.json", json.dumps(summary, default=_json_default)
                  .encode("utf-8"))
    _point(project, fp)
    _sweep(project, keep=2)
    _remember(project, fp, summary, result["arrays"])
    return summary


def _json_default(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


def _sweep(project, keep):
    folder = _folder(project)
    pointed = _pointer(project).get("fingerprint")
    metas = sorted((p for p in folder.glob("*.json")
                    if p.name not in ("current.json", "settings.json", "running.json")
                    and p.stem != pointed),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in metas[keep:]:
        for path in (stale, stale.with_suffix(".npz")):
            try:
                path.unlink()
            except OSError:
                pass


def _load(project, fp):
    with _GUARD:
        held = _MEMORY.get((project, fp))
    if held is not None:
        return held
    folder = _folder(project)
    summary = _read_json(folder / f"{fp}.json")
    if not summary or summary.get("version") != VERSION:
        return None
    try:
        with np.load(folder / f"{fp}.npz") as stored:
            arrays = {k: stored[k] for k in stored.files}
    except (OSError, ValueError):
        return None
    _remember(project, fp, summary, arrays)
    return summary, arrays


def load_summary(project, fp):
    held = _load(project, fp) if fp else None
    return held[0] if held else None


def load_arrays(project, fp):
    held = _load(project, fp) if fp else None
    return held[1] if held else None


def current_fingerprint(project):
    return _pointer(project).get("fingerprint")


def current(project):
    """The summary of the last result, or None."""
    return load_summary(project, current_fingerprint(project))


def clear(project) -> bool:
    """Forget the current result (the files are swept later)."""
    had = current_fingerprint(project) is not None
    _point(project, None)
    return had


def load_or_run(session, project, *, params=None, force=False, progress=None,
                check_cancelled=None) -> tuple:
    """(summary, reused)."""
    fp, context = plan(session, project, params=params)
    if not force:
        summary = load_summary(project, fp)
        if summary is not None:
            _point(project, fp)
            return summary, True
    result = run(session, project, fp, context, progress=progress,
                 check_cancelled=check_cancelled)
    return _save(project, fp, result), False


def note_running(project, job_id):
    path = _folder(project) / "running.json"
    if job_id:
        _atomic_write(path, json.dumps({"job_id": job_id}).encode("utf-8"))
    else:
        try:
            path.unlink()
        except OSError:
            pass


def running_job(project):
    running = _read_json(_folder(project) / "running.json") or {}
    if not running.get("job_id"):
        return None
    from plexora.agent import jobs

    record = jobs.store().get(running["job_id"])
    if record and record.get("status") in ("queued", "running"):
        return {k: record.get(k) for k in ("job_id", "status", "progress")}
    return None


def color_of(project, category) -> str:
    return category_settings(project, category).get("color") or PALETTE[category]


def shown(session, project, names=None) -> list:
    """The channels listed in the panel's filter ([] = every channel)."""
    if names is None:
        names = channel_names(session.project(project))
    return [n for n in (settings(project).get("channels") or []) if n in names]


def public_summary(summary):
    """A summary without its objects and histogram (a run's answer)."""
    if summary is None:
        return None
    return {k: v for k, v in summary.items() if k not in ("objects", "histogram")}


def public_status(session, project, *, include_regions=False, thresholds=None,
                  categories=None, channel=None, max_objects=200) -> dict:
    """What the panel and an agent read: availability, the listed channels,
    per category its colour, threshold and what it keeps, the result and a
    running job."""
    record = session.project(project)
    names = channel_names(record)
    state = availability(session, project)
    summary = current(project)
    out = {"available": state["available"], "available_reason": state["reason"],
           "available_message": state["message"], "channels": names[:200],
           "nuclear": (nuclear_names(names) or [None])[0], "shown": shown(session, project,
                                                                           names),
           "categories": [], "results": None, "job": running_job(project)}
    bars = {**thresholds_of(project, summary), **(thresholds or {})}
    listed = [channel] if channel else None
    result = evaluate(summary, bars, channels=listed) if summary else None
    for c in CATEGORIES:
        threshold = threshold_of(project, c, summary)
        if thresholds and c in thresholds:
            threshold = {**threshold, "value": float(thresholds[c]), "source": "preview"}
        entry = {"key": c, "label": CATEGORY_LABEL[c], "class": CATEGORY_CLASS[c],
                 "words": CATEGORY_WORDS[c], "color": color_of(project, c),
                 "threshold": threshold, "enabled": c in ((summary or {}).get("categories")
                                                           or CATEGORIES)}
        if result is not None:
            entry.update({k: result["per_category"][c][k]
                          for k in ("n_objects", "n_total", "area_um2", "flagged_pct")})
        out["categories"].append(entry)
    if summary is not None:
        stale = False
        if state["available"]:
            try:
                fp, _context = plan(session, project, params=summary.get("params"))
                stale = fp != summary.get("fingerprint")
            except AgentError:
                stale = True
        out["results"] = {
            "fingerprint": summary.get("fingerprint"), "status": summary.get("status"),
            "stale": stale, "computed_at": summary.get("computed_at"),
            "n_channels": len(summary.get("channels") or []),
            "pan_channels": summary.get("pan_channels"),
            "pixel_um": summary.get("pixel_um"),
            "counts": summary.get("counts"), "residual": summary.get("residual"),
            "timing": summary.get("timing"), "pixels_read": summary.get("pixels_read"),
            "warnings": summary.get("warnings") or [],
            "tissue": summary.get("tissue"),
            "saturated_channels": summary.get("saturated_channels") or [],
            "n_objects": result["n_objects"] if result else 0}
        if include_regions:
            wanted = categories or CATEGORIES
            out["results"]["objects"] = objects_at(summary, bars, categories=wanted,
                                                   channels=listed, geometry=True,
                                                   limit=max_objects)
    return out


def viewer_objects(project) -> dict:
    """Every object with its geometry and score, for the viewer to filter
    locally (fetched once per result)."""
    summary = current(project)
    if summary is None:
        return {"available": False, "objects": []}
    keys = ("id", "category", "class", "score", "strength", "channels", "source_channel",
            "channel_evidence", "geometry", "bbox", "centroid", "area_um2", "metrics",
            "refined", "on_tissue")
    return {"available": True, "fingerprint": summary["fingerprint"],
            "objects": [{k: o.get(k) for k in keys} for o in summary.get("objects") or []],
            "thresholds": thresholds_of(project, summary)}


# -- Auto QC ---------------------------------------------------------------------------------


def score_field(summary, arrays, category):
    """One category's objects as a `score_fields.ScoreField`: values are the
    object scores on the 25 µm field (NaN off every object), the weight the
    tissue, and `objects` the objects themselves -- the snug regions."""
    from plexora.plugins.qc.server import score_fields

    field = summary["field"]
    values = np.asarray(arrays[f"field_{category}"], dtype=np.float64)
    weight = np.asarray(arrays["tissue_fraction"], dtype=np.float64)
    objects = [o for o in summary.get("objects") or [] if o["category"] == category]
    return score_fields.ScoreField(
        check="artifacts", values=values, weight=weight,
        grid={k: field[k] for k in ("x0", "y0", "step", "nx", "ny", "image_size")},
        fingerprint=summary.get("fingerprint") or "",
        auto_threshold=auto_threshold(category, summary),
        cell_um=FIELD_CELL_UM, pixel_um=summary.get("pixel_um"),
        denominator="analysis_roi", channel=None, reference=category,
        stats={"category": category, "n_objects": len(objects),
               "saturated_channels": summary.get("saturated_channels") or []},
        evaluable=np.asarray(arrays["roi_fraction"], dtype=np.float64) > 0,
        objects=objects, display_channels=display_channels(summary, objects))


def display_channels(summary, objects) -> list:
    """[nuclear, lead]: what a category's review tiles are drawn in -- the
    nuclear stain, then the channel its objects show most (the
    `source_channel` covering the most area; for saturation the channel
    most saturated), else the first pan channel. Drawn with no channel, a
    review of a detector across every channel was black."""
    nuclear = next(iter(summary.get("nuclear") or ()), None)
    area = {}
    for o in objects or ():
        name = o.get("source_channel")
        if name:
            area[name] = area.get(name, 0.0) + float(o.get("area_um2") or 0.0)
    lead = max(area, key=lambda n: (area[n], n)) if area else None
    if lead is None or lead == nuclear:
        lead = next((n for n in summary.get("pan_channels") or () if n != nuclear), lead)
    return [n for n in dict.fromkeys((nuclear, lead)) if n]


def _object_cells(geometry, grid, shape):
    """The field cells an object's outline covers (at least its centre's)."""
    import cv2

    step = float(grid["step"])
    canvas = np.zeros(shape, dtype=np.uint8)
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon"         else [geometry["coordinates"]]
    for polygon in polygons:
        ring = np.asarray(polygon[0], dtype=np.float64) / step
        cv2.fillPoly(canvas, [np.round(ring - 0.5).astype(np.int32)], 1)
        ys, xs = np.clip(ring[:, 1].astype(int), 0, shape[0] - 1),             np.clip(ring[:, 0].astype(int), 0, shape[1] - 1)
        canvas[ys, xs] = 1
    return canvas.astype(bool)


def regions_from_objects(field, threshold, *, min_cells=None, geometry=True,
                         max_regions=200) -> dict:
    """`score_fields.regions`' answer from a field's objects: each object at
    or above `threshold` is a region whose outline is the object's own (the
    object is the trace), largest first."""
    threshold = float(threshold)
    shape = np.asarray(field.values).shape
    kept_objects = sorted((o for o in field.objects or [] if o["score"] >= threshold),
                          key=lambda o: (-o["area_um2"], -o["score"]))
    labels = np.zeros(shape, dtype=np.int32)
    weight = np.asarray(field.weight, dtype=np.float64)
    valid = np.asarray(field.valid, dtype=bool)
    denominator = float(weight[valid].sum())
    regions = []
    kept = []
    for k, o in enumerate(kept_objects[:max_regions]):
        cells = _object_cells(o["geometry"], field.grid, shape) & (labels == 0)
        labels[cells] = k + 1
        kept.append(k + 1)
        ys, xs = np.nonzero(cells)
        x0, y0, x1, y1 = o["bbox"]
        peak_cell = [int(ys[0]), int(xs[0])] if ys.size else             [int(o["centroid"][1] // field.grid["step"]), int(o["centroid"][0]
                                                            // field.grid["step"])]
        region = {"id": o["id"], "cells": int(max(1, ys.size)),
                  "area_px2": o.get("area_px2"), "area_um2": o["area_um2"],
                  "bbox": [x0, y0, x1, y1], "mean": o["score"], "max": o["score"],
                  "peak_cell": peak_cell, "peak": [float(v) for v in o["centroid"]],
                  "weight_pct": round(100.0 * float(weight[ys, xs].sum()) / denominator, 3)
                  if denominator > 0 else 0.0,
                  "object": o["id"], "class": o["class"], "category": o["category"],
                  "channels": list(o.get("channels") or []),
                  "source_channel": o.get("source_channel")}
        if geometry:
            region["geometry"] = o["geometry"]
        regions.append(region)
    mask = labels > 0
    flagged_pct = round(100.0 * float(weight[mask & valid].sum()) / denominator, 2)         if denominator > 0 else 0.0
    return {"threshold": threshold, "min_cells": 1, "flagged_pct": flagged_pct,
            "denominator": field.denominator, "n_regions": len(kept_objects),
            "residual": max(0, len(kept_objects) - max_regions), "regions": regions,
            "mask": mask, "labels": labels, "kept": kept}
