"""A marker's display window anchored on its negative cells, read where they are drawn.

The overview window (`calibration.channel_window`) is a percentile of a coarse
pyramid level's pixels. Applied to level-0 crops it saturates: a coarse level
averages a membrane ring with the dark cytoplasm beside it, so its p99.5 sits
well under what a ring's own pixels reach.

Here the window comes from the cells themselves, at the level the collage
reads (level 0), and from the NEGATIVE cells only -- the one population every
marker has and no artifact owns:

- `low`: the median pixel of clearly negative cells (a band of the marker's
  distribution well below the middle), so background draws dark;
- `noise_p99`: how bright background gets -- each cell's own p99, the median
  across cells (a speck of debris owns a pooled percentile, and the cells it
  falls in; it does not own the median cell);
- `high`: `low` plus `spans` times (`noise_p99` - `low`): a window measured
  in units of the background's own spread, so a cell is drawn bright by how
  far it stands out of background, whatever the camera's offset. Anchoring
  white on the brightest cells instead (an earlier rule) let a few bright
  cells -- or a fold, or an antibody aggregate -- set the scale, and a cell
  at a human's gate drew at 1-4 % of white: black.

Pixels are read in a disc of the typical cell's diameter (`crops.
CELL_DIAMETER_UM`) around each cell's table centroid, never inside its mask:
a project may have no mask, and a mask may be a bare nucleus, a feathered
one or a Voronoi fill -- the disc means the same thing on all of them.

QC-aware: cells QC left out of estimation (`cell_exclusions`) are never
anchor cells. Deterministic: cells are picked by a seeded draw over the
sorted band, and the seed is recorded with the window.
"""

from __future__ import annotations

import numpy as np

#: [cal] which cells anchor the window, and how wide it is drawn: `spans`
#: background spreads (noise p99 - median) from black to white.
CELL_WINDOW = {"negative_pct": (30, 50), "n_each": 24, "noise_pct": 99, "spans": 10.0,
               "min_cells": 200}
#: The pyramid level the crops are read at (the collage's own).
LEVEL = 0
#: The prefix a window from here carries in `window_source` (a calibration).
SOURCE = "calib:cells"
#: A disc must hold at least this many pixels to be read.
MIN_DISC_PX = 8


def select_anchor_cells(values, ids, xs, ys, *, seed=0, spec=CELL_WINDOW):
    """({"negatives": [{cell_id, x, y, value}]}, info), or (None, info) when
    the column cannot anchor a window.

    The body is the column's positive values when it is non-negative with
    enough of them (the calibration's own non-zero rule), else every finite
    value; the band is a percentile range of the body. NaN rows (QC's
    left-out cells) are never in it."""
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
    lo = int(np.floor(spec["negative_pct"][0] / 100.0 * n))
    hi = max(lo + 1, int(np.ceil(spec["negative_pct"][1] / 100.0 * n)))
    members = order[lo:min(hi, n)]
    take = min(int(spec["n_each"]), members.size)
    picked = np.sort(rng.choice(members.size, size=take, replace=False)) if take else []
    negatives = [{"cell_id": int(ids[i]), "x": float(xs[i]), "y": float(ys[i]),
                  "value": float(values[i])} for i in members[picked]]
    info.update(n_negative=len(negatives))
    if not negatives:
        return None, info
    return {"negatives": negatives}, info


def window_from_crops(crops, name, negatives, *, radius_px, spec=CELL_WINDOW,
                      min_contrast=3.0):
    """{low, noise_p99, high, spans, n_negative, radius_px, level} from level-0
    crops (`crops` maps a cell id to a `crops.Crop`), or None."""
    from plexora.agent.evidence import crops as cropmod

    pixels, peaks = [], []
    for cell in negatives:
        crop = crops.get(int(cell["cell_id"]))
        if crop is None:
            continue
        inside = cropmod.in_disc(crop, name, radius_px)
        if inside is not None and inside.size >= MIN_DISC_PX:
            pixels.append(inside)
            peaks.append(float(np.percentile(inside, spec["noise_pct"])))
    if not pixels:
        return None
    low = float(np.median(np.concatenate(pixels)))
    noise = float(np.median(peaks))
    high = low + float(spec["spans"]) * max(noise - low, 0.0)
    high = max(high, min_contrast * max(low, 1e-6))
    if not high > low:
        high = low + 1.0
    return {"low": low, "noise_p99": noise, "high": high, "spans": float(spec["spans"]),
            "n_negative": len(pixels), "radius_px": float(radius_px), "level": LEVEL}


def cell_windows(session, project, channels, *, nuclear=None, seed=0) -> dict:
    """{channel: window record} for the marker channels of `channels` (names
    -> keys) that have a table column; a channel that cannot be anchored is
    simply absent (its overview window stands)."""
    from plexora.agent import cell_exclusions
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
        columns = {}
        for name in channels:
            if name == nuclear:
                continue
            column = vocabulary.match_name(name, markers)
            if column is not None:
                columns[name] = column
        if not columns:
            return {}
        ids, keep = cell_ids(frame, schema.cell_id)
        xs = frame[schema.x].to_numpy().astype(np.float64)[keep]
        ys = frame[schema.y].to_numpy().astype(np.float64)[keep]
        values = ds.table.columns(sorted(set(columns.values())))
        record = session.project(project)
        exclusions = cell_exclusions.current(ds)
        radius, _how = cropmod.cell_radius_px(record)
    except Exception:  # an anchor is a refinement; a window without one is still a window
        return {}
    side = int(np.ceil(2 * radius + 4))
    out = {}
    for name, column in columns.items():
        try:
            raw = np.asarray(values[column], dtype=np.float64)
            column_values = np.asarray(cell_exclusions.masked(ds, column, raw, exclusions),
                                       dtype=np.float64)[keep]
            anchors, info = select_anchor_cells(column_values, ids, xs, ys, seed=seed)
            if anchors is None:
                continue
            crops, _read = cropmod.read_cell_crops(
                session, record, anchors["negatives"], channels={name: channels[name]},
                crop_px=side, tile_px=side, mask=False, level=LEVEL)
            window = window_from_crops(crops, name, anchors["negatives"], radius_px=radius)
            if window is not None:
                out[name] = {**window, "crop_px": side, "seed": int(seed), "column": column}
        except Exception:
            continue
    return out
