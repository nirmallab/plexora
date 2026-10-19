"""Per-cell pixel features of one marker: one pass over the mask and its plane.

What the Thresholding panel's Single Cell Classifier learns from beside the
table's own number for the marker. A table column is one summary of a cell (a
mean, usually); the pixels say more -- whether the stain is bright at the
edge or the middle, how it sits against the tissue around it, whether it is
spread or one speck. These are the features that tell a real membrane stain
from glow off a neighbour, which is what a person clicking cells is judging.

Core, not the plugin, and numpy only: the same code runs on this machine for
a local image and on the data node that holds a remote one (node/api.py
`/cells/features`), and a plugin may not be imported by core. Every
statistic is a running sum per label, accumulated tile by tile with
`np.bincount`, so the pass is O(pixels) in time and O(cells) in memory
whatever the image's size.

The readers are callables so this module never knows where pixels live:
`read_marker(box)` and `read_labels(box)` return the box asked for, padded
with zeros where it runs off the image (`label_overlay.padded_label_region`
does that for a mask); `read_dna(box)` likewise, or None for no nuclear
channel. Boxes are (x0, y0, x1, y1) at the chosen level.
"""

from __future__ import annotations

import io

import numpy as np

#: Bumped when a feature's definition changes, so a cached matrix built by the
#: old code is not read as the new one.
FEATURE_VERSION = 1

#: Tile side read at once (level px). With a 1 px halo, three float32 planes
#: and the label plane, one tile is ~16 MB.
TILE = 1024

#: Side of the blocks the local background is estimated in.
BACKGROUND_BLOCK = 128

#: The background is this low percentile of a block's unlabelled pixels...
BACKGROUND_PERCENTILE = 20.0
#: ...when the block has at least this many of them.
BACKGROUND_MIN_PIXELS = 64

#: The features `features()` returns, in order. `nuclear_ratio` only with a
#: nuclear channel.
NAMES = ("px_mean", "px_std", "px_max", "px_peak_ratio", "px_membrane_ratio",
         "px_background", "px_contrast", "px_gradient", "px_area", "px_nuclear_ratio")


def tiles(width, height, tile=TILE):
    """Every (x0, y0, x1, y1) tile covering a width x height level."""
    for y0 in range(0, int(height), tile):
        for x0 in range(0, int(width), tile):
            yield x0, y0, min(x0 + tile, int(width)), min(y0 + tile, int(height))


class _Sums:
    """Per-tile partial sums, reduced into one row per label at the end."""

    FIELDS = ("n", "sum", "sumsq", "edge_n", "edge_sum", "bg_n", "bg_sum", "grad_sum",
              "dna_sum", "dna_wsum")

    def __init__(self):
        self.ids = []
        self.parts = {name: [] for name in self.FIELDS}
        self.max_parts = []

    def add(self, ids, values, maxima):
        self.ids.append(ids)
        for name in self.FIELDS:
            self.parts[name].append(values[name])
        self.max_parts.append(maxima)

    def reduce(self):
        if not self.ids:
            empty = np.zeros(0, dtype=np.float64)
            return np.zeros(0, dtype=np.int64), {name: empty for name in self.FIELDS}, empty
        ids = np.concatenate(self.ids)
        labels, inverse = np.unique(ids, return_inverse=True)
        out = {name: np.bincount(inverse, weights=np.concatenate(self.parts[name]),
                                 minlength=labels.size)
               for name in self.FIELDS}
        maxima = np.full(labels.size, -np.inf)
        np.maximum.at(maxima, inverse, np.concatenate(self.max_parts))
        return labels, out, maxima


def _background_map(marker, labels):
    """Per pixel, the local background: a low percentile of each block's
    unlabelled pixels; NaN where a block has too few."""
    height, width = marker.shape
    out = np.full(marker.shape, np.nan, dtype=np.float32)
    step = BACKGROUND_BLOCK
    for y0 in range(0, height, step):
        for x0 in range(0, width, step):
            block = marker[y0:y0 + step, x0:x0 + step]
            empty = labels[y0:y0 + step, x0:x0 + step] == 0
            if int(empty.sum()) >= BACKGROUND_MIN_PIXELS:
                out[y0:y0 + step, x0:x0 + step] = np.percentile(block[empty],
                                                                BACKGROUND_PERCENTILE)
    return out


