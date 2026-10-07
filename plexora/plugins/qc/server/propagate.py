"""From QC regions to cells: which cells a region covers, and by how much.

A cell belongs to a region when enough of its segmentation mask lies inside
the polygon (`cells.roi_overlap_fraction` of its area, holes honoured) --
membership by overlap, because a cell half in a fold is half unreadable, and
a centroid says nothing about that. Without a mask (or when a region is too
large to read within the pixel budget) the rule falls back to the centroid,
and says so (`method`). The fraction of every overlapping cell is kept, so a
strictness change re-derives membership without reading the mask again. A
cell in several regions is listed under each; it is never counted twice.
"""

from __future__ import annotations

import math

import numpy as np
import polars as pl

#: [cal] the largest region read (level pixels), and all regions together.
MAX_ROI_PIXELS = 16_000_000
MAX_TOTAL_PIXELS = 400_000_000
BLOCK_PX = 2048


def _shape(geometry):
    from shapely.geometry import shape

    return shape(geometry).buffer(0)


def _ids_and_xy(ds):
    from plexora.server.utils.label_overlay import cell_ids

    frame = ds.table.geometry()
    schema = ds.schema
    ids, keep = cell_ids(frame, schema.cell_id if schema else None)
    xs = frame[schema.x].to_numpy().astype(np.float64)[keep]
    ys = frame[schema.y].to_numpy().astype(np.float64)[keep]
    return ids.astype(np.int64), xs, ys


def _mask_provider(ds):
    """The mask's provider, opened once and shared with the renderer
    (`render._mask_for`): a SegHandle builds -- and opens -- a new one on
    every read."""
    from plexora.agent import render

    record = ds.project
    if not record.segmentation.available:
        return None, "no segmentation mask"
    provider, status, reason, _locator = render._mask_for(record)
    if provider is None:
        return None, reason or f"the mask is {status}"
    return provider, None


def _fill(shape, box, level, size):
    """The polygon rasterised over a level box: 1 inside, 0 in holes."""
    import cv2
    from shapely.geometry import MultiPolygon

    div = 2 ** level
    canvas = np.zeros(size, dtype=np.uint8)
    polygons = shape.geoms if isinstance(shape, MultiPolygon) else [shape]

    def ring(coords):
        points = np.asarray(coords, dtype=np.float64)
        points = points / div - np.array([box[0], box[1]])
        return np.round(points).astype(np.int32).reshape(-1, 1, 2)

    for polygon in polygons:
        if polygon.is_empty:
            continue
        cv2.fillPoly(canvas, [ring(polygon.exterior.coords)], 1)
        for hole in polygon.interiors:
            cv2.fillPoly(canvas, [ring(hole.coords)], 0)
    return canvas.astype(bool)


def propagate(ds, regions, *, median_diameter_px=None, budget=MAX_TOTAL_PIXELS):
    """(pairs DataFrame(cell_id, roi_id, fraction, method), per_roi) for the
    regions [{roi_id, geometry, ...}]."""
    import shapely

    ids, xs, ys = _ids_and_xy(ds)
    provider, why = _mask_provider(ds)
    record = ds.project
    extra = int(getattr(record.segmentation, "extra_levels", 0) or 0)
    width, height = record.image.width or 0, record.image.height or 0
    pad = 2 * float(median_diameter_px) if median_diameter_px else 64.0
    rows_cell, rows_roi, rows_fraction, rows_method = [], [], [], []
    per_roi = {}
    spent = 0
    known = np.unique(ids)  # sorted: `isin` against it, once, not a list per region
    for region in regions:
        shape = _shape(region["geometry"])
        if shape.is_empty:
            continue
        roi_id = region["roi_id"]
        x0, y0, x1, y1 = shape.bounds
        x0, y0 = max(0.0, x0 - pad), max(0.0, y0 - pad)
        x1, y1 = min(float(width or x1 + pad), x1 + pad), min(float(height or y1 + pad),
                                                              y1 + pad)
        area = (x1 - x0) * (y1 - y0)
        level = 0
        while area / (4 ** level) > MAX_ROI_PIXELS:
            level += 1
        cost = area / (4 ** level)
        method = "mask" if provider is not None and spent + cost <= budget else "centroid"
        if method == "mask":
            try:
                inside, total = _overlap(provider, shape, (x0, y0, x1, y1), level, extra)
            except Exception as exc:
                method, why = "centroid", f"mask read failed: {exc}"
            else:
                spent += cost
                labels = np.nonzero(inside)[0]
                fraction = inside[labels] / np.maximum(1, total[labels])
                keep = np.isin(labels, known) if known.size else np.zeros(0, bool)
                labels, fraction = labels[keep], fraction[keep]
                rows_cell.extend(labels.tolist())
                rows_roi.extend([roi_id] * labels.size)
                rows_fraction.extend(fraction.astype(np.float32).tolist())
                rows_method.extend(["mask"] * labels.size)
                per_roi[roi_id] = {"method": "mask", "level": level, "n_cells": int(labels.size),
                                   "pixels_read": int(cost)}
                continue
        inside = np.asarray(shapely.contains_xy(shape, xs, ys), dtype=bool)
        chosen = ids[inside]
        rows_cell.extend(chosen.tolist())
        rows_roi.extend([roi_id] * chosen.size)
        rows_fraction.extend([1.0] * chosen.size)
        rows_method.extend(["centroid"] * chosen.size)
        per_roi[roi_id] = {"method": "centroid", "n_cells": int(chosen.size),
                           "reason": why or "over the pixel budget"}
    pairs = pl.DataFrame({"cell_id": pl.Series(rows_cell, dtype=pl.Int64),
                          "roi_id": pl.Series(rows_roi, dtype=pl.Utf8),
                          "fraction": pl.Series(rows_fraction, dtype=pl.Float32),
                          "method": pl.Series(rows_method, dtype=pl.Utf8)})
    if pairs.height:
        pairs = pairs.unique(subset=["cell_id", "roi_id"], keep="first", maintain_order=True)
    return pairs, per_roi


