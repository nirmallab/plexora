"""What one channel's overview says before any cell is looked at.

The coarsest pyramid level with at least a megapixel is read once per channel
(a few hundred kilobytes) and reduced to the numbers that catch an image-level
failure: pixels at the dtype's ceiling (saturation), an empty plane, a channel
with no signal above its own background, a background that drifts across the
slide (illumination), and how much brighter tissue is than glass.

Pooling biases a coarse level's saturation low -- a clipped cell averaged with
its neighbours is no longer at the ceiling -- so a small saturated fraction
here is a floor, not an estimate; the cell table's pileup check
(`autogate.profile`) is the other half.
"""

from __future__ import annotations

import numpy as np

#: [cal]
THRESHOLDS = {"saturated": 0.01, "gradient_r2": 0.4, "gradient_range_log": 0.4,
              "empty_mad": 3.0, "near_zero": 0.5, "min_tissue_pixels": 500}

#: The overview is at least this many pixels (when the image has them).
MIN_OVERVIEW_PIXELS = 1_000_000


def overview_level(source, min_pixels=MIN_OVERVIEW_PIXELS, max_pixels=16_000_000):
    """The coarsest level with at least `min_pixels`, capped at `max_pixels`."""
    best = 0
    for level in range(max(1, source.levels)):
        height, width = source.level_shape(level)
        if height * width >= min_pixels:
            best = level
        else:
            break
    while best < source.levels - 1:
        height, width = source.level_shape(best)
        if height * width <= max_pixels:
            break
        best += 1
    return best


def read_overview(source, channel_key, level=None):
    """(plane float32, level, dtype max)."""
    from plexora.agent.errors import AgentError

    index = source.channel_index(channel_key)
    if index is None:
        raise AgentError("invalid_input", f"{channel_key!r} is not a channel of this image")
    level = overview_level(source) if level is None else level
    height, width = source.level_shape(level)
    plane, _clipped = source.read(index, level, (0, 0, width, height))
    plane = np.asarray(plane)
    if plane.dtype.kind in "ui":
        ceiling = float(np.iinfo(plane.dtype).max)
    else:
        ceiling = float(np.nanmax(plane)) if plane.size else 1.0
    return plane.astype(np.float32, copy=False), level, ceiling


def tissue_mask(nuclear):
    """Pixels on tissue: the nuclear overview above its Otsu threshold, dilated
    a little so cytoplasm between nuclei counts. None when it cannot be told."""
    if nuclear is None or nuclear.size < 100:
        return None
    logged = np.log1p(np.maximum(nuclear, 0))
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
    mask = logged > centres[int(np.argmax(between))]
    from scipy.ndimage import binary_dilation

    return binary_dilation(mask, iterations=2)


def _surface(plane_log, mask, size=128):
    """(r2, range) of a quadratic surface fitted to the dimmer half of tissue
    pixels on a ~size x size grid -- the background's drift."""
    height, width = plane_log.shape
    step = max(1, int(max(height, width) / size))
    sub = plane_log[::step, ::step]
    keep = mask[::step, ::step] if mask is not None else np.ones_like(sub, dtype=bool)
    if keep.sum() < 50:
        return None, None
    cutoff = np.median(sub[keep])
    keep = keep & (sub <= cutoff)
    ys, xs = np.nonzero(keep)
    if ys.size < 50:
        return None, None
    u = xs / max(1, sub.shape[1] - 1) * 2 - 1
    v = ys / max(1, sub.shape[0] - 1) * 2 - 1
    design = np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=1)
    y = sub[keep]
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    fitted = design @ coef
    total = ((y - y.mean()) ** 2).sum()
    r2 = float(1 - ((y - fitted) ** 2).sum() / total) if total > 0 else 0.0
    return r2, float(fitted.max() - fitted.min())


def overview_qc(source, channel_key, nuclear_key=None, *, level=None) -> dict:
    """The overview block of a marker's QC (JSON-safe)."""
    t = THRESHOLDS
    plane, level, ceiling = read_overview(source, channel_key, level)
    nuclear = None
    if nuclear_key and nuclear_key != channel_key:
        nuclear, _l, _c = read_overview(source, nuclear_key, level)
    mask = tissue_mask(nuclear)
    on = mask if mask is not None and mask.sum() >= t["min_tissue_pixels"] else None
    values = plane[on] if on is not None else plane.ravel()
    flags = []
    zero_fraction = float((plane == 0).mean()) if plane.size else 1.0
    saturation = float((values >= 0.98 * ceiling).mean()) if values.size else 0.0
    p50, p999 = (np.percentile(values, [50, 99.9]) if values.size else (0.0, 0.0))
    mad = float(np.median(np.abs(values - p50))) if values.size else 0.0
    off = None
    if on is not None and (~on).sum() >= t["min_tissue_pixels"]:
        off = float(np.median(plane[~on]))
    tissue_ratio = (float(p50) / max(off, 1.0)) if off is not None else None
    r2, spread = _surface(np.log1p(np.maximum(plane, 0)), on)
    if saturation > t["saturated"]:
        flags.append("saturated")
    if r2 is not None and r2 > t["gradient_r2"] and spread > t["gradient_range_log"]:
        flags.append("illumination_gradient")
    if p999 <= p50 + t["empty_mad"] * max(mad, 1e-6):
        flags.append("empty_channel")
    if zero_fraction > t["near_zero"]:
        flags.append("near_zero_plane")
    return {
        "channel": channel_key, "level": int(level), "shape": list(plane.shape),
        "ceiling": ceiling, "saturation_fraction": saturation,
        "zero_fraction": zero_fraction, "p50_tissue": float(p50), "p999_tissue": float(p999),
        "p50_off_tissue": off, "tissue_ratio": tissue_ratio,
        "tissue_fraction": float(on.mean()) if on is not None else None,
        "dynamic_range_decades": float(np.log10((p999 + 1) / (p50 + 1))),
        "illumination": {"r2": r2, "range_log": spread},
        "flags": flags,
    }
