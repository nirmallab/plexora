"""Drawing a label mask over a rendered region, server side.

The viewer draws segmentation in the browser (`views/labelTile.js`): every
pixel whose 8-neighbourhood holds a different label is an outline pixel, and a
cell layer colours cells by looking their label up in a table. A rendered
region -- an agent's visual evidence, see plexora/agent/render.py -- has to
show the same outlines on the same cells, so the arithmetic is written out
here once rather than approximated by whoever needs it next.

Nothing here opens a file or reads a project. It takes label arrays and
colours in, and hands pixels back, so it can be tested against hand-built
masks.
"""

from __future__ import annotations

import numpy as np
import zarr


def label_region(pyramid, level, y, x, height, width):
    """A label mask's tile at `level`, even when the mask has no such level.

    A single-level mask (a bare `zarr.Array`) or a pyramid shorter than the
    image used to be read at full resolution for every level, which drew each
    zoomed-out tile 2**level times too large -- cells from elsewhere in the
    slide sat on top of the tissue. Instead take every 2**(level - b)-th pixel
    of the finest level `b` below it. Nearest-neighbour is exact for labels;
    it is slower than a real pyramid level (a strip-compressed mask decodes
    whole strips), so the pyramid stays the fast path and the tile caches
    absorb repeats.

    Moved here from `data_model._label_region`, which is now this function.
    """
    if isinstance(pyramid, zarr.Array):
        base, array = 0, pyramid
    elif str(level) in pyramid:
        array = pyramid[str(level)]
        return np.asarray(array[y:y + height, x:x + width])
    else:
        present = [int(k) for k in pyramid.array_keys() if str(k).isdigit()]
        below = [k for k in present if k <= level]
        base = max(below) if below else min(present)
        array = pyramid[str(base)]
    factor = 2 ** max(0, level - base)
    if factor == 1:
        return np.asarray(array[y:y + height, x:x + width])
    rows = np.arange(y * factor, (y + height) * factor, factor)
    columns = np.arange(x * factor, (x + width) * factor, factor)
    rows = rows[rows < array.shape[0]]
    columns = columns[columns < array.shape[1]]
    if not len(rows) or not len(columns):
        return np.zeros((len(rows), len(columns)), dtype=array.dtype)
    return np.asarray(array.oindex[rows, columns])


