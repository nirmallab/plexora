"""The deterministic pre-scan: every channel, every part of the tissue, cheaply.

Finding where to look is code's job, so before an agent sees anything each
channel is read once, block by block, at a pyramid level where one map cell
(`CELL_UM`, 50 µm by default) is a few dozen pixels, and reduced to spatial QC
maps: intensity quantiles, saturation, empty pixels, focus (Laplacian energy),
compact bright objects (a white top-hat), and from those the derived maps the
detectors read -- relative focus, background, the illumination surface and its
residual, diffuse bright patches, tile-seam steps and cross-cycle registration.
An overview level (about a megapixel) gives each channel's global numbers and
the tissue mask.

Every pixel is read through `SourceImage.read(channel, level, box)`, the one
path that also works for an image on a data node, in bounded blocks, so peak
memory stays small whatever the slide's size. The result is a pure function of
the image, the parameters and the cycle override (`fingerprint`), stored once
as `<agent_root>/qc/<project>/scan_<fingerprint>.npz` + `.json` and reused by
every later session and every strictness.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from plexora.plugins.qc.server import schemas

#: [cal] one map cell's side in microns, and in full-resolution pixels when
#: the image states no pixel size.
CELL_UM = 50.0
CELL_PX = 128
#: A map cell is at least this many pixels a side at the level it is read at.
MAP_MIN_LEVEL_PX = 12
MAP_TARGET_LEVEL_PX = 16
#: Blocks are at most this many level pixels a side (about 4 MP).
BLOCK_PX = 2048
#: Extra pixels read around a block so neighbourhood filters are exact.
HALO = 8
#: [cal] the compact-object top-hat's radius, microns (capped in pixels):
#: smaller than a nucleus, so a cell survives the opening and a speck does not.
TOPHAT_UM = 4.0
TOPHAT_MAX_PX = 4
SATURATION_OF_CEILING = 0.98
#: [cal] the smallest spread a diffuse-brightness z is scaled by (log units).
DIFFUSE_FLOOR = 0.05
#: Keeps a flat cell's focus ratio finite (log units squared).
FOCUS_EPS = 1e-3
#: [cal] a tile seam: a grid line whose median step between neighbouring
#: cells (log units) is this many robust sds above the rest, and at least
#: SEAM_MIN_STEP (a 10 % intensity step) -- the floor keeps a flat channel's
#: zero spread from turning noise into seams.
SEAM_MIN_Z = 4.0
SEAM_MIN_STEP = 0.1
SEAM_NOISE_FLOOR = 0.02
#: [cal] an empty channel: tissue no brighter than glass, and no bright tail.
EMPTY_TISSUE_RATIO = 1.15
EMPTY_DECADES = 0.15
#: [cal] how far the stain is smoothed before the tissue threshold, microns.
TISSUE_SMOOTH_UM = 15.0

#: Per-cell metrics of each channel, in this order in the maps.
MAP_METRICS = ("mean", "p10", "median", "p90", "p99", "saturation", "zero", "focus",
               "contrast", "bright_compact")
DERIVED_METRICS = ("focus_rel", "background", "illumination_fit", "illumination_residual",
                   "bright_diffuse", "seam")

PARAMS_DEFAULT = {"cell_um": CELL_UM, "cell_px": CELL_PX}


class ScanResult:
    """A scan in memory: JSON `meta` and named numpy `maps`."""

    def __init__(self, meta, maps):
        self.meta = meta
        self.maps = maps

    @property
    def grid(self):
        return self.meta["grid"]

    @property
    def shape(self):
        return tuple(self.meta["grid"]["shape"])

    @property
    def channels(self):
        return self.meta["channels"]

    def channel(self, name):
        for channel in self.meta["channels"]:
            if channel["name"] == name:
                return channel
        raise KeyError(name)

    def map(self, channel, metric):
        return self.maps.get(f"{channel}::{metric}")

    def shared(self, name):
        return self.maps.get(f"::{name}")

    def nuclear(self):
        return self.meta.get("nuclear")

    def tissue(self, core=False):
        fraction = self.shared("tissue_fraction")
        if fraction is None:
            return np.ones(self.shape, dtype=bool)
        return fraction >= (0.9 if core else 0.25)


# -- the map grid ---------------------------------------------------------------


def choose_map_grid(source, pixel_um, *, cell_um=CELL_UM, cell_px=CELL_PX) -> dict:
    """Which pyramid level a map is read at, and how big a map cell is."""
    height, width = source.level_shape(0)
    levels = max(1, int(source.levels))
    target_full = cell_um / pixel_um if pixel_um else float(cell_px)
    target_full = max(float(MAP_MIN_LEVEL_PX), target_full)
    level = int(np.clip(math.floor(math.log2(max(1.0, target_full / MAP_TARGET_LEVEL_PX))),
                        0, levels - 1))
    while level > 0:
        lh, lw = source.level_shape(level)
        factor = width / max(1, lw)
        if target_full / factor >= MAP_MIN_LEVEL_PX:
            break
        level -= 1
    lh, lw = source.level_shape(level)
    factor = width / max(1, lw)
    cell_level_px = max(1, int(round(target_full / factor)))
    cell_full_px = cell_level_px * factor
    ny = int(math.ceil(lh / cell_level_px))
    nx = int(math.ceil(lw / cell_level_px))
    return {"level": level, "level_shape": [int(lh), int(lw)], "factor": float(factor),
            "cell_level_px": cell_level_px, "cell_full_px": float(cell_full_px),
            "shape": [ny, nx], "image_size": [int(width), int(height)],
            "cell_um": float(cell_full_px * pixel_um) if pixel_um else None}


def cell_box_fullres(grid, iy, ix) -> tuple:
    s = grid["cell_full_px"]
    w, h = grid["image_size"]
    return (ix * s, iy * s, min(w, (ix + 1) * s), min(h, (iy + 1) * s))


def bbox_fullres(grid, mask) -> tuple | None:
    """(x0, y0, x1, y1) in full-resolution pixels of a map mask's cells."""
    ys, xs = np.nonzero(mask)
    if not ys.size:
        return None
    s = grid["cell_full_px"]
    w, h = grid["image_size"]
    return (float(xs.min() * s), float(ys.min() * s), float(min(w, (xs.max() + 1) * s)),
            float(min(h, (ys.max() + 1) * s)))