def _tile_sums(marker_h, labels_h, dna_h):
    """One tile's sums. The `_h` planes carry a 1 px halo all round, which the
    boundary and gradient need; the sums cover the core only."""
    core = (slice(1, -1), slice(1, -1))
    labels = labels_h[core]
    inside = labels > 0
    if not inside.any():
        return None
    marker = marker_h[core]
    # A boundary pixel has a 4-neighbour with another label (background too):
    # the membrane ring `segqc`'s perimeter counts.
    edge = ((labels_h[:-2, 1:-1] != labels) | (labels_h[2:, 1:-1] != labels)
            | (labels_h[1:-1, :-2] != labels) | (labels_h[1:-1, 2:] != labels))
    gx = (marker_h[1:-1, 2:] - marker_h[1:-1, :-2]) * 0.5
    gy = (marker_h[2:, 1:-1] - marker_h[:-2, 1:-1]) * 0.5
    gradient = np.hypot(gx, gy)
    background = _background_map(marker, labels)

    ids_all = labels[inside].astype(np.int64)
    ids, inverse = np.unique(ids_all, return_inverse=True)
    count = ids.size
    values = marker[inside].astype(np.float64)

    def total(weights):
        return np.bincount(inverse, weights=weights, minlength=count)

    edge_in = edge[inside]
    bg_in = background[inside].astype(np.float64)
    bg_ok = np.isfinite(bg_in)
    sums = {
        "n": np.bincount(inverse, minlength=count).astype(np.float64),
        "sum": total(values),
        "sumsq": total(values * values),
        "edge_n": total(edge_in.astype(np.float64)),
        "edge_sum": total(np.where(edge_in, values, 0.0)),
        "bg_n": total(bg_ok.astype(np.float64)),
        "bg_sum": total(np.where(bg_ok, bg_in, 0.0)),
        "grad_sum": total(gradient[inside].astype(np.float64)),
    }
    if dna_h is not None:
        dna = np.clip(dna_h[core][inside].astype(np.float64), 0.0, None)
        sums["dna_sum"] = total(dna)
        sums["dna_wsum"] = total(dna * values)
    else:
        sums["dna_sum"] = np.zeros(count)
        sums["dna_wsum"] = np.zeros(count)
    maxima = np.full(count, -np.inf)
    np.maximum.at(maxima, inverse, values)
    return ids, sums, maxima


def accumulate(read_marker, read_labels, *, width, height, read_dna=None, tile=TILE,
               progress=None, cancelled=None):
    """`(labels, features)` for every label in the mask: `labels` int64
    sorted, `features` {name: float32 array aligned to it} (see NAMES;
    `px_nuclear_ratio` only with `read_dna`).

    `progress(done, total)` after every tile; `cancelled()` is polled before
    each and raises InterruptedError when it answers True.
    """
    boxes = list(tiles(width, height, tile))
    sums = _Sums()
    for done, (x0, y0, x1, y1) in enumerate(boxes):
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")
        halo = (x0 - 1, y0 - 1, x1 + 1, y1 + 1)
        labels_h = np.asarray(read_labels(halo))
        if labels_h[1:-1, 1:-1].any():
            marker_h = np.asarray(read_marker(halo), dtype=np.float32)
            dna_h = (np.asarray(read_dna(halo), dtype=np.float32)
                     if read_dna is not None else None)
            got = _tile_sums(marker_h, labels_h, dna_h)
            if got is not None:
                sums.add(got[0], got[1], got[2])
        if progress is not None:
            progress(done + 1, len(boxes))
    labels, s, maxima = sums.reduce()
    return labels, _finish(s, maxima, nuclear=read_dna is not None)