def mask_level_shape(pyramid, level):
    """(height, width) of the mask at `level`, derived when the level is absent."""
    if isinstance(pyramid, zarr.Array):
        base, array = 0, pyramid
    elif str(level) in pyramid:
        array = pyramid[str(level)]
        return tuple(array.shape[-2:])
    else:
        present = [int(k) for k in pyramid.array_keys() if str(k).isdigit()]
        below = [k for k in present if k <= level]
        base = max(below) if below else min(present)
        array = pyramid[str(base)]
    factor = 2 ** max(0, level - base)
    return (-(-array.shape[-2] // factor), -(-array.shape[-1] // factor))


def padded_label_region(pyramid, level, box):
    """Labels for `box` = (x0, y0, x1, y1) at `level`, 0 where it runs off.

    Padded for the same reason `source_image.composite` pads: the caller asked
    for this box, and the labels have to land on the pixels of the same box.
    """
    x0, y0, x1, y1 = (int(v) for v in box)
    height, width = max(0, y1 - y0), max(0, x1 - x0)
    full_h, full_w = mask_level_shape(pyramid, level)
    out = np.zeros((height, width), dtype=np.uint32)
    cx0, cy0 = max(0, min(x0, full_w)), max(0, min(y0, full_h))
    cx1, cy1 = max(cx0, min(x1, full_w)), max(cy0, min(y1, full_h))
    if cx1 > cx0 and cy1 > cy0:
        block = label_region(pyramid, level, cy0, cx0, cy1 - cy0, cx1 - cx0)
        block = np.asarray(block).astype(np.uint32, copy=False)
        out[cy0 - y0:cy0 - y0 + block.shape[0],
            cx0 - x0:cx0 - x0 + block.shape[1]] = block[:height, :width]
    return out


def boundary_mask(labels, radius=1):
    """True where a labelled pixel has a differently-labelled neighbour within
    `radius` (Chebyshev) -- at radius 1, `labelTile.js isBoundary`, vectorised.

    A neighbour outside the array does not count, so a cell cut by the edge of
    the region is not outlined along the cut. A larger radius draws a thicker
    outline, inward from each cell's edge, for a picture shown larger than the
    screen the viewer draws one-pixel outlines on.
    """
    labels = np.asarray(labels)
    height, width = labels.shape
    edge = np.zeros((height, width), dtype=bool)
    radius = max(1, int(radius))
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if not dy and not dx:
                continue
            ys = slice(max(0, dy), height + min(0, dy))
            xs = slice(max(0, dx), width + min(0, dx))
            yn = slice(max(0, -dy), height + min(0, -dy))
            xn = slice(max(0, -dx), width + min(0, -dx))
            edge[yn, xn] |= labels[yn, xn] != labels[ys, xs]
    return edge & (labels != 0)


#: The share of labelled pixels on a boundary at which outlines stop reading as
#: outlines -- labelTile.js's OUTLINE_READABLE / OUTLINE_UNREADABLE.
OUTLINE_READABLE = 0.5
OUTLINE_UNREADABLE = 0.75


def small_cell_weight(labels):
    """0 when cells are large enough to outline, 1 when they are so small that
    only a fill can be read -- the browser's blend between the two."""
    labels = np.asarray(labels)[::2, ::2]
    labelled = labels != 0
    if not labelled.any():
        return 0.0
    share = float(boundary_mask(labels)[labelled].mean())
    t = (share - OUTLINE_READABLE) / (OUTLINE_UNREADABLE - OUTLINE_READABLE)
    return float(min(1.0, max(0.0, t)))


def resize_labels_nearest(labels, width, height):
    """Labels to exactly (width, height) by nearest neighbour -- the only
    resampling that never invents a label."""
    labels = np.asarray(labels)
    src_h, src_w = labels.shape
    if (src_w, src_h) == (width, height):
        return labels
    if src_h == 0 or src_w == 0:
        return np.zeros((height, width), dtype=labels.dtype)
    rows = np.minimum((np.arange(height) + 0.5) * src_h / height, src_h - 1).astype(np.int64)
    columns = np.minimum((np.arange(width) + 0.5) * src_w / width, src_w - 1).astype(np.int64)
    return labels[rows][:, columns]


def _rgb(colour):
    if isinstance(colour, str):
        value = colour.lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    return tuple(int(c) for c in colour[:3])


def paint_labels(rgb, labels, *, mode="outlines", colour_for=None, default=(255, 255, 255),
                 alpha=1.0, fill_alpha=0.45, thickness=1):
    """Draw labels over an (H, W, 3) uint8 image, in place; returns it.

    `colour_for` maps a label id to a colour (hex or triple), or None to leave
    that cell undrawn; without it every cell gets `default`. `mode` is
    `outlines` (boundary pixels only), `filled` (the interior tinted at
    `fill_alpha` with the boundary drawn solid) or `none`.
    """
    if mode == "none":
        return rgb
    labels = np.asarray(labels)
    if labels.shape != rgb.shape[:2]:
        raise ValueError("labels and image must be the same size")
    present = np.unique(labels)
    present = present[present != 0]
    if not present.size:
        return rgb

    lut_ids = []
    lut_colours = []
    for label in present.tolist():
        colour = colour_for(label) if colour_for is not None else default
        if colour is None:
            continue
        lut_ids.append(label)
        lut_colours.append(_rgb(colour))
    if not lut_ids:
        return rgb
    lut_ids = np.asarray(lut_ids, dtype=labels.dtype)
    lut_colours = np.asarray(lut_colours, dtype=np.float32)

    # Per pixel: which drawn cell it belongs to, or -1.
    position = np.searchsorted(lut_ids, labels)
    position = np.clip(position, 0, len(lut_ids) - 1)
    drawn = lut_ids[position] == labels
    drawn &= labels != 0

    edge = boundary_mask(labels, thickness) & drawn
    out = rgb.astype(np.float32)
    colours = lut_colours[position]
    if mode == "filled":
        interior = drawn & ~edge
        out[interior] = out[interior] * (1 - fill_alpha) + colours[interior] * fill_alpha
    out[edge] = out[edge] * (1 - alpha) + colours[edge] * alpha
    rgb[...] = np.clip(out, 0, 255).astype(np.uint8)
    return rgb


def cell_ids(frame, id_column=None):
    """(ids, keep) for a table: each row's mask label, and which rows have one.

    The rule the viewer's cell layers use (cell_explorer's `cell_ids`, the
    centroid tiles): the cell-id role's column when the table carries it,
    otherwise the adapter's positional `id`. A value that will not survive the
    trip to uint32 cannot match a mask label and is dropped rather than drawn
    somewhere arbitrary.
    """
    import polars as pl

    if frame is None:
        return np.empty(0, dtype=np.uint32), np.empty(0, dtype=bool)
    column = id_column if id_column and id_column in frame.columns else "id"
    if column not in frame.columns:
        return np.empty(0, dtype=np.uint32), np.zeros(frame.height, dtype=bool)
    raw = (frame[column].cast(pl.Float64, strict=False)
           .fill_null(float("nan")).to_numpy())
    keep = np.isfinite(raw) & (raw >= 0)
    return raw[keep].astype(np.uint32, copy=False), keep
