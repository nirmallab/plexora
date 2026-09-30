"""Blur QC: which parts of the image are out of focus, without a model.

Each listed channel (default the first three nuclear ones; the darkness plane
for a brightfield slide) has its own result, threshold and colour. A channel
is read once, block by block, at a pinned analysis
resolution (`analysis_um_px`, 0.5 µm per pixel, so the filter scales mean the
same on every image), and reduced to per-cell sums of the Sobel gradient
energy (Tenengrad) at three scales: the plane itself and after a Gaussian of
1 and 2 pixels. A cell is `cell_um` (40 µm) across; each score describes the
3 x 3 cells centred on it (a 120 µm tile, overlapping its neighbours by two
thirds).

Blur takes fine-scale energy away and leaves the coarse, so a cell's focus
is its *fine share* -- fine energy over coarse energy, both less the noise
measured on the glass -- which cancels how much stain the cell holds. The
sharp reference is the image itself: the median fine share of its sharpest
cells (`reference_fraction`). A fine share runs from that reference down to 1
(nothing finer than the coarse scale), so a cell's deficit is the share of
that range it has lost, on a log scale, averaged over the fine scales. Its
Blur Score is the mean deficit of the evaluable cells of the 3 x 3 tile
centred on it (pooled in the log domain: summed energy would let a tile's
sharpest part outweigh the rest), smoothed over its neighbours, 0..1. Cells
that are mostly padding, glass or saturation, or whose coarse structure is
not above the noise, are *not evaluable* and are never called blurred.

Only the scores are stored (`<QC store>/blur/<fp>.npz|.json`, the
fingerprint covering the image, channel, level, grid and parameters); the
threshold, the minimum region size and the regions are applied afterwards
from them (`evaluate`), so moving the slider never reads a pixel. The
automatic threshold is robust (median + 3 MAD of the image's own scores,
floored at one halving of fine detail); `global_blur` warns when even the
sharpest tiles have little fine detail, where an in-image reference cannot
see blur that is everywhere.

`evaluate` takes any (ny, nx) score grid, so a learned classifier can produce
the scores behind the same summary, panel and ROI write.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import threading
from time import perf_counter

import numpy as np

from plexora.agent.errors import AgentError

VERSION = "1"
PARAMS_DEFAULT = {
    # The analysis resolution (µm per pixel): the level nearest it is read.
    "analysis_um_px": 0.5,
    # One grid cell's side; `cell_px` in full-resolution pixels without a
    # pixel size.
    "cell_um": 40.0, "cell_px": 96,
    # Gaussian scales before the Sobel, in pixels of the level read.
    "scales_px": [0.0, 1.0, 2.0],
    "reference_fraction": 0.15, "min_reference_tiles": 20,
    "smooth_sigma_cells": 0.7,
    "min_valid_fraction": 0.5, "min_tissue": 0.5,
    # [cal] a tile's coarse energy must be this many times the glass's.
    "k_noise": 4.0,
    "max_pixels": 400_000_000,
}
#: Cells per tile side.
TILE_CELLS = 3
#: The share of the dynamic range above which a pixel is saturated.
SATURATION_OF_CEILING = 0.98
#: [cal] under this fine share of the sharpest tiles, the whole image may be
#: out of focus (see the tests' globally blurred scene).
GLOBAL_MIN_FINE_SHARE = 1.6
#: A reference fine share is never taken as less than 1 + this (a range of
#: nothing would make every tile "fully blurred").
MIN_FINE_RANGE = 0.05
#: [cal] the automatic threshold is never under this: a third of the image's
#: own fine detail lost.
AUTO_FLOOR = 0.35
#: Cells with less tissue than this are glass, where the noise is measured.
BACKGROUND_TISSUE = 0.05
MIN_NOISE_CELLS = 30
HISTOGRAM_BINS = 50
MAX_REGIONS = 200
MIN_REGION_TILES = 4
AUTO_RANGE = (0.2, 0.8)
PHASES = ("Finding the tissue", "Scoring focus", "Scoring tiles")
#: A brightfield slide's one plane, where a channel name would go.
BRIGHTFIELD = "brightfield"
#: The nuclear channels listed before anyone picks: the first this many.
DEFAULT_SHOWN = 3
MAX_SHOWN = 12
#: The listed channels' colours, in order, until one is picked: core's
#: swatch presets (Orange, Magenta, Cyan, Yellow, Violet, Green), so the
#: picker shows the colour as one of its own.
PALETTE = ("#f97316", "#ec4899", "#22e6e6", "#ffd60a", "#a78bfa", "#2bd46f")

_GUARD = threading.Lock()
_MEMORY: dict = {}
_EVALUATIONS: dict = {}


# -- where it lives --------------------------------------------------------------------


def _folder(project):
    from plexora import api

    path = api.store(project, "qc").directory() / "blur"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path, data: bytes):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def settings(project) -> dict:
    """{channels (the listed ones; absent: the first nuclear ones), per:
    {channel: {threshold (absent: the automatic one), color}},
    min_region_tiles}."""
    return _read_json(_folder(project) / "settings.json") or {}


def save_settings(project, **values):
    """Merge `values` in; a None removes the key (its default again)."""
    merged = {k: v for k, v in {**settings(project), **values}.items() if v is not None}
    _atomic_write(_folder(project) / "settings.json",
                  json.dumps(merged, sort_keys=True).encode("utf-8"))
    return merged


def label_of(channel):
    """A result's name in the store: its channel, or `brightfield`."""
    return channel or BRIGHTFIELD