def iter_blocks(grid):
    """(y0, x0, y1, x1) in level pixels, aligned to map cells, ≤ BLOCK_PX a side."""
    s = grid["cell_level_px"]
    lh, lw = grid["level_shape"]
    step = max(s, (BLOCK_PX // s) * s)
    for y0 in range(0, lh, step):
        for x0 in range(0, lw, step):
            yield (y0, x0, min(lh, y0 + step), min(lw, x0 + step))


# -- reading ------------------------------------------------------------------------


def _read(source, index, level, box, brightfield=False):
    """(plane float32, clipped box) of one channel at one level; brightfield
    reads the darkness plane (255 - luminance)."""
    if brightfield:
        rgb = source.read_rgb(level, box)
        lum = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        return (255.0 - lum).astype(np.float32), box
    plane, clipped = source.read(index, level, box)
    return np.asarray(plane, dtype=np.float32), clipped


def _ceiling(source, index, brightfield):
    if brightfield:
        return 255.0
    try:
        plane, _ = source.read(index, max(0, source.levels - 1), (0, 0, 2, 2))
        dtype = np.asarray(plane).dtype
        if dtype.kind in "ui":
            return float(np.iinfo(dtype).max)
    except Exception:
        pass
    return None


def _block_metrics(plane, s, ceiling):
    """Per-cell intensity metrics of one block (level pixels)."""
    h, w = plane.shape
    cy, cx = int(math.ceil(h / s)), int(math.ceil(w / s))
    padded = np.full((cy * s, cx * s), np.nan, dtype=np.float32)
    padded[:h, :w] = plane
    cells = padded.reshape(cy, s, cx, s).transpose(0, 2, 1, 3).reshape(cy, cx, s * s)
    out = {}
    with np.errstate(all="ignore"):
        out["mean"] = np.nanmean(cells, axis=-1)
        q = np.nanpercentile(cells, [10, 50, 90, 99], axis=-1)
        out["p10"], out["median"], out["p90"], out["p99"] = q
        n = np.maximum(1, np.isfinite(cells).sum(axis=-1))
        filled = np.nan_to_num(cells, nan=-1)
        if ceiling:
            out["saturation"] = (filled >= SATURATION_OF_CEILING * ceiling).sum(axis=-1) / n
        else:
            out["saturation"] = np.zeros((cy, cx), dtype=np.float32)
        out["zero"] = (filled == 0).sum(axis=-1) / n
    return {k: np.asarray(v, dtype=np.float32) for k, v in out.items()}


def _reduce(values, s, cy, cx):
    h, w = values.shape
    padded = np.full((cy * s, cx * s), np.nan, dtype=np.float32)
    padded[:h, :w] = values
    with np.errstate(all="ignore"):
        return np.nanmean(padded.reshape(cy, s, cx, s), axis=(1, 3))


def _disk(radius):
    r = int(radius)
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    return (xx * xx + yy * yy) <= r * r


# -- the overview pass --------------------------------------------------------------


def _otsu_log(plane):
    logged = np.log1p(np.maximum(plane, 0))
    counts, edges = np.histogram(logged, bins=256)
    centres = (edges[:-1] + edges[1:]) / 2
    p = counts / max(1, counts.sum())
    w0 = np.cumsum(p)
    mu = np.cumsum(p * centres)
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu[-1] * w0 - mu) ** 2 / (w0 * (1 - w0))
    between[~np.isfinite(between)] = -1
    if between.max() <= 0:
        return None
    return logged > centres[int(np.argmax(between))]


def _smoothed_log(plane, sigma):
    from scipy import ndimage

    return ndimage.gaussian_filter(np.log1p(np.maximum(plane, 0)).astype(np.float32), sigma)


def _otsu_plane(logged):
    counts, edges = np.histogram(logged, bins=256)
    centres = (edges[:-1] + edges[1:]) / 2
    p = counts / max(1, counts.sum())
    w0 = np.cumsum(p)
    mu = np.cumsum(p * centres)
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu[-1] * w0 - mu) ** 2 / (w0 * (1 - w0))
    between[~np.isfinite(between)] = -1
    if between.max() <= 0:
        return None
    return logged > centres[int(np.argmax(between))]