#: Labels up to this are counted with one `bincount` over the label range (no
#: sort); a mask with larger ids falls back to `np.unique`. Either way the
#: counts are the same.
DENSE_LABEL_MAX = 1 << 26


def _overlap(provider, shape, box, level, extra):
    """(inside, total) pixel counts per label within `box` at `level`."""
    div = 2 ** level
    lx0, ly0 = int(math.floor(box[0] / div)), int(math.floor(box[1] / div))
    lx1, ly1 = int(math.ceil(box[2] / div)), int(math.ceil(box[3] / div))
    inside_counts, total_counts = {}, {}
    dense_total = dense_inside = None
    for by in range(ly0, ly1, BLOCK_PX):
        for bx in range(lx0, lx1, BLOCK_PX):
            block = (bx, by, min(lx1, bx + BLOCK_PX), min(ly1, by + BLOCK_PX))
            labels = np.asarray(provider.read_region(level + extra, block,
                                                     max_pixels=BLOCK_PX * BLOCK_PX))
            if labels.ndim > 2:
                labels = labels.reshape(labels.shape[-2:])
            h, w = labels.shape
            fill = _fill(shape, block, level, (h, w))
            flat = labels.ravel().astype(np.int64)
            if not flat.size:
                continue
            low, high = int(flat.min()), int(flat.max())
            if low >= 0 and high <= DENSE_LABEL_MAX:
                # A sort per 4-megapixel block was most of the time: count
                # over the label range instead.
                total = np.bincount(flat, minlength=high + 1)
                inner = np.bincount(flat[fill.ravel()], minlength=high + 1)
                if dense_total is None or dense_total.size < total.size:
                    grown = np.zeros(total.size, dtype=np.int64)
                    grown_in = np.zeros(total.size, dtype=np.int64)
                    if dense_total is not None:
                        grown[:dense_total.size] = dense_total
                        grown_in[:dense_inside.size] = dense_inside
                    dense_total, dense_inside = grown, grown_in
                dense_total[:total.size] += total
                dense_inside[:inner.size] += inner
                continue
            present, index = np.unique(flat, return_inverse=True)
            total = np.bincount(index, minlength=present.size)
            inner = np.bincount(index, weights=fill.ravel().astype(np.float64),
                                minlength=present.size)
            for label, t, i in zip(present.tolist(), total.tolist(), inner.tolist()):
                if label == 0:
                    continue
                total_counts[label] = total_counts.get(label, 0) + t
                if i:
                    inside_counts[label] = inside_counts.get(label, 0) + i
    top = max(total_counts) if total_counts else 0
    if dense_total is not None:
        dense_total[0] = dense_inside[0] = 0  # the background
        counted = np.flatnonzero(dense_total)
        if counted.size:
            top = max(top, int(counted[-1]))
        size = top + 1
        inside = np.zeros(size, dtype=np.float64)
        total = np.zeros(size, dtype=np.float64)
        n = min(size, dense_total.size)
        total[:n] = dense_total[:n]
        inside[:n] = dense_inside[:n]
        for label, count in total_counts.items():
            total[label] += count
        for label, count in inside_counts.items():
            inside[label] += count
        return inside, total
    size = (max(total_counts) + 1) if total_counts else 1
    inside = np.zeros(size, dtype=np.float64)
    total = np.zeros(size, dtype=np.float64)
    for label, count in total_counts.items():
        total[label] = count
    for label, count in inside_counts.items():
        inside[label] = count
    return inside, total
