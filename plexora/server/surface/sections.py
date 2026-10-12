"""Where a surface cuts a plane: the line it draws in XY, XZ and YZ.

The 2-D views show a surface as the curve where it crosses the plane on
screen (and the section panes' cuts), so the mesh can be judged against the
voxels it was made from, physically registered: the same level-0 voxel frame
as the tiles. Each crossing triangle gives one segment; the segments are
grouped by object so the views can colour them and highlight a selection.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np

#: Segments per answer at most.
MAX_SEGMENTS = 300_000
AXES = {"z": (2, (0, 1)), "y": (1, (0, 2)), "x": (0, (1, 2))}

_cache: OrderedDict = OrderedDict()
_lock = threading.Lock()


def section(surface, axis, value) -> dict:
    """`{object id: [a0, b0, a1, b1, ...]}` -- segments where `surface`
    crosses the plane `axis = value` (level-0 voxels), in the plane's two
    other axes: (x, y) for z, (x, z) for y, (y, z) for x."""
    if axis not in AXES or not surface.n_triangles:
        return {}
    a, keep = AXES[axis]
    p = surface.positions.astype(np.float64)
    # A hair off the plane: a vertex lying exactly on it would count twice.
    d = p[:, a] - (float(value) + 1e-4)
    t = surface.triangles.astype(np.int64)
    above = d[t] > 0
    count = above.sum(axis=1)
    cross = (count == 1) | (count == 2)
    if not cross.any():
        return {}
    t = t[cross]
    dv = d[t]
    points = np.full((len(t), 3, 2), np.nan)
    hits = np.zeros((len(t), 3), bool)
    for k, (i, j) in enumerate(((0, 1), (1, 2), (2, 0))):
        di, dj = dv[:, i], dv[:, j]
        edge = (di > 0) != (dj > 0)
        f = np.where(edge, di / np.where(edge, di - dj, 1.0), 0.0)
        pi, pj = p[t[:, i]], p[t[:, j]]
        q = pi + (pj - pi) * f[:, None]
        points[:, k] = q[:, keep]
        hits[:, k] = edge
    order = np.argsort(~hits, axis=1, kind="stable")[:, :2]
    rows = np.arange(len(t))[:, None]
    segments = points[rows, order].reshape(len(t), 4)
    owners = surface.object_id[t[:, 0]].astype(np.int64)
    if len(segments) > MAX_SEGMENTS:
        segments, owners = segments[:MAX_SEGMENTS], owners[:MAX_SEGMENTS]
    out = {}
    sort = np.argsort(owners, kind="stable")
    owners, segments = owners[sort], segments[sort]
    ids, first = np.unique(owners, return_index=True)
    bounds = list(first) + [len(owners)]
    for index, object_id in enumerate(ids.tolist()):
        part = segments[bounds[index]:bounds[index + 1]]
        out[int(object_id)] = np.round(part, 2).ravel().tolist()
    return out


def cached(key, build):
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    value = build()
    with _lock:
        _cache[key] = value
        while len(_cache) > 96:
            _cache.popitem(last=False)
    return value
