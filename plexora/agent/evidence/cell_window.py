"""What a marker's pixels look like in its own cells, read where they are drawn.

The overview window (`calibration.channel_window`) is a percentile of a coarse
pyramid level's pixels. Applied to level-0 crops it saturates: a coarse level
averages a membrane ring with the dark cytoplasm beside it, so its p99.5 sits
well under what a ring's own pixels reach.

Here the measurements come from the cells themselves, at the level the
collage reads (level 0), in bands of the marker's own distribution:

- `negatives` (a band well below the middle): the background every marker
  has and no artifact owns. `background` is their median pixel; `noise_p99`
  how bright background gets (each cell's own p99, the median across cells:
  a speck of debris owns a pooled percentile, not the median cell);
  `black` each cell's own low percentile, the median across cells -- the
  display's black sits there, under the background, so background draws as
  a faint floor rather than clipped to black;
- `low` (the bottom of the distribution): a second background, used when the
  marker is on most cells and the negatives band is itself stained
  (`reanchored`);
- `bright` (the top of the distribution): the `titre`, the median over these
  cells of each one's p99 -- how bright a strongly stained cell's brightest
  structure (a ring, a nucleus, granules) is;
- `near` (between): a dim positive, for the contrast grid.

For the contrast check's second picture (`display_grid.render_spread_strip`)
`cells.spread` holds a few of the bright and the near cells picked spread out
across the image (`_spread`): a bright stain that is debris sits in a few
places, a real one across the tissue, and a dim cell the gate must not miss
may live away from the brightest.

The nuclear stain stains every cell, so it has no background band:
`measure_nuclear` reads how bright its typical and its bright nuclei are,
for the window the viewer draws it at (`calibration.nuclear_view`).

`calibration` turns the measurements into a curve; nothing here decides how
bright anything is drawn.

Pixels are read in a disc of the typical cell's diameter (`crops.
CELL_DIAMETER_UM`) around each cell's table centroid, never inside its mask:
a project may have no mask, and a mask may be a bare nucleus, a feathered
one or a Voronoi fill -- the disc means the same thing on all of them.

QC-aware: cells QC left out of estimation (`cell_exclusions`) are never
picked. Deterministic: cells are picked by a seeded draw over the sorted
band, and the seed is recorded with the measurements.
"""

from __future__ import annotations

import numpy as np

#: [cal] the bands cells are picked from (percentiles of the marker's body),
#: how many from each, and what is read in their discs.
CELL_WINDOW = {"negative_pct": (30, 50), "n_each": 24, "noise_pct": 99, "black_pct": 5,
               "low_pct": (2, 10), "n_low": 12, "near_pct": (85, 92), "n_near": 8,
               "bright_pct": (97, 99.5), "n_bright": 16, "titre_pct": 99,
               "reanchor_share": 0.1,
               # the bright and near cells the spread strip shows
               "spread_bright": 6, "spread_dim": 4,
               "min_cells": 200}
#: The pyramid level the crops are read at (the collage's own).
LEVEL = 0
#: The prefix a window from here carries in `window_source` (a calibration).
SOURCE = "calib:cells"
#: A disc must hold at least this many pixels to be read.
MIN_DISC_PX = 8
#: Fewer bright discs than this: no titre.
MIN_BRIGHT = 4


def _body(values, xs, ys):
    """Sorted indices of the column's body: its positive values when it is
    non-negative with enough of them (the calibration's own non-zero rule),
    else every finite value. NaN rows (QC's left-out cells) never."""
    values = np.asarray(values, dtype=np.float64)
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    ok = np.isfinite(values) & np.isfinite(xs) & np.isfinite(ys)
    if ok.sum() >= 100 and values[ok].min() >= 0 and (values[ok] > 0).sum() >= 100:
        ok &= values > 0
    index = np.flatnonzero(ok)
    return index[np.argsort(values[index], kind="stable")]


def _band(order, pct, take, rng):
    n = order.size
    lo = int(np.floor(pct[0] / 100.0 * n))
    hi = max(lo + 1, int(np.ceil(pct[1] / 100.0 * n)))
    members = order[lo:min(hi, n)]
    take = min(int(take), members.size)
    if not take:
        return members[:0]
    return members[np.sort(rng.choice(members.size, size=take, replace=False))]


def _cells(index, ids, xs, ys, values):
    return [{"cell_id": int(ids[i]), "x": float(xs[i]), "y": float(ys[i]),
             "value": float(values[i])} for i in index]


