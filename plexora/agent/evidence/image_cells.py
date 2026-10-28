"""Cells found in the image itself, for a project with no cell table.

A contrast check shows the model a marker's own cells -- a negative, a dim
positive, two bright ones -- picked from bands of the marker's distribution
over the cells (`cell_window.select_display_cells`). With a table the cells
and their values are its rows. Without one they are found here, in a few
fields of the image read at the coarsest level where a typical cell is still
`min_diameter_px` across:

- where the cells are: the segmentation mask's labels (each label's
  centroid), or with no mask, the nuclei of the nuclear stain (the peaks of
  the smoothed stain above its Otsu level). Every cell has a nucleus, so the
  negatives band is still there to anchor the background on;
- how bright each is in a channel: the channel's mean in a disc of the
  typical cell's diameter around it (`crops.CELL_DIAMETER_UM`) -- the disc
  `cell_window` then reads at level 0, so the ranking and the measurement
  mean the same thing.

The fields tile the image without overlap and are a systematic sample of the
tissue tiles (the nuclear overview's `image_qc.tissue_mask`), so a few
thousand cells carry the whole image's percentiles in expectation. Only the
bands' order comes from here: every number a window is set from is still
read at level 0, by `cell_window.measure`. Deterministic for a seed. A
project with neither a mask nor a nuclear stain has no cells to find (None),
and the calibration's overview window stands.
"""

from __future__ import annotations

import math

import numpy as np

#: [cal] the fields read (`field_px` square at the field level, at most
#: `n_fields`, a tile counting as tissue from `min_tissue` of its area), the
#: level (the coarsest where a typical cell is `min_diameter_px` across),
#: a nucleus's diameter for finding one, and the smallest mask label kept (a
#: share of the typical cell's disc: a sliver at a field's edge is not a cell).
IMAGE_CELLS = {"field_px": 512, "n_fields": 16, "min_tissue": 0.5, "min_diameter_px": 10.0,
               "nucleus_um": 6.0, "min_label_share": 0.1}
#: How the cells were found (`sample`'s `source`).
SOURCE_MASK = "mask"
SOURCE_NUCLEI = "nuclei"
#: A nucleus's id within its field: cells found in field `i` are numbered
#: from `i * ID_STRIDE + 1` (there is no table id to give them).
ID_STRIDE = 1_000_000


def field_level(levels, radius_px, spec=IMAGE_CELLS) -> int:
    """The coarsest of `levels` at which a cell of `radius_px` (full
    resolution) is still `min_diameter_px` across."""
    level = 0
    while level + 1 < levels and 2 * radius_px / 2 ** (level + 1) >= spec["min_diameter_px"]:
        level += 1
    return level


def fields(shape, level, tissue=None, tissue_level=None, *, seed=0, spec=IMAGE_CELLS) -> list:
    """[(x0, y0, x1, y1)] at `level` (whose (height, width) is `shape`): up to
    `n_fields` tiles of a `field_px` tiling of the image, a systematic sample
    of those that are tissue (`tissue`, a mask at `tissue_level`) from a
    seeded start -- spread across the tissue in raster order, never
    overlapping. Without a tissue mask every tile is a candidate."""
    height, width = int(shape[0]), int(shape[1])
    side = int(spec["field_px"])
    tiles = [(x, y, min(x + side, width), min(y + side, height))
             for y in range(0, height, side) for x in range(0, width, side)]
    share = np.ones(len(tiles))
    if tissue is not None and tissue_level is not None:
        scale = 2.0 ** (int(tissue_level) - int(level))
        for i, (x0, y0, x1, y1) in enumerate(tiles):
            r0, c0 = int(y0 / scale), int(x0 / scale)
            seen = tissue[r0:max(r0 + 1, int(math.ceil(y1 / scale))),
                          c0:max(c0 + 1, int(math.ceil(x1 / scale)))]
            share[i] = float(seen.mean()) if seen.size else 0.0
    candidates = [i for i in range(len(tiles)) if share[i] >= spec["min_tissue"]] \
        or [i for i in range(len(tiles)) if share[i] > 0] or list(range(len(tiles)))
    n = min(int(spec["n_fields"]), len(candidates))
    step = len(candidates) / n
    start = float(np.random.default_rng(seed).uniform(0, step))
    return [tiles[candidates[min(int(start + k * step), len(candidates) - 1)]]
            for k in range(n)]