def channel_settings(project, label) -> dict:
    return dict((settings(project).get("per") or {}).get(label) or {})


def save_channel_settings(project, label, **values):
    """Merge `values` into one channel's settings; a None removes the key."""
    stored = settings(project)
    per = dict(stored.get("per") or {})
    mine = {**(per.get(label) or {}), **values}
    per[label] = {k: v for k, v in mine.items() if v is not None}
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


def nuclear_candidates(names) -> list:
    from plexora.agent import presets

    return presets.nuclear_channels(names)


def default_channel(names):
    found = nuclear_candidates(names)
    return found[0] if found else (names[0] if names else None)


def _choose_level(source, pixel_um, params):
    """(level, factor): the level whose µm per pixel is nearest the analysis
    resolution (level 0 without a pixel size), coarsened while it holds more
    than `max_pixels`. The factor is measured, never assumed a power of two."""
    width0 = source.level_shape(0)[1]
    levels = max(1, int(source.levels))

    def factor_of(level):
        return width0 / max(1, source.level_shape(level)[1])

    level = 0
    if pixel_um:
        target = float(params["analysis_um_px"])
        level = min(range(levels),
                    key=lambda lv: abs(math.log(pixel_um * factor_of(lv) / target)))
    while level + 1 < levels:
        h, w = source.level_shape(level)
        if h * w <= float(params["max_pixels"]):
            break
        level += 1
    return level, factor_of(level)


def _grid(source, level, factor, pixel_um, params) -> dict:
    """The cell grid at `level`, in `scan.choose_map_grid`'s shape (so
    `scan.iter_blocks` and `polygons.mask_to_geometry` read it): the integer
    level cell first, the full-resolution cell from it."""
    height, width = source.level_shape(0)
    lh, lw = source.level_shape(level)
    target_full = float(params["cell_um"]) / pixel_um if pixel_um else float(params["cell_px"])
    cell_level_px = max(4, int(round(target_full / factor)))
    cell_full_px = cell_level_px * factor
    return {"level": int(level), "level_shape": [int(lh), int(lw)], "factor": float(factor),
            "cell_level_px": cell_level_px, "cell_full_px": float(cell_full_px),
            "shape": [int(math.ceil(lh / cell_level_px)), int(math.ceil(lw / cell_level_px))],
            "image_size": [int(width), int(height)],
            "cell_um": float(cell_full_px * pixel_um) if pixel_um else None}


def plan(session, project, *, channel=None, params=None) -> tuple:
    """(fingerprint, context) of a run, without reading a pixel."""
    from plexora.agent.render import resolve_channel
    from plexora.server.utils import pixel_scale, source_image

    record = session.project(project)
    if record.image.is_blank:
        raise AgentError("unsupported_modality", "this sample has no image to check")
    names = channel_names(record)
    params = {**PARAMS_DEFAULT, **(params or {})}
    image_data = session.image_data(project)
    pixel = pixel_scale.pixel_size(record)
    pixel_um = float(pixel["value"]) if pixel else None
    with source_image.SHELF.reader(image_data) as source:
        brightfield = bool(source.is_brightfield)
        level, factor = _choose_level(source, pixel_um, params)
        grid = _grid(source, level, factor, pixel_um, params)
    if brightfield:
        chosen, key = None, None
    else:
        if channel and channel not in names:
            raise AgentError("invalid_input", f"{channel!r} is not a channel of this image",
                             detail={"channels": names[:100]})
        chosen = channel or default_channel(names)
        if not chosen:
            raise AgentError("precondition_missing", "the image has no channel to check")
        _index, found = resolve_channel(chosen, list(record.image.real_channels))
        key = source_image.channel_key(found)
    identity = source_image.ReaderShelf.identity_of(image_data)
    image_stamp = None if str(identity or "").startswith("node://") else _stamp(identity)
    blob = json.dumps({"v": VERSION, "image": identity, "image_stamp": image_stamp,
                       "key": key, "brightfield": brightfield, "level": level,
                       "cell": grid["cell_level_px"], "shape": grid["shape"],
                       "params": params, "pixel_um": pixel_um}, sort_keys=True, default=str)
    fp = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]
    return fp, {"record": record, "image_data": image_data, "identity": identity,
                "image_stamp": image_stamp, "channel": chosen, "key": key, "names": names,
                "brightfield": brightfield, "grid": grid, "params": params,
                "pixel_um": pixel_um}