def select_anchor_cells(values, ids, xs, ys, *, seed=0, spec=CELL_WINDOW):
    """({"negatives": [{cell_id, x, y, value}]}, info), or (None, info) when
    the column cannot anchor a window: fewer than `min_cells` in its body."""
    values = np.asarray(values, dtype=np.float64)
    ids = np.asarray(ids)
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    order = _body(values, xs, ys)
    info = {"n_body": int(order.size), "seed": int(seed)}
    if order.size < spec["min_cells"]:
        return None, info
    rng = np.random.default_rng(seed)
    negatives = _cells(_band(order, spec["negative_pct"], spec["n_each"], rng),
                       ids, xs, ys, values)
    info.update(n_negative=len(negatives))
    if not negatives:
        return None, info
    return {"negatives": negatives}, info


def select_display_cells(values, ids, xs, ys, *, seed=0, spec=CELL_WINDOW):
    """({negatives, low, near, bright}, info), each a list of {cell_id, x, y,
    value}; (None, info) when the column cannot anchor (`select_anchor_cells`,
    whose negatives these are). Each band has its own seeded draw."""
    anchors, info = select_anchor_cells(values, ids, xs, ys, seed=seed, spec=spec)
    if anchors is None:
        return None, info
    values = np.asarray(values, dtype=np.float64)
    ids = np.asarray(ids)
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    order = _body(values, xs, ys)
    out = {"negatives": anchors["negatives"]}
    for offset, (band, pct, take) in enumerate((("low", "low_pct", "n_low"),
                                                ("near", "near_pct", "n_near"),
                                                ("bright", "bright_pct", "n_bright"))):
        rng = np.random.default_rng([int(seed), offset + 1])
        out[band] = _cells(_band(order, spec[pct], spec[take], rng), ids, xs, ys, values)
    info.update({f"n_{band}": len(out[band]) for band in ("low", "near", "bright")})
    return out, info


def _discs(crops, name, cells, radius_px):
    """[(cell, pixels)] for the cells whose disc could be read."""
    from plexora.agent.evidence import crops as cropmod

    out = []
    for cell in cells or []:
        crop = crops.get(int(cell["cell_id"]))
        if crop is None:
            continue
        inside = cropmod.in_disc(crop, name, radius_px)
        if inside is not None and inside.size >= MIN_DISC_PX:
            out.append((cell, inside))
    return out


def _background(discs, spec):
    pooled = np.concatenate([px for _cell, px in discs])
    return {"background": float(np.median(pooled)),
            "noise_p99": float(np.median([np.percentile(px, spec["noise_pct"])
                                          for _cell, px in discs])),
            "black": float(np.median([np.percentile(px, spec["black_pct"])
                                      for _cell, px in discs])),
            "n": len(discs)}


def _stained(negatives, low, titre, spec):
    """True when the negatives band is itself stained: its median pixel stands
    more than one background gap above the low band's noise p99, and by at
    least `reanchor_share` of the way from the low band's black to the titre
    (a flat or noiseless background differs from the low band by nothing)."""
    gap = max(low["noise_p99"] - low["black"], 0.0)
    above = negatives["background"] - low["noise_p99"]
    if not above > gap:
        return False
    if titre is None or not titre > low["black"]:
        return True
    return negatives["background"] - low["black"] > spec["reanchor_share"] * (titre - low["black"])


def _closest(discs, measure_of, target, k):
    ranked = sorted(discs, key=lambda item: (abs(float(measure_of(item[1])) - target),
                                             int(item[0]["cell_id"])))
    return [dict(cell) for cell, _px in ranked[:k]]


def _spread(discs, measure_of, target, k):
    """`k` of the discs' cells spread out in space: the one whose measure is
    nearest `target` first, then each time the cell farthest from those
    already taken (ties by cell id). Deterministic for the same discs."""
    if not discs or k <= 0:
        return []
    ranked = sorted(discs, key=lambda item: (abs(float(measure_of(item[1])) - target),
                                             int(item[0]["cell_id"])))
    taken = [ranked[0][0]]
    rest = [cell for cell, _px in ranked[1:]]
    while rest and len(taken) < k:
        def gap(cell):
            return min((cell["x"] - t["x"]) ** 2 + (cell["y"] - t["y"]) ** 2 for t in taken)
        best = max(rest, key=lambda cell: (gap(cell), -int(cell["cell_id"])))
        taken.append(best)
        rest.remove(best)
    return [dict(cell) for cell in taken]


