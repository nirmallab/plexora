"""Where a cell's signal may come from: crowding, local background, bright neighbours.

Automatic gating asks whether a threshold is right. A cell can read above it
for reasons no threshold fixes -- a bright neighbour's fluorescence in its
mask (dense tissue), a locally raised background (a fold's rim, a bright
stromal patch), a mask that took part of another cell -- and a cell can read
below it while carrying a real, dim stain. This module measures, for every
cell and cheaply, the circumstances under which each of those becomes likely,
so the looks can be shown like-for-like cells and the decision rules can tell
an error that is everywhere (the threshold) from one concentrated in one
environment (spill-over, background). It decides nothing itself:

- the evidence mode (`detect_mode`): A -- a mask that matches the table, and
  the image; B -- the image and the table's centroids only (no mask, an
  outline mask, or one that does not match); C -- expression only (no image,
  a brightfield image, no channel for the marker);
- density (`density_class`): sparse / intermediate / dense terciles of the
  physical neighbour count within ~3 cell diameters, over the cells QC kept;
- local background and noise (`background_map`): per ~50 µm tile of a coarse
  pyramid level, the dimmer half of the tissue's pixels; each cell reads its
  tile, and an environment's SNR is the positive population's level over it;
- bright neighbours (`per_cell`): the share of a cell's neighbours that are
  strong positives, the brightest of them, how close the nearest strong one
  sits; at a gate, `spill` says whether a cell near the gate has a bright
  neighbour touching it (`schemas.SPILL_STATES`);
- the targeted pass (`targeted_from_crops`): for the few cells a look shows,
  where the stain lies in and around the cell -- at the interface with the
  bright neighbour, or around the cell's own outline.

The safeguards, as code: proximity to a bright cell is a circumstance, never a
verdict (`spill` stays `candidate` or `untested` until the pixels support
it); a weak cell is not a negative one; a membrane marker's ring is its
compartment, and an asymmetric one is not contamination unless the side away
from the neighbour is at background; a Voronoi region is not a boundary
(mode B claims nothing about membranes, and says its numbers are estimates);
a tile whose cells are mostly positive is not background; and nothing here
changes a value, a gate or a call. Corrected values (`corrected_values`) are
an empty, separate seam.

Whole-column work is vectorised: one neighbour walk per marker
(`kernels.neighbour_values`), a sort per pyramid tile block; the targeted
pass reads only the crops a collage already reads
(`collage.render_collage(crop_hook=...)`).
"""

from __future__ import annotations

import base64
import collections
import hashlib
import math
import threading

import numpy as np

from plexora.agent import gate_rule
from plexora.plugins.gating.server.autogate import cells as cellmod
from plexora.plugins.gating.server.autogate import profile as profmod
from plexora.plugins.gating.server.autogate import schemas

SPATIAL_VERSION = schemas.SPATIAL_VERSION
TARGETED_VERSION = schemas.TARGETED_VERSION


def _adjacent_diameters():
    from plexora.plugins.gating.server.autogate import bivariate

    return float(bivariate.THRESHOLDS["adjacent_diameters"])


#: [cal] the feature layer's cut-points: first guesses on one image
#: (lsp11385_2) and the synthetic scenes; validation pending. The neighbourhood
#: radius is `cells.NEIGHBOUR_RADIUS_*` and "touching" is
#: `bivariate.THRESHOLDS["adjacent_diameters"]` (reused, not restated).
SPATIAL = {
    # density strata: terciles, and how many cells they need; the dense /
    # sparse split of the environments leaves at least this share on a side
    "density_percentiles": (100.0 / 3.0, 200.0 / 3.0), "min_strata_cells": 300,
    "dense_min_share": 0.1,
    # a positive fraction per stratum is reported only over this many cells
    "min_stratum_pf_cells": 50,
    # a "strong" positive: at the positive population's centre (the fit's
    # pools), else the column's 90th percentile
    "strong_percentile": 90.0,
    # a bright neighbour is brighter than the cell by this many band sds
    "contrast_min_sd": 1.0,
    # the background map: tiles of ~50 µm (128 px uncalibrated), read at the
    # coarsest level that keeps a tile >= 10 px, the plane <= 16 MP
    "tile_um": 50.0, "tile_px": 128.0, "tile_target_level_px": 10.0,
    "tile_min_level_px": 6.0, "map_max_pixels": 16_000_000,
    # per tile: background = the median of the dimmer half of tissue pixels
    # (its q25), noise = the dimmer half's IQR / 1.349
    "bg_quantile": 0.25, "min_tile_tissue_px": 16,
    # a tile most of whose cells the gate calls positive is not background
    "bg_unreliable_positive_share": 0.5, "bg_min_tile_cells": 5,
    # table and pixels comparable: clear negatives' median over the map's
    "unit_ratio": (0.25, 4.0),
    # low SNR: under half the image's median environment SNR, and never on a
    # flat background
    "snr_low_factor": 0.5, "bg_flat_log": 0.15, "bg_smooth_r2": 0.4,
    # the targeted pass
    "rim_px": 2, "interface_ratio": 1.5, "half_disc_ratio": 1.3, "away_max_sd": 1.0,
    "max_targeted": 64,
    # mode detection: a mask that misses cells or sits off the centroids is
    # no boundary
    "mask_probe_cells": 16, "mask_offset_px": 5.0, "mask_found_share": 0.5,
    # per-cell arrays kept for this many markers: ~75 bytes a cell each, and a
    # session works one marker at a time (a recompute is ~1.4 s per million
    # cells)
    "per_cell_cache_markers": 2, "isolated_query_max": 50_000,
    # risk: a reason to look like-for-like, never a verdict; each term an
    # excess of the cells at the gate over clear positives (`schemas.
    # SPATIAL_RULES`), the background term only where the map varies
    "risk": {"density_excess": 0.15, "spill_excess": 0.15, "snr_excess": 0.15,
             "bg_hetero_log": 0.3,
             "w_density": 0.35, "w_spill": 0.35, "w_background": 0.30,
             "moderate": 0.3, "high": 0.6},
}

#: How each mode is said where a person reads it.
MODE_WORDS = {"A": "mask and image", "B": "centroids and image", "C": "expression only"}

#: Integer codes of the per-cell classes (index into the vocabulary).
SNR_LOW, SNR_HIGH, SNR_UNKNOWN = (schemas.SNR_CLASSES.index(w)
                                  for w in ("low", "high", "unknown"))
SPILL = {word: index for index, word in enumerate(schemas.SPILL_STATES)}


# -- evidence mode ------------------------------------------------------------------


def _channel_names(ds):
    return [c.get("fullname") or c.get("name") for c in ds.project.image.real_channels]


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
                return {"blank": False, "brightfield": bool(source.is_brightfield),
                        "levels": int(source.levels),
                        "nuclear": nuclear_channel(_channel_names(ds))}
        except Exception as exc:  # an unreadable image is expression-only evidence
            return {"blank": True, "brightfield": False, "levels": 0, "nuclear": None,
                    "error": str(exc)[:200]}

    return ds.cached(("autogate.spatial.image", SPATIAL_VERSION), compute)


def _mask_identity(record):
    seg = record.segmentation
    return (seg.derived, seg.source_key, int(getattr(seg, "scale", 1) or 1),
            getattr(seg, "mode", None))