# -- the run ---------------------------------------------------------------------------------


def _cell_sums(values, s, cy, cx):
    """(cy, cx) float64 sums of a (h, w) block over s x s cells."""
    h, w = values.shape
    padded = np.zeros((cy * s, cx * s), dtype=np.float32)
    padded[:h, :w] = values
    return padded.reshape(cy, s, cx, s).sum(axis=(1, 3), dtype=np.float64)


def _tile_sums(grid_values):
    """3 x 3 box sums of a cell grid (zero outside it)."""
    padded = np.pad(grid_values, 1)
    out = np.zeros_like(grid_values)
    ny, nx = grid_values.shape
    for dy in range(TILE_CELLS):
        for dx in range(TILE_CELLS):
            out += padded[dy:dy + ny, dx:dx + nx]
    return out


def _tissue(source, context, grid):
    """(tissue fraction per cell, method): the nuclear stain's tissue mask
    whichever channel is analysed (a marker's own Otsu calls "tissue"
    wherever the marker is)."""
    from plexora.agent.evidence import image_qc
    from plexora.agent.render import resolve_channel
    from plexora.plugins.qc.server import scan
    from plexora.server.utils import source_image

    overview_level = image_qc.overview_level(source)
    ov_h, ov_w = source.level_shape(overview_level)
    overview_factor = grid["image_size"][0] / max(1, ov_w)
    brightfield = context["brightfield"]
    if brightfield:
        name, index = "brightfield", 0
    else:
        nuclear = nuclear_candidates(context["names"])
        name = nuclear[0] if nuclear else context["channel"]
        _i, found = resolve_channel(name, list(context["record"].image.real_channels))
        index = source.channel_index(source_image.channel_key(found))
    plane, _ = scan._read(source, index, overview_level, (0, 0, ov_w, ov_h), brightfield)
    pixel_um = context["pixel_um"]
    sigma_px = (scan.TISSUE_SMOOTH_UM / (pixel_um * overview_factor)) if pixel_um else None
    tissue = scan.tissue_estimate({name: plane}, None if brightfield else name, brightfield,
                                  sigma_px=sigma_px)
    return scan._grid_from_overview(tissue["mask"], grid, ov_w, ov_h), tissue["method"]