def mask_cells(labels, radius, spec=IMAGE_CELLS):
    """(ids, xs, ys) of a field's mask labels (`labels`, the field's pixels):
    each label's centroid in field pixels. Left out: a label the field's edge
    cuts (its centroid would be the cut part's), and one smaller than
    `min_label_share` of a typical cell's disc (`radius`, field px)."""
    labels = np.asarray(labels)
    flat = labels.ravel()
    keep = flat > 0
    if not keep.any():
        return _none()
    ids, inverse, counts = np.unique(flat[keep], return_inverse=True, return_counts=True)
    rows, cols = np.divmod(np.flatnonzero(keep), labels.shape[1])
    ys = np.bincount(inverse, weights=rows) / counts
    xs = np.bincount(inverse, weights=cols) / counts
    edge = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    whole = ~np.isin(ids, edge)
    big = whole & (counts >= spec["min_label_share"] * math.pi * radius ** 2)
    return ids[big].astype(np.int64), xs[big], ys[big]


def nuclei(plane, radius):
    """(xs, ys) of the nuclei in a field of the nuclear stain: the peaks of
    the stain smoothed at a nucleus's scale (`radius`, field px), one per
    plateau, above the field's Otsu level (log), and no two within `radius`
    of each other (the brighter kept)."""
    from scipy import ndimage

    from plexora.agent.evidence import image_qc

    plane = np.asarray(plane, dtype=np.float64)
    if plane.size < 100:
        return np.empty(0), np.empty(0)
    smooth = ndimage.gaussian_filter(plane, max(1.0, radius / 2.0))
    logged = np.log1p(np.maximum(smooth, 0))
    level = image_qc.otsu_level(logged)
    if level is None:
        return np.empty(0), np.empty(0)
    size = 2 * max(1, int(round(radius))) + 1
    top = ndimage.maximum_filter(smooth, size=size)
    # A flat-topped nucleus is one peak, not every pixel of its plateau.
    peaks = (smooth >= top - 1e-6 * np.maximum(np.abs(top), 1.0)) & (logged > level)
    labelled, count = ndimage.label(peaks)
    if not count:
        return np.empty(0), np.empty(0)
    centres = np.asarray(ndimage.center_of_mass(peaks, labelled, range(1, count + 1)),
                         dtype=np.float64).reshape(-1, 2)
    ys, xs = centres[:, 0], centres[:, 1]
    height = smooth[np.clip(np.rint(ys).astype(int), 0, plane.shape[0] - 1),
                    np.clip(np.rint(xs).astype(int), 0, plane.shape[1] - 1)]
    kept = []
    for i in np.argsort(-height, kind="stable"):
        if all((xs[i] - xs[j]) ** 2 + (ys[i] - ys[j]) ** 2 > radius ** 2 for j in kept):
            kept.append(i)
    kept = np.sort(np.asarray(kept, dtype=np.int64))
    return xs[kept], ys[kept]


def disc_means(plane, xs, ys, radius):
    """Each cell's mean of `plane` in the disc of `radius` around (x, y);
    every disc must lie inside the plane (`_inside`)."""
    r = int(math.ceil(radius))
    dy, dx = np.mgrid[-r:r + 1, -r:r + 1]
    inside = dy ** 2 + dx ** 2 <= radius ** 2
    dy, dx = dy[inside], dx[inside]
    rows = np.rint(ys).astype(np.int64)[:, None] + dy[None, :]
    cols = np.rint(xs).astype(np.int64)[:, None] + dx[None, :]
    return np.asarray(plane, dtype=np.float64)[rows, cols].mean(axis=1)


def _inside(xs, ys, shape, radius):
    margin = math.ceil(radius) + 1
    return (xs >= margin) & (ys >= margin) & (xs < shape[1] - margin) & (ys < shape[0] - margin)


