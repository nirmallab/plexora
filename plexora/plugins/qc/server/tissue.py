"""The tissue every check works inside, and the feathered mask round it.

One function owns what "tissue" means: `estimate` wraps the scan's own
`scan.tissue_estimate` (the nuclear stain smoothed over a few cells and
split by Otsu, dust dropped, small holes filled) and adds the FEATHERED
mask -- the same tissue grown `schemas.TISSUE["feather_um"]` (an exact
Euclidean distance on the overview, cheap at a megapixel). The two have
different jobs:

- the tight mask is every score's denominator (the scan detectors,
  `candidates.area_fraction`, the sheets' tissue box, the check units):
  feathering it would count glass as tissue;
- the feathered mask says which cells are OUTSIDE the tissue -- the
  Background ROI (`background.py`) is the glass beyond it, so a cell at the
  tissue's ragged edge is not called background for a few microns.

`for_project` reads it back from the newest stored scan (no pixel read), or
reads one nuclear overview when there is none.
"""

from __future__ import annotations

import json

import numpy as np

from plexora.plugins.qc.server import schemas


def feather(mask, feather_px):
    """`mask` grown by `feather_px` pixels: every pixel within that Euclidean
    distance of the tissue. Nothing to grow (no tissue, or every pixel
    tissue, or no feather) returns the mask as it is."""
    mask = np.asarray(mask, dtype=bool)
    if feather_px is None or feather_px <= 0 or not mask.any() or mask.all():
        return mask.copy()
    from scipy import ndimage

    distance = ndimage.distance_transform_edt(~mask)
    return mask | (distance <= float(feather_px))


def feather_px_at(pixel_um, factor=1.0):
    """The feather in pixels of a level `factor` times coarser than full
    resolution: `feather_um` with a pixel size, else `feather_px`."""
    factor = max(float(factor or 1.0), 1e-9)
    if pixel_um:
        return schemas.TISSUE["feather_um"] / (float(pixel_um) * factor)
    return float(schemas.TISSUE["feather_px"]) / factor


def estimate(overviews, nuclear, brightfield=False, *, sigma_px=None, pixel_um=None,
             factor=1.0) -> dict:
    """`scan.tissue_estimate`'s {mask, method, channel, holes} plus
    `feathered` (the mask grown by the feather at this level, `factor` full-
    resolution pixels per overview pixel) and `feather_px` (that feather, in
    overview pixels). A method that found no tissue (`all_pixels`) is not
    feathered: every pixel is tissue already."""
    from plexora.plugins.qc.server import scan

    found = scan.tissue_estimate(overviews, nuclear, brightfield, sigma_px=sigma_px)
    px = feather_px_at(pixel_um, factor)
    found["feather_px"] = px
    found["feathered"] = found["mask"].copy() if found["method"] == "all_pixels" \
        else feather(found["mask"], px)
    return found


def to_shape(mask, shape):
    """A boolean mask brought to `shape` (rows, cols) by nearest neighbour."""
    mask = np.asarray(mask, dtype=bool)
    rows, cols = int(shape[0]), int(shape[1])
    if mask.shape == (rows, cols):
        return mask
    ys = np.minimum((np.arange(rows) * mask.shape[0] / max(rows, 1)).astype(np.int64),
                    mask.shape[0] - 1)
    xs = np.minimum((np.arange(cols) * mask.shape[1] / max(cols, 1)).astype(np.int64),
                    mask.shape[1] - 1)
    return mask[ys[:, None], xs[None, :]]


