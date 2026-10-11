"""A marker's cells and their nearest bright neighbour: what the cell model selects with.

The cell model (`cellcall.py`) reads cells near a gate and calibrates its
pixel gate on isolated ones -- cells no strong positive touches. This module
gives it, row-aligned and cheaply, each cell's value in the fit space,
whether it can be read at all, and how far its nearest strong positive sits
(one neighbour walk per marker, `kernels.neighbour_values`), plus the image's
facts (blank, brightfield, levels, the nuclear channel) and the identities
of the mask and the QC calls a stored run is keyed by.
"""

from __future__ import annotations

import collections
import threading

import numpy as np

from plexora.plugins.gating.server.autogate import cells as cellmod
from plexora.plugins.gating.server.autogate import profile as profmod

#: Bumped when what is computed here changes meaning (part of cache keys).
VERSION = "2"

#: [cal] a "strong" positive: at the positive population's centre (the fit's
#: pools), else the column's `strong_percentile`; the per-marker arrays kept
#: for this many markers (N-sized, so never on the handle set's cache).
NEIGHBOURHOOD = {"strong_percentile": 90.0, "cache_markers": 2}


def image_facts(session, ds) -> dict:
    """{blank, brightfield, levels, nuclear}: one reader open per image, cached."""
    def compute():
        image = ds.project.image
        if getattr(image, "is_blank", False) or session is None:
            return {"blank": True, "brightfield": False, "levels": 0, "nuclear": None}
        from plexora.agent.presets import nuclear_channel
        from plexora.server.utils import source_image

        try:
            with source_image.SHELF.reader(session.image_data(ds.name)) as source:
                names = [c.get("fullname") or c.get("name") for c in image.real_channels]
                return {"blank": False, "brightfield": bool(source.is_brightfield),
                        "levels": int(source.levels), "nuclear": nuclear_channel(names)}
        except Exception as exc:  # an unreadable image: nothing to read crops from
            return {"blank": True, "brightfield": False, "levels": 0, "nuclear": None,
                    "error": str(exc)[:200]}

    return ds.cached(("autogate.neighbourhood.image", VERSION), compute)


def mask_identity(record):
    """What identifies a project's segmentation mask (part of cache keys)."""
    seg = record.segmentation
    return (seg.derived, seg.source_key, int(getattr(seg, "scale", 1) or 1),
            getattr(seg, "mode", None))


def qc_fingerprint(ds) -> str:
    from plexora.agent import cell_exclusions

    return cell_exclusions.fingerprint(cell_exclusions.current(ds))


def radius_px(ds, pixel_um=None) -> float:
    """The neighbourhood radius: ~3 cell diameters (`cells.NEIGHBOUR_RADIUS_*`)."""
    c = cellmod.cells(ds)
    if pixel_um:
        return float(cellmod.NEIGHBOUR_RADIUS_UM / pixel_um)
    if c.pixel_um:
        return float(cellmod.NEIGHBOUR_RADIUS_UM / c.pixel_um)
    return float(cellmod.NEIGHBOUR_RADIUS_PX)


class Neighbourhood:
    """Row-aligned, gate-independent: `vf` (values in the fit space, NaN
    where QC left a cell out or it has none), `eligible`, `nearest_strong`
    (px to the nearest strong positive within the radius, inf beyond),
    `adjacent_px` (closer than this is touching)."""

    __slots__ = ("vf", "eligible", "nearest_strong", "adjacent_px")


_CACHE = collections.OrderedDict()
_LOCK = threading.Lock()


def _strong(ds, marker, col) -> float:
    fit = profmod.fit_for(ds, marker)
    if fit is not None and len(fit.get("sds") or ()) >= 2:
        return float(profmod.pools(fit)["mu_pos"])
    if not col.n_finite:
        return float("inf")
    return float(np.percentile(col.fit, NEIGHBOURHOOD["strong_percentile"]))


def of(ds, marker, *, pixel_um=None) -> Neighbourhood:
    """The marker's `Neighbourhood`, kept for the last few markers."""
    from plexora.plugins.gating.server.autogate import bivariate, kernels

    radius = radius_px(ds, pixel_um)
    token = ds.cached(("autogate.neighbourhood.token",), object)
    key = (token, marker, VERSION, profmod.fingerprint(ds), qc_fingerprint(ds), round(radius, 3))
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
    c = cellmod.cells(ds)
    col = profmod.column(ds, marker)
    v = cellmod.values(ds, marker)
    vf = col.to_fit(v).astype(np.float64)
    vf[~np.isfinite(v)] = np.nan
    eligible = c.valid & np.isfinite(vf)
    diameter = bivariate._cell_diameter(c)
    if pixel_um and c.area is None:
        # No areas: a typical cell in the session's own pixel size.
        diameter = 10.0 / float(pixel_um)
    out = Neighbourhood()
    out.vf, out.eligible = vf, eligible
    out.adjacent_px = float(bivariate.THRESHOLDS["adjacent_diameters"] * diameter)
    query = np.flatnonzero(eligible)
    strong = eligible & (vf >= _strong(ds, marker, col))
    nb = kernels.neighbour_values(cellmod.grid(ds, radius), np.where(eligible, vf, np.nan),
                                  strong, query=query, radius=radius)
    out.nearest_strong = np.full(c.n, np.inf)
    out.nearest_strong[query] = nb["nearest_flagged"]
    with _LOCK:
        _CACHE[key] = out
        while len(_CACHE) > NEIGHBOURHOOD["cache_markers"]:
            _CACHE.popitem(last=False)
    return out


def clear_cache():
    with _LOCK:
        _CACHE.clear()