def measure(crops, name, bands, *, radius_px, spec=CELL_WINDOW):
    """The marker's measurements from level-0 crops (`crops` maps a cell id to
    a `crops.Crop`; `bands` is `select_display_cells`'s, or just its
    negatives), or None when no negative disc could be read:

    {black, background, noise_p99, band, reanchored, titre, n_negative,
     n_bright, radius_px, level, cells: {negative, near, bright, spread},
     low_band?}

    `titre` is None with fewer than MIN_BRIGHT bright discs. A negatives band
    whose median pixel is above the low band's noise p99 is stained itself
    (a marker on most cells): the low band is the background instead.
    Otherwise `low_band` holds the low band's own {black, background,
    noise_p99, titre}, for a vision check that finds the negatives stained."""
    negatives = _discs(crops, name, bands.get("negatives"), radius_px)
    if not negatives:
        return None
    background = _background(negatives, spec)
    band, reanchored, low_band = "negatives", False, None
    bright = _discs(crops, name, bands.get("bright"), radius_px)
    titre = None
    if len(bright) >= MIN_BRIGHT:
        titre = float(np.median([np.percentile(px, spec["titre_pct"]) for _c, px in bright]))
    low = _discs(crops, name, bands.get("low"), radius_px)
    if len(low) >= 3:
        low_background = _background(low, spec)
        if _stained(background, low_background, titre, spec):
            background, band, reanchored = low_background, "low", True
        else:
            low_band = low_background
    near = _discs(crops, name, bands.get("near"), radius_px)
    near_mid = float(np.median([np.median(px) for _c, px in near])) if near else 0.0
    cells = {
        "negative": _closest(low if reanchored else negatives, np.median,
                             background["background"], 1),
        "near": _closest(near, np.median, near_mid, 1),
        "bright": _closest(bright, lambda px: np.percentile(px, spec["titre_pct"]),
                           titre if titre is not None else 0.0, 2),
        "spread": {"bright": _spread(bright, lambda px: np.percentile(px, spec["titre_pct"]),
                                     titre if titre is not None else 0.0,
                                     spec["spread_bright"]),
                   "dim": _spread(near, np.median, near_mid, spec["spread_dim"])}}
    out = {"black": min(background["black"], background["background"]),
           "background": background["background"], "noise_p99": background["noise_p99"],
           "band": band, "reanchored": reanchored, "titre": titre,
           "n_negative": background["n"], "n_bright": len(bright),
           "radius_px": float(radius_px), "level": LEVEL, "cells": cells}
    if low_band is not None:
        out["low_band"] = {"black": min(low_band["black"], low_band["background"]),
                           "background": low_band["background"],
                           "noise_p99": low_band["noise_p99"], "titre": titre}
    return out


def measure_nuclear(crops, name, bands, *, radius_px, spec=CELL_WINDOW):
    """The nuclear stain's measurements from level-0 crops of its own cells,
    banded on its table column (`select_display_cells`), or None when no
    typical disc could be read:

    {black, typical, titre, n_typical, n_bright, radius_px, level}

    Every cell is stained, so there is no negative band: the band below the
    middle holds `typical` nuclei, and `typical` is their brightest
    structure (each disc's p99, the median across cells); `black` is the
    discs' low percentile (the gaps between nuclei); `titre` the bright
    band's p99, None with fewer than MIN_BRIGHT bright discs."""
    typical = _discs(crops, name, bands.get("negatives"), radius_px)
    if not typical:
        return None
    bright = _discs(crops, name, bands.get("bright"), radius_px)
    titre = None
    if len(bright) >= MIN_BRIGHT:
        titre = float(np.median([np.percentile(px, spec["titre_pct"]) for _c, px in bright]))
    return {"black": float(np.median([np.percentile(px, spec["black_pct"])
                                      for _c, px in typical])),
            "typical": float(np.median([np.percentile(px, spec["noise_pct"])
                                        for _c, px in typical])),
            "titre": titre, "n_typical": len(typical), "n_bright": len(bright),
            "radius_px": float(radius_px), "level": LEVEL}


def cell_windows(session, project, channels, *, nuclear=None, seed=0) -> dict:
    """{channel: measurements} (`measure`, plus crop_px, seed and column) for
    the marker channels of `channels` (names -> keys) that have a table
    column, and the nuclear stain's (`measure_nuclear`) when it has one; a
    channel that cannot be anchored is simply absent (its overview window
    stands)."""
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
            bands, info = select_display_cells(column_values, ids, xs, ys, seed=seed)
            if bands is None:
                continue
            if name == nuclear:
                # Its typical and bright nuclei only: there is no background band.
                bands = {"negatives": bands["negatives"], "bright": bands["bright"]}
            # One read for every band: a few dozen cells per marker.
            wanted = list({int(c["cell_id"]): c for band in bands.values()
                           for c in band}.values())
            crops, _read = cropmod.read_cell_crops(
                session, record, wanted, channels={name: channels[name]},
                crop_px=side, tile_px=side, mask=False, level=LEVEL)
            measured = (measure_nuclear if name == nuclear else measure)(
                crops, name, bands, radius_px=radius)
            if measured is not None:
                out[name] = {**measured, "crop_px": side, "seed": int(seed), "column": column}
        except Exception:
            continue
    return out