def _none():
    return np.empty(0, dtype=np.int64), np.empty(0), np.empty(0)


def _read(source, index, level, box):
    """A field of one channel as float32, zero where the image ends."""
    x0, y0, x1, y1 = box
    plane, clipped = source.read(index, level, box)
    plane = np.asarray(plane, dtype=np.float32)
    if plane.shape == (y1 - y0, x1 - x0):
        return plane
    canvas = np.zeros((y1 - y0, x1 - x0), dtype=np.float32)
    top, left = clipped[1] - y0, clipped[0] - x0
    h, w = min(plane.shape[0], canvas.shape[0] - top), min(plane.shape[1], canvas.shape[1] - left)
    if h > 0 and w > 0:
        canvas[top:top + h, left:left + w] = plane[:h, :w]
    return canvas


def sample(session, project, channels, *, nuclear=None, seed=0, spec=IMAGE_CELLS) -> dict | None:
    """Cells found in `project`'s image and each one's brightness in every
    channel of `channels` (names -> keys, as `calibration` passes them):

    {ids, xs, ys (level 0), values: {name: array}, source, level, n_fields}

    or None when the image cannot say where its cells are (no mask and no
    `nuclear` stain, brightfield) or finds none."""
    from plexora.agent import render
    from plexora.agent.evidence import crops as cropmod
    from plexora.agent.evidence import image_qc
    from plexora.server.utils import source_image

    record = session.project(project)
    radius0, _how = cropmod.cell_radius_px(record)
    provider = render._mask_for(record)[0] if record.segmentation.available else None
    if provider is None and nuclear not in channels:
        return None
    with source_image.SHELF.reader(session.image_data(project)) as source:
        if source.is_brightfield:
            return None
        level = field_level(source.levels, radius0, spec)
        div = 2 ** level
        radius = radius0 / div
        tissue = tissue_level = None
        if nuclear in channels:
            tissue_level = image_qc.overview_level(source)
            overview, _l, _c = image_qc.read_overview(source, channels[nuclear], tissue_level)
            tissue = image_qc.tissue_mask(overview)
        boxes = fields(source.level_shape(level), level, tissue, tissue_level, seed=seed,
                       spec=spec)
        indices = {name: source.channel_index(key) for name, key in channels.items()}
        nucleus = spec["nucleus_um"] / cropmod.CELL_DIAMETER_UM * radius
        found_ids, found_x, found_y = [], [], []
        values = {name: [] for name in channels}
        for number, box in enumerate(boxes):
            planes = {}
            if provider is not None:
                labels = provider.read_region(level + record.segmentation.extra_levels, box)
                ids, xs, ys = mask_cells(labels, radius, spec)
            else:
                planes[nuclear] = _read(source, indices[nuclear], level, box)
                xs, ys = nuclei(planes[nuclear], nucleus)
                ids = number * ID_STRIDE + 1 + np.arange(xs.size, dtype=np.int64)
            shape = (box[3] - box[1], box[2] - box[0])
            keep = _inside(xs, ys, shape, radius)
            if not keep.any():
                continue
            ids, xs, ys = ids[keep], xs[keep], ys[keep]
            for name, index in indices.items():
                plane = planes.get(name)
                if plane is None:
                    plane = _read(source, index, level, box)
                values[name].append(disc_means(plane, xs, ys, radius))
            found_ids.append(ids)
            # A field pixel's centre, in full-resolution pixels.
            found_x.append((box[0] + xs) * div + (div - 1) / 2.0)
            found_y.append((box[1] + ys) * div + (div - 1) / 2.0)
    if not found_ids:
        return None
    ids = np.concatenate(found_ids)
    # Fields never overlap, but a mask label may straddle two: kept once.
    _unique, first = np.unique(ids, return_index=True)
    first = np.sort(first)
    return {"ids": ids[first], "xs": np.concatenate(found_x)[first],
            "ys": np.concatenate(found_y)[first],
            "values": {name: np.concatenate(parts)[first] for name, parts in values.items()},
            "source": SOURCE_MASK if provider is not None else SOURCE_NUCLEI,
            "level": int(level), "n_fields": len(boxes)}