def run(session, project, fp, context, *, progress=None, check_cancelled=None) -> dict:
    import cv2

    from plexora.plugins.qc.server import scan
    from plexora.server.utils import source_image

    def say(done, total, message):
        if progress is not None:
            progress(done=done, total=total, message=message)

    def stop_if_asked():
        if check_cancelled is not None:
            check_cancelled()

    started = perf_counter()
    timing = {}
    params = context["params"]
    grid = context["grid"]
    brightfield = context["brightfield"]
    scales = [float(v) for v in params["scales_px"]]
    n_scales = len(scales)
    s = grid["cell_level_px"]
    ny, nx = grid["shape"]
    lh, lw = grid["level_shape"]
    level = grid["level"]
    # The widest filter's reach: cv2's Gaussian kernel (radius 4 sigma at
    # most for float input) and the Sobel's one pixel, with one to spare.
    reach = int(math.ceil(4 * max(scales))) + 2
    blocks = list(scan.iter_blocks(grid))
    total = len(blocks) + 2
    sum_e = np.zeros((n_scales, ny, nx))
    sum_i = np.zeros((ny, nx))
    sum_i2 = np.zeros((ny, nx))
    n_valid = np.zeros((ny, nx))
    with source_image.SHELF.reader(context["image_data"]) as source:
        say(0, total, PHASES[0])
        if brightfield:
            index = 0
        else:
            index = source.channel_index(context["key"])
            if index is None:
                raise AgentError("invalid_input",
                                 f"{context['channel']!r} is not a channel of this image")
        tissue_fraction, tissue_method = _tissue(source, context, grid)
        ceiling = scan._ceiling(source, index, brightfield)
        top = SATURATION_OF_CEILING * (ceiling if ceiling else np.inf)
        erode = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * reach + 1, 2 * reach + 1))
        timing["tissue_s"] = round(perf_counter() - started, 3)
        t0 = perf_counter()
        for done, (y0, x0, y1, x1) in enumerate(blocks):
            stop_if_asked()
            say(done + 1, total, PHASES[1])
            hy0, hx0 = max(0, y0 - reach), max(0, x0 - reach)
            hy1, hx1 = min(lh, y1 + reach), min(lw, x1 + reach)
            plane, _clipped = scan._read(source, index, level, (hx0, hy0, hx1, hy1),
                                         brightfield)
            plane = np.ascontiguousarray(plane, dtype=np.float32)
            want = (hy1 - hy0, hx1 - hx0)
            if plane.shape != want:
                fixed = np.zeros(want, dtype=np.float32)
                fixed[:plane.shape[0], :plane.shape[1]] = plane[:want[0], :want[1]]
                plane = fixed
            # Zeros are padding on a fluorescence image; glass counts on a
            # brightfield darkness plane (it is where the noise is measured).
            valid = plane < top
            if not brightfield:
                valid &= plane > 0
            valid_u8 = valid.astype(np.uint8)
            # A gradient is only trusted where its whole filter window is valid.
            support = cv2.erode(valid_u8, erode, borderType=cv2.BORDER_REPLICATE).astype(bool)
            window = (slice(y0 - hy0, y1 - hy0), slice(x0 - hx0, x1 - hx0))
            cy, cx = int(math.ceil((y1 - y0) / s)), int(math.ceil((x1 - x0) / s))
            gy0, gx0 = y0 // s, x0 // s
            inner_support = support[window]
            weight = inner_support.astype(np.float32)
            n_valid[gy0:gy0 + cy, gx0:gx0 + cx] += _cell_sums(weight, s, cy, cx)
            inner = plane[window] * weight
            sum_i[gy0:gy0 + cy, gx0:gx0 + cx] += _cell_sums(inner, s, cy, cx)
            sum_i2[gy0:gy0 + cy, gx0:gx0 + cx] += _cell_sums(inner * inner, s, cy, cx)
            del inner
            for k, sigma in enumerate(scales):
                smoothed = cv2.GaussianBlur(plane, (0, 0), sigma) if sigma > 0 else plane
                gx = cv2.Sobel(smoothed, cv2.CV_32F, 1, 0, ksize=3)
                gy = cv2.Sobel(smoothed, cv2.CV_32F, 0, 1, ksize=3)
                energy = (gx * gx + gy * gy)[window] * weight
                sum_e[k, gy0:gy0 + cy, gx0:gx0 + cx] += _cell_sums(energy, s, cy, cx)
                del gx, gy, energy
        timing["blocks_s"] = round(perf_counter() - t0, 3)
    stop_if_asked()
    say(len(blocks) + 1, total, PHASES[2])
    t0 = perf_counter()
    # Pixels each cell really has (the last row / column is partial).
    rows = np.minimum(s, lh - np.arange(ny) * s).astype(np.float64)
    cols = np.minimum(s, lw - np.arange(nx) * s).astype(np.float64)
    n_total = rows[:, None] * cols[None, :]
    # Focus is measured per cell and pooled over a tile in the log domain:
    # summed over a tile, the energy of its sharpest part would outweigh the
    # rest (a sharp edge holds far more gradient than a blurred one), and a
    # tile half in a blurred field would read sharp.
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_e = sum_e / n_valid
        mean_i = sum_i / n_valid
        variance = np.maximum(sum_i2 / n_valid - mean_i ** 2, 0.0)
    valid_fraction = n_valid / np.maximum(n_total, 1.0)
    tissue_fraction = np.asarray(tissue_fraction, dtype=np.float64)
    # The glass's energy at each scale: the noise floor every cell carries.
    glass = (tissue_fraction < BACKGROUND_TISSUE) & (valid_fraction > 0.5)
    if int(glass.sum()) >= MIN_NOISE_CELLS:
        noise_e = np.array([float(np.median(mean_e[k][glass])) for k in range(n_scales)])
        noise_source = "glass"
    else:
        noise_e = np.zeros(n_scales)
        noise_source = "none"
    corrected = np.maximum(np.nan_to_num(mean_e) - noise_e[:, None, None], 0.0)
    coarse = corrected[-1]
    in_tissue = (valid_fraction >= params["min_valid_fraction"]) & \
        (tissue_fraction >= params["min_tissue"])
    typical = float(np.median(coarse[in_tissue])) if in_tissue.any() else 0.0
    eps = max(1e-12, 1e-6 * typical)
    evaluable = in_tissue & (coarse >= max(params["k_noise"] * noise_e[-1], eps))
    # Finer-scale energy is never below the coarse (smoothing only removes
    # gradient), so the share is floored at 1: a fine energy lost in the noise
    # subtraction reads "no fine detail", never an infinite deficit.
    with np.errstate(divide="ignore", invalid="ignore"):
        fine_share = np.stack([np.maximum(corrected[k], coarse) / coarse
                               for k in range(n_scales - 1)])
    fine_share = np.where(evaluable[None], fine_share, np.nan)
    n_eval = int(evaluable.sum())
    k_ref = max(int(params["min_reference_tiles"]),
                int(math.ceil(params["reference_fraction"] * n_eval)))
    status = "ok" if n_eval >= 2 * max(int(params["min_reference_tiles"]), 1) \
        else "insufficient"
    nan_grid = np.full((ny, nx), np.nan)
    blur = blur_raw = deficit = nan_grid
    reference = {"fine_share": None, "tiles": 0, "fraction": params["reference_fraction"]}
    auto = None
    histogram = {"edges": np.linspace(0, 1, HISTOGRAM_BINS + 1).round(4).tolist(),
                 "counts": [0] * HISTOGRAM_BINS}
    if status == "ok":
        order = np.argsort(-fine_share[0][evaluable])
        top_idx = order[:min(k_ref, n_eval)]
        ref = [float(np.median(fine_share[k][evaluable][top_idx])) for k in range(n_scales - 1)]
        reference = {"fine_share": [round(v, 4) for v in ref], "tiles": int(top_idx.size),
                     "fraction": params["reference_fraction"]}
        # A fine share runs from the reference (sharp) down to 1 (no fine
        # detail left at all), so the deficit is read against that range:
        # the share of the image's own fine detail, on a log scale, this cell
        # has lost -- 0 as sharp as the sharpest tiles, 1 nothing finer than
        # the coarse scale.
        lost = np.stack([np.clip(np.log(ref[k] / np.where(evaluable, fine_share[k], 1.0))
                                 / np.log(max(ref[k], 1.0 + MIN_FINE_RANGE)), 0.0, 1.0)
                         for k in range(n_scales - 1)])
        deficit = np.where(evaluable, lost.mean(axis=0), np.nan)
        # The tile: the evaluable cells of the 3 x 3 centred on each one.
        weight = evaluable.astype(np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            pooled = _tile_sums(np.where(evaluable, deficit, 0.0)) / _tile_sums(weight)
        blur_raw = np.where(evaluable, np.clip(pooled, 0, 1), np.nan)
        blur = _smooth(blur_raw, evaluable, params["smooth_sigma_cells"])
        values = blur[evaluable]
        auto = auto_threshold(values, params)
        counts, _edges = np.histogram(values, bins=HISTOGRAM_BINS, range=(0.0, 1.0))
        histogram["counts"] = counts.astype(int).tolist()
    r0 = reference["fine_share"][0] if reference["fine_share"] else None
    global_blur = {"possible": bool(status != "ok" or (r0 is not None
                                                       and r0 < GLOBAL_MIN_FINE_SHARE)),
                   "fine_share": r0, "floor": GLOBAL_MIN_FINE_SHARE,
                   "reference_tiles": reference["tiles"]}
    timing["score_s"] = round(perf_counter() - t0, 3)
    # The image must still be the one measured.
    if context["image_stamp"] is not None and _stamp(context["identity"]) != \
            context["image_stamp"]:
        raise AgentError("conflict", "the image changed while Blur QC ran; run it again",
                         retryable=True)
    timing["total_s"] = round(perf_counter() - started, 3)
    pixel_um = context["pixel_um"]
    summary = {
        "version": VERSION, "fingerprint": fp, "project": project,
        "channel": context["channel"], "key": context["key"], "brightfield": brightfield,
        "level": int(level), "factor": float(grid["factor"]),
        "um_px": round(pixel_um * grid["factor"], 4) if pixel_um else None,
        "pixel_um": pixel_um, "grid": grid, "params": params, "scales_px": scales,
        "tissue_method": tissue_method,
        "noise": {"source": noise_source, "energy": [float(v) for v in noise_e]},
        "reference": reference, "auto_threshold": auto, "histogram": histogram,
        "global_blur": global_blur, "n_tiles": int(ny * nx), "n_evaluable": n_eval,
        "status": status, "timing": timing,
    }
    arrays = {"blur": blur, "blur_raw": blur_raw, "deficit": deficit,
              "fine_share": fine_share, "mean_energy": mean_e, "variance": variance,
              "valid_fraction": valid_fraction, "tissue_fraction": tissue_fraction,
              "evaluable": evaluable}
    return {"summary": summary,
            "arrays": {k: np.asarray(v, dtype=bool if k == "evaluable" else np.float32)
                       for k, v in arrays.items()}}


def _smooth(values, evaluable, sigma):
    """A Gaussian over the evaluable tiles only: weights are the evaluable
    mask, so a not-evaluable tile never bleeds into its neighbours."""
    from scipy import ndimage

    if sigma <= 0:
        return np.where(evaluable, values, np.nan)
    weight = evaluable.astype(np.float64)
    filled = np.where(evaluable, values, 0.0)
    num = ndimage.gaussian_filter(filled, sigma, mode="constant")
    den = ndimage.gaussian_filter(weight, sigma, mode="constant")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = num / den
    return np.where(evaluable & (den > 1e-6), np.clip(out, 0, 1), np.nan)


def auto_threshold(values, params) -> float:
    """median + 3 robust SD of the image's own scores, never below
    `AUTO_FLOOR`, within `AUTO_RANGE`."""
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    floor = AUTO_FLOOR
    if not values.size:
        return round(float(np.clip(floor, *AUTO_RANGE)), 4)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median))) * 1.4826
    return round(float(np.clip(max(floor, median + 3.0 * mad), *AUTO_RANGE)), 4)