def _finish(s, maxima, *, nuclear):
    n = np.maximum(s["n"], 1.0)
    mean = s["sum"] / n
    var = np.maximum(s["sumsq"] / n - mean * mean, 0.0)
    interior_n = s["n"] - s["edge_n"]
    edge_mean = np.where(s["edge_n"] > 0, s["edge_sum"] / np.maximum(s["edge_n"], 1.0), mean)
    interior_mean = np.where(interior_n > 0,
                             (s["sum"] - s["edge_sum"]) / np.maximum(interior_n, 1.0), mean)
    background = np.where(s["bg_n"] > 0, s["bg_sum"] / np.maximum(s["bg_n"], 1.0), np.nan)
    if background.size and np.isnan(background).any():
        # A cell packed in so tightly that no block around it shows tissue
        # between cells takes the image's typical background.
        known = background[np.isfinite(background)]
        background = np.where(np.isfinite(background), background,
                              float(np.median(known)) if known.size else 0.0)
    eps = 1e-6
    out = {
        "px_mean": mean,
        "px_std": np.sqrt(var),
        "px_max": maxima,
        "px_peak_ratio": maxima / (np.abs(mean) + eps),
        "px_membrane_ratio": (edge_mean + eps) / (np.abs(interior_mean) + eps),
        "px_background": background,
        "px_contrast": mean - background,
        "px_gradient": s["grad_sum"] / n,
        "px_area": s["n"],
    }
    if nuclear:
        nuclear_mean = np.where(s["dna_sum"] > 0,
                                s["dna_wsum"] / np.maximum(s["dna_sum"], eps), mean)
        out["px_nuclear_ratio"] = (nuclear_mean + eps) / (np.abs(mean) + eps)
    return {name: np.asarray(value, dtype=np.float32) for name, value in out.items()}


def choose_level(width, height, levels, max_pixels=400_000_000):
    """The finest level whose plane has at most `max_pixels`, so a whole-slide
    image is read at a resolution that still resolves a cell (one halving of
    a 40x scan) rather than for an hour."""
    level = 0
    w, h = int(width), int(height)
    while w * h > max_pixels and level + 1 < int(levels):
        level += 1
        w, h = (w + 1) // 2, (h + 1) // 2
    return level


def pack(labels, features) -> bytes:
    """The result as .npz bytes, how it crosses from a data node."""
    buffer = io.BytesIO()
    np.savez(buffer, labels=np.asarray(labels, dtype=np.int64),
             **{name: np.asarray(value, dtype=np.float32) for name, value in features.items()})
    return buffer.getvalue()


def unpack(data: bytes):
    """`pack`'s inverse: `(labels, features)`."""
    with np.load(io.BytesIO(data), allow_pickle=False) as held:
        labels = np.asarray(held["labels"], dtype=np.int64)
        features = {name: np.asarray(held[name], dtype=np.float32)
                    for name in held.files if name != "labels"}
    return labels, features


def pad_to_box(plane, clipped, box):
    """A clipped read (`plane` covering `clipped`) placed on the box asked
    for, zeros where the image does not reach."""
    x0, y0, x1, y1 = (int(v) for v in box)
    out = np.zeros((max(0, y1 - y0), max(0, x1 - x0)), dtype=np.float32)
    cx0, cy0, cx1, cy1 = (int(v) for v in clipped)
    if cx1 > cx0 and cy1 > cy0:
        plane = np.asarray(plane, dtype=np.float32)
        h = min(plane.shape[0], cy1 - cy0, out.shape[0] - (cy0 - y0))
        w = min(plane.shape[1], cx1 - cx0, out.shape[1] - (cx0 - x0))
        if h > 0 and w > 0:
            out[cy0 - y0:cy0 - y0 + h, cx0 - x0:cx0 - x0 + w] = plane[:h, :w]
    return out


def clipped_box(box, width, height):
    """`box` clipped to a width x height level."""
    x0, y0, x1, y1 = (int(v) for v in box)
    cx0, cy0 = max(0, min(x0, width)), max(0, min(y0, height))
    return cx0, cy0, max(cx0, min(x1, width)), max(cy0, min(y1, height))