def tissue_estimate(overviews, nuclear, brightfield=False, *, sigma_px=None) -> dict:
    """{mask (overview level), method, channel, holes}: the tissue denominator.

    The stain is smoothed over a few cells' width before Otsu, so tissue is
    the region nuclei are in -- not the nuclei themselves -- and components
    smaller than a percent of the largest are dropped (dust on the glass)."""
    from scipy import ndimage

    if not overviews:
        return {"mask": np.ones((1, 1), dtype=bool), "method": "all_pixels", "channel": None,
                "holes": np.zeros((1, 1), dtype=bool)}
    shape = next(iter(overviews.values())).shape
    sigma = sigma_px if sigma_px else max(2.0, min(shape) / 150.0)
    if brightfield:
        logged = _smoothed_log(next(iter(overviews.values())), sigma)
        method, channel = "brightfield_otsu", None
    elif nuclear and nuclear in overviews:
        logged = _smoothed_log(overviews[nuclear], sigma)
        method, channel = "nuclear_otsu", nuclear
    else:
        stack = []
        for plane in overviews.values():
            top = float(np.percentile(plane, 99)) or 1.0
            stack.append(plane / top)
        logged = _smoothed_log(np.max(stack, axis=0) * 1000.0, sigma)
        method, channel = "max_projection_otsu", None
    mask = _otsu_plane(logged)
    if mask is None or mask.mean() < 0.01:
        return {"mask": np.ones(shape, dtype=bool), "method": "all_pixels", "channel": None,
                "holes": np.zeros(shape, dtype=bool)}
    filled = ndimage.binary_fill_holes(mask)
    holes = filled & ~mask
    # Small holes (a vessel lumen) are tissue; big ones are kept out, and
    # are what `tissue_loss` looks at.
    labels, n = ndimage.label(holes)
    if n:
        sizes = ndimage.sum(np.ones_like(labels), labels, index=np.arange(1, n + 1))
        small = np.isin(labels, 1 + np.flatnonzero(sizes < 0.005 * max(1, filled.sum())))
        mask = mask | small
    labels, n = ndimage.label(mask)
    if n > 1:
        sizes = ndimage.sum(np.ones_like(labels), labels, index=np.arange(1, n + 1))
        keep = 1 + np.flatnonzero(sizes >= 0.01 * sizes.max())
        mask = np.isin(labels, keep)
    return {"mask": mask, "method": method, "channel": channel, "holes": filled & ~mask}


# -- derived maps ---------------------------------------------------------------------


def surface_fit(values, weights):
    """A weighted quadratic surface over the grid: (fitted, r2)."""
    ny, nx = values.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    u = (xx / max(1, nx - 1)) * 2 - 1
    v = (yy / max(1, ny - 1)) * 2 - 1
    keep = np.isfinite(values) & (weights > 0)
    if keep.sum() < 12:
        return np.full_like(values, np.nan, dtype=np.float32), None
    design = np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=-1)
    a = design[keep] * np.sqrt(weights[keep])[:, None]
    b = values[keep] * np.sqrt(weights[keep])
    coef, *_ = np.linalg.lstsq(a, b, rcond=None)
    fitted = (design @ coef).astype(np.float32)
    y = values[keep]
    total = float(((y - y.mean()) ** 2).sum())
    r2 = float(1 - ((y - fitted[keep]) ** 2).sum() / total) if total > 0 else 0.0
    return fitted, r2


