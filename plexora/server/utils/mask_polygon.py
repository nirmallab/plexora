"""A boolean mask on a crop as a polygon in full-resolution image pixels.

Shared by QC's tracer (`plugins/qc/server/refine.py`) and magic select
(`plexora/vision/segment.py`), so a region the agent traced and one a person
clicked come out of one polygoniser: same pixel-edge convention, same
simplification budget, same rounding. Nothing here validates against the ROI
plugin's rules -- core may not import plugins; each caller validates at its
own boundary.

All mask work is whole-array: OpenCV's connected components and contours, a
`bincount` over labels, no per-pixel Python.
"""

from __future__ import annotations

import numpy as np

MAX_VERTICES = 2_000


def polygonal(shape):
    """The polygon parts of a shapely result (an intersection can add lines
    and points where two outlines only touch), or None."""
    import shapely
    from shapely.geometry import MultiPolygon, Polygon

    if shape is None or shape.is_empty:
        return None
    parts = [p for p in shapely.get_parts(shape) if isinstance(p, (Polygon, MultiPolygon))
             and not p.is_empty]
    flat = []
    for part in parts:
        flat.extend(part.geoms if isinstance(part, MultiPolygon) else [part])
    if not flat:
        return None
    return MultiPolygon(flat) if len(flat) > 1 else flat[0]


def vertex_count(shape) -> int:
    from shapely.geometry import MultiPolygon

    polygons = shape.geoms if isinstance(shape, MultiPolygon) else [shape]
    total = 0
    for polygon in polygons:
        total += len(polygon.exterior.coords) + sum(len(r.coords) for r in polygon.interiors)
    return total


def _lists(value):
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (int, float)):
            return [round(float(v), 2) for v in value]
        return [_lists(v) for v in value]
    return value


def to_geojson(shape, *, simplify_px, max_vertices=MAX_VERTICES, min_area_px=0.0):
    """GeoJSON of a shapely (Multi)Polygon simplified to `max_vertices`, or None.

    Coordinates are rounded to 2 decimals. Not validated -- see the module
    docstring."""
    from shapely.geometry import MultiPolygon, Polygon, mapping

    if shape is None or shape.is_empty:
        return None
    shape = shape.buffer(0)
    tolerance = float(simplify_px)
    simplified = shape.simplify(tolerance, preserve_topology=True) if tolerance > 0 else shape
    for _ in range(12):
        if vertex_count(simplified) <= max_vertices:
            break
        tolerance *= 1.5
        simplified = shape.simplify(tolerance, preserve_topology=True)
    parts = [p for p in (simplified.geoms if isinstance(simplified, MultiPolygon)
                         else [simplified]) if isinstance(p, Polygon) and p.area > min_area_px]
    if not parts:
        return None
    geometry = MultiPolygon(parts) if len(parts) > 1 else parts[0]
    geojson = mapping(geometry)
    return {"type": geojson["type"], "coordinates": _lists(geojson["coordinates"])}


def mask_to_shape(mask, origin, factor):
    """Shapely geometry (full-resolution pixels, holes kept) of a crop's mask
    whose pixel (0, 0) is level pixel `origin`, or None.

    `cv2.findContours` with RETR_CCOMP gives every outer ring and its holes
    through the boundary pixels' centres; half a pixel of mitred buffer puts
    the edges back on the pixels' outer sides (and a hole's on its inner)."""
    import cv2
    import shapely
    from shapely.geometry import LineString, Point, Polygon

    mask = np.ascontiguousarray(np.asarray(mask).astype(np.uint8, copy=False))
    if not mask.any():
        return None
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    hierarchy = hierarchy[0]
    solid, thin = [], []

    def ring(index):
        return contours[index].reshape(-1, 2).astype(np.float64) + 0.5

    for index in np.flatnonzero(hierarchy[:, 3] == -1):
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
        merged = polygonal(shapely.union_all(solid))
        if merged is not None:
            parts.append(merged.buffer(0.5, join_style="mitre", mitre_limit=2.0))
    parts.extend(thin)
    if not parts:
        return None
    shape = shapely.union_all(parts)
    ox, oy = float(origin[0]), float(origin[1])
    shape = shapely.transform(shape, lambda xy: (xy + [ox, oy]) * float(factor))
    return polygonal(shape)


