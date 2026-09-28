"""Map masks as ROI geometry: GeoJSON in full-resolution image pixels.

A candidate lives on the scan's map grid (50 µm cells); an ROI is a polygon
the user can edit. The conversion is exact -- the union of the mask's cell
squares, holes kept -- then simplified to a vertex budget and validated by the
ROI plugin's own rule, so what is written is always a shape the ROI panel
accepts. Variants (tight, standard, generous, hull, bbox) are the outlines a
localisation packet offers; the grid fallback lets an agent name squares
instead of coordinates.
"""

from __future__ import annotations

import string

import numpy as np

MAX_VERTICES = 2_000


def _dilate(mask, cells):
    from scipy import ndimage

    if cells <= 0:
        return mask
    return ndimage.binary_dilation(mask, structure=np.ones((3, 3), dtype=bool),
                                   iterations=int(cells))


def _close(mask, cells):
    from scipy import ndimage

    if cells <= 0:
        return mask
    return ndimage.binary_closing(mask, structure=np.ones((3, 3), dtype=bool),
                                  iterations=int(cells))


def shapely_of(mask, grid):
    """The union of the mask's cell squares, clipped to the image."""
    import shapely

    ys, xs = np.nonzero(mask)
    if not ys.size:
        return None
    s = float(grid["cell_full_px"])
    width, height = grid["image_size"]
    boxes = shapely.box(xs * s, ys * s, np.minimum((xs + 1) * s, width),
                        np.minimum((ys + 1) * s, height))
    return shapely.union_all(boxes)


def to_geojson(shape, *, simplify_px, max_vertices=MAX_VERTICES, min_area_px=0.0):
    """GeoJSON of a shapely (Multi)Polygon, simplified to `max_vertices`."""
    from shapely.geometry import MultiPolygon, Polygon, mapping

    from plexora.plugins.roi.server import geometry as roi_geometry

    if shape is None or shape.is_empty:
        return None
    shape = shape.buffer(0)
    tolerance = float(simplify_px)
    simplified = shape.simplify(tolerance, preserve_topology=True) if tolerance > 0 else shape
    for _ in range(12):
        if _vertices(simplified) <= max_vertices:
            break
        tolerance *= 1.5
        simplified = shape.simplify(tolerance, preserve_topology=True)
    parts = [p for p in (simplified.geoms if isinstance(simplified, MultiPolygon)
                         else [simplified]) if isinstance(p, Polygon) and p.area > min_area_px]
    if not parts:
        return None
    geometry = MultiPolygon(parts) if len(parts) > 1 else parts[0]
    geojson = mapping(geometry)
    geojson = {"type": geojson["type"], "coordinates": _lists(geojson["coordinates"])}
    return roi_geometry.validate_geometry(geojson, max_vertices=max_vertices * 2)


def _lists(value):
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (int, float)):
            return [round(float(v), 2) for v in value]
        return [_lists(v) for v in value]
    return value


def _vertices(shape):
    from shapely.geometry import MultiPolygon

    polygons = shape.geoms if isinstance(shape, MultiPolygon) else [shape]
    total = 0
    for polygon in polygons:
        total += len(polygon.exterior.coords) + sum(len(r.coords) for r in polygon.interiors)
    return total


def mask_to_geometry(mask, grid, *, dilate_cells=0, closing_cells=0, simplify_px=None,
                     max_vertices=MAX_VERTICES):
    mask = _close(_dilate(mask.astype(bool), dilate_cells), closing_cells)
    shape = shapely_of(mask, grid)
    return to_geojson(shape, simplify_px=simplify_px if simplify_px is not None
                      else 0.35 * float(grid["cell_full_px"]), max_vertices=max_vertices)


