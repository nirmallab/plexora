"""Segmentation QC's pixel loops, compiled (plexora/server/utils/jit.py).

Two passes over a tile's interior that numpy would do with a sort of every
pixel (np.unique) per tile: a direct accumulation into per-label arrays is
O(pixels) with no sort, which is what makes a 1M-cell slide a few minutes.
Both run unchanged as Python under PLEXORA_NO_NUMBA=1 (the tests compare).
"""

from __future__ import annotations

import math

import numpy as np

from plexora.server.utils.jit import njit


@njit
def label_stats(labels, dna, y_off, x_off, iy0, iy1, ix0, ix1, bg,
                count, sum_dna, sum_y, sum_x, perimeter, max_dna, excess):
    """Add the interior window [iy0:iy1, ix0:ix1] of a tile to the per-label
    arrays: pixel count, sum of the smoothed (log) DNA, sum of y and x
    (global level pixels), perimeter (4-neighbour pairs that leave the label,
    counted once per pixel side, including pairs reaching into the halo), the
    brightest smoothed DNA, and the DNA mass above the tile's background `bg`
    (linear intensity: sum of max(expm1(dna) - bg, 0))."""
    height, width = labels.shape
    for y in range(iy0, iy1):
        for x in range(ix0, ix1):
            label = labels[y, x]
            if label == 0:
                continue
            value = dna[y, x]
            count[label] += 1
            sum_dna[label] += value
            if value > max_dna[label]:
                max_dna[label] = value
            above = math.expm1(value) - bg
            if above > 0:
                excess[label] += above
            sum_y[label] += y + y_off
            sum_x[label] += x + x_off
            edge = 0
            if y == 0 or labels[y - 1, x] != label:
                edge += 1
            if y == height - 1 or labels[y + 1, x] != label:
                edge += 1
            if x == 0 or labels[y, x - 1] != label:
                edge += 1
            if x == width - 1 or labels[y, x + 1] != label:
                edge += 1
            perimeter[label] += edge


@njit
def count_pairs(labels, iy0, iy1, ix0, ix1):
    """How many right / down neighbour pairs in the window join two labels."""
    height, width = labels.shape
    n = 0
    for y in range(iy0, iy1):
        for x in range(ix0, ix1):
            a = labels[y, x]
            if a == 0:
                continue
            if x + 1 < width:
                b = labels[y, x + 1]
                if b != 0 and b != a:
                    n += 1
            if y + 1 < height:
                b = labels[y + 1, x]
                if b != 0 and b != a:
                    n += 1
    return n


@njit
def fill_pairs(labels, dna, y_off, x_off, iy0, iy1, ix0, ix1, lo, hi, value, py, px):
    """The pairs `count_pairs` counted: the two labels (lo < hi), the mean
    smoothed DNA of the two pixels, and the pair's midpoint (global level
    pixels). A pair is owned by the window its first pixel is in, so every
    boundary in the image is counted exactly once."""
    height, width = labels.shape
    k = 0
    for y in range(iy0, iy1):
        for x in range(ix0, ix1):
            a = labels[y, x]
            if a == 0:
                continue
            if x + 1 < width:
                b = labels[y, x + 1]
                if b != 0 and b != a:
                    lo[k] = min(a, b)
                    hi[k] = max(a, b)
                    value[k] = 0.5 * (dna[y, x] + dna[y, x + 1])
                    py[k] = y + y_off
                    px[k] = x + x_off + 0.5
                    k += 1
            if y + 1 < height:
                b = labels[y + 1, x]
                if b != 0 and b != a:
                    lo[k] = min(a, b)
                    hi[k] = max(a, b)
                    value[k] = 0.5 * (dna[y, x] + dna[y + 1, x])
                    py[k] = y + y_off + 0.5
                    px[k] = x + x_off
                    k += 1
    return k


def pairs(labels, dna, y_off, x_off, window):
    """(lo, hi, value, y, x) arrays of the window's label-label pairs."""
    iy0, iy1, ix0, ix1 = window
    n = count_pairs(labels, iy0, iy1, ix0, ix1)
    lo = np.empty(n, dtype=np.int64)
    hi = np.empty(n, dtype=np.int64)
    value = np.empty(n, dtype=np.float64)
    py = np.empty(n, dtype=np.float64)
    px = np.empty(n, dtype=np.float64)
    if n:
        fill_pairs(labels, dna, y_off, x_off, iy0, iy1, ix0, ix1, lo, hi, value, py, px)
    return lo, hi, value, py, px


def prime():
    labels = np.array([[0, 1, 1], [2, 2, 1], [2, 0, 0]], dtype=np.uint32)
    dna = np.ones((3, 3), dtype=np.float32)
    arrays = [np.zeros(3, dtype=np.float64) for _ in range(7)]
    label_stats(labels, dna, 0, 0, 0, 3, 0, 3, 0.5, *arrays)
    pairs(labels, dna, 0, 0, (0, 3, 0, 3))