def robust_z(values, mask, *, floor=1e-9):
    """(values - median) / (1.4826 MAD) over `mask`, the scale never below
    `floor` -- a map that is mostly one value (a fraction that is 0 almost
    everywhere) would otherwise turn every non-zero cell into a huge z."""
    data = values[mask & np.isfinite(values)]
    if data.size < 5:
        return np.zeros_like(values, dtype=np.float32)
    median = float(np.median(data))
    mad = float(np.median(np.abs(data - median))) * 1.4826
    scale = mad if mad > 1e-9 else (float(np.std(data)) or 1.0)
    return ((values - median) / max(scale, floor)).astype(np.float32)


def _seam_map(median_log, tissue):
    """Per-cell step strength across the grid's straight lines: a tile seam is
    a whole row or column of cells whose neighbours differ in the same way."""
    ny, nx = median_log.shape
    out = np.zeros((ny, nx), dtype=np.float32)
    if ny < 4 or nx < 4:
        return out, {"columns": [], "rows": []}
    dx = np.abs(np.diff(median_log, axis=1))
    dy = np.abs(np.diff(median_log, axis=0))
    tx = tissue[:, 1:] & tissue[:, :-1]
    ty = tissue[1:, :] & tissue[:-1, :]
    col = np.array([np.nanmedian(dx[:, j][tx[:, j]]) if tx[:, j].sum() >= 3 else np.nan
                    for j in range(nx - 1)])
    row = np.array([np.nanmedian(dy[i, :][ty[i, :]]) if ty[i, :].sum() >= 3 else np.nan
                    for i in range(ny - 1)])
    found = {"columns": [], "rows": []}
    for name, profile in (("columns", col), ("rows", row)):
        finite = profile[np.isfinite(profile)]
        if finite.size < 4:
            continue
        median = float(np.median(finite))
        mad = max(SEAM_NOISE_FLOOR, float(np.median(np.abs(finite - median))) * 1.4826)
        z = (profile - median) / mad
        strong = (np.nan_to_num(z) >= SEAM_MIN_Z) & (np.nan_to_num(profile) >= SEAM_MIN_STEP)
        for index in np.flatnonzero(strong):
            found[name].append({"index": int(index), "z": float(z[index]),
                                "step": float(profile[index])})
            if name == "columns":
                out[:, index] = np.maximum(out[:, index], z[index])
                out[:, index + 1] = np.maximum(out[:, index + 1], z[index])
            else:
                out[index, :] = np.maximum(out[index, :], z[index])
                out[index + 1, :] = np.maximum(out[index + 1, :], z[index])
    out[~tissue] = 0
    return out, found


def derive(maps, name, tissue_fraction):
    """The derived maps of one channel (in place in `maps`); returns its
    illumination numbers."""
    from scipy import ndimage

    tissue = tissue_fraction >= 0.25
    core = tissue_fraction >= 0.9
    if core.sum() < 5:
        core = tissue
    focus = maps[f"{name}::focus"]
    with np.errstate(all="ignore"):
        ref = float(np.nanmedian(focus[core])) if core.any() else float(np.nanmedian(focus))
    maps[f"{name}::focus_rel"] = (focus / ref).astype(np.float32) if ref and ref > 0 \
        else np.ones_like(focus, dtype=np.float32)
    background = np.log1p(np.maximum(maps[f"{name}::p10"], 0))
    maps[f"{name}::background"] = background.astype(np.float32)
    fitted, r2 = surface_fit(background, tissue_fraction.astype(np.float64))
    maps[f"{name}::illumination_fit"] = fitted
    maps[f"{name}::illumination_residual"] = (background - fitted).astype(np.float32)
    median_log = np.log1p(np.maximum(maps[f"{name}::median"], 0)).astype(np.float32)
    # Glass is filled with the tissue's median, so the surround of an edge
    # cell is tissue, not the dark slide (which would make every edge "bright").
    on_tissue = tissue & np.isfinite(median_log)
    fill = float(np.nanmedian(median_log[on_tissue])) if on_tissue.any() \
        else float(np.nanmedian(median_log))
    filled = np.where(on_tissue, median_log, fill).astype(np.float32)
    local = ndimage.gaussian_filter(filled, 1.0)
    surround = ndimage.median_filter(filled, size=9, mode="nearest")
    diffuse = local - surround
    maps[f"{name}::bright_diffuse"] = robust_z(diffuse, tissue, floor=DIFFUSE_FLOOR)
    seam, seams = _seam_map(filled, tissue)
    maps[f"{name}::seam"] = seam
    finite = fitted[np.isfinite(fitted) & tissue]
    spread = float(finite.max() - finite.min()) if finite.size else None
    return {"r2": r2, "range_log": spread, "seams": seams}