def mask_probe(session, ds, pixel_um=None, *, seed=0) -> dict:
    """Whether the mask's labels are the table's cells: `mask_probe_cells`
    seeded cells read at level 0 (mask only), the share whose label is found
    in its crop and the median distance of the label's centroid from the
    table's. Cached by the mask's identity."""
    from plexora.agent.evidence import crops as cropmod

    record = ds.project

    def compute():
        c = cellmod.cells(ds)
        rows = cellmod.seeded_subset(np.flatnonzero(cellmod.eligible(ds)),
                                     int(SPATIAL["mask_probe_cells"]), seed + 31)
        if not rows.size:
            return {"usable": False, "reason": "no cells to probe", "n": 0}
        radius = (cropmod.CELL_DIAMETER_UM / 2.0 / pixel_um) if pixel_um else \
            cropmod.CELL_DIAMETER_UM / 2.0 / cropmod.DEFAULT_UM_PER_PX
        side = int(math.ceil(4 * radius + 4))
        cells = [{"cell_id": int(c.ids[r]), "x": float(c.xs[r]), "y": float(c.ys[r])}
                 for r in rows]
        try:
            crops, _info = cropmod.read_cell_crops(session, record, cells, channels={},
                                                   crop_px=side, tile_px=side, mask=True,
                                                   level=0)
        except Exception as exc:
            return {"usable": False, "reason": f"the mask could not be read: {exc}"[:200],
                    "n": int(rows.size)}
        found, offsets = 0, []
        for crop in crops.values():
            stats = crop.mask_stats or {}
            if stats.get("area_level_px"):
                found += 1
                if stats.get("centroid_offset_px") is not None:
                    offsets.append(float(stats["centroid_offset_px"]))
        share = found / max(1, len(crops))
        offset = float(np.median(offsets)) if offsets else None
        usable = share >= SPATIAL["mask_found_share"] and offset is not None \
            and offset <= SPATIAL["mask_offset_px"]
        reason = None
        if not usable:
            reason = (f"only {found} of {len(crops)} probed cells have their label under "
                      "their centroid" if share < SPATIAL["mask_found_share"] else
                      f"the mask's labels sit {offset:.1f} px from the table's centroids"
                      if offset is not None else "no probed label could be measured")
        return {"usable": bool(usable), "reason": reason, "n": len(crops),
                "found_share": share, "median_offset_px": offset}

    return ds.cached(("autogate.spatial.mask_probe", SPATIAL_VERSION,
                      _mask_identity(record), seed), compute)


def detect_mode(session, ds, marker=None, pixel=None, *, seed=0) -> dict:
    """The evidence mode for `marker` (or the image, without one): A, B or C,
    with why, the pixel size it measures in, and what the mask and image are.
    `pixel` is the session's `{value, source}` (`Engine.pixel_for`); without
    one the image's own calibration, else pixel units (`pixel_source:
    "none"` -- never a reason to downgrade)."""
    from plexora.agent import render
    from plexora.plugins.gating.server.autogate import views

    record = ds.project
    pixel_um, pixel_source = None, "none"
    if pixel and pixel.get("value"):
        pixel_um, pixel_source = float(pixel["value"]), pixel.get("source") or "metadata"
    elif cellmod.pixel_um(ds):
        pixel_um, pixel_source = float(cellmod.pixel_um(ds)), "metadata"
    facts = image_facts(session, ds)
    out = {"version": SPATIAL_VERSION, "pixel_um": pixel_um, "pixel_source": pixel_source,
           "image": {k: facts.get(k) for k in ("blank", "brightfield", "levels", "nuclear")},
           "mask": {"status": "none", "usable": False}}
    if facts.get("blank"):
        return {**out, "mode": "C", "image_mode": "C", "why": "no image"}
    if facts.get("brightfield"):
        return {**out, "mode": "C", "image_mode": "C", "why": "a brightfield image"}
    seg = record.segmentation
    try:
        provider, status, reason, _locator = render._mask_for(record)
    except Exception as exc:  # no mask is mode B, never a failure
        provider, status, reason = None, "unavailable", str(exc)[:200]
    mask = {"status": status, "reason": reason, "mode": getattr(seg, "mode", None),
            "scale": int(getattr(seg, "scale", 1) or 1),
            "extra_levels": int(getattr(seg, "extra_levels", 0) or 0), "usable": False}
    image_mode, why = "A", "a mask that matches the table, and the image"
    if provider is None:
        image_mode, why = "B", reason or "no segmentation mask"
    elif mask["mode"] == "outlines":
        image_mode, why = "B", "the mask holds outlines, not filled cells"
    else:
        probe = mask_probe(session, ds, pixel_um, seed=seed)
        mask["probe"] = probe
        if not probe.get("usable"):
            image_mode, why = "B", probe.get("reason") or "the mask does not match the table"
        else:
            mask["usable"] = True
    out.update(mask=mask, image_mode=image_mode, mode=image_mode, why=why)
    if marker is not None:
        channel = views.image_channel(ds, marker)
        out["image"]["channel"] = channel
        if channel is None:
            out.update(mode="C", why="the marker has no image channel")
    return out


# -- density ----------------------------------------------------------------------


def _qc_fp(ds):
    from plexora.agent import cell_exclusions

    return cell_exclusions.fingerprint(cell_exclusions.current(ds))


def radius_for(ds, pixel_um=None):
    """(radius px, basis): ~3 cell diameters (`cells.NEIGHBOUR_RADIUS_*`)."""
    c = cellmod.cells(ds)
    if pixel_um:
        return float(cellmod.NEIGHBOUR_RADIUS_UM / pixel_um), "um"
    if c.pixel_um:
        return float(cellmod.NEIGHBOUR_RADIUS_UM / c.pixel_um), "um"
    return float(cellmod.NEIGHBOUR_RADIUS_PX), "px"


def density_class(ds, radius_px=None):
    """(class per row -- 0 sparse, 1 intermediate, 2 dense, -1 not eligible
    -- , info). Terciles of the PHYSICAL neighbour count
    (`cells.neighbour_density`: every valid cell crowds, QC or not) taken over
    the cells QC kept, so the strata describe the cells the gate is estimated
    on. Boundaries are actual counts (`method="lower"`), ties to the lower
    class (`searchsorted(side="left")`): deterministic. Fewer than
    `min_strata_cells` eligible cells: all intermediate, and said."""
    c = cellmod.cells(ds)
    radius = radius_for(ds)[0] if radius_px is None else float(radius_px)

    def compute():
        counts = cellmod.neighbour_density(ds, radius)
        rows = np.flatnonzero(cellmod.eligible(ds))
        classes = np.full(c.n, -1, dtype=np.int8)
        info = {"radius_px": radius, "n_eligible": int(rows.size)}
        if rows.size < SPATIAL["min_strata_cells"]:
            classes[rows] = 1
            info.update(boundaries=None, median=None, dense_from=None, dense_rule=None,
                        reason=f"fewer than {SPATIAL['min_strata_cells']} eligible cells")
            return classes, counts, info
        lo, hi = np.percentile(counts[rows], SPATIAL["density_percentiles"], method="lower")
        median = float(np.percentile(counts[rows], 50.0, method="lower"))
        classes[rows] = np.searchsorted(np.array([lo, hi], dtype=np.float64),
                                        counts[rows].astype(np.float64), side="left")
        split, rule = dense_split(counts[rows])
        info.update(boundaries=[float(lo), float(hi)], median=median, dense_from=split,
                    dense_rule=rule, collapsed=bool(lo == hi), reason=None,
                    counts={name: int((classes[rows] == i).sum())
                            for i, name in enumerate(schemas.DENSITY_CLASSES)})
        return classes, counts, info

    return ds.cached(("autogate.spatial.density", SPATIAL_VERSION, round(radius, 3),
                      _qc_fp(ds)), compute)


def dense_split(counts):
    """(count, rule): the neighbour count above which a cell's surroundings
    are `dense` for the environments a look is spread over -- the Otsu split
    of the counts (tissue usually holds crowded and open regions), or the
    median when that split leaves under `dense_min_share` on a side."""
    counts = np.asarray(counts, dtype=np.int64)
    median = float(np.percentile(counts, 50.0, method="lower"))
    hist = np.bincount(np.maximum(counts, 0))
    if hist.size < 3:
        return median, "median"
    p = hist / hist.sum()
    levels = np.arange(hist.size, dtype=np.float64)
    w0 = np.cumsum(p)
    mu = np.cumsum(p * levels)
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu[-1] * w0 - mu) ** 2 / (w0 * (1 - w0))
    between[~np.isfinite(between)] = -1.0
    k = int(np.argmax(between))
    dense = 1.0 - float(w0[k])
    low = SPATIAL["dense_min_share"]
    if between[k] <= 0 or not low <= dense <= 1.0 - low:
        return median, "median"
    return float(k), "otsu"


# -- local background ----------------------------------------------------------------


