"""The triangle mesh every surface is, its file, and what is measured on it.

**Coordinates.** Vertex positions are in the volume's level-0 VOXEL frame,
`(x, y, z)` with a voxel's centre at whole numbers -- the frame the viewer's
box and every table's X/Y/Z already use. Physical positions are those times
the spacing, applied when something is measured, never stored, so a z step
corrected later re-measures a surface without re-extracting it.

**Objects.** Every vertex belongs to one object (`object_id`): a connected
piece of an isosurface (numbered by size, 1 the largest), the label id of a
segmented cell, or whatever an imported file grouped. No vertex is shared
between two objects, so hiding, isolating and colouring an object is a
per-vertex lookup, and the triangles are stored sorted by object with
`objects` = `[id, first triangle, count]` per object.

**The file** (`mesh-L<k>.bin`, the brick payload's convention):

    b"PXMS" | uint32 LE header length | JSON header | arrays

Each array is little-endian and starts on a 4-byte boundary; the header's
`arrays` lists `{name, dtype, shape, offset}` (offset from the file start).
`positions f32 (N,3)`, `normals i8 (N,4)` (unit normal * 127, the fourth byte
zero), `object_id u32 (N)`, `triangles u32 (M,3)`, `objects u32 (K,3)`, and
optionally `positions_raw f32 (N,3)` -- the unsmoothed vertices, for the
"compare unsmoothed" view. A browser reads it with typed-array views and no
copy.

**Normals** are computed in the voxel frame. The viewer draws the box scaled
by the image's anisotropy and transforms normals by the inverse transpose of
that scale, so they stay right whatever z step is set later.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from plexora.server.utils.jit import njit, register_primer

MAGIC = b"PXMS"
FORMAT_VERSION = 1


class MeshError(ValueError):
    """A mesh file that cannot be read; the message says why."""


@dataclass
class Mesh:
    positions: np.ndarray                      # (N, 3) float32, level-0 voxels, (x, y, z)
    triangles: np.ndarray                      # (M, 3) uint32
    object_id: np.ndarray                      # (N,) uint32
    normals: np.ndarray | None = None          # (N, 3) float32, voxel frame
    positions_raw: np.ndarray | None = None    # (N, 3) float32, before smoothing
    #: [id, first triangle, count] per object, triangles sorted by object.
    objects: np.ndarray | None = None
    header: dict = field(default_factory=dict)

    @property
    def n_vertices(self) -> int:
        return int(len(self.positions))

    @property
    def n_triangles(self) -> int:
        return int(len(self.triangles))

    @property
    def n_objects(self) -> int:
        return 0 if self.objects is None else int(len(self.objects))

    def ids(self) -> np.ndarray:
        return (np.zeros(0, np.uint32) if self.objects is None
                else self.objects[:, 0].astype(np.uint32))


def empty() -> Mesh:
    return Mesh(positions=np.zeros((0, 3), np.float32), triangles=np.zeros((0, 3), np.uint32),
                object_id=np.zeros(0, np.uint32), normals=np.zeros((0, 3), np.float32),
                objects=np.zeros((0, 3), np.uint32))


def concat(meshes) -> Mesh:
    """Several meshes as one (their vertices kept apart)."""
    meshes = [m for m in meshes if m is not None and m.n_vertices]
    if not meshes:
        return empty()
    offsets = np.cumsum([0] + [m.n_vertices for m in meshes[:-1]])
    out = Mesh(
        positions=np.concatenate([m.positions for m in meshes]).astype(np.float32),
        triangles=np.concatenate([m.triangles.astype(np.int64) + off
                                  for m, off in zip(meshes, offsets)]).astype(np.uint32),
        object_id=np.concatenate([m.object_id for m in meshes]).astype(np.uint32))
    if all(m.normals is not None for m in meshes):
        out.normals = np.concatenate([m.normals for m in meshes]).astype(np.float32)
    if all(m.positions_raw is not None for m in meshes):
        out.positions_raw = np.concatenate([m.positions_raw for m in meshes]).astype(np.float32)
    return out


# -- topology -------------------------------------------------------------------------


def weld(positions, triangles, object_id=None):
    """Merge vertices with bitwise-equal positions (and the same object).

    Exact on purpose: marching cubes over z-strips puts the vertices of the
    plane two strips share at identical coordinates (the strip offset is a
    whole number, and an edge's vertex is interpolated from the same two
    voxels either side), so no tolerance is needed and none can join two
    vertices that merely lie close. Returns `(positions, triangles, object_id)`
    with degenerate triangles dropped."""
    positions = np.ascontiguousarray(positions, dtype=np.float32)
    n = len(positions)
    if n == 0:
        return positions, np.zeros((0, 3), np.uint32), np.zeros(0, np.uint32)
    ids = (np.zeros(n, np.uint32) if object_id is None
           else np.ascontiguousarray(object_id, dtype=np.uint32))
    rows = np.empty((n, 4), np.uint32)
    rows[:, :3] = positions.view(np.uint32)
    rows[:, 3] = ids
    keys = rows.view(np.dtype((np.void, 16))).ravel()
    _, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    inverse = inverse.ravel()
    order = np.argsort(first)               # keep the original vertex order
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    tris = rank[inverse[np.asarray(triangles, dtype=np.int64)]]
    keep = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
    picked = first[order]
    return positions[picked], tris[keep].astype(np.uint32), ids[picked]


def compact(mesh: Mesh) -> Mesh:
    """Drop vertices no triangle uses."""
    if not mesh.n_triangles:
        return replace(mesh, positions=mesh.positions[:0], object_id=mesh.object_id[:0],
                       normals=None if mesh.normals is None else mesh.normals[:0],
                       positions_raw=None if mesh.positions_raw is None
                       else mesh.positions_raw[:0])
    used = np.zeros(mesh.n_vertices, bool)
    used[mesh.triangles.ravel()] = True
    if used.all():
        return mesh
    remap = np.cumsum(used) - 1
    return replace(
        mesh, positions=mesh.positions[used], object_id=mesh.object_id[used],
        triangles=remap[mesh.triangles].astype(np.uint32),
        normals=None if mesh.normals is None else mesh.normals[used],
        positions_raw=None if mesh.positions_raw is None else mesh.positions_raw[used])


def components(n_vertices: int, triangles) -> tuple[int, np.ndarray]:
    """Connected pieces of the vertex graph: `(count, label per vertex)`."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if n_vertices == 0:
        return 0, np.zeros(0, np.int64)
    t = np.asarray(triangles, dtype=np.int64)
    rows = np.concatenate([t[:, 0], t[:, 1]])
    cols = np.concatenate([t[:, 1], t[:, 2]])
    graph = coo_matrix((np.ones(len(rows), np.int8), (rows, cols)),
                       shape=(n_vertices, n_vertices))
    count, labels = connected_components(graph, directed=False)
    return int(count), labels.astype(np.int64)