def geometry_variants(mask, grid, *, pixel_um=None) -> dict:
    """{name: {geometry, area_px2, area_um2, cells, vertices}} of the outlines
    a localisation packet offers. `standard` (one cell of margin) is what an
    ROI is written with unless the agent chose otherwise."""
    from shapely.geometry import box, shape as to_shape

    out = {}
    specs = {"tight": (0, 0), "standard": (1, 0), "generous": (2, 1)}
    for name, (dilate, closing) in specs.items():
        variant = _close(_dilate(mask, dilate), closing)
        geometry = to_geojson(shapely_of(variant, grid),
                              simplify_px=0.35 * float(grid["cell_full_px"]))
        if geometry is None:
            continue
        out[name] = _describe(geometry, int(variant.sum()), pixel_um)
    if "standard" in out:
        hull = to_shape(out["standard"]["geometry"]).convex_hull
        geometry = to_geojson(hull, simplify_px=0)
        if geometry is not None:
            out["hull"] = _describe(geometry, None, pixel_um)
        bounds = to_shape(out["tight"]["geometry"] if "tight" in out
                          else out["standard"]["geometry"]).bounds
        geometry = to_geojson(box(*bounds), simplify_px=0)
        out["bbox"] = _describe(geometry, None, pixel_um)
    return out


def _describe(geometry, cells, pixel_um):
    from shapely.geometry import shape as to_shape

    area = float(to_shape(geometry).area)
    from plexora.plugins.roi.server import geometry as roi_geometry

    return {"geometry": geometry, "area_px2": area,
            "area_um2": area * pixel_um * pixel_um if pixel_um else None,
            "cells": cells, "vertices": roi_geometry.vertex_count(geometry)}


def geometry_hash(geometry) -> str:
    """A stable hash of a geometry, coordinates to 3 decimals: how a user's
    edit to a QC ROI is told from the shape QC wrote."""
    import hashlib
    import json

    def canon(value):
        if isinstance(value, (list, tuple)):
            if value and isinstance(value[0], (int, float)):
                return [round(float(v), 3) for v in value]
            return [canon(v) for v in value]
        return value

    body = {"type": (geometry or {}).get("type"),
            "coordinates": canon((geometry or {}).get("coordinates"))}
    return hashlib.sha1(json.dumps(body, separators=(",", ":")).encode("utf-8")).hexdigest()


# -- the grid fallback -------------------------------------------------------------------


def _square_label(row, column):
    return f"{string.ascii_uppercase[row]}{column + 1}"


def localisation_grid(mask, grid, *, side=8, pad_cells=2) -> dict:
    """A labelled grid over the candidate's box: squares A1..H8, each with its
    full-resolution bounds and the map cells it covers."""
    ys, xs = np.nonzero(mask)
    ny, nx = grid["shape"]
    if not ys.size:
        y0, x0, y1, x1 = 0, 0, ny, nx
    else:
        y0 = max(0, int(ys.min()) - pad_cells)
        x0 = max(0, int(xs.min()) - pad_cells)
        y1 = min(ny, int(ys.max()) + 1 + pad_cells)
        x1 = min(nx, int(xs.max()) + 1 + pad_cells)
    rows = min(side, y1 - y0)
    columns = min(side, x1 - x0)
    edges_y = np.linspace(y0, y1, rows + 1)
    edges_x = np.linspace(x0, x1, columns + 1)
    s = float(grid["cell_full_px"])
    width, height = grid["image_size"]
    squares = []
    for r in range(rows):
        for c in range(columns):
            cy0, cy1 = int(round(edges_y[r])), int(round(edges_y[r + 1]))
            cx0, cx1 = int(round(edges_x[c])), int(round(edges_x[c + 1]))
            squares.append({"id": _square_label(r, c), "row": r, "column": c,
                            "cells": [cy0, cx0, cy1, cx1],
                            "bounds": [cx0 * s, cy0 * s, min(width, cx1 * s),
                                       min(height, cy1 * s)],
                            "covered": float(mask[cy0:cy1, cx0:cx1].mean())
                            if (cy1 > cy0 and cx1 > cx0) else 0.0})
    return {"rows": rows, "columns": columns, "region_cells": [y0, x0, y1, x1],
            "square_px": [(edges_x[1] - edges_x[0]) * s if columns else 0,
                          (edges_y[1] - edges_y[0]) * s if rows else 0],
            "squares": squares}


def rebuild_from_grid(spec, include, shape, tissue=None):
    """The map mask of the squares named, within the tissue."""
    mask = np.zeros(tuple(shape), dtype=bool)
    wanted = set(include)
    for square in spec["squares"]:
        if square["id"] in wanted:
            y0, x0, y1, x1 = square["cells"]
            mask[y0:y1, x0:x1] = True
    if tissue is not None:
        mask &= tissue
    return mask