# -- after the run: threshold, regions ----------------------------------------------------


def _component_geometry(ys, xs, grid):
    """GeoJSON of the union of these cells' squares (full-resolution px)."""
    import shapely

    from plexora.plugins.qc.server import polygons

    s = float(grid["cell_full_px"])
    width, height = grid["image_size"]
    boxes = shapely.box(xs * s, ys * s, np.minimum((xs + 1) * s, width),
                        np.minimum((ys + 1) * s, height))
    return polygons.to_geojson(shapely.union_all(boxes), simplify_px=0.35 * s)


def evaluate(arrays, summary, threshold, *, min_region_tiles=MIN_REGION_TILES,
             geometry=True) -> dict:
    """The blurred area and regions at `threshold`, from the stored scores
    (never a pixel). `blurred_pct` is the tissue in flagged tiles over the
    tissue in evaluable ones; regions are 8-connected groups of at least
    `min_region_tiles` flagged tiles, largest first."""
    from scipy import ndimage

    threshold = float(np.clip(float(threshold), 0.0, 1.0))
    min_region_tiles = max(1, int(min_region_tiles))
    evaluable = np.asarray(arrays["evaluable"], dtype=bool)
    blur = np.asarray(arrays["blur"], dtype=np.float64)
    tissue = np.asarray(arrays["tissue_fraction"], dtype=np.float64)
    with np.errstate(invalid="ignore"):
        flagged = evaluable & (np.nan_to_num(blur, nan=-1.0) >= threshold)
    labels, n = ndimage.label(flagged, structure=np.ones((3, 3), dtype=bool))
    mask = np.zeros(flagged.shape, dtype=bool)
    regions = []
    grid = summary["grid"]
    s = float(grid["cell_full_px"])
    pixel_um = summary.get("pixel_um")
    kept = []
    if n:
        sizes = ndimage.sum(np.ones_like(labels), labels, index=np.arange(1, n + 1))
        kept = [int(i) + 1 for i in np.argsort(-sizes) if sizes[i] >= min_region_tiles]
        mask = np.isin(labels, kept)
    denominator = float(tissue[evaluable].sum())
    blurred_pct = round(100.0 * float(tissue[mask].sum()) / denominator, 2) \
        if denominator > 0 else 0.0
    for k, label in enumerate(kept[:MAX_REGIONS]):
        ys, xs = np.nonzero(labels == label)
        area_px2 = float(ys.size) * s * s
        region = {"id": f"blur_{k + 1}", "tiles": int(ys.size), "area_px2": round(area_px2, 1),
                  "area_um2": round(area_px2 * pixel_um * pixel_um, 1) if pixel_um else None,
                  "bbox": [round(float(xs.min() * s), 1), round(float(ys.min() * s), 1),
                           round(float(min(grid["image_size"][0], (xs.max() + 1) * s)), 1),
                           round(float(min(grid["image_size"][1], (ys.max() + 1) * s)), 1)],
                  "mean_blur": round(float(np.nanmean(blur[ys, xs])), 4),
                  "max_blur": round(float(np.nanmax(blur[ys, xs])), 4)}
        if geometry:
            region["geometry"] = _component_geometry(ys, xs, grid)
        regions.append(region)
    return {"threshold": threshold, "min_region_tiles": min_region_tiles,
            "blurred_pct": blurred_pct, "denominator": "evaluable_tissue",
            "n_regions": len(kept), "residual": max(0, len(kept) - MAX_REGIONS),
            "regions": regions, "mask": mask}