def _edge_keys(triangles, n_vertices):
    t = np.asarray(triangles, dtype=np.int64)
    a = np.concatenate([t[:, 0], t[:, 1], t[:, 2]])
    b = np.concatenate([t[:, 1], t[:, 2], t[:, 0]])
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    return lo * np.int64(n_vertices) + hi, lo


def boundary_vertices(mesh: Mesh) -> np.ndarray:
    """Vertex indices on an edge only one triangle uses (an open rim)."""
    if not mesh.n_triangles:
        return np.zeros(0, np.int64)
    keys, lo = _edge_keys(mesh.triangles, mesh.n_vertices)
    unique, counts = np.unique(keys, return_counts=True)
    single = unique[counts == 1]
    if not len(single):
        return np.zeros(0, np.int64)
    n = np.int64(mesh.n_vertices)
    return np.unique(np.concatenate([single // n, single % n]))


def sort_by_object(mesh: Mesh) -> Mesh:
    """Triangles grouped by object (ascending id) and the `objects` ranges."""
    if not mesh.n_triangles:
        return replace(mesh, objects=np.zeros((0, 3), np.uint32))
    owner = mesh.object_id[mesh.triangles[:, 0]]
    order = np.argsort(owner, kind="stable")
    triangles = mesh.triangles[order]
    owner = owner[order]
    ids, first, counts = np.unique(owner, return_index=True, return_counts=True)
    objects = np.stack([ids, first, counts], axis=1).astype(np.uint32)
    return replace(mesh, triangles=triangles, objects=objects)


def renumber_by_size(mesh: Mesh, labels: np.ndarray) -> Mesh:
    """`object_id` from component `labels`, 1 = the piece with most triangles."""
    if not mesh.n_vertices:
        return mesh
    owner = labels[mesh.triangles[:, 0].astype(np.int64)]
    sizes = np.bincount(owner, minlength=int(labels.max()) + 1)
    order = np.argsort(-sizes, kind="stable")
    rank = np.empty_like(order)
    rank[order] = np.arange(1, len(order) + 1)
    return replace(mesh, object_id=rank[labels].astype(np.uint32))


# -- geometry -------------------------------------------------------------------------
#
# The per-vertex and per-triangle loops below are compiled kernels
# (plexora/server/utils/jit.py): on a cell-surface mesh of millions of
# triangles, numpy's scatter-adds and a scipy sparse operator were most of the
# build. Each sums in one fixed order, so a mesh is the same bytes wherever it
# is made.


@njit
def _face_normals_into(p, t, out):  # pragma: no cover - jit
    for i in range(t.shape[0]):
        a = t[i, 0]
        b = t[i, 1]
        c = t[i, 2]
        ux = p[b, 0] - p[a, 0]
        uy = p[b, 1] - p[a, 1]
        uz = p[b, 2] - p[a, 2]
        vx = p[c, 0] - p[a, 0]
        vy = p[c, 1] - p[a, 1]
        vz = p[c, 2] - p[a, 2]
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        for v in (a, b, c):
            out[v, 0] += nx
            out[v, 1] += ny
            out[v, 2] += nz
    return out


def vertex_normals(positions, triangles, scale=None) -> np.ndarray:
    """Area-weighted vertex normals, unit length, in the frame `positions *
    scale` (the voxel frame when `scale` is None)."""
    p = np.asarray(positions, dtype=np.float64)
    if scale is not None:
        p = p * np.asarray(scale, dtype=np.float64)
    out = np.zeros_like(p)
    if len(triangles):
        _face_normals_into(np.ascontiguousarray(p),
                           np.ascontiguousarray(triangles, dtype=np.int64), out)
    norm = np.linalg.norm(out, axis=1)
    norm[norm == 0] = 1.0
    return (out / norm[:, None]).astype(np.float32)


@njit
def _adjacency(n, t):  # pragma: no cover - jit
    """Each vertex's neighbours, once each and ascending: `(indptr, indices)`."""
    count = np.zeros(n + 1, np.int64)
    for i in range(t.shape[0]):
        for k in range(3):
            count[t[i, k] + 1] += 2
    start = np.cumsum(count)
    fill = start[:-1].copy()
    entries = np.empty(start[-1], np.int64)
    for i in range(t.shape[0]):
        for k in range(3):
            a = t[i, k]
            b = t[i, (k + 1) % 3]
            entries[fill[a]] = b
            fill[a] += 1
            entries[fill[b]] = a
            fill[b] += 1
    indptr = np.zeros(n + 1, np.int64)
    indices = np.empty(start[-1], np.int64)
    m = 0
    for v in range(n):
        # A vertex has a handful of neighbours: an insertion sort in place.
        a0 = start[v]
        a1 = start[v + 1]
        for i in range(a0 + 1, a1):
            key = entries[i]
            j = i - 1
            while j >= a0 and entries[j] > key:
                entries[j + 1] = entries[j]
                j -= 1
            entries[j + 1] = key
        last = -1
        for i in range(a0, a1):
            if entries[i] != last:
                last = entries[i]
                indices[m] = last
                m += 1
        indptr[v + 1] = m
    return indptr, indices[:m]


@njit
def _umbrella_step(p, indptr, indices, factor, out):  # pragma: no cover - jit
    """`out = p + factor * (mean of the neighbours - p)`, vertex by vertex."""
    for v in range(p.shape[0]):
        k0 = indptr[v]
        k1 = indptr[v + 1]
        if k1 == k0:
            out[v, 0] = p[v, 0]
            out[v, 1] = p[v, 1]
            out[v, 2] = p[v, 2]
            continue
        sx = 0.0
        sy = 0.0
        sz = 0.0
        for k in range(k0, k1):
            j = indices[k]
            sx += p[j, 0]
            sy += p[j, 1]
            sz += p[j, 2]
        d = float(k1 - k0)
        out[v, 0] = p[v, 0] + factor * (sx / d - p[v, 0])
        out[v, 1] = p[v, 1] + factor * (sy / d - p[v, 1])
        out[v, 2] = p[v, 2] + factor * (sz / d - p[v, 2])
    return out


@njit
def _signed_volumes(p, t, starts, counts, scale, out):  # pragma: no cover - jit
    sx = scale[0]
    sy = scale[1]
    sz = scale[2]
    for o in range(starts.shape[0]):
        total = 0.0
        for i in range(starts[o], starts[o] + counts[o]):
            a = t[i, 0]
            b = t[i, 1]
            c = t[i, 2]
            ax, ay, az = p[a, 0] * sx, p[a, 1] * sy, p[a, 2] * sz
            bx, by, bz = p[b, 0] * sx, p[b, 1] * sy, p[b, 2] * sz
            cx, cy, cz = p[c, 0] * sx, p[c, 1] * sy, p[c, 2] * sz
            total += ax * (by * cz - bz * cy) + ay * (bz * cx - bx * cz) + az * (bx * cy - by * cx)
        out[o] = total / 6.0
    return out


def object_volumes(mesh: Mesh, scale=(1.0, 1.0, 1.0)) -> np.ndarray:
    """Each object's signed enclosed volume in `scale` units, in `objects` order."""
    mesh = mesh if mesh.objects is not None else sort_by_object(mesh)
    out = np.zeros(mesh.n_objects, np.float64)
    if not mesh.n_objects:
        return out
    return _signed_volumes(np.ascontiguousarray(mesh.positions, dtype=np.float64),
                           np.ascontiguousarray(mesh.triangles, dtype=np.int64),
                           mesh.objects[:, 1].astype(np.int64),
                           mesh.objects[:, 2].astype(np.int64),
                           np.asarray(scale, dtype=np.float64), out)


@dataclass(frozen=True)
class Smoothing:
    """Taubin smoothing: `iterations` of a shrink step `lam` and an inflate step
    `mu`, each vertex kept within `constraint` voxels (per axis, at the
    extraction level) of where marching cubes put it."""

    iterations: int = 0
    lam: float = 0.5
    mu: float = -0.53
    constraint: float = 0.5

    PRESETS = {"none": 0, "light": 6, "medium": 12, "strong": 24}

    @classmethod
    def preset(cls, name, constraint=0.5):
        return cls(iterations=cls.PRESETS.get(str(name or "none"), 0),
                   constraint=float(constraint))

    def to_dict(self) -> dict:
        return {"method": "taubin", "iterations": self.iterations, "lambda": self.lam,
                "mu": self.mu, "constraint_vox": self.constraint}


def taubin(positions, triangles, smoothing: Smoothing, *, scale=(1.0, 1.0, 1.0),
           voxel=(1.0, 1.0, 1.0), progress=None):
    """Smoothed positions and a report of how far they moved.

    Computed in the physical frame (`positions * scale`), so an anisotropic
    stack is smoothed as the tissue was, not as its voxels are; `voxel` is
    the extraction level's voxel size in level-0 voxels per axis, which the
    constraint is counted in. Each step moves a vertex towards the mean of
    its neighbours (each counted once). Returns `(positions, report)`; the
    report is in the units of `scale` (µm when it is the spacing).
    `progress(done, total)` is told after each iteration."""
    positions = np.asarray(positions, dtype=np.float64)
    if smoothing.iterations <= 0 or not len(triangles):
        return positions.astype(np.float32), {"max_displacement": 0.0,
                                              "mean_displacement": 0.0}
    scale = np.asarray(scale, dtype=np.float64)
    start = positions * scale
    cap = smoothing.constraint * np.asarray(voxel, dtype=np.float64) * scale
    indptr, indices = _adjacency(len(positions), np.ascontiguousarray(triangles, dtype=np.int64))
    p = start.copy()
    q = np.empty_like(p)
    lo, hi = start - cap, start + cap
    total = int(smoothing.iterations)
    for done in range(total):
        _umbrella_step(p, indptr, indices, float(smoothing.lam), q)
        _umbrella_step(q, indptr, indices, float(smoothing.mu), p)
        np.clip(p, lo, hi, out=p)
        if progress is not None:
            progress(done + 1, total)
    moved = np.linalg.norm(p - start, axis=1)
    report = {"max_displacement": float(moved.max()) if len(moved) else 0.0,
              "mean_displacement": float(moved.mean()) if len(moved) else 0.0}
    return (p / scale).astype(np.float32), report


def inset(positions, normals, distance: float) -> np.ndarray:
    """Every vertex moved `distance` voxels against its normal: neighbouring
    label surfaces, which meet on the same voxel faces, then sit apart instead
    of drawing through each other."""
    if not distance:
        return np.asarray(positions, dtype=np.float32)
    return (np.asarray(positions, dtype=np.float64)
            - float(distance) * np.asarray(normals, dtype=np.float64)).astype(np.float32)


def signed_volume(positions, triangles, scale=(1.0, 1.0, 1.0)) -> float:
    p = np.asarray(positions, dtype=np.float64) * np.asarray(scale, dtype=np.float64)
    t = np.asarray(triangles, dtype=np.int64)
    if not len(t):
        return 0.0
    return float(np.einsum("ij,ij->i", p[t[:, 0]], np.cross(p[t[:, 1]], p[t[:, 2]])).sum() / 6.0)


def orient_outward(mesh: Mesh) -> Mesh:
    """Flip every object whose enclosed volume comes out negative, so normals
    point out of what the surface encloses."""
    if not mesh.n_triangles:
        return mesh
    mesh = sort_by_object(mesh) if mesh.objects is None else mesh
    t = mesh.triangles.astype(np.int64)
    p = mesh.positions.astype(np.float64)
    per = np.einsum("ij,ij->i", p[t[:, 0]], np.cross(p[t[:, 1]], p[t[:, 2]]))
    volumes = np.add.reduceat(per, mesh.objects[:, 1].astype(np.int64))
    flip = np.repeat(volumes < 0, mesh.objects[:, 2].astype(np.int64))
    if not flip.any():
        return mesh
    triangles = mesh.triangles.copy()
    triangles[flip] = triangles[flip][:, [0, 2, 1]]
    return replace(mesh, triangles=triangles)


# -- measurement ----------------------------------------------------------------------


@njit
def _object_sums(v, t, starts, counts, scale, sums, box):  # pragma: no cover - jit
    """Per object, over its triangles: area, signed volume, the volume- and
    area-weighted centroid sums (`sums`, scaled), and the box (`box`, unscaled)."""
    sx = scale[0]
    sy = scale[1]
    sz = scale[2]
    for o in range(starts.shape[0]):
        for q in range(8):
            sums[o, q] = 0.0
        for q in range(3):
            box[o, q] = np.inf
            box[o, 3 + q] = -np.inf
        for i in range(starts[o], starts[o] + counts[o]):
            a = t[i, 0]
            b = t[i, 1]
            c = t[i, 2]
            ax, ay, az = v[a, 0] * sx, v[a, 1] * sy, v[a, 2] * sz
            bx, by, bz = v[b, 0] * sx, v[b, 1] * sy, v[b, 2] * sz
            cx, cy, cz = v[c, 0] * sx, v[c, 1] * sy, v[c, 2] * sz
            ux, uy, uz = bx - ax, by - ay, bz - az
            wx, wy, wz = cx - ax, cy - ay, cz - az
            nx = uy * wz - uz * wy
            ny = uz * wx - ux * wz
            nz = ux * wy - uy * wx
            tri_area = 0.5 * np.sqrt(nx * nx + ny * ny + nz * nz)
            tri_vol = (ax * (by * cz - bz * cy) + ay * (bz * cx - bx * cz)
                       + az * (bx * cy - by * cx)) / 6.0
            mx, my, mz = ax + bx + cx, ay + by + cy, az + bz + cz
            sums[o, 0] += tri_area
            sums[o, 1] += tri_vol
            sums[o, 2] += mx / 4.0 * tri_vol
            sums[o, 3] += my / 4.0 * tri_vol
            sums[o, 4] += mz / 4.0 * tri_vol
            sums[o, 5] += mx / 3.0 * tri_area
            sums[o, 6] += my / 3.0 * tri_area
            sums[o, 7] += mz / 3.0 * tri_area
            for corner in (a, b, c):
                for q in range(3):
                    value = v[corner, q]
                    if value < box[o, q]:
                        box[o, q] = value
                    if value > box[o, 3 + q]:
                        box[o, 3 + q] = value
    return sums


@njit
def _object_moments(v, t, starts, counts, scale, centroid, out):  # pragma: no cover - jit
    """Per object, the area-weighted second moment of its triangles' centres
    about `centroid`: (xx, xy, xz, yy, yz, zz)."""
    sx = scale[0]
    sy = scale[1]
    sz = scale[2]
    for o in range(starts.shape[0]):
        for q in range(6):
            out[o, q] = 0.0
        gx, gy, gz = centroid[o, 0], centroid[o, 1], centroid[o, 2]
        for i in range(starts[o], starts[o] + counts[o]):
            a = t[i, 0]
            b = t[i, 1]
            c = t[i, 2]
            ax, ay, az = v[a, 0] * sx, v[a, 1] * sy, v[a, 2] * sz
            bx, by, bz = v[b, 0] * sx, v[b, 1] * sy, v[b, 2] * sz
            cx, cy, cz = v[c, 0] * sx, v[c, 1] * sy, v[c, 2] * sz
            ux, uy, uz = bx - ax, by - ay, bz - az
            wx, wy, wz = cx - ax, cy - ay, cz - az
            nx = uy * wz - uz * wy
            ny = uz * wx - ux * wz
            nz = ux * wy - uy * wx
            tri_area = 0.5 * np.sqrt(nx * nx + ny * ny + nz * nz)
            rx = (ax + bx + cx) / 3.0 - gx
            ry = (ay + by + cy) / 3.0 - gy
            rz = (az + bz + cz) / 3.0 - gz
            out[o, 0] += rx * rx * tri_area
            out[o, 1] += rx * ry * tri_area
            out[o, 2] += rx * rz * tri_area
            out[o, 3] += ry * ry * tri_area
            out[o, 4] += ry * rz * tri_area
            out[o, 5] += rz * rz * tri_area
    return out


def _rim_vertices(triangles, n_vertices) -> np.ndarray:
    """The lower vertex of every edge only one triangle uses."""
    keys, _ = _edge_keys(triangles, n_vertices)
    keys.sort()
    if not len(keys):
        return np.zeros(0, np.int64)
    step = keys[1:] != keys[:-1]
    single = np.concatenate([[True], step]) & np.concatenate([step, [True]])
    return keys[single] // np.int64(n_vertices)


def measure(mesh: Mesh, *, scale=(1.0, 1.0, 1.0), physical=False, dims=None) -> dict:
    """One row per object, as columns (numpy arrays).

    `scale` is the spacing (µm per voxel) when `physical`, else ones and the
    columns are in voxels. `dims` = (width, height, depth) of the volume, for
    `touches_bounds`. A volume is reported only for a closed object; an open
    one (a piece cut by an imported file, say) has NaN and `closed` False.
    """
    unit = "um" if physical else "vox"
    mesh = mesh if mesh.objects is not None else sort_by_object(mesh)
    ids = mesh.ids()
    k = len(ids)
    out = {"object": ids.astype(np.int64)}
    names = [f"mesh_area_{unit}2", f"mesh_volume_{unit}3", f"equivalent_diameter_{unit}",
             "sphericity", "elongation", "centroid_x", "centroid_y", "centroid_z",
             f"centroid_x_{unit}", f"centroid_y_{unit}", f"centroid_z_{unit}",
             "bbox_x0", "bbox_x1", "bbox_y0", "bbox_y1", "bbox_z0", "bbox_z1"]
    if k == 0:
        out.update({name: np.zeros(0) for name in names})
        out.update({"n_triangles": np.zeros(0, np.int64), "closed": np.zeros(0, bool),
                    "touches_bounds": np.zeros(0, bool)})
        return out
    scale = np.asarray(scale, dtype=np.float64)
    t = np.ascontiguousarray(mesh.triangles, dtype=np.int64)
    v = np.ascontiguousarray(mesh.positions, dtype=np.float64)
    starts = mesh.objects[:, 1].astype(np.int64)
    counts = mesh.objects[:, 2].astype(np.int64)
    box = np.zeros((k, 6))
    sums = _object_sums(v, t, starts, counts, scale, np.zeros((k, 8)), box)
    area, volume = sums[:, 0], sums[:, 1]
    # Volume-weighted centroid of a closed surface; area-weighted otherwise.
    vc, ac = sums[:, 2:5], sums[:, 5:8]

    # Closedness per object: an edge used by one triangle only is a rim.
    rim_vertices = _rim_vertices(t, mesh.n_vertices)
    rims = np.bincount(np.searchsorted(ids, mesh.object_id[rim_vertices]), minlength=k) \
        if len(rim_vertices) else np.zeros(k, np.int64)
    closed = rims[:k] == 0

    with np.errstate(divide="ignore", invalid="ignore"):
        centroid = np.where(closed[:, None] & (np.abs(volume)[:, None] > 0),
                            vc / volume[:, None], ac / area[:, None])
        volume_out = np.where(closed, np.abs(volume), np.nan)
        eq_diam = np.cbrt(6.0 * volume_out / math.pi)
        sphericity = (math.pi ** (1 / 3)) * (6.0 * volume_out) ** (2 / 3) / area

    # Elongation: the spread of the surface along its longest axis over its
    # shortest (1 for a sphere), from the area-weighted second moment.
    second = _object_moments(v, t, starts, counts, scale,
                             np.ascontiguousarray(np.nan_to_num(centroid)), np.zeros((k, 6)))
    moments = np.empty((k, 3, 3))
    for (i, j), q in zip(((0, 0), (0, 1), (0, 2), (1, 1), (1, 2), (2, 2)), range(6)):
        moments[:, i, j] = moments[:, j, i] = second[:, q]
    with np.errstate(divide="ignore", invalid="ignore"):
        moments = np.nan_to_num(moments / area[:, None, None])
        eig = np.linalg.eigvalsh(moments)
        elong = np.sqrt(np.where(eig[:, 0] > 0, eig[:, 2] / eig[:, 0], np.nan))

    lo_box, hi_box = box[:, :3], box[:, 3:]
    out.update({
        f"mesh_area_{unit}2": area,
        f"mesh_volume_{unit}3": volume_out,
        f"equivalent_diameter_{unit}": eq_diam,
        "sphericity": sphericity,
        "elongation": elong,
        "centroid_x": centroid[:, 0] / scale[0],
        "centroid_y": centroid[:, 1] / scale[1],
        "centroid_z": centroid[:, 2] / scale[2],
        f"centroid_x_{unit}": centroid[:, 0],
        f"centroid_y_{unit}": centroid[:, 1],
        f"centroid_z_{unit}": centroid[:, 2],
        "bbox_x0": lo_box[:, 0], "bbox_x1": hi_box[:, 0],
        "bbox_y0": lo_box[:, 1], "bbox_y1": hi_box[:, 1],
        "bbox_z0": lo_box[:, 2], "bbox_z1": hi_box[:, 2],
        "n_triangles": counts,
        "closed": closed,
    })
    if dims is not None:
        # A surface closed by the volume's edge lies on the outermost voxels'
        # centres (an isosurface) or their outer faces (a label surface).
        top = np.asarray(dims, dtype=np.float64) - 1.0
        out["touches_bounds"] = ((lo_box <= 0.5).any(axis=1)
                                 | (hi_box >= top - 0.5).any(axis=1))
    else:
        out["touches_bounds"] = np.zeros(k, bool)
    return out


# -- the file -------------------------------------------------------------------------


def _pack_normals(normals) -> np.ndarray:
    n = np.asarray(normals, dtype=np.float32)
    out = np.zeros((len(n), 4), np.int8)
    out[:, :3] = np.clip(np.rint(n * 127.0), -127, 127).astype(np.int8)
    return out


def _unpack_normals(packed) -> np.ndarray:
    n = np.asarray(packed[:, :3], dtype=np.float32) / 127.0
    norm = np.linalg.norm(n, axis=1)
    norm[norm == 0] = 1.0
    return n / norm[:, None]


def to_bytes(mesh: Mesh, header: dict | None = None) -> bytes:
    """The mesh as one `mesh-L<k>.bin` payload."""
    mesh = mesh if mesh.objects is not None else sort_by_object(mesh)
    normals = mesh.normals if mesh.normals is not None else vertex_normals(
        mesh.positions, mesh.triangles)
    arrays = [("positions", np.ascontiguousarray(mesh.positions, "<f4")),
              ("normals", _pack_normals(normals)),
              ("object_id", np.ascontiguousarray(mesh.object_id, "<u4")),
              ("triangles", np.ascontiguousarray(mesh.triangles, "<u4")),
              ("objects", np.ascontiguousarray(mesh.objects, "<u4"))]
    if mesh.positions_raw is not None:
        arrays.append(("positions_raw", np.ascontiguousarray(mesh.positions_raw, "<f4")))
    meta = dict(header or {})
    meta.update({"format": "plexora-mesh", "version": FORMAT_VERSION,
                 "vertices": mesh.n_vertices, "triangles": mesh.n_triangles,
                 "objects": mesh.n_objects})
    if mesh.n_vertices:
        meta["bounds"] = [mesh.positions.min(axis=0).tolist(), mesh.positions.max(axis=0).tolist()]
    # Two passes: the offsets depend on the header's own length.
    table = []
    for _ in range(2):
        meta["arrays"] = table
        text = json.dumps(meta, separators=(",", ":")).encode("utf-8")
        start = 8 + len(text)
        start += (-start) % 8
        table = []
        offset = start
        for name, array in arrays:
            table.append({"name": name, "dtype": array.dtype.str.lstrip("<|"),
                          "shape": list(array.shape), "offset": offset})
            offset += array.nbytes
            offset += (-offset) % 4
    meta["arrays"] = table
    text = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    head = MAGIC + struct.pack("<I", len(text)) + text
    head += b" " * ((-len(head)) % 8)
    parts = [head]
    position = len(head)
    for (name, array), entry in zip(arrays, table):
        if entry["offset"] != position:
            parts.append(b"\0" * (entry["offset"] - position))
            position = entry["offset"]
        data = array.tobytes()
        parts.append(data)
        position += len(data)
        pad = (-position) % 4
        if pad:
            parts.append(b"\0" * pad)
            position += pad
    return b"".join(parts)


def from_bytes(data: bytes) -> Mesh:
    if len(data) < 8 or data[:4] != MAGIC:
        raise MeshError("not a Plexora mesh file")
    (length,) = struct.unpack("<I", data[4:8])
    try:
        meta = json.loads(data[8:8 + length].decode("utf-8"))
    except ValueError:
        raise MeshError("the mesh header is not JSON") from None
    arrays = {}
    for entry in meta.get("arrays") or ():
        dtype = np.dtype("<" + entry["dtype"]) if entry["dtype"] not in ("i1", "u1") \
            else np.dtype(entry["dtype"])
        count = int(np.prod(entry["shape"])) if entry["shape"] else 0
        arrays[entry["name"]] = np.frombuffer(data, dtype=dtype, count=count,
                                              offset=int(entry["offset"])).reshape(entry["shape"])
    try:
        mesh = Mesh(positions=arrays["positions"].astype(np.float32),
                    triangles=arrays["triangles"].astype(np.uint32),
                    object_id=arrays["object_id"].astype(np.uint32),
                    normals=_unpack_normals(arrays["normals"]),
                    objects=arrays["objects"].astype(np.uint32).reshape(-1, 3),
                    positions_raw=(arrays["positions_raw"].astype(np.float32)
                                   if "positions_raw" in arrays else None),
                    header=meta)
    except KeyError as missing:
        raise MeshError(f"the mesh file has no {missing.args[0]} array") from None
    return mesh


def write(mesh: Mesh, path, header: dict | None = None) -> dict:
    """Write atomically; returns `{bytes, sha256, triangles, vertices}`."""
    data = to_bytes(mesh, header)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "triangles": mesh.n_triangles, "vertices": mesh.n_vertices}


def read(path) -> Mesh:
    return from_bytes(Path(path).read_bytes())


def subset(mesh: Mesh, ids) -> Mesh:
    """Only the objects in `ids` (a sorted mesh's ranges make this a slice)."""
    mesh = mesh if mesh.objects is not None else sort_by_object(mesh)
    wanted = np.isin(mesh.objects[:, 0], np.asarray(list(ids), dtype=np.uint32))
    if wanted.all():
        return mesh
    ranges = mesh.objects[wanted]
    pick = (np.concatenate([np.arange(f, f + c, dtype=np.int64) for _, f, c in ranges])
            if len(ranges) else np.zeros(0, np.int64))
    out = replace(mesh, triangles=mesh.triangles[pick], objects=None)
    return sort_by_object(compact(out))


def _prime():
    tri = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 1], [1, 3, 2]], np.int64)
    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], np.float64)
    vertex_normals(pos, tri)
    taubin(pos, tri, Smoothing(iterations=1))
    tetra = sort_by_object(Mesh(positions=pos.astype(np.float32), triangles=tri.astype(np.uint32),
                                object_id=np.ones(4, np.uint32)))
    object_volumes(tetra)
    measure(tetra)


register_primer(__name__)


def prime():
    _prime()