def mask_to_polygon(mask, origin, factor, *, simplify_px, min_area_px=0.0,
                    max_vertices=None):
    """GeoJSON (unvalidated) of `mask_to_shape`, simplified to the budget."""
    return to_geojson(mask_to_shape(mask, origin, factor), simplify_px=simplify_px,
                      max_vertices=max_vertices or MAX_VERTICES, min_area_px=min_area_px)


# -- cleaning a predicted mask ------------------------------------------------


def _components(mask):
    import cv2

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        np.ascontiguousarray(mask.astype(np.uint8, copy=False)), connectivity=8)
    return count, labels, stats[:, cv2.CC_STAT_AREA]


def keep_parts_touching(mask, points_rc, *, radius=0):
    """The connected parts of `mask` containing (or within `radius` px of) any
    of `points_rc` (row, col). An empty point list keeps everything."""
    mask = np.asarray(mask, bool)
    points = np.asarray(points_rc, np.int64).reshape(-1, 2)
    if not points.size or not mask.any():
        return mask
    count, labels, _ = _components(mask)
    h, w = mask.shape
    keep = np.zeros(count, bool)
    if radius > 0:
        offsets = np.arange(-radius, radius + 1)
        dy, dx = np.meshgrid(offsets, offsets, indexing="ij")
        disc = (dy ** 2 + dx ** 2) <= radius ** 2
        rows = (points[:, None, 0] + dy[disc][None, :]).ravel()
        cols = (points[:, None, 1] + dx[disc][None, :]).ravel()
    else:
        rows, cols = points[:, 0], points[:, 1]
    inside = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
    keep[labels[rows[inside], cols[inside]]] = True
    keep[0] = False
    if not keep.any():
        return mask
    return keep[labels]


def fill_small_holes(mask, max_area_px):
    """Fill holes no larger than `max_area_px` (background parts that do not
    touch the crop border)."""
    import cv2

    mask = np.asarray(mask, bool)
    if max_area_px <= 0 or not mask.any():
        return mask
    background = (~mask).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(background, connectivity=4)
    if count <= 1:
        return mask
    area = stats[:, cv2.CC_STAT_AREA]
    x, y = stats[:, cv2.CC_STAT_LEFT], stats[:, cv2.CC_STAT_TOP]
    bw, bh = stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]
    h, w = mask.shape
    border = (x == 0) | (y == 0) | (x + bw >= w) | (y + bh >= h)
    fill = (area <= max_area_px) & ~border
    fill[0] = False
    if not fill.any():
        return mask
    return mask | fill[labels]


def clean_mask(mask, *, positives_rc=(), min_area_px=0, hole_fraction=0.005,
               keep_largest=True):
    """The tidy-up every predicted mask gets before it becomes a polygon.

    Parts not under a positive prompt go (when any positive is given); parts
    smaller than `min_area_px` go; holes up to `hole_fraction` of the mask
    are filled; with `keep_largest` only the biggest part survives, so the
    result is one editable ring.
    """
    mask = keep_parts_touching(mask, positives_rc)
    if not mask.any():
        return mask
    count, labels, area = _components(mask)
    keep = area >= max(1, min_area_px)
    keep[0] = False
    if keep_largest and keep[1:].any():
        best = 1 + int(np.argmax(np.where(keep[1:], area[1:], -1)))
        keep[:] = False
        keep[best] = True
    if not keep.any():
        return np.zeros_like(mask, bool)
    mask = keep[labels]
    return fill_small_holes(mask, hole_fraction * float(mask.sum()))