# -- cache and store -------------------------------------------------------------------------


def _pointers(project) -> dict:
    """{label: fingerprint}: each channel's last result."""
    pointer = _read_json(_folder(project) / "current.json") or {}
    return dict(pointer.get("by_channel") or {})


def _point(project, label, fp):
    pointers = _pointers(project)
    if fp is None:
        pointers.pop(label, None)
    else:
        pointers[label] = fp
    _atomic_write(_folder(project) / "current.json",
                  json.dumps({"by_channel": pointers}, sort_keys=True).encode("utf-8"))


def _save(project, fp, result):
    import io

    from plexora.plugins.qc.server.results import now_iso

    folder = _folder(project)
    summary = {**result["summary"], "computed_at": now_iso()}
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **result["arrays"])
    _atomic_write(folder / f"{fp}.npz", buffer.getvalue())
    _atomic_write(folder / f"{fp}.json", json.dumps(summary, default=str).encode("utf-8"))
    _point(project, label_of(summary.get("channel")), fp)
    _sweep(project, keep=3)
    _remember(project, fp, summary, result["arrays"])
    return summary


def _remember(project, fp, summary, arrays):
    with _GUARD:
        _MEMORY[(project, fp)] = (summary, arrays)
        while len(_MEMORY) > 2 * MAX_SHOWN:
            _MEMORY.pop(next(iter(_MEMORY)))