# -- cross-cycle -------------------------------------------------------------------------


def cross_cycle(overviews, cycles, tissue_mask, overview_factor, pixel_um, sigma_px=3.0):
    """Registration shift and tissue loss of every cycle's nuclear channel
    against the first cycle's, from the overview planes."""
    from skimage.registration import phase_cross_correlation

    # Each cycle keeps its own index: a cycle without a nuclear stain is
    # skipped, never allowed to shift the next one's number.
    pairs = [(c["index"], c.get("nuclear")) for c in cycles.get("cycles") or []]
    pairs = [(i, n) for i, n in pairs if n and n in overviews]
    nuclear = [n for _i, n in pairs]
    if len(nuclear) < 2:
        return {"available": False, "reason": "fewer than two nuclear cycles", "cycles": []}
    first = overviews[nuclear[0]]
    first_log = np.log1p(np.maximum(first, 0))
    out = []
    h, w = first.shape
    blocks = 8
    for index, name in pairs[1:]:
        plane = overviews[name]
        plane_log = np.log1p(np.maximum(plane, 0))
        try:
            shift, error, _ = phase_cross_correlation(first_log, plane_log,
                                                      normalization="phase")
        except Exception:
            shift, error = (0.0, 0.0), 1.0
        shift = [float(shift[0]), float(shift[1])]
        local = []
        for by in range(blocks):
            for bx in range(blocks):
                y0, y1 = by * h // blocks, (by + 1) * h // blocks
                x0, x1 = bx * w // blocks, (bx + 1) * w // blocks
                if y1 - y0 < 16 or x1 - x0 < 16:
                    continue
                if tissue_mask[y0:y1, x0:x1].mean() < 0.2:
                    continue
                try:
                    s, _e, _ = phase_cross_correlation(first_log[y0:y1, x0:x1],
                                                       plane_log[y0:y1, x0:x1],
                                                       normalization="phase")
                except Exception:
                    continue
                local.append({"box": [x0, y0, x1, y1], "dy": float(s[0]), "dx": float(s[1])})
        # Tissue loss: tissue in the first cycle, dark in this one, with this
        # cycle's threshold scaled by the median ratio of the two.
        on = tissue_mask
        ratio = float(np.median(plane[on]) / max(1e-6, np.median(first[on]))) if on.any() \
            else 1.0
        smooth_first = _smoothed_log(first, sigma_px)
        smooth_this = _smoothed_log(plane, sigma_px)
        # Four times dimmer than the first cycle predicts, after the two
        # cycles' overall difference: the nuclei there are gone.
        lost = on & (smooth_this < smooth_first + np.log(max(ratio, 1e-6)) - np.log(4.0))
        full_shift_px = [v * overview_factor for v in shift]
        out.append({"cycle": index, "channel": name, "shift_px_overview": shift,
                    "shift_px": full_shift_px,
                    "shift_um": [v * pixel_um for v in full_shift_px] if pixel_um else None,
                    "error": float(error), "local": local,
                    "intensity_ratio": ratio, "lost_mask": lost})
    return {"available": True, "reference": nuclear[0], "cycles": out}


# -- the scan ---------------------------------------------------------------------------


def fingerprint(identity, grid, keys, pixel_um, params, override) -> str:
    blob = json.dumps({"v": schemas.SCAN_VERSION, "identity": identity, "grid": grid,
                       "keys": keys, "pixel": pixel_um, "params": params,
                       "cycles": override}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _folder(project) -> Path:
    from plexora import paths

    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(project))
    return paths.agent_root() / "qc" / safe


def load(project, fp):
    folder = _folder(project)
    meta_path = folder / f"scan_{fp}.json"
    maps_path = folder / f"scan_{fp}.npz"
    if not meta_path.is_file() or not maps_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        with np.load(maps_path, allow_pickle=False) as held:
            maps = {k: held[k] for k in held.files}
    except Exception:
        return None
    if meta.get("version") != schemas.SCAN_VERSION:
        return None
    return ScanResult(meta, maps)


