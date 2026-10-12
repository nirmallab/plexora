"""A 3-D mask's cells where an XZ or YZ section cuts them.

The section panes (client views/sectionPanes.js) show every plane of the stack
along one line of the plane: XZ along a row y, YZ along a column x. A cell is
in a section exactly where its voxels lie on that line, plane by plane, so its
cut is a run of pixels along the line on each plane it crosses -- what this
reads from the label volume, the same ids the plane's outlines are drawn from.

`runs` answers `(z, label, a0, a1)` arrays, one row per run of one cell on one
plane, `a0..a1` inclusive in full-resolution pixels along the line. Read at a
level the pane can use (the label volume's XY levels are strided copies), and
coarser again past `MAX_VOXELS`, so a deep stack never costs more than a few
chunk reads per plane. Works the same for a label volume a data node holds
(providers/node.NodeLabelVolume answers the same `read`).
"""

from __future__ import annotations

import numpy as np

#: The most label voxels one section reads (planes x pixels along the line).
MAX_VOXELS = 16_000_000

AXES = ("xz", "yz")


def runs(labels, axis, at, a0=None, a1=None, level=0):
    """`(z, label, a0, a1, level)` for the cut at row (XZ) or column (YZ)
    `at` of a label volume frame, between `a0` and `a1` (full-resolution
    pixels along the line, inclusive; the whole line by default)."""
    if axis not in AXES:
        raise ValueError(f"axis is one of {', '.join(AXES)}, not {axis!r}")
    depth = int(labels.depth)
    length = int(labels.width if axis == "xz" else labels.height)
    across = int(labels.height if axis == "xz" else labels.width)
    at = min(max(int(at), 0), across - 1)
    a0 = 0 if a0 is None else min(max(int(a0), 0), length - 1)
    a1 = length - 1 if a1 is None else min(max(int(a1), a0), length - 1)
    levels = max(1, int(getattr(labels, "levels", 1) or 1))
    level = min(max(int(level), 0), levels - 1)
    while level < levels - 1 and depth * ((a1 - a0) // (2 ** level) + 1) > MAX_VOXELS:
        level += 1
    step = 2 ** level
    lo, hi, line = a0 // step, a1 // step, at // step
    if axis == "xz":
        block = labels.read(level, 0, depth - 1, slice(line, line + 1), slice(lo, hi + 1))[:, 0, :]
    else:
        block = labels.read(level, 0, depth - 1, slice(lo, hi + 1), slice(line, line + 1))[:, :, 0]
    block = np.asarray(block)
    if not block.size:
        empty = np.zeros(0, np.int64)
        return empty, empty, empty, empty, level
    width = block.shape[1]
    flat = block.ravel()
    starts = np.ones(flat.size, bool)
    starts[1:] = flat[1:] != flat[:-1]
    starts[::width] = True                 # every plane starts a run of its own
    start = np.flatnonzero(starts)
    end = np.append(start[1:], flat.size) - 1
    label = flat[start].astype(np.int64)
    keep = label != 0
    start, end, label = start[keep], end[keep], label[keep]
    z = start // width
    first = (lo + start % width) * step
    last = np.minimum((lo + end % width + 1) * step - 1, length - 1)
    return z.astype(np.int64), label, first.astype(np.int64), last.astype(np.int64), level