def _newest_scan(project):
    """The newest stored scan of the current version, or None."""
    from plexora.plugins.qc.server import scan

    folder = scan._folder(project)
    if not folder.is_dir():
        return None
    metas = sorted((p for p in folder.glob("scan_*.json") if ".tmp." not in p.name),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in metas:
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("version") != schemas.SCAN_VERSION:
            continue
        held = scan.load(project, meta.get("fingerprint"))
        if held is not None and held.shared("tissue_feathered") is not None:
            return held
    return None


def from_scan(result) -> dict | None:
    """{mask, feathered, method, factor, pixel_um, source} of a stored scan's
    overview masks, or None when it has no feathered mask."""
    feathered = result.shared("tissue_feathered")
    tight = result.shared("tissue_overview")
    if feathered is None or tight is None:
        return None
    meta = result.meta
    return {"mask": tight.astype(bool), "feathered": feathered.astype(bool),
            "method": (meta.get("tissue") or {}).get("method"),
            "factor": float(meta.get("overview_factor") or 1.0),
            "pixel_um": meta.get("pixel_um"), "source": "scan",
            "fingerprint": meta.get("fingerprint")}


def for_project(session, project, *, scan_result=None) -> dict:
    """The project's tissue: {mask, feathered (overview level, bool), method,
    factor (full-resolution px per overview px), pixel_um, source}. From
    `scan_result` or the newest stored scan; else one read of the nuclear
    channel's overview (the first nuclear channel by name, else every
    channel's max)."""
    held = from_scan(scan_result) if scan_result is not None else None
    if held is None:
        stored = _newest_scan(project)
        held = from_scan(stored) if stored is not None else None
    if held is not None:
        return held
    from plexora.agent.evidence import image_qc
    from plexora.agent.render import resolve_channel
    from plexora.plugins.qc.server import cycles as cycle_rules
    from plexora.plugins.qc.server import scan
    from plexora.server.utils import pixel_scale, source_image

    record = session.project(project)
    channel_records = list(record.image.real_channels)
    names = [c.get("fullname") or c.get("name") for c in channel_records]
    pixel = pixel_scale.pixel_size(record)
    pixel_um = float(pixel["value"]) if pixel else None
    with source_image.SHELF.reader(session.image_data(project)) as source:
        level = image_qc.overview_level(source)
        ov_h, ov_w = source.level_shape(level)
        width = source.level_shape(0)[1]
        factor = width / max(1, ov_w)
        brightfield = bool(source.is_brightfield)
        nuclear = None if brightfield else next(
            (n for n in names if cycle_rules.is_nuclear(n)), None)
        read = names[:1] if brightfield else ([nuclear] if nuclear else names)
        overviews = {}
        for name in read:
            if brightfield:
                index = 0
            else:
                _i, found = resolve_channel(name, channel_records)
                index = source.channel_index(source_image.channel_key(found))
            overviews[name], _ = scan._read(source, index, level, (0, 0, ov_w, ov_h),
                                            brightfield)
    sigma_px = (scan.TISSUE_SMOOTH_UM / (pixel_um * factor)) if pixel_um else None
    found = estimate(overviews, nuclear, brightfield, sigma_px=sigma_px, pixel_um=pixel_um,
                     factor=factor)
    return {"mask": found["mask"], "feathered": found["feathered"], "method": found["method"],
            "factor": float(factor), "pixel_um": pixel_um, "source": "overview",
            "fingerprint": None}


# -- a region against the tissue ---------------------------------------------------
#
# The visual pass (`overview.py`, `segment_qc_roi`) writes only what lies on
# the tissue: an outline is clipped to the ALLOWED region -- the tight mask
# with its holes filled (a tear is missing tissue inside the body, and stays)
# grown by the feather (a ragged edge stays) -- and refused when too little
# of it was there (`schemas.VISUAL["min_on_tissue"]`). The masks are the
# overview-level ones `for_project` returns; a geometry is rasterised onto
# them the way `report.affected_area` does.


def _rasterise(geometry, shape_hw, factor):
    import cv2
    from shapely.geometry import MultiPolygon, shape

    h, w = int(shape_hw[0]), int(shape_hw[1])
    canvas = np.zeros((h, w), dtype=np.uint8)
    if not geometry:
        return canvas.astype(bool)
    found = shape(geometry).buffer(0)
    parts = found.geoms if isinstance(found, MultiPolygon) else [found]
    for part in parts:
        if part.is_empty or not hasattr(part, "exterior"):
            continue
        ring = np.asarray(part.exterior.coords) / float(factor)
        cv2.fillPoly(canvas, [np.round(ring).astype(np.int32).reshape(-1, 1, 2)], 1)
        for hole in part.interiors:
            ring = np.asarray(hole.coords) / float(factor)
            cv2.fillPoly(canvas, [np.round(ring).astype(np.int32).reshape(-1, 1, 2)], 0)
    return canvas.astype(bool)


def allowed_mask(found) -> np.ndarray:
    """The overview-level mask a visual-pass region may cover: the tight
    tissue with its holes filled, grown by the feather."""
    from scipy import ndimage

    tight = np.asarray(found["mask"], dtype=bool)
    if not tight.any() or tight.all():
        return tight.copy() if tight.any() else np.ones_like(tight)
    body = ndimage.binary_fill_holes(tight)
    return feather(body, feather_px_at(found.get("pixel_um"), found.get("factor") or 1.0))


def share_on_tissue(found, geometry) -> dict:
    """{on_tissue_fraction, tissue_fraction, area_px} of a full-resolution
    GeoJSON region: how much of it lies on the allowed tissue, and how much
    of the tight tissue it covers."""
    factor = float(found.get("factor") or 1.0)
    allowed = allowed_mask(found)
    tight = np.asarray(found["mask"], dtype=bool)
    region = _rasterise(geometry, allowed.shape, factor)
    covered = int(region.sum())
    per_px = factor * factor
    return {"on_tissue_fraction": (float((region & allowed).sum()) / covered) if covered
            else 0.0,
            "tissue_fraction": (float((region & tight).sum()) / float(tight.sum()))
            if tight.any() else None,
            "area_px": covered * per_px}


def clip_to_tissue(found, geometry, *, max_vertices=2000):
    """`geometry` (full-resolution GeoJSON) cut to the allowed tissue, or None
    when nothing of it is there. The allowed region's outline is the mask's
    (overview pixels, so the cut is a few microns coarse at most)."""
    from plexora.plugins.qc.server import polygons
    from plexora.server.utils import mask_polygon

    allowed = allowed_mask(found)
    if allowed.all():
        return geometry
    factor = float(found.get("factor") or 1.0)
    outline = mask_polygon.mask_to_polygon(allowed, (0, 0), factor,
                                           simplify_px=0.5 * factor, max_vertices=max_vertices)
    if outline is None:
        return None
    return polygons.clip_to(geometry, outline, max_vertices=max_vertices)
