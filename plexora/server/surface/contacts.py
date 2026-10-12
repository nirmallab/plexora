"""Which objects are near which: distances between surfaces, and contact areas.

Geometry, never interaction: two cells whose surfaces are 0.4 µm apart are
near each other; whether they talk is a biological claim this module cannot
make, and the answers say "near", "touching" and "contact area", nothing more.

* Centroid distance -- between the objects' centres, from the objects table.
* Surface distance -- the shortest distance between two meshes' vertices, in
  the physical frame (one KD-tree over the chosen object's vertices, every
  other vertex asked once). Smoothing and the inset move surfaces by up to
  about half a voxel, which bounds how exact this is.
* Contact area (cell surfaces only) -- from the MASK, not the mesh: the voxel
  faces the two cells share, each weighted by its physical area. Exact for
  the segmentation, and independent of how the surfaces were smoothed.
"""

from __future__ import annotations

import numpy as np

#: Neighbours reported at most.
LIMIT = 500


def centroid_neighbours(frame, target, within, *, physical=True):
    """`[(object, distance)]` of objects whose centroid is within `within` of
    `target`'s, nearest first (from an objects table)."""
    unit = "um" if physical else "vox"
    ids = np.asarray(frame["object"], dtype=np.int64)
    columns = [f"centroid_{axis}_{unit}" for axis in "xyz"]
    points = np.stack([np.asarray(frame[c], dtype=np.float64) for c in columns], axis=1)
    where = np.nonzero(ids == int(target))[0]
    if not len(where):
        return []
    distance = np.linalg.norm(points - points[where[0]], axis=1)
    keep = (distance <= within) & (ids != int(target))
    order = np.argsort(distance[keep])[:LIMIT]
    return list(zip(ids[keep][order].tolist(), distance[keep][order].tolist()))


def surface_neighbours(surface, target, within, *, scale=(1.0, 1.0, 1.0)):
    """`[(object, shortest distance)]` between `target`'s surface and every
    other object's within `within` (units of `scale`), nearest first."""
    from scipy.spatial import cKDTree

    owner = surface.object_id.astype(np.int64)
    mine = owner == int(target)
    if not mine.any():
        return []
    p = surface.positions.astype(np.float64) * np.asarray(scale, dtype=np.float64)
    tree = cKDTree(p[mine])
    others = np.nonzero(~mine)[0]
    if not len(others):
        return []
    distance, _ = tree.query(p[others], k=1, distance_upper_bound=float(within))
    close = np.isfinite(distance)
    if not close.any():
        return []
    ids = owner[others][close]
    d = distance[close]
    order = np.lexsort((d, ids))
    ids, d = ids[order], d[order]
    first = np.r_[True, ids[1:] != ids[:-1]]
    pairs = sorted(zip(ids[first].tolist(), d[first].tolist()), key=lambda pair: pair[1])
    return pairs[:LIMIT]


def contact_areas(label_volume, label, box, *, spacing=(1.0, 1.0, 1.0)):
    """`{neighbour label: shared area}` of cell `label` from the mask: voxel
    faces between `label` and another label, each `sy*sz`, `sx*sz` or `sx*sy`
    by its axis. `box` is the cell's (z0, z1, y0, y1, x0, x1) at level 0."""
    z0, z1, y0, y1, x0, x1 = (int(v) for v in box)
    z0, y0, x0 = max(0, z0 - 1), max(0, y0 - 1), max(0, x0 - 1)
    z1 = min(label_volume.depth - 1, z1 + 1)
    y1 = min(label_volume.height - 1, y1 + 1)
    x1 = min(label_volume.width - 1, x1 + 1)
    block = np.asarray(label_volume.read(0, z0, z1, slice(y0, y1 + 1), slice(x0, x1 + 1)),
                       dtype=np.int64)
    sx, sy, sz = (float(v) for v in spacing)
    areas = {}
    for axis, face in ((2, sy * sz), (1, sx * sz), (0, sx * sy)):
        a = np.moveaxis(block, axis, 0)
        left, right = a[:-1], a[1:]
        hit = ((left == label) & (right != label) & (right != 0))
        other = right[hit]
        hit_back = ((right == label) & (left != label) & (left != 0))
        other = np.concatenate([other, left[hit_back]])
        if other.size:
            ids, counts = np.unique(other, return_counts=True)
            for neighbour, n in zip(ids.tolist(), counts.tolist()):
                areas[neighbour] = areas.get(neighbour, 0.0) + n * face
    return areas