def save(result):
    folder = _folder(result.meta["project"])
    folder.mkdir(parents=True, exist_ok=True)
    fp = result.meta["fingerprint"]
    tmp = folder / f"scan_{fp}.tmp.npz"
    np.savez_compressed(tmp, **result.maps)
    tmp.replace(folder / f"scan_{fp}.npz")
    (folder / f"scan_{fp}.json").write_text(json.dumps(result.meta, default=str),
                                            encoding="utf-8")
    sweep(result.meta["project"])


def sweep(project, keep=3):
    folder = _folder(project)
    if not folder.is_dir():
        return
    metas = sorted(folder.glob("scan_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in metas[keep:]:
        for path in (stale, stale.with_suffix(".npz")):
            try:
                path.unlink()
            except OSError:
                pass


_MEMORY: dict = {}


def plan(session, project, *, params=None, override=None, channels=None):
    """(fingerprint, context) of a scan without running it."""
    from plexora.server.utils import pixel_scale, source_image
    from plexora.agent.render import resolve_channel

    params = {**PARAMS_DEFAULT, **(params or {})}
    record = session.project(project)
    if record.image.is_blank:
        from plexora.agent.errors import AgentError

        raise AgentError("unsupported_modality", "this sample has no image to check")
    image_data = session.image_data(project)
    pixel = pixel_scale.pixel_size(record)
    pixel_um = float(pixel["value"]) if pixel else None
    if params.get("pixel_um") and not pixel_um:
        pixel_um = float(params["pixel_um"])
    channel_records = list(record.image.real_channels)
    names = [c.get("fullname") or c.get("name") for c in channel_records]
    wanted = list(channels) if channels else names
    keys = {}
    for name in wanted:
        index, found = resolve_channel(name, channel_records)
        keys[found.get("fullname") or found.get("name")] = source_image.channel_key(found)
    with source_image.SHELF.reader(image_data) as source:
        grid = choose_map_grid(source, pixel_um, cell_um=params["cell_um"],
                               cell_px=params["cell_px"])
        brightfield = bool(source.is_brightfield)
    identity = source_image.ReaderShelf.identity_of(image_data)
    fp = fingerprint(identity, grid, keys, pixel_um, params, override)
    return fp, {"record": record, "image_data": image_data, "pixel_um": pixel_um,
                "names": names, "keys": keys, "grid": grid, "params": params,
                "brightfield": brightfield, "identity": identity, "override": override}


def load_or_run(session, project, *, params=None, override=None, channels=None,
                progress=None, cancelled=None):
    """(ScanResult, reused)."""
    fp, context = plan(session, project, params=params, override=override,
                       channels=channels)
    held = _MEMORY.get((project, fp))
    if held is not None:
        return held, True
    stored = load(project, fp)
    if stored is not None:
        _MEMORY[(project, fp)] = stored
        return stored, True
    result = run(session, project, fp, context, progress=progress, cancelled=cancelled)
    save(result)
    if len(_MEMORY) > 8:
        _MEMORY.pop(next(iter(_MEMORY)))
    _MEMORY[(project, fp)] = result
    return result, False


def run(session, project, fp, context, *, progress=None, cancelled=None) -> ScanResult:
    from plexora.agent.evidence import image_qc
    from plexora.plugins.qc.server import cycles as cycle_rules
    from plexora.server.utils import source_image

    grid = context["grid"]
    keys = context["keys"]
    names = list(keys)
    pixel_um = context["pixel_um"]
    brightfield = context["brightfield"]
    nuclear = next((n for n in names if cycle_rules.is_nuclear(n)), None)
    cycles = cycle_rules.infer(names, override=context["override"]) if not brightfield \
        else {"cycles": [], "of_channel": {}, "method": "none", "confidence": 0.0,
              "evidence": {"brightfield": True}}
    maps = {}
    channels_meta = []
    s = grid["cell_level_px"]
    level = grid["level"]
    ny, nx = grid["shape"]
    blocks = list(iter_blocks(grid))
    tophat_px = int(min(TOPHAT_MAX_PX, math.ceil(TOPHAT_UM / (pixel_um * grid["factor"]))
                        if pixel_um else 3))
    with source_image.SHELF.reader(context["image_data"]) as source:
        overview_level = image_qc.overview_level(source)
        ov_h, ov_w = source.level_shape(overview_level)
        overview_factor = grid["image_size"][0] / max(1, ov_w)
        overviews = {}
        read_names = names if not brightfield else names[:1]
        for name in read_names:
            index = source.channel_index(keys[name]) if not brightfield else 0
            plane, _ = _read(source, index, overview_level, (0, 0, ov_w, ov_h), brightfield)
            overviews[name] = plane
        sigma_px = (TISSUE_SMOOTH_UM / (pixel_um * overview_factor)) if pixel_um else None
        tissue = tissue_estimate(overviews, nuclear, brightfield, sigma_px=sigma_px)
        tissue_fraction = _grid_from_overview(tissue["mask"], grid, ov_w, ov_h)
        maps["::tissue_fraction"] = tissue_fraction.astype(np.float32)
        holes = tissue.get("holes")
        if holes is not None:
            maps["::tissue_holes"] = _grid_from_overview(holes, grid, ov_w, ov_h)
        total = len(read_names) * len(blocks)
        done = 0
        for name in read_names:
            index = source.channel_index(keys[name]) if not brightfield else 0
            plane_ov = overviews[name]
            on = tissue["mask"] if tissue["mask"].shape == plane_ov.shape else None
            values = plane_ov[on] if on is not None and on.any() else plane_ov.ravel()
            p50 = float(np.percentile(values, 50)) if values.size else 0.0
            p999 = float(np.percentile(values, 99.9)) if values.size else 0.0
            compact_threshold = max(1.0, 0.25 * (p999 - p50))
            ceiling = _ceiling(source, index, brightfield)
            channel_maps = {m: np.full((ny, nx), np.nan, dtype=np.float32)
                            for m in MAP_METRICS}
            for (y0, x0, y1, x1) in blocks:
                if cancelled is not None and cancelled():
                    from plexora.agent.errors import AgentError

                    raise AgentError("cancelled", "the scan was stopped")
                hy0, hx0 = max(0, y0 - HALO), max(0, x0 - HALO)
                hy1 = min(grid["level_shape"][0], y1 + HALO)
                hx1 = min(grid["level_shape"][1], x1 + HALO)
                plane, _clipped = _read(source, index, level, (hx0, hy0, hx1, hy1),
                                        brightfield)
                inner = plane[y0 - hy0:y0 - hy0 + (y1 - y0), x0 - hx0:x0 - hx0 + (x1 - x0)]
                metrics = _block_metrics(inner, s, ceiling)
                # Neighbourhood filters on the haloed plane, cropped after.
                full = _filtered(plane, tophat_px, compact_threshold)
                cy, cx = metrics["mean"].shape
                reduced = {}
                for key in ("lap2", "log", "log2", "bright_compact"):
                    cropped = full[key][y0 - hy0:y0 - hy0 + (y1 - y0),
                                        x0 - hx0:x0 - hx0 + (x1 - x0)]
                    reduced[key] = _reduce(cropped, s, cy, cx)
                with np.errstate(all="ignore"):
                    variance = np.maximum(reduced["log2"] - reduced["log"] ** 2, 0)
                    # High-frequency energy over the cell's own spread: blur
                    # lowers the first much more than the second, while how
                    # much stain a cell holds moves both together.
                    metrics["focus"] = (reduced["lap2"] / (variance + FOCUS_EPS)).astype(
                        np.float32)
                    metrics["contrast"] = np.sqrt(variance).astype(np.float32)
                metrics["bright_compact"] = reduced["bright_compact"]
                gy0, gx0 = y0 // s, x0 // s
                for key, value in metrics.items():
                    cy, cx = value.shape
                    channel_maps[key][gy0:gy0 + cy, gx0:gx0 + cx] = value
                done += 1
                if progress is not None:
                    progress(done=done, total=total, message=f"scanned {name}")
            for key, value in channel_maps.items():
                maps[f"{name}::{key}"] = value
            illumination = derive(maps, name, tissue_fraction)
            overview = _overview_numbers(plane_ov, on, ceiling)
            flags = list(overview["flags"])
            if illumination["r2"] is not None and illumination["r2"] > 0.4 \
                    and (illumination["range_log"] or 0) > 0.4:
                flags.append("illumination_gradient")
            channels_meta.append({
                "name": name, "key": keys[name], "index": index,
                "nuclear": name == nuclear, "cycle": cycles["of_channel"].get(name),
                "summary": {**overview, "illumination": {k: illumination[k] for k in (
                    "r2", "range_log")}, "seams": illumination["seams"],
                    "focus_rel_p10": _finite_percentile(maps[f"{name}::focus_rel"],
                                                        tissue_fraction >= 0.25, 10),
                    "bright_compact_fraction": _finite_mean(
                        maps[f"{name}::bright_compact"], tissue_fraction >= 0.25)},
                "flags": sorted(set(flags))})
        registration = cross_cycle(overviews, cycles, tissue["mask"], overview_factor,
                                   pixel_um, sigma_px or 3.0) \
            if not brightfield else {"available": False}
    for entry in registration.get("cycles") or []:
        lost = entry.pop("lost_mask")
        maps[f"::tissue_loss:c{entry['cycle']}"] = _grid_from_overview(lost, grid, ov_w, ov_h)
    tissue_px = float(tissue["mask"].mean()) * grid["image_size"][0] * grid["image_size"][1]
    meta = {
        "version": schemas.SCAN_VERSION, "project": project, "fingerprint": fp,
        "identity": context["identity"], "grid": grid, "overview_level": overview_level,
        "overview_shape": [int(ov_h), int(ov_w)], "overview_factor": overview_factor,
        "pixel_um": pixel_um, "brightfield": brightfield, "nuclear": nuclear,
        "channels": channels_meta, "cycles": cycles, "cross_cycle": registration,
        "tissue": {"method": tissue["method"], "channel": tissue["channel"],
                   "area_px": tissue_px,
                   "area_um2": tissue_px * pixel_um * pixel_um if pixel_um else None,
                   "fraction_of_image": float(tissue["mask"].mean())},
        "params": context["params"], "tophat_px": tophat_px,
    }
    # Kept for the tissue denominator at the overview level (union areas).
    maps["::tissue_overview"] = tissue["mask"].astype(np.uint8)
    return ScanResult(meta, maps)


def _grid_from_overview(mask, grid, ov_w, ov_h):
    import cv2

    ny, nx = grid["shape"]
    s = grid["cell_full_px"]
    full_w, full_h = grid["image_size"]
    cover_w = int(round(nx * s * ov_w / full_w)) or 1
    cover_h = int(round(ny * s * ov_h / full_h)) or 1
    padded = np.zeros((max(cover_h, ov_h), max(cover_w, ov_w)), dtype=np.float32)
    padded[:ov_h, :ov_w] = mask.astype(np.float32)
    return cv2.resize(padded[:cover_h, :cover_w], (nx, ny), interpolation=cv2.INTER_AREA)


def _filtered(plane, tophat_px, compact_threshold):
    from scipy import ndimage

    logged = np.log1p(np.maximum(plane, 0)).astype(np.float32)
    lap = ndimage.laplace(logged)
    out = {"lap2": (lap * lap).astype(np.float32), "log": logged,
           "log2": (logged * logged).astype(np.float32)}
    if tophat_px >= 1:
        opened = ndimage.grey_opening(plane, footprint=_disk(tophat_px))
        out["bright_compact"] = ((plane - opened) > compact_threshold).astype(np.float32)
    else:
        out["bright_compact"] = np.zeros_like(plane, dtype=np.float32)
    return out


def _finite_mean(values, mask):
    data = values[mask & np.isfinite(values)]
    return float(data.mean()) if data.size else None


def _finite_percentile(values, mask, q):
    data = values[mask & np.isfinite(values)]
    return float(np.percentile(data, q)) if data.size else None


def _overview_numbers(plane, on, ceiling):
    """A channel's global numbers from its overview (the `image_qc` rules)."""
    t = {"saturated": 0.01, "empty_mad": 3.0, "near_zero": 0.5}
    values = plane[on] if on is not None and on.any() else plane.ravel()
    zero_fraction = float((plane == 0).mean()) if plane.size else 1.0
    saturation = float((values >= SATURATION_OF_CEILING * ceiling).mean()) \
        if ceiling and values.size else 0.0
    p50, p999 = (np.percentile(values, [50, 99.9]) if values.size else (0.0, 0.0))
    mad = float(np.median(np.abs(values - p50))) if values.size else 0.0
    off = float(np.median(plane[~on])) if on is not None and (~on).sum() >= 100 else None
    flags = []
    if saturation > t["saturated"]:
        flags.append("saturated")
    ratio = float(p50) / max(off, 1.0) if off is not None else None
    decades = float(np.log10((p999 + 1) / (p50 + 1)))
    if p999 <= p50 + t["empty_mad"] * max(mad, 1e-6) or (
            ratio is not None and ratio < EMPTY_TISSUE_RATIO and decades < EMPTY_DECADES):
        flags.append("empty_channel")
    if zero_fraction > t["near_zero"]:
        flags.append("near_zero_plane")
    return {"p50_tissue": float(p50), "p999_tissue": float(p999), "p50_off_tissue": off,
            "tissue_ratio": float(p50) / max(off, 1.0) if off is not None else None,
            "dynamic_range_decades": float(np.log10((p999 + 1) / (p50 + 1))),
            "saturation_fraction": saturation, "zero_fraction": zero_fraction,
            "ceiling": ceiling, "flags": flags}