def _sweep(project, keep):
    """Every channel's current result, and the `keep` newest others."""
    folder = _folder(project)
    pointed = set(_pointers(project).values())
    metas = sorted((p for p in folder.glob("*.json")
                    if p.name not in ("current.json", "settings.json", "running.json")
                    and p.stem not in pointed),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in metas[keep:]:
        for path in (stale, stale.with_suffix(".npz")):
            try:
                path.unlink()
            except OSError:
                pass


def _load(project, fp):
    """(summary, arrays) of a stored result, or None."""
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
    held = _load(project, fp)
    return held[0] if held else None


def load_arrays(project, fp):
    held = _load(project, fp) if fp else None
    return held[1] if held else None


def current_fingerprint(project, label):
    return _pointers(project).get(label)


def current(project, label):
    """The summary of `label`'s last result, or None."""
    fp = current_fingerprint(project, label)
    return load_summary(project, fp) if fp else None


def clear(project, label=None) -> list:
    """Forget one channel's result, or every one: the labels forgotten."""
    pointers = _pointers(project)
    gone = [label] if label is not None and label in pointers else \
        (list(pointers) if label is None else [])
    for name in gone:
        _point(project, name, None)
    return gone


def shown(session, project, names=None) -> list:
    """The channels listed, in order: the ones picked, else the first
    `DEFAULT_SHOWN` nuclear ones (the first channel without any); the
    brightfield plane on a brightfield slide."""
    record = session.project(project)
    names = names if names is not None else channel_names(record)
    image_data = session.image_data(project)
    from plexora.server.utils import source_image

    try:
        with source_image.SHELF.reader(image_data) as source:
            if source.is_brightfield:
                return [BRIGHTFIELD]
    except Exception:
        pass
    picked = [n for n in (settings(project).get("channels") or []) if n in names]
    if picked:
        return picked[:MAX_SHOWN]
    found = nuclear_candidates(names)
    return found[:DEFAULT_SHOWN] or names[:1]


def resolve(session, project, channel=None) -> str:
    """The label a call means: `channel` checked, else the first listed."""
    names = channel_names(session.project(project))
    listed = shown(session, project, names)
    if channel is None or listed == [BRIGHTFIELD]:
        return listed[0] if listed else BRIGHTFIELD
    if channel not in names:
        raise AgentError("invalid_input", f"{channel!r} is not a channel of this image",
                         detail={"channels": names[:100]})
    return channel


def load_or_run(session, project, *, channel=None, params=None, force=False,
                progress=None, check_cancelled=None) -> tuple:
    """(summary, reused), for one channel."""
    fp, context = plan(session, project, channel=None if channel == BRIGHTFIELD else channel,
                       params=params)
    if not force:
        summary = load_summary(project, fp)
        if summary is not None:
            _point(project, label_of(context["channel"]), fp)
            return summary, True
    result = run(session, project, fp, context, progress=progress,
                 check_cancelled=check_cancelled)
    return _save(project, fp, result), False


def color_of(project, label, index=0) -> str:
    """The colour a channel's map and regions are drawn in."""
    own = channel_settings(project, label).get("color")
    return own or PALETTE[index % len(PALETTE)]


def threshold_of(project, label, summary=None) -> dict:
    """{value, auto, source}: the user's threshold for this channel if one is
    set, else its result's automatic one."""
    summary = summary if summary is not None else current(project, label)
    auto = summary.get("auto_threshold") if summary else None
    user = channel_settings(project, label).get("threshold")
    if user is not None:
        return {"value": float(user), "auto": auto, "source": "user"}
    fallback = auto if auto is not None else AUTO_FLOOR
    return {"value": float(fallback), "auto": auto, "source": "auto"}


def min_region_tiles_of(project) -> int:
    value = settings(project).get("min_region_tiles")
    return int(value) if value else MIN_REGION_TILES


def evaluation(project, label, *, threshold=None, min_region_tiles=None, geometry=True):
    """`evaluate` of a channel's result at its effective (or the given)
    threshold, cached per (result, threshold, size): or None without one."""
    summary = current(project, label)
    if summary is None or summary.get("status") != "ok":
        return None
    arrays = load_arrays(project, summary["fingerprint"])
    if arrays is None:
        return None
    t = threshold if threshold is not None else threshold_of(project, label, summary)["value"]
    m = min_region_tiles if min_region_tiles is not None else min_region_tiles_of(project)
    key = (project, summary["fingerprint"], round(float(t), 6), int(m), bool(geometry))
    with _GUARD:
        held = _EVALUATIONS.get(key)
    if held is not None:
        return held
    out = evaluate(arrays, summary, t, min_region_tiles=m, geometry=geometry)
    with _GUARD:
        _EVALUATIONS[key] = out
        while len(_EVALUATIONS) > 32:
            _EVALUATIONS.pop(next(iter(_EVALUATIONS)))
    return out


def public_evaluation(result, *, regions=False):
    if result is None:
        return None
    out = {k: result[k] for k in ("threshold", "min_region_tiles", "blurred_pct",
                                  "denominator", "n_regions", "residual")}
    if regions:
        out["regions"] = result["regions"]
    return out


def channel_status(session, project, label, index=0, *, include_regions=False,
                   available=True) -> dict:
    """One listed channel: its colour, result, threshold, blurred area and
    whether these inputs would still give that result (`stale`)."""
    summary = current(project, label)
    out = {"channel": label, "color": color_of(project, label, index), "summary": None,
           "threshold": threshold_of(project, label, summary), "evaluation": None,
           "stale": False}
    if summary is None:
        return out
    out["summary"] = summary
    if available:
        try:
            fp, _context = plan(session, project, channel=summary.get("channel"),
                                params=summary.get("params"))
            out["stale"] = fp != summary.get("fingerprint")
        except AgentError:
            out["stale"] = True
    out["evaluation"] = public_evaluation(
        evaluation(project, label, geometry=include_regions), regions=include_regions)
    return out


def public_status(session, project, *, include_regions=False, channel=None) -> dict:
    """What the panel and an agent read: the listed channels (or just
    `channel`), each with its result, threshold and blurred area; the nuclear
    candidates a + adds from; and a running job."""
    record = session.project(project)
    names = channel_names(record)
    available = not record.image.is_blank
    listed = shown(session, project, names) if available else []
    wanted = [resolve(session, project, channel)] if channel is not None else listed
    out = {"available": available, "brightfield": listed == [BRIGHTFIELD],
           "shown": listed, "candidates": nuclear_candidates(names),
           "channels": names[:200], "detected": default_channel(names),
           "min_region_tiles": min_region_tiles_of(project),
           "results": [channel_status(session, project, label,
                                      listed.index(label) if label in listed else 0,
                                      include_regions=include_regions, available=available)
                       for label in wanted],
           "job": None}
    running = _read_json(_folder(project) / "running.json") or {}
    if running.get("job_id"):
        from plexora.agent import jobs

        record_job = jobs.store().get(running["job_id"])
        if record_job and record_job.get("status") in ("queued", "running"):
            out["job"] = {**{k: record_job.get(k) for k in ("job_id", "status", "progress")},
                          "channels": running.get("channels") or []}
    return out


def note_running(project, job_id, channels=None):
    path = _folder(project) / "running.json"
    if job_id:
        _atomic_write(path, json.dumps({"job_id": job_id,
                                        "channels": list(channels or [])}).encode("utf-8"))
    else:
        try:
            path.unlink()
        except OSError:
            pass


# -- what the viewer draws --------------------------------------------------------------------


def _b64(values) -> str:
    return base64.b64encode(np.ascontiguousarray(values, dtype=np.uint8).tobytes()).decode(
        "ascii")


def viewer_map(project, label) -> dict:
    """One channel's continuous score grid for the heatmap: uint8 (255 = Blur
    1.0; 0 where not evaluable) row-major, with the grid in full-resolution px."""
    summary = current(project, label)
    if summary is None or summary.get("status") != "ok":
        return {"available": False, "channel": label}
    arrays = load_arrays(project, summary["fingerprint"])
    if arrays is None:
        return {"available": False, "channel": label}
    grid = summary["grid"]
    ny, nx = grid["shape"]
    evaluable = np.asarray(arrays["evaluable"], dtype=bool)
    blur = np.nan_to_num(np.asarray(arrays["blur"], dtype=np.float64), nan=0.0)
    return {"available": True, "channel": label, "fingerprint": summary["fingerprint"],
            "grid": {"x0": 0.0, "y0": 0.0, "step": float(grid["cell_full_px"]), "nx": int(nx),
                     "ny": int(ny)},
            "blur": _b64(np.where(evaluable, np.round(np.clip(blur, 0, 1) * 255), 0)),
            "evaluable": _b64(evaluable)}


def viewer_mask(project, label, threshold=None, min_region_tiles=None) -> dict:
    """One channel's thresholded mask and regions (a preview: nothing is
    stored)."""
    result = evaluation(project, label, threshold=threshold,
                        min_region_tiles=min_region_tiles)
    if result is None:
        return {"available": False, "channel": label}
    out = public_evaluation(result, regions=True)
    out.update(available=True, channel=label,
               fingerprint=current_fingerprint(project, label), mask=_b64(result["mask"]))
    return out
