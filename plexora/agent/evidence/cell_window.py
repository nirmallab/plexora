"""A marker's display window anchored on its cells, read where they are drawn.

The overview window (`calibration.channel_window`) is a percentile of a coarse
pyramid level's pixels. Applied to level-0 crops it saturates: a coarse level
averages a membrane ring with the dark cytoplasm beside it, so its p99.5 sits
well under what a ring's own pixels reach, and every positive cell draws as a
white blob -- the ring, the one thing that says "membrane", is gone.

Here the window comes from the cells themselves, at the level the collage
reads (level 0):

- `low`: the median in-mask pixel of clearly negative cells (a band of the
  marker's distribution well below the middle), so background draws dark but
  not black;
- `high`: across clearly positive cells (the top of the distribution), each
  cell's own p90 in-mask pixel, then the median across cells. Not a pooled
  percentile of every positive pixel: a few bright specks inside cells
  (debris, a hot pixel cluster) would own a pooled p99 -- and the cells that
  carry specks read brightest in the table, so they crowd the top band; one
  cell's p90 ignores a speck covering a few percent of it, and the median
  ignores the cells a speck covers more of. A membrane ring, a fifth or more
  of a nucleus-based mask, still reaches a cell's p90.

Deterministic: cells are picked by a seeded draw over the sorted band, and
the seed is recorded with the window.
"""

from __future__ import annotations

import numpy as np

#: [cal] which cells anchor the window, and how their pixels are reduced.
CELL_WINDOW = {"negative_pct": (30, 50), "positive_pct": (97, 99.5), "n_each": 24,
               "cell_pixel_pct": 90, "across_cells_pct": 50, "min_cells": 200}
#: The pyramid level the crops are read at (the collage's own).
LEVEL = 0
#: The prefix a window from here carries in `window_source` (a calibration).
SOURCE = "calib:cells"
#: A cell's mask must cover at least this many crop pixels to be used alone.
MIN_MASK_PX = 8
#: Crop side, full-resolution pixels: a cell and a margin.
CROP_PX = 32


def select_anchor_cells(values, ids, xs, ys, *, seed=0, spec=CELL_WINDOW):
    """({"negatives": [...], "positives": [...]}, info) of `{cell_id, x, y,
    value}`, or (None, info) when the column cannot anchor a window.

    The body is the column's positive values when it is non-negative with
    enough of them (the calibration's own non-zero rule), else every finite
    value; the bands are percentiles of the body."""
    values = np.asarray(values, dtype=np.float64)
    ids = np.asarray(ids)
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    ok = np.isfinite(values) & np.isfinite(xs) & np.isfinite(ys)
    if ok.sum() >= 100 and values[ok].min() >= 0 and (values[ok] > 0).sum() >= 100:
        ok &= values > 0
    index = np.flatnonzero(ok)
    info = {"n_body": int(index.size), "seed": int(seed)}
    if index.size < spec["min_cells"]:
        return None, info
    order = index[np.argsort(values[index], kind="stable")]
    n = order.size
    rng = np.random.default_rng(seed)

    def band(pct):
        lo = int(np.floor(pct[0] / 100.0 * n))
        hi = max(lo + 1, int(np.ceil(pct[1] / 100.0 * n)))
        members = order[lo:min(hi, n)]
        take = min(int(spec["n_each"]), members.size)
        picked = np.sort(rng.choice(members.size, size=take, replace=False)) if take else []
        return [{"cell_id": int(ids[i]), "x": float(xs[i]), "y": float(ys[i]),
                 "value": float(values[i])} for i in members[picked]]

    negatives, positives = band(spec["negative_pct"]), band(spec["positive_pct"])
    info.update(n_negative=len(negatives), n_positive=len(positives))
    if not negatives or not positives:
        return None, info
    return {"negatives": negatives, "positives": positives}, info


def _in_mask(crop, name):
    plane = crop.planes.get(name)
    if plane is None or not plane.size:
        return None, False
    if crop.labels is not None:
        own = crop.labels == crop.cell_id
        if int(own.sum()) >= MIN_MASK_PX:
            return plane[own], True
    return plane.ravel(), False


def window_from_crops(crops, name, negatives, positives, *, spec=CELL_WINDOW,
                      min_contrast=3.0):
    """{low, high, n_negative, n_positive, masked, level} from level-0 crops
    (`crops` maps a cell id to a `crops.Crop`), or None."""
    neg_pixels, masked = [], 0
    for cell in negatives:
        crop = crops.get(int(cell["cell_id"]))
        if crop is None:
            continue
        pixels, used_mask = _in_mask(crop, name)
        if pixels is not None and pixels.size:
            neg_pixels.append(pixels)
            masked += int(used_mask)
    peaks = []
    for cell in positives:
        crop = crops.get(int(cell["cell_id"]))
        if crop is None:
            continue
        pixels, used_mask = _in_mask(crop, name)
        if pixels is not None and pixels.size:
            peaks.append(float(np.percentile(pixels, spec["cell_pixel_pct"])))
            masked += int(used_mask)
    if not neg_pixels or not peaks:
        return None
    low = float(np.median(np.concatenate(neg_pixels)))
    high = float(np.percentile(np.asarray(peaks), spec["across_cells_pct"]))
    high = max(high, min_contrast * max(low, 1e-6))
    if not high > low:
        high = low + 1.0
    return {"low": low, "high": high, "n_negative": len(neg_pixels), "n_positive": len(peaks),
            "masked": masked, "level": LEVEL}


def cell_windows(session, project, channels, *, nuclear=None, seed=0) -> dict:
    """{channel: window record} for the marker channels of `channels` (names
    -> keys) that have a table column; a channel that cannot be anchored is
    simply absent (its overview window stands)."""
    from plexora.agent.evidence import crops as cropmod
    from plexora.ai import vocabulary
    from plexora.server.utils.label_overlay import cell_ids

    try:
        ds = session.data(project)
        schema = ds.schema
        frame = ds.table.geometry()
        if frame is None or schema is None or schema.x not in frame.columns \
                or schema.y not in frame.columns:
            return {}
        markers = list(ds.table.markers)
        folded = {vocabulary.fold(m): m for m in markers}
        columns = {}
        for name in channels:
            if name == nuclear:
                continue
            column = name if name in markers else folded.get(vocabulary.fold(name))
            if column is not None:
                columns[name] = column
        if not columns:
            return {}
        ids, keep = cell_ids(frame, schema.cell_id)
        xs = frame[schema.x].to_numpy().astype(np.float64)[keep]
        ys = frame[schema.y].to_numpy().astype(np.float64)[keep]
        values = ds.table.columns(sorted(set(columns.values())))
        record = session.project(project)
    except Exception:  # an anchor is a refinement; a window without one is still a window
        return {}
    out = {}
    for name, column in columns.items():
        try:
            column_values = np.asarray(values[column], dtype=np.float64)[keep]
            anchors, info = select_anchor_cells(column_values, ids, xs, ys, seed=seed)
            if anchors is None:
                continue
            cells = anchors["negatives"] + anchors["positives"]
            crops, _read = cropmod.read_cell_crops(
                session, record, cells, channels={name: channels[name]}, crop_px=CROP_PX,
                tile_px=CROP_PX, mask=True, level=LEVEL)
            window = window_from_crops(crops, name, anchors["negatives"],
                                       anchors["positives"])
            if window is not None:
                out[name] = {**window, "crop_px": CROP_PX, "seed": int(seed),
                             "column": column}
        except Exception:
            continue
    return out