def _map_level(source, tile_full_px):
    """(level, tile side in level px): the coarsest level whose tile is still
    `tile_target_level_px`, but never a plane over `map_max_pixels` -- then
    the finest level under it, the tile grown to `tile_min_level_px` if it
    must."""
    levels = max(1, int(source.levels))
    target = float(SPATIAL["tile_target_level_px"])
    l_tile = max(0, int(math.floor(math.log2(max(tile_full_px / target, 1.0)))))
    l_pixels = levels - 1
    for level in range(levels):
        height, width = source.level_shape(level)
        if height * width <= SPATIAL["map_max_pixels"]:
            l_pixels = level
            break
    level = min(max(l_tile, l_pixels), levels - 1)
    tile = max(float(SPATIAL["tile_min_level_px"]), tile_full_px / 2 ** level)
    return level, int(round(tile))


def _tile_quantiles(plane, tissue, t, quantiles):
    """{q: (ny, nx) float32} of the tissue pixels per t x t tile, and the
    tissue pixel count per tile -- one sort of the padded plane's blocks."""
    height, width = plane.shape
    ny, nx = -(-height // t), -(-width // t)
    padded = np.full((ny * t, nx * t), np.nan, dtype=np.float32)
    padded[:height, :width] = np.where(tissue, plane, np.nan) if tissue is not None else plane
    blocks = padded.reshape(ny, t, nx, t).transpose(0, 2, 1, 3).reshape(ny, nx, t * t)
    del padded
    blocks.sort(axis=2)                       # NaN sorts last
    n = np.isfinite(blocks).sum(axis=2)
    out = {}
    for q in quantiles:
        index = np.clip(np.floor(q * (n - 1)).astype(np.int64), 0, t * t - 1)
        out[q] = np.take_along_axis(blocks, index[..., None], axis=2)[..., 0]
    return out, n


def background_map(session, ds, channel) -> dict | None:
    """The marker channel's local background and noise per tissue tile, or
    None (no image, brightfield, unreadable). Cached per channel.

    {bg, noise, filled (ny, nx), tile_px (full-resolution px per tile),
    level, tissue ("nuclear" | "all pixels"), info}: background is the dimmer
    half's median (`bg_quantile`), noise its IQR / 1.349; a tile with fewer
    than `min_tile_tissue_px` tissue pixels takes its nearest measured tile's
    values (`filled`). Raw pixel units."""
    from plexora.agent.evidence import image_qc
    from plexora.agent.render import resolve_channel
    from plexora.server.utils import source_image

    facts = image_facts(session, ds)
    if facts.get("blank") or facts.get("brightfield") or channel is None:
        return None
    pixel_um = cellmod.pixel_um(ds)
    tile_full = (SPATIAL["tile_um"] / pixel_um) if pixel_um else SPATIAL["tile_px"]
    records = list(ds.project.image.real_channels)
    _i, found = resolve_channel(channel, records)
    key = source_image.channel_key(found)
    nuclear = facts.get("nuclear")

    def compute():
        with source_image.SHELF.reader(session.image_data(ds.name)) as source:
            level, t = _map_level(source, tile_full)
            plane, level, _ceiling = image_qc.read_overview(source, key, level)
            tissue = None
            if nuclear:
                tissue = _tissue(session, ds, source, nuclear, level)
        how = "nuclear" if tissue is not None and tissue.sum() >= 100 else "all pixels"
        q, n = _tile_quantiles(plane, tissue if how == "nuclear" else None, t,
                               (0.125, SPATIAL["bg_quantile"], 0.375))
        ok = n >= SPATIAL["min_tile_tissue_px"]
        if not ok.any():
            return None
        bg = q[SPATIAL["bg_quantile"]].astype(np.float32)
        noise = ((q[0.375] - q[0.125]) / 1.349).astype(np.float32)
        floor = max(1e-6, 1e-3 * float(np.nanmedian(bg[ok])))
        noise = np.maximum(noise, floor)
        filled = ~ok
        if filled.any():
            from scipy.ndimage import distance_transform_edt

            index = distance_transform_edt(filled, return_distances=False,
                                           return_indices=True)
            bg = bg[index[0], index[1]]
            noise = noise[index[0], index[1]]
        info = structure_of(bg, ok)
        return {"bg": bg, "noise": noise, "filled": filled, "level": int(level),
                "tile_px": float(t * 2 ** level), "tile_level_px": int(t),
                "tile_um": (t * 2 ** level * pixel_um) if pixel_um else None,
                "tissue": how, "channel": key, "info": info}

    return ds.cached(("autogate.spatial.bgmap", SPATIAL_VERSION, key, round(tile_full, 3)),
                     compute)


def _tissue(session, ds, source, nuclear, level):
    """The tissue mask at `level` from the nuclear overview, once per image."""
    from plexora.agent.evidence import image_qc
    from plexora.agent.render import resolve_channel
    from plexora.server.utils import source_image

    def compute():
        _j, nuc = resolve_channel(nuclear, list(ds.project.image.real_channels))
        plane, _level, _c = image_qc.read_overview(source, source_image.channel_key(nuc),
                                                   level)
        return image_qc.tissue_mask(plane)

    return ds.cached(("autogate.spatial.tissue", SPATIAL_VERSION, nuclear, int(level)),
                     compute)


def structure_of(bg, measured) -> dict:
    """How the background varies: `heterogeneity_log` (p95 - p5 of log1p
    background over measured tiles), `surface_r2` (a smooth quadratic's
    share, `qc_cells.surface_fit`) and `structure`: flat, smooth or patchy."""
    from plexora.plugins.gating.server.autogate import qc_cells

    values = np.log1p(np.maximum(bg[measured].astype(np.float64), 0))
    if values.size < 4:
        return {"heterogeneity_log": None, "surface_r2": None, "structure": "flat",
                "n_tiles": int(measured.sum())}
    hetero = float(np.percentile(values, 95) - np.percentile(values, 5))
    ny, nx = bg.shape
    cy = (np.arange(ny) + 0.5) / ny * 2 - 1
    cx = (np.arange(nx) + 0.5) / nx * 2 - 1
    u, v = np.meshgrid(cx, cy)
    centres = np.stack([u[measured], v[measured]], axis=1)
    r2, _range = qc_cells.surface_fit(centres, values)
    if hetero < SPATIAL["bg_flat_log"]:
        structure = "flat"
    elif r2 is not None and r2 >= SPATIAL["bg_smooth_r2"]:
        structure = "smooth"
    else:
        structure = "patchy"
    return {"heterogeneity_log": hetero, "surface_r2": r2, "structure": structure,
            "n_tiles": int(measured.sum()),
            "filled_share": float(1 - measured.mean()) if measured.size else None}


def encode_map(bgmap) -> dict | None:
    """The map as a table operation's payload: float16 planes, base64 (a few
    hundred KB at most), with its fingerprint. Per-cell arrays never cross."""
    if bgmap is None:
        return None
    ceiling = np.float32(np.finfo(np.float16).max)

    def pack(array):
        return base64.b64encode(np.minimum(array, ceiling).astype("<f2").tobytes()).decode()

    body = {"bg": pack(bgmap["bg"]), "noise": pack(bgmap["noise"]),
            "filled": base64.b64encode(np.packbits(bgmap["filled"].ravel())).decode(),
            "shape": list(bgmap["bg"].shape), "tile_px": float(bgmap["tile_px"]),
            "level": int(bgmap["level"]), "tissue": bgmap.get("tissue"),
            "info": bgmap.get("info") or {}}
    blob = (body["bg"] + body["noise"] + body["filled"] + repr(body["shape"])
            + repr(body["tile_px"])).encode()
    body["fingerprint"] = hashlib.sha1(blob).hexdigest()[:16]
    return body


def decode_map(payload) -> dict | None:
    if not payload:
        return None
    shape = tuple(int(s) for s in payload["shape"])
    count = shape[0] * shape[1]

    def unpack(text):
        return np.frombuffer(base64.b64decode(text), dtype="<f2").astype(np.float32) \
            .reshape(shape)

    filled = np.unpackbits(np.frombuffer(base64.b64decode(payload["filled"]),
                                         dtype=np.uint8))[:count].astype(bool).reshape(shape)
    return {"bg": unpack(payload["bg"]), "noise": unpack(payload["noise"]),
            "filled": filled, "tile_px": float(payload["tile_px"]),
            "level": int(payload["level"]), "tissue": payload.get("tissue"),
            "info": payload.get("info") or {}, "fingerprint": payload.get("fingerprint")}


def map_fingerprint(bgmap) -> str:
    if bgmap is None:
        return ""
    if bgmap.get("fingerprint"):
        return bgmap["fingerprint"]
    return encode_map(bgmap)["fingerprint"]


def tile_of(bgmap, xs, ys):
    """(flat tile index per point, valid) on the map's grid."""
    ny, nx = bgmap["bg"].shape
    side = float(bgmap["tile_px"])
    ok = np.isfinite(xs) & np.isfinite(ys)
    ix = np.clip(np.floor(np.where(ok, xs, 0) / side).astype(np.int64), 0, nx - 1)
    iy = np.clip(np.floor(np.where(ok, ys, 0) / side).astype(np.int64), 0, ny - 1)
    return iy * nx + ix, ok


# -- per cell -------------------------------------------------------------------------


class PerCell:
    """Row-aligned, gate-independent features of one marker's cells."""

    __slots__ = ("n", "cells", "vf", "eligible", "density_class", "density_count", "dense_env",
                 "nn1", "nb_n", "nb_strong", "nb_max_fit", "brightest_row", "nearest_strong",
                 "local_bg", "local_noise", "tile", "tile_filled", "strong_fit", "strong_rule",
                 "sd_edge", "radius_px", "radius_basis", "adjacent_px", "cell_diameter_px",
                 "density_info", "map_fp", "map_info", "linear_ok")


_CACHE = collections.OrderedDict()
_CACHE_LOCK = threading.Lock()


def _token(ds):
    """An object that lives as long as `ds`'s cache: a per-cell cache key that
    two handle sets never share, whatever their names."""
    return ds.cached(("autogate.spatial.token",), object)


def _strong(ds, marker, col):
    """(strong level in fit units, rule): the positive population's centre,
    else the column's `strong_percentile`."""
    fit = profmod.fit_for(ds, marker)
    if fit is not None and len(fit.get("sds") or ()) >= 2:
        pools = profmod.pools(fit)
        return float(pools["mu_pos"]), "mu_pos", float(pools["sd_edge"])
    if not col.n_finite:
        return float("inf"), "none", 1.0
    q = float(np.percentile(col.fit, SPATIAL["strong_percentile"]))
    lo, hi = np.percentile(col.fit, [25, 75])
    return q, f"p{SPATIAL['strong_percentile']:g}", max(1e-9, float(hi - lo) / 1.349)


def per_cell(ds, marker, *, bgmap=None, pixel_um=None) -> PerCell:
    """The marker's gate-independent features (`PerCell`), kept for the last
    `per_cell_cache_markers` markers (they are N-sized, so never on the
    handle set's cache)."""
    from plexora.plugins.gating.server.autogate import bivariate, kernels

    radius, basis = radius_for(ds, pixel_um)
    key = (_token(ds), marker, SPATIAL_VERSION, profmod.fingerprint(ds), _qc_fp(ds),
           round(radius, 3), map_fingerprint(bgmap))
    with _CACHE_LOCK:
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
    classes, counts, dinfo = density_class(ds, radius)
    strong_fit, rule, sd_edge = _strong(ds, marker, col)
    out = PerCell()
    out.n, out.cells, out.vf, out.eligible = c.n, c, vf, eligible
    out.density_class, out.density_count, out.density_info = classes, counts, dinfo
    split = dinfo.get("dense_from")
    out.dense_env = (counts > split) if split is not None else np.zeros(c.n, dtype=bool)
    out.strong_fit, out.strong_rule, out.sd_edge = strong_fit, rule, sd_edge
    out.radius_px, out.radius_basis = radius, basis
    diameter = bivariate._cell_diameter(c)
    if pixel_um and c.area is None:
        # No areas: a typical cell in the session's own pixel size.
        diameter = 10.0 / float(pixel_um)
    out.cell_diameter_px = float(diameter)
    out.adjacent_px = float(_adjacent_diameters() * diameter)
    grid = cellmod.grid(ds, radius)
    strong = eligible & (vf >= strong_fit)
    query = np.flatnonzero(eligible)
    nb = kernels.neighbour_values(grid, np.where(eligible, vf, np.nan), strong, query=query,
                                  radius=radius)
    n = c.n
    out.nn1 = np.full(n, np.inf)
    out.nb_n = np.zeros(n, dtype=np.int32)
    out.nb_strong = np.zeros(n, dtype=np.int32)
    out.nb_max_fit = np.full(n, np.nan)
    out.brightest_row = np.full(n, -1, dtype=np.int32)
    out.nearest_strong = np.full(n, np.inf)
    out.nn1[query] = nb["nearest_any"]
    out.nb_n[query] = nb["n_valued"]
    out.nb_strong[query] = nb["n_flagged"]
    out.nb_max_fit[query] = nb["max"]
    out.brightest_row[query] = nb["argmax"]
    out.nearest_strong[query] = nb["nearest_flagged"]
    out.local_bg = np.full(n, np.nan)
    out.local_noise = np.full(n, np.nan)
    out.tile = np.full(n, -1, dtype=np.int32)
    out.tile_filled = np.zeros(n, dtype=bool)
    out.map_fp, out.map_info, out.linear_ok = map_fingerprint(bgmap), None, False
    if bgmap is not None:
        tile, ok = tile_of(bgmap, c.xs, c.ys)
        ok &= c.valid
        out.tile[ok] = tile[ok]
        out.local_bg[ok] = bgmap["bg"].ravel()[tile[ok]]
        out.local_noise[ok] = bgmap["noise"].ravel()[tile[ok]]
        out.tile_filled[ok] = bgmap["filled"].ravel()[tile[ok]]
        out.map_info = {k: bgmap.get(k) for k in ("level", "tile_px", "tissue")}
        out.linear_ok = col.linear(np.array([0.0])) is not None
    with _CACHE_LOCK:
        _CACHE[key] = out
        while len(_CACHE) > SPATIAL["per_cell_cache_markers"]:
            _CACHE.popitem(last=False)
    return out


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


class AtGate:
    """The gate-dependent half: calls, the band, spill and SNR classes."""

    __slots__ = ("g", "h", "positive", "near", "spill", "spill_suspect", "bright_neighbour",
                 "snr_class", "env_snr", "cell_snr", "snr", "unreliable")


def at_gate(ds, marker, feats, low, high=None, *, mode="A", bgmap=None) -> AtGate:
    """`feats` at a gate, O(N): which cells are near it (within two band
    halves, `sampler`'s just-below to just-above), which of those have a
    bright neighbour touching them (`spill`: `candidate` in mode C,
    `untested` with an image until the targeted pass reads the pixels), and
    each cell's SNR class."""
    from plexora.plugins.gating.server.autogate import sampler

    col = profmod.column(ds, marker)
    high = float(col.sorted32[-1]) if high is None and col.n_finite else high
    fit = profmod.fit_for(ds, marker)
    _edges, h = sampler.stratum_edges(fit, col, low)
    g = float(col.to_fit(low))
    vf = feats.vf
    elig = feats.eligible
    out = AtGate()
    out.g, out.h = g, float(h)
    out.positive = elig & gate_rule.passes(cellmod.values(ds, marker), low, high)
    out.near = elig & (vf > g - 2 * h) & (vf <= g + 2 * h)
    contrast = (feats.nb_max_fit - vf) / max(feats.sd_edge, 1e-9)
    with np.errstate(invalid="ignore"):
        out.bright_neighbour = elig & (feats.nb_max_fit >= feats.strong_fit) & \
            (contrast >= SPATIAL["contrast_min_sd"])
    out.spill_suspect = elig & (feats.nearest_strong <= feats.adjacent_px)
    candidate = out.near & out.bright_neighbour & out.spill_suspect
    spill = np.full(feats.n, SPILL["none"], dtype=np.int8)
    spill[candidate] = SPILL["candidate"] if mode == "C" else SPILL["untested"]
    out.spill = spill
    _snr(out, ds, marker, feats, col, bgmap, mode)
    return out


def _snr(out, ds, marker, feats, col, bgmap, mode):
    n = feats.n
    out.snr_class = np.full(n, SNR_UNKNOWN, dtype=np.int8)
    out.env_snr = np.full(n, np.nan)
    out.cell_snr = np.full(n, np.nan)
    out.unreliable = np.zeros(n, dtype=bool)
    out.snr = {"available": False, "reason": None}
    if mode == "C" or bgmap is None:
        out.snr["reason"] = "no image" if mode == "C" else "no background map"
        return
    if not feats.linear_ok:
        out.snr["reason"] = "the table's values are not intensities (z-scores?)"
        return
    elig = feats.eligible & (feats.tile >= 0)
    clear_negative = elig & (feats.vf <= out.g - 2 * out.h)
    measured = ~bgmap["filled"]
    map_median = float(np.median(bgmap["bg"][measured])) if measured.any() else None
    if clear_negative.sum() < 20 or not map_median:
        out.snr["reason"] = "too few clear negatives to compare units"
        return
    linear_neg = col.linear(feats.vf[clear_negative])
    ratio = float(np.median(linear_neg)) / map_median
    lo, hi = SPATIAL["unit_ratio"]
    out.snr["unit_ratio"] = ratio
    if not lo <= ratio <= hi:
        out.snr["reason"] = (f"the table's negatives read {ratio:.2g}x the image's background: "
                             "not the same units")
        return
    # A tile most of whose cells are positive measures the stain, not the
    # background (safeguard: a positive region is never read as background).
    n_tiles = bgmap["bg"].size
    tiles = feats.tile[elig]
    cells_per = np.bincount(tiles, minlength=n_tiles)
    pos_per = np.bincount(tiles, weights=out.positive[elig].astype(np.float64),
                          minlength=n_tiles)
    with np.errstate(invalid="ignore", divide="ignore"):
        share = pos_per / cells_per
    bad_tile = (cells_per >= SPATIAL["bg_min_tile_cells"]) & \
        (share > SPATIAL["bg_unreliable_positive_share"])
    out.unreliable = elig & bad_tile[np.maximum(feats.tile, 0)]
    mu_pos = col.linear(np.array([feats.strong_fit]))[0]
    noise = np.maximum(feats.local_noise, 1e-9)
    out.env_snr = np.where(elig, (mu_pos - feats.local_bg) / noise, np.nan)
    linear = np.where(feats.eligible, col.linear(np.where(feats.eligible, feats.vf, 0.0)),
                      np.nan)
    out.cell_snr = np.where(elig, (linear - feats.local_bg) / noise, np.nan)
    known = elig & ~feats.tile_filled & ~out.unreliable & np.isfinite(out.env_snr)
    structure = (bgmap.get("info") or {}).get("structure")
    out.snr.update(available=True, structure=structure,
                   unreliable_share=float(out.unreliable[elig].mean()) if elig.any() else None)
    if not known.any():
        out.snr.update(available=False, reason="no tile measured cleanly")
        return
    median = float(np.median(out.env_snr[known]))
    boundary = SPATIAL["snr_low_factor"] * median
    out.snr["boundary"] = boundary
    out.snr_class[known] = SNR_HIGH
    if structure != "flat":
        out.snr_class[known & (out.env_snr < boundary)] = SNR_LOW


def env_of(feats, gate, rows):
    """The environment of each row: `dense/low`, `sparse/high`, ... -- or
    `dense` / `sparse` when SNR is unknown for the image (mode C, no map)."""
    dense = feats.dense_env[rows]
    if not gate.snr.get("available"):
        return np.where(dense, "dense", "sparse")
    snr = gate.snr_class[rows]
    density = np.where(dense, "dense", "sparse")
    word = np.where(snr == SNR_LOW, "low", np.where(snr == SNR_HIGH, "high", "unknown"))
    return np.char.add(np.char.add(density.astype(str), "/"), word.astype(str))


def cell_record(feats, gate, row) -> dict:
    """The sampler's per-cell spatial keys (`sampler._record`)."""
    density = int(feats.density_class[row])
    snr = int(gate.snr_class[row])
    nb_n = int(feats.nb_n[row])
    nearest = float(feats.nearest_strong[row])
    out = {"density_class": schemas.DENSITY_CLASSES[density] if density >= 0 else None,
           "snr_class": schemas.SNR_CLASSES[snr],
           "env": str(env_of(feats, gate, np.array([row]))[0]),
           "strong_neighbour_frac": round(int(feats.nb_strong[row]) / nb_n, 3) if nb_n else 0.0,
           "nearest_strong_px": round(nearest, 1) if math.isfinite(nearest) else None,
           "spill": schemas.SPILL_STATES[int(gate.spill[row])]}
    brightest = int(feats.brightest_row[row])
    if brightest >= 0:
        # Where the brightest neighbour is: the direction a centroid-only
        # targeted read compares toward and away from.
        out["brightest"] = {"x": round(float(feats.cells.xs[brightest]), 1),
                            "y": round(float(feats.cells.ys[brightest]), 1)}
    return out


# -- the summary --------------------------------------------------------------------


def _pf(positive, members):
    n = int(members.sum())
    if n < SPATIAL["min_stratum_pf_cells"]:
        return None, n
    return float(positive[members].mean()), n


def _ratio(a, b):
    if a is None or b is None:
        return None
    if b <= 0:
        return None if a <= 0 else float("inf")
    return float(a / b)


def _excess(flag, near, reference):
    """(share of `near` cells with `flag`, the same among `reference`, their
    difference): None where either side has under `min_stratum_pf_cells`."""
    a = _pf(flag, near)[0]
    b = _pf(flag, reference)[0]
    return a, b, (a - b) if a is not None and b is not None else None


def excesses(feats, gate) -> dict:
    """{spill, density, snr: (share near the gate, share among clear
    positives, excess)}: how much more often the cells at the gate touch a
    strong positive, sit in dense tissue, sit in low SNR than clear positives
    do -- the self-calibrated numbers every rule reads (`schemas.
    SPATIAL_RULES`). `snr` is (None, None, None) without an SNR."""
    near = gate.near
    clear_positive = feats.eligible & (feats.vf > gate.g + 2 * gate.h)
    out = {"spill": _excess(gate.spill_suspect, near, clear_positive),
           "density": _excess(feats.dense_env, near, clear_positive),
           "snr": (None, None, None)}
    if gate.snr.get("available"):
        known = gate.snr_class != SNR_UNKNOWN
        out["snr"] = _excess(gate.snr_class == SNR_LOW, near & known, clear_positive & known)
    return out


def spatial_summary(ds, marker, low, high=None, *, bgmap=None, mode=None, pixel_um=None,
                    seed=0) -> dict:
    """What the session keeps of the feature layer at one gate (< 4 KB,
    JSON-safe): density and SNR strata with the positive fraction in each,
    the bright-neighbour shares near the gate and among clear positives (the
    self-calibrated `spill_excess`), the background's structure, and a risk
    score that says whether a look should compare like-for-like cells. A
    summary decides nothing; `verdicts.spatial_case` reads it beside the
    looks' words."""
    import time

    from plexora.plugins.gating.server.autogate import kernels

    started = time.perf_counter()
    mode = mode or "C"
    feats = per_cell(ds, marker, bgmap=bgmap, pixel_um=pixel_um)
    gate = at_gate(ds, marker, feats, low, high, mode=mode, bgmap=bgmap)
    elig = feats.eligible
    density = {"boundaries": feats.density_info.get("boundaries"),
               "reason": feats.density_info.get("reason"), "counts": {}, "pf": {},
               "pf_near_gate": {}}
    for index, name in enumerate(schemas.DENSITY_CLASSES):
        members = elig & (feats.density_class == index)
        density["pf"][name], density["counts"][name] = _pf(gate.positive, members)
        density["pf_near_gate"][name] = _pf(gate.near, members)[0]
    pfs = [p for p in density["pf"].values() if p is not None]
    density["pf_spread"] = _ratio(max(pfs), min(pfs)) if len(pfs) >= 2 else None
    near = gate.near
    clear_positive = elig & (feats.vf > gate.g + 2 * gate.h)
    # The self-calibrated numbers the rules read (`excesses`). Positive
    # fractions per stratum stay as description only: positives that cluster
    # by biology make them differ on any real tissue.
    found = excesses(feats, gate)
    density["share_near_gate"], density["share_clear_positive"], density["excess"] = \
        found["density"]
    snr = {k: v for k, v in gate.snr.items()}
    snr.update(counts={}, pf={})
    if gate.snr.get("available"):
        for index, name in enumerate(schemas.SNR_CLASSES):
            members = elig & (gate.snr_class == index)
            snr["pf"][name], snr["counts"][name] = _pf(gate.positive, members)
        snr["pf_ratio"] = _ratio(snr["pf"].get("low"), snr["pf"].get("high"))
        snr["share_near_gate"], snr["share_clear_positive"], snr["excess"] = found["snr"]
    share_near = float(gate.spill_suspect[near].mean()) if near.sum() else None
    share_pos = float(gate.spill_suspect[clear_positive].mean()) if clear_positive.sum() \
        else None
    excess = (share_near - share_pos) if share_near is not None and share_pos is not None \
        else None
    isolated = None
    positives = np.flatnonzero(gate.positive)
    if positives.size >= 20:
        query = cellmod.seeded_subset(positives, SPATIAL["isolated_query_max"], seed + 41)
        grid = cellmod.grid(ds, feats.radius_px)
        n_nb, n_pos = kernels.neighbour_counts(grid, query=query, flags=gate.positive)
        enough = n_nb >= 3
        isolated = float((n_pos[enough] == 0).mean()) if enough.any() else None
    nn1 = feats.nn1[elig & np.isfinite(feats.nn1)]
    neighbours = {
        "strong_rule": feats.strong_rule, "strong_fit": feats.strong_fit,
        "n_strong": int((elig & (feats.vf >= feats.strong_fit)).sum()),
        "n_near_gate": int(near.sum()), "n_clear_positive": int(clear_positive.sum()),
        "spill_suspect_share_near_gate": share_near,
        "spill_suspect_share_clear_positive": share_pos, "spill_excess": excess,
        "bright_neighbour_share_near_gate":
            float(gate.bright_neighbour[near].mean()) if near.sum() else None,
        "n_spill_candidates": int((gate.spill != SPILL["none"]).sum()),
        "isolated_positive_share": isolated,
        "nn1_median": float(np.median(nn1)) if nn1.size else None,
        "adjacent_px": feats.adjacent_px}
    background = None
    if bgmap is not None:
        info = bgmap.get("info") or {}
        background = {"level": bgmap.get("level"), "tile_px": bgmap.get("tile_px"),
                      "tissue": bgmap.get("tissue"), "n_tiles": info.get("n_tiles"),
                      "heterogeneity_log": info.get("heterogeneity_log"),
                      "surface_r2": info.get("surface_r2"),
                      "structure": info.get("structure"),
                      "filled_share": info.get("filled_share"),
                      "unreliable_share": gate.snr.get("unreliable_share")}
    out = {"version": SPATIAL_VERSION, "marker": marker, "mode": mode,
           "pixel_um": pixel_um, "radius_px": feats.radius_px,
           "radius_basis": feats.radius_basis, "cell_diameter_px": feats.cell_diameter_px,
           "n_eligible": int(elig.sum()), "gate": float(low), "density": density, "snr": snr,
           "background": background, "neighbours": neighbours}
    out["risk"] = risk_of(out)
    out["cost_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return _jsonable(out)


def risk_of(summary) -> dict:
    """{score, level, reasons}: cells at the gate concentrated in dense
    tissue, beside strong positives, or in high background -- each beyond
    where clear positives sit (an excess, so clustered biology adds
    nothing), each a reason to compare like-for-like cells before moving the
    gate, never a verdict. Mode C has no background term."""
    r = SPATIAL["risk"]
    score, reasons = 0.0, []
    density = (summary.get("density") or {}).get("excess")
    if density is not None and density >= r["density_excess"]:
        score += r["w_density"]
        reasons.append("density")
    excess = (summary.get("neighbours") or {}).get("spill_excess")
    if excess is not None and excess >= r["spill_excess"]:
        score += r["w_spill"]
        reasons.append("spill")
    snr = summary.get("snr") or {}
    hetero = (summary.get("background") or {}).get("heterogeneity_log")
    background = snr.get("excess")
    if summary.get("mode") != "C" and snr.get("available") and hetero is not None \
            and hetero >= r["bg_hetero_log"] and background is not None \
            and background >= r["snr_excess"]:
        score += r["w_background"]
        reasons.append("background")
    level = "high" if score >= r["high"] else "moderate" if score >= r["moderate"] else "low"
    return {"score": round(score, 3), "level": level, "reasons": reasons}


def _jsonable(value):
    from plexora.plugins.gating.server.autogate.tableops import jsonable

    return jsonable(value)


def brief(summary) -> dict | None:
    """What a packet's evidence carries of a summary: a few numbers."""
    if not summary:
        return None
    nb = summary.get("neighbours") or {}
    snr = summary.get("snr") or {}
    density = summary.get("density") or {}
    return {"mode": summary.get("mode"),
            "segmentation": {"A": "mask", "B": "centroid"}.get(summary.get("mode")),
            "risk": (summary.get("risk") or {}).get("level"),
            "risk_reasons": (summary.get("risk") or {}).get("reasons"),
            "spill_excess": nb.get("spill_excess"),
            "density_excess": density.get("excess"),
            "snr_excess": snr.get("excess") if snr.get("available") else None}


# -- the targeted pass ----------------------------------------------------------------


def _crop_background(plane, outside, *, pure=False):
    """(background, noise sd) of a crop. `pure`: `outside` holds background
    only (a mask's label 0) -- its median and IQR / 1.349; otherwise it may
    hold other cells' stain, so the dimmer side is read: the lower quartile,
    and its distance from the median as 0.674 sd."""
    pixels = plane[outside]
    if pixels.size < 8:
        pixels = plane.ravel()
    q25, q50, q75 = np.percentile(pixels, [25, 50, 75])
    if pure:
        return float(q50), max(float(q75 - q25) / 1.349, 1e-6)
    return float(q25), max(float(q50 - q25) / 0.674, 1e-6)


def _toward_away(points_y, points_x, centre, target, plane_values):
    """(toward mean, away mean) of `plane_values` split by the half-plane
    through `centre` facing `target`."""
    dy, dx = target[0] - centre[0], target[1] - centre[1]
    norm = math.hypot(dx, dy)
    if norm < 1e-6 or plane_values.size < 4:
        return None, None
    side = (points_y - centre[0]) * dy + (points_x - centre[1]) * dx
    toward, away = plane_values[side > 0], plane_values[side <= 0]
    if not toward.size or not away.size:
        return None, None
    return float(toward.mean()), float(away.mean())


def _reading(compartment):
    if compartment in ("membrane", "cytoplasmic", "extracellular"):
        return "expected"
    if compartment in ("nuclear", "nuclear_cytoplasmic"):
        return "informative"
    return "uninterpreted"


def targeted_from_crops(crops, marker, cells, *, mode, compartment, radius_px,
                        neighbours=None) -> dict:
    """{cell_id: record} for the crops a collage read (`crops` by cell id,
    `cells` the placed cells). Mode A reads the mask: the cell's core and
    rim, the interface with each touching cell, the brightest of them, the
    halves toward and away from it; B reads discs around the centroids
    (estimates, said so), toward and away from the table's brightest
    neighbour -- a placed cell's `brightest` (`cell_record`), or
    `neighbours[cell_id]`. Never more than `max_targeted` cells."""
    out = {}
    for cell in list(cells)[:int(SPATIAL["max_targeted"])]:
        cid = int(cell["cell_id"])
        crop = crops.get(cid)
        if crop is None or crop.planes.get(marker) is None:
            continue
        record = None
        neighbour = (neighbours or {}).get(cid) or cell.get("brightest")
        if mode == "A" and crop.labels is not None:
            record = _targeted_mask(crop, marker, compartment, radius_px, neighbour)
        if record is None and mode in ("A", "B"):
            record = _targeted_disc(crop, marker, radius_px, neighbour)
        if record is not None:
            out[cid] = record
    return out


def _supported(toward_over_away, away, bg, noise, reading, *, interface=None,
               reference=None):
    """Whether the pixels support spill-over: brighter toward the bright
    neighbour than away from it, at the interface than elsewhere on the
    outline (when there is one), and -- for a compartment whose stain is a
    ring anyway -- the side away from the neighbour at background (a
    polarised ring is the cell's own)."""
    if toward_over_away is None or toward_over_away < SPATIAL["half_disc_ratio"]:
        return False
    if interface is not None and reference is not None \
            and interface < SPATIAL["interface_ratio"] * max(reference, 1e-6):
        return False
    if reading == "expected" and away is not None:
        return (away - bg) <= SPATIAL["away_max_sd"] * noise
    return True


def _targeted_mask(crop, marker, compartment, radius_px, neighbour=None):
    from scipy.ndimage import binary_erosion

    plane = np.asarray(crop.planes[marker], dtype=np.float64)
    labels = np.asarray(crop.labels)
    if labels.shape != plane.shape:
        return None
    own = labels == crop.cell_id
    if own.sum() < 6:
        return None
    div = 2 ** int(crop.level)
    rim_px = max(1, int(round(SPATIAL["rim_px"] / max(1, div))))
    core = binary_erosion(own, iterations=rim_px)
    rim = own & ~core
    bg, noise = _crop_background(plane, labels == 0, pure=True)
    # Which other label touches each own pixel (4-neighbourhood).
    touching = np.zeros_like(labels)
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        shifted = np.roll(np.roll(labels, dy, axis=0), dx, axis=1)
        take = own & (shifted != crop.cell_id) & (shifted != 0) & (touching == 0)
        touching[take] = shifted[take]
    edge = own & ~binary_erosion(own, iterations=1)
    contact = touching > 0
    non_contact = edge & ~contact
    ids, inverse = np.unique(labels.ravel(), return_inverse=True)
    sums = np.bincount(inverse, weights=plane.ravel())
    counts = np.bincount(inverse)
    means = dict(zip(ids.tolist(), (sums / np.maximum(counts, 1)).tolist()))
    adjacent = sorted({int(t) for t in np.unique(touching[contact])})
    own_mean = float(plane[own].mean())
    record = {"targeted_version": TARGETED_VERSION, "mode": "A", "centroid_estimate": False,
              "label_found": True, "crop_bg": bg, "crop_noise": noise,
              "own_contrast": (own_mean - bg) / noise,
              "core_mean": float(plane[core].mean()) if core.any() else None,
              "rim_mean": float(plane[rim].mean()) if rim.any() else None,
              "n_adjacent": len(adjacent),
              "interface_share": float(contact.sum() / max(1, edge.sum())),
              "non_contact_boundary_mean": float(plane[non_contact].mean())
              if non_contact.any() else None,
              "compartment_reading": _reading(compartment)}
    if record["core_mean"] and record["rim_mean"] is not None:
        record["rim_over_core"] = record["rim_mean"] / max(record["core_mean"], 1e-6)
    record["spill"] = "none"
    if not adjacent:
        if neighbour is None:
            return record
        # Not touching: the bright neighbour's light crosses a gap. The
        # cell's own pixels toward and away from the table's brightest
        # neighbour say whether it reaches this one.
        div_ = 2 ** int(crop.level)
        target = (float(neighbour["y"]) / div_ - crop.box[1],
                  float(neighbour["x"]) / div_ - crop.box[0])
        yy, xx = np.nonzero(own)
        toward, away = _toward_away(yy.astype(np.float64), xx.astype(np.float64),
                                    (yy.mean(), xx.mean()), target, plane[own])
        record["toward_over_away"] = (toward / max(away, 1e-6)) if toward is not None \
            else None
        record["brightest_neighbour"] = {"x": float(neighbour["x"]), "y": float(neighbour["y"])}
        supported = _supported(record["toward_over_away"], away, bg, noise,
                               record["compartment_reading"])
        record["interface_support"] = bool(supported)
        record["spill"] = "supported" if supported else "unsupported"
        return record
    brightest = max(adjacent, key=lambda label: means.get(label, 0.0))
    record["brightest_adjacent"] = int(brightest)
    record["brightest_adjacent_mean"] = float(means.get(brightest, 0.0))
    interface = own & (touching == brightest)
    interface_mean = float(plane[interface].mean()) if interface.any() else None
    record["interface_mean"] = interface_mean
    yy, xx = np.nonzero(own)
    by, bx = np.nonzero(labels == brightest)
    toward, away = _toward_away(yy.astype(np.float64), xx.astype(np.float64),
                                (yy.mean(), xx.mean()), (by.mean(), bx.mean()),
                                plane[own])
    record["toward_over_away"] = (toward / max(away, 1e-6)) if toward is not None else None
    if record["brightest_adjacent_mean"] <= own_mean:
        return record
    reference = record["non_contact_boundary_mean"]
    if reference is None:
        reference = away
    # A membrane ring brighter where the cell meets another may be
    # polarisation (a synapse): spill only when the side AWAY from the
    # neighbour is at background (`_supported`).
    supported = interface_mean is not None and reference is not None and _supported(
        record["toward_over_away"], away, bg, noise, record["compartment_reading"],
        interface=interface_mean, reference=reference)
    record["interface_support"] = bool(supported)
    record["spill"] = "supported" if supported else "unsupported"
    return record


def _targeted_disc(crop, marker, radius_px, neighbour):
    plane = np.asarray(crop.planes[marker], dtype=np.float64)
    side = plane.shape[0]
    div = 2 ** int(crop.level)
    cx = crop.x / div - crop.box[0]
    cy = crop.y / div - crop.box[1]
    r = max(1.0, float(radius_px) / div)
    yy, xx = np.mgrid[:side, :side]
    distance = np.hypot(xx - cx, yy - cy)
    disc = distance <= r
    annulus = (distance > r) & (distance <= 2 * r)
    outside = distance > 2 * r
    if disc.sum() < 4:
        return None
    bg, noise = _crop_background(plane, outside)
    disc_median = float(np.median(plane[disc]))
    annulus_median = float(np.median(plane[annulus])) if annulus.any() else None
    rings = []
    for lo, hi in ((0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0)):
        ring = (distance > lo * r) & (distance <= hi * r)
        rings.append(float(plane[ring].mean()) if ring.any() else None)
    record = {"targeted_version": TARGETED_VERSION, "mode": "B", "centroid_estimate": True,
              "support_strength": "approximate", "label_found": False,
              "crop_bg": bg, "crop_noise": noise, "own_contrast": (disc_median - bg) / noise,
              "disc_median": disc_median, "annulus_median": annulus_median,
              "centre_over_annulus": disc_median / max(annulus_median, 1e-6)
              if annulus_median is not None else None,
              "radial": rings, "spill": "none"}
    if neighbour is None:
        return record
    tx, ty = float(neighbour["x"]) / div - crop.box[0], float(neighbour["y"]) / div - crop.box[1]
    toward, away = _toward_away(yy[disc].astype(np.float64), xx[disc].astype(np.float64),
                                (cy, cx), (ty, tx), plane[disc])
    record["brightest_neighbour"] = {"x": float(neighbour["x"]), "y": float(neighbour["y"])}
    record["toward_half_median"] = toward
    record["away_half_median"] = away
    record["toward_over_away"] = (toward / max(away, 1e-6)) if toward is not None else None
    # Without a mask nothing says what is membrane: the far side must be at
    # background whatever the compartment.
    supported = away is not None and _supported(record["toward_over_away"], away, bg, noise,
                                                "expected")
    record["spill"] = "supported" if supported else "unsupported"
    return record


# -- corrected values: a seam, empty ---------------------------------------------------

#: {method: function(ds, marker, params) -> values}. Empty: no correction is
#: validated, so none is offered. A method lands here only with a benchmark
#: behind it, and is then used only when a session names it
#: (`SessionOptions.correction`), with both gates recorded.
CORRECTIONS: dict = {}


def corrected_values(ds, marker, method, params=None) -> dict:
    """{values, method, params, version, source_fingerprint, n_changed}: a
    corrected copy of the column, never the column itself -- the stored
    matrix, `cells.values` and `profile.column` are untouched. Raises
    `capability_unavailable` while `CORRECTIONS` is empty."""
    from plexora.agent.errors import AgentError

    fn = CORRECTIONS.get(method)
    if fn is None:
        raise AgentError("capability_unavailable", f"no validated correction named {method!r}",
                         detail={"available": sorted(CORRECTIONS)})
    raw = np.asarray(cellmod.values(ds, marker), dtype=np.float32)
    values = np.asarray(fn(ds, marker, dict(params or {})), dtype=np.float32)
    changed = int(np.sum(np.isfinite(raw) & (values != raw)))
    return {"values": values, "method": method, "params": dict(params or {}),
            "version": SPATIAL_VERSION, "source_fingerprint": profmod.fingerprint(ds),
            "n_changed": changed}


def corrected_column(ds, marker, method, params=None):
    """A SEPARATE `profile.Column` over corrected values (cached apart from the
    marker's own)."""
    import json

    blob = json.dumps(params or {}, sort_keys=True)

    def compute():
        found = corrected_values(ds, marker, method, params)
        return profmod.Column(marker, found["values"], ds.table.log_transformed)

    return ds.cached(("autogate.spatial.corrected", marker, method, blob,
                      profmod.fingerprint(ds), _qc_fp(ds)), compute)


# -- what a session keeps ------------------------------------------------------------


def map_payload(session, ds, channel):
    """The channel's background map, encoded for a table operation
    (`encode_map`), cached on the handle set; None without one."""
    def compute():
        try:
            return encode_map(background_map(session, ds, channel))
        except Exception:  # no map is mode-C SNR (unknown), never a failure
            return None

    return ds.cached(("autogate.spatial.map_payload", SPATIAL_VERSION, channel), compute)


def payload_for(session, ds, marker, mode, pixel_um=None) -> dict:
    """{mode, map, pixel_um}: what the table operations read the feature
    layer with -- the map only in modes A and B."""
    from plexora.plugins.gating.server.autogate import views

    out = {"mode": mode, "pixel_um": pixel_um, "map": None}
    if mode in ("A", "B") and session is not None:
        channel = views.image_channel(ds, marker)
        if channel:
            out["map"] = map_payload(session, ds, channel)
    return out


def compact(summary) -> dict:
    """What a unit keeps of a summary: a few hundred bytes (the record is
    saved on every answer)."""
    if not summary:
        return {}
    density = summary.get("density") or {}
    snr = summary.get("snr") or {}
    nb = summary.get("neighbours") or {}
    bg = summary.get("background") or {}
    return {"at": summary.get("gate"), "risk": summary.get("risk"),
            "n_near_gate": nb.get("n_near_gate"),
            "density_pf": density.get("pf"), "density_counts": density.get("counts"),
            "density_excess": density.get("excess"),
            "snr_available": bool(snr.get("available")),
            "snr_pf": snr.get("pf") or None, "snr_counts": snr.get("counts") or None,
            "snr_excess": snr.get("excess"),
            "spill_share_near_gate": nb.get("spill_suspect_share_near_gate"),
            "spill_share_clear_positive": nb.get("spill_suspect_share_clear_positive"),
            "spill_excess": nb.get("spill_excess"),
            "bg_heterogeneity": bg.get("heterogeneity_log"), "bg_structure": bg.get("structure"),
            "cost_ms": summary.get("cost_ms")}


def bulk_record(session, ds, marker, low, high=None, *, pixel=None, seed=0) -> dict:
    """The unit's `spatial` record from the bulk pass: the evidence mode and,
    at the Auto gate, the compact summary (`compact`)."""
    mode = detect_mode(session, ds, marker, pixel, seed=seed)
    record = {"mode": mode["mode"], "why": mode.get("why"), "image_mode": mode.get("image_mode"),
              "pixel_um": mode.get("pixel_um"), "pixel_source": mode.get("pixel_source"),
              "mask": {k: (mode.get("mask") or {}).get(k) for k in ("status", "usable")}}
    if low is None:
        return record
    payload = payload_for(session, ds, marker, mode["mode"], mode.get("pixel_um"))
    from plexora.plugins.gating.server.autogate import tableops

    summary = tableops.local_or_node(ds, "gating.autogate.spatial", {
        **payload, "marker": marker, "low": float(low), "high": high, "seed": int(seed)})
    return {**record, **compact(summary)}


def t1_audit(record) -> list:
    """Why a T1 (numbers-only) acceptance should get a look after all, from
    the unit's spatial record: high risk, or the cells at the gate
    concentrated in dense tissue, low SNR or beside strong positives beyond
    where clear positives sit (an excess over at least `min_stratum_cells`
    cells at the gate). Numeric only; [] when nothing says so."""
    rules = schemas.SPATIAL_RULES
    reasons = []
    if (record.get("risk") or {}).get("level") == "high":
        reasons.append("high spatial risk")
    enough = (record.get("n_near_gate") or 0) >= rules["min_stratum_cells"]
    density = record.get("density_excess")
    if enough and density is not None and density >= rules["density_excess_trigger"]:
        reasons.append(f"cells at the gate sit in dense tissue {density:.2f} more often "
                       "than clear positives do")
    snr = record.get("snr_excess")
    if enough and snr is not None and snr >= rules["snr_excess_trigger"]:
        reasons.append(f"cells at the gate sit in high background {snr:.2f} more often "
                       "than clear positives do")
    excess = record.get("spill_excess")
    if enough and excess is not None and excess >= rules["spill_excess_trigger"]:
        reasons.append(f"cells at the gate touch strong positives {excess:.2f} more often "
                       "than clear positives do")
    return reasons


# -- one cell, for `explain_cell` (`Plugin.cell_features_factory`) -------------------


def cell_features(session, data, cell_id, marker) -> dict:
    """What the spatial evidence says about one cell's surroundings for
    `marker`, at the marker's current gate: the evidence mode, its density
    class and neighbour count, its local background and SNR class, and
    whether a bright neighbour touches it (with where that neighbour is).
    {} when the marker is not in the table."""
    from plexora.plugins.gating.server import model

    if marker not in data.table.markers:
        return {}
    c = cellmod.cells(data)
    where = np.flatnonzero(c.ids == int(cell_id))
    if not where.size:
        return {}
    row = int(where[0])
    mode = detect_mode(session, data, marker)
    payload = payload_for(session, data, marker, mode["mode"], mode.get("pixel_um"))
    bgmap = decode_map(payload.get("map"))
    feats = per_cell(data, marker, bgmap=bgmap, pixel_um=mode.get("pixel_um"))
    gate = model.get_gate(data, marker)
    at = at_gate(data, marker, feats, float(gate["low"]), float(gate["high"]),
                 mode=mode["mode"], bgmap=bgmap)
    record = cell_record(feats, at, row)
    bg = feats.local_bg[row]
    return {"mode": mode["mode"], "mode_words": MODE_WORDS.get(mode["mode"]),
            "density_class": record["density_class"],
            "neighbours_within_radius": int(feats.density_count[row]),
            "radius_um": cellmod.NEIGHBOUR_RADIUS_UM if feats.radius_basis == "um" else None,
            "radius_px": round(feats.radius_px, 1), "snr_class": record["snr_class"],
            "local_background": None if not np.isfinite(bg) else round(float(bg), 2),
            "spill": record["spill"], "brightest_neighbour": record.get("brightest"),
            "nearest_strong_px": record["nearest_strong_px"],
            "gate": float(gate["low"]),
            "note": "circumstances, not a verdict: a bright neighbour or a high background "
                    "makes a cell's stain worth a look, never wrong by itself"}
