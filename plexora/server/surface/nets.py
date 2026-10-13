"""Surface nets over a whole label volume: every cell's closed surface in one pass.

The dual of the voxel grid. A CUBE is the space between eight neighbouring
voxel centres; a cube whose corners are not all one label holds one vertex for
each label among its corners, at the centroid of the midpoints of the cube's
edges that cross that label's boundary. Every voxel face between two different
labels -- an EDGE of the dual grid -- is crossed by one quad of each nonzero
label beside it, joining that label's vertices in the four cubes around the
edge, wound so it faces out of the label. So each cell is closed by
construction, two cells that touch stay two surfaces (each has its own
vertices along the shared faces), there is no case table, and no cell is ever
cut out of the volume as a binary mask of its own: the whole volume is read
once, whatever the number of cells.

**Coordinates.** Grid voxel `(z, y, x)` is centred at `(x, y, z)`. The grid
is padded by one background voxel on every side, so a cell cut by the edge of
the volume is capped on its outer voxel faces; a cube is named by its padded
index (0 .. size along each axis), a vertex by `(cube, label)`.

**Blocks.** The volume is read in z-slabs, each slab in tiles that follow the
store's chunks, on a pool of threads (`nogil` kernels; see
plexora/server/utils/jit.py for why never `parallel=True`). A tile reads only
its own voxels -- a halo would decode a neighbouring chunk for one row -- plus
one plane below it along z (chunks are one plane deep, so that costs one
plane). The cubes that straddle two tiles are made afterwards from the rows
and columns each tile kept of its edges: one strip per seam, the y-seams
first, then the x-seams without the cubes the y-seams made.

**Determinism.** A vertex's position is an exact sum divided once, computed
from its global cube index; the vertices are ordered by (label, cube) and the
quads by (label, cube, axis) after every block is in. The mesh is therefore
the same bytes for any tile size, any number of threads, compiled or
interpreted -- which is how a data node's build equals this machine's.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from plexora.server.utils.jit import njit, register_primer

#: Tiles follow the label store's chunks (volume/labels.CHUNK).
TILE = 1024
#: Voxels of the level read by one block at most (uint32: 64 MB). A thread
#: holds about two such buffers while it works.
BLOCK_VOXELS = 16 * 1024 * 1024
#: Threads at most, whatever the machine.
MAX_THREADS = 8

#: Corner c of a cube is at offset (x, y, z) = (c & 1, (c >> 1) & 1, c >> 2).
_CORNERS = np.array([[c & 1, (c >> 1) & 1, c >> 2] for c in range(8)], np.int64)
#: The cube's twelve edges as corner pairs: four along x, four along y, four along z.
_EDGES = np.array([[0, 1], [2, 3], [4, 5], [6, 7],
                   [0, 2], [1, 3], [4, 6], [5, 7],
                   [0, 4], [1, 5], [2, 6], [3, 7]], np.int64)


# -- kernels ---------------------------------------------------------------------------


@njit
def _pool_runs(planes, stride, out):  # pragma: no cover - jit
    """Each run of `stride` planes as one plane: per pixel, the nonzero label
    on most of the run's planes (the earliest on a tie), 0 where none is. A
    short last run pools the planes there are."""
    n, height, width = planes.shape
    for p in range(out.shape[0]):
        z0 = p * stride
        z1 = min(n, z0 + stride)
        for y in range(height):
            for x in range(width):
                first = planes[z0, y, x]
                same = True
                for z in range(z0 + 1, z1):
                    if planes[z, y, x] != first:
                        same = False
                        break
                if same:
                    out[p, y, x] = first
                    continue
                best = 0
                best_n = 0
                for z in range(z0, z1):
                    value = planes[z, y, x]
                    if value == 0 or value == best:
                        continue
                    count = 0
                    for zz in range(z0, z1):
                        if planes[zz, y, x] == value:
                            count += 1
                    if count > best_n:
                        best = value
                        best_n = count
                out[p, y, x] = best
    return out


@njit
def _corners(data, i, j, k, c):  # pragma: no cover - jit
    c[0] = data[i, j, k]
    c[1] = data[i, j, k + 1]
    c[2] = data[i, j + 1, k]
    c[3] = data[i, j + 1, k + 1]
    c[4] = data[i + 1, j, k]
    c[5] = data[i + 1, j, k + 1]
    c[6] = data[i + 1, j + 1, k]
    c[7] = data[i + 1, j + 1, k + 1]
    for n in range(1, 8):
        if c[n] != c[0]:
            return False
    return True


@njit
def _distinct(c, seen):  # pragma: no cover - jit
    """The nonzero labels among the corners into `seen`, in corner order."""
    m = 0
    for n in range(8):
        value = c[n]
        if value == 0:
            continue
        new = True
        for q in range(m):
            if seen[q] == value:
                new = False
                break
        if new:
            seen[m] = value
            m += 1
    return m


@njit
def _count(data, skip_y):  # pragma: no cover - jit
    """`(vertices, quads)` the cubes of `data` make (rows `skip_y` left out)."""
    nz, ny, nx = data.shape
    c = np.empty(8, data.dtype)
    seen = np.empty(8, data.dtype)
    nv = 0
    nq = 0
    for i in range(nz - 1):
        for j in range(ny - 1):
            if skip_y[j]:
                continue
            for k in range(nx - 1):
                if _corners(data, i, j, k, c):
                    continue
                nv += _distinct(c, seen)
                low = c[0]
                for e in (1, 2, 4):
                    high = c[e]
                    if high != low:
                        if low != 0:
                            nq += 1
                        if high != 0:
                            nq += 1
    return nv, nq


@njit
def _fill(data, skip_y, cz0, cy0, cx0, stride_y, stride_z, corners, edges,
          vcube, vlabel, vpos, qcube, qaxis, qlow, qlabel):  # pragma: no cover - jit
    """The vertices and quads `_count` counted. `data[0, 0, 0]` is padded
    voxel (cz0, cy0, cx0); a cube's global index is `z * stride_z + y *
    stride_y + x`. A vertex's position is in grid voxels (x, y, z); a quad is
    its edge's cube (whose corner 0 is the edge's low end), axis (0 x, 1 y,
    2 z), whether its label is on the low end, and the label."""
    nz, ny, nx = data.shape
    c = np.empty(8, data.dtype)
    seen = np.empty(8, data.dtype)
    nv = 0
    nq = 0
    for i in range(nz - 1):
        for j in range(ny - 1):
            if skip_y[j]:
                continue
            for k in range(nx - 1):
                if _corners(data, i, j, k, c):
                    continue
                gz = cz0 + i
                gy = cy0 + j
                gx = cx0 + k
                cube = gz * stride_z + gy * stride_y + gx
                m = _distinct(c, seen)
                for q in range(m):
                    label = seen[q]
                    n = 0
                    sx = 0
                    sy = 0
                    sz = 0
                    for e in range(12):
                        a = edges[e, 0]
                        b = edges[e, 1]
                        if (c[a] == label) != (c[b] == label):
                            n += 1
                            sx += corners[a, 0] + corners[b, 0]
                            sy += corners[a, 1] + corners[b, 1]
                            sz += corners[a, 2] + corners[b, 2]
                    # Padded index - 1 is the grid voxel; each sum is of
                    # twice the edge midpoints, divided once.
                    vcube[nv] = cube
                    vlabel[nv] = label
                    vpos[nv, 0] = (gx - 1) + sx / (2.0 * n)
                    vpos[nv, 1] = (gy - 1) + sy / (2.0 * n)
                    vpos[nv, 2] = (gz - 1) + sz / (2.0 * n)
                    nv += 1
                low = c[0]
                for axis in range(3):
                    high = c[1 << axis]
                    if high == low:
                        continue
                    if low != 0:
                        qcube[nq] = cube
                        qaxis[nq] = axis
                        qlow[nq] = 1
                        qlabel[nq] = low
                        nq += 1
                    if high != 0:
                        qcube[nq] = cube
                        qaxis[nq] = axis
                        qlow[nq] = 0
                        qlabel[nq] = high
                        nq += 1
    return nv, nq


# -- one block -------------------------------------------------------------------------


class Piece:
    """What one block made: vertices `(cube, label, position)` and quads
    `(cube, axis, low, label)`."""

    __slots__ = ("vcube", "vlabel", "vpos", "qcube", "qaxis", "qlow", "qlabel")

    def __init__(self, nv=0, nq=0):
        self.vcube = np.zeros(nv, np.int64)
        self.vlabel = np.zeros(nv, np.uint32)
        self.vpos = np.zeros((nv, 3), np.float64)
        self.qcube = np.zeros(nq, np.int64)
        self.qaxis = np.zeros(nq, np.int8)
        self.qlow = np.zeros(nq, np.int8)
        self.qlabel = np.zeros(nq, np.uint32)


def block_surface(data, origin, cube_shape, skip_y=None) -> Piece:
    """The vertices and quads of every cube of `data` (uint32, padded voxels
    starting at `origin` = (z, y, x)) whose rows are not in `skip_y`.
    `cube_shape` is the whole volume's cube count (Z + 1, H + 1, W + 1)."""
    data = np.ascontiguousarray(data, dtype=np.uint32)
    if skip_y is None:
        skip_y = np.zeros(max(0, data.shape[1] - 1), np.bool_)
    if min(data.shape) < 2:
        return Piece()
    nv, nq = _count(data, skip_y)
    piece = Piece(nv, nq)
    if nv or nq:
        _, cy, cx = (int(v) for v in cube_shape)
        _fill(data, skip_y, int(origin[0]), int(origin[1]), int(origin[2]),
              np.int64(cx), np.int64(cy) * np.int64(cx), _CORNERS, _EDGES,
              piece.vcube, piece.vlabel, piece.vpos, piece.qcube, piece.qaxis, piece.qlow,
              piece.qlabel)
    return piece


# -- the volume, in blocks -------------------------------------------------------------


def pool(planes, stride):
    """`planes` (n, H, W) pooled `stride` at a time (`_pool_runs`)."""
    planes = np.ascontiguousarray(planes, dtype=np.uint32)
    stride = max(1, int(stride))
    if stride == 1:
        return planes
    out = np.zeros((-(-planes.shape[0] // stride),) + planes.shape[1:], np.uint32)
    return _pool_runs(planes, np.int64(stride), out)


def _spans(size, tile):
    return [(a, min(size, a + tile)) for a in range(0, size, tile)]


def _slabs(depth, rows, cols, stride, block_voxels):
    """Cube ranges along z (cubes 0 .. depth inclusive): as many grid planes
    per slab as keep one tile's read under `block_voxels`."""
    per = max(1, int(block_voxels) // max(1, rows * cols * stride) - 1)
    edges = list(range(0, depth + 1, per)) + [depth + 1]
    return [(a, b) for a, b in zip(edges[:-1], edges[1:]) if b > a]


def _overlaps(regions, z, y, x):
    """Whether any region (grid voxels, inclusive) meets the box `z, y, x`
    (half-open ranges)."""
    if regions is None:
        return True
    if not len(regions):
        return False
    r = regions
    return bool(np.any((r[:, 0] < z[1]) & (r[:, 1] >= z[0]) & (r[:, 2] < y[1])
                       & (r[:, 3] >= y[0]) & (r[:, 4] < x[1]) & (r[:, 5] >= x[0])))


class Volume:
    """The grid one extraction reads: `read(z0, z1, rows, cols)` returns grid
    planes z0..z1-1 (pooled already) of the given rows and columns, uint32,
    with ids not wanted set to 0."""

    def __init__(self, label_volume, level, shape, *, z_stride=1, ids=None):
        self.source = label_volume
        self.level = int(level)
        self.shape = tuple(int(v) for v in shape)       # grid (Z, H, W)
        self.stride = max(1, int(z_stride))
        self.depth0 = int(label_volume.depth)
        self.ids = None if ids is None else np.unique(np.asarray(list(ids), dtype=np.uint32))

    def read(self, z0, z1, rows, cols):
        a = z0 * self.stride
        b = min(self.depth0, z1 * self.stride) - 1
        planes = np.asarray(self.source.read(self.level, a, b, slice(*rows), slice(*cols)))
        grid = pool(planes, self.stride)
        if self.ids is not None:
            grid = np.where(np.isin(grid, self.ids), grid, np.uint32(0))
        return grid


def _tile_block(volume, slab, rows, cols):
    """Read one tile of one slab into its padded block. Returns `(data,
    origin, edges)`, `edges` the tile's first and last rows and columns
    (every plane of the block) for the seams."""
    depth, height, width = volume.shape
    cz0, cz1 = slab
    y0, y1 = rows
    x0, x1 = cols
    py = 0 if y0 == 0 else y0 + 1
    qy = height + 1 if y1 == height else y1
    px = 0 if x0 == 0 else x0 + 1
    qx = width + 1 if x1 == width else x1
    data = np.zeros((cz1 - cz0 + 1, qy - py + 1, qx - px + 1), np.uint32)
    g0, g1 = max(0, cz0 - 1), min(depth, cz1)
    if g1 > g0:
        block = volume.read(g0, g1, rows, cols)
        lz = g0 - (cz0 - 1)
        data[lz:lz + block.shape[0], y0 + 1 - py:y1 + 1 - py, x0 + 1 - px:x1 + 1 - px] = block
    ry0, ry1 = y0 + 1 - py, y1 - py              # local rows of grid y0 and y1 - 1
    rx0, rx1 = x0 + 1 - px, x1 - px
    edges = (np.ascontiguousarray(data[:, ry0, rx0:rx1 + 1]),
             np.ascontiguousarray(data[:, ry1, rx0:rx1 + 1]),
             np.ascontiguousarray(data[:, ry0:ry1 + 1, rx0]),
             np.ascontiguousarray(data[:, ry0:ry1 + 1, rx1]))
    if not data.any():
        return None, (cz0, py, px), None
    return data, (cz0, py, px), edges


def surface_pieces(volume, *, regions=None, tile=TILE, block_voxels=BLOCK_VOXELS,
                   threads=None, progress=None, cancelled=None):
    """Every block's `Piece` of `volume` (a `Volume`), in block order, and a
    report. `regions` (n, 6) grid boxes `(z0, z1, y0, y1, x0, x1)` inclusive
    limit the tiles read to those that meet one; None reads every tile.
    `tile` is the tile's side, or `(rows, columns)`."""
    depth, height, width = volume.shape
    cube_shape = (depth + 1, height + 1, width + 1)
    tile_y, tile_x = (tile, tile) if np.isscalar(tile) else tile
    tile_y, tile_x = max(2, int(tile_y)), max(2, int(tile_x))
    row_spans, col_spans = _spans(height, tile_y), _spans(width, tile_x)
    rows = max(b - a for a, b in row_spans)
    cols = max(b - a for a, b in col_spans)
    slabs = _slabs(depth, rows, cols, volume.stride, block_voxels)
    regions = None if regions is None else np.asarray(regions, dtype=np.int64).reshape(-1, 6)
    tiles = [(s, ty, tx) for s in range(len(slabs)) for ty in range(len(row_spans))
             for tx in range(len(col_spans))
             if _overlaps(regions, (slabs[s][0] - 1, slabs[s][1]), row_spans[ty],
                          col_spans[tx])]
    y_seams = [a for a, _ in row_spans[1:]]
    x_seams = [a for a, _ in col_spans[1:]]
    seams = [("y", s, n) for s in range(len(slabs)) for n in range(len(y_seams))] + \
            [("x", s, n) for s in range(len(slabs)) for n in range(len(x_seams))]
    total = len(tiles) + len(seams)
    workers = _threads(threads)
    edges = {}
    pieces = {}
    done = [0]
    lock = threading.Lock()

    def tick():
        with lock:
            done[0] += 1
            count = done[0]
        if progress is not None:
            progress(count, max(1, total))

    def run_tile(key):
        s, ty, tx = key
        data, origin, kept = _tile_block(volume, slabs[s], row_spans[ty], col_spans[tx])
        if kept is not None:
            edges[key] = kept
        return block_surface(data, origin, cube_shape) if data is not None else None

    def run_seam(key):
        axis, s, n = key
        cz0, cz1 = slabs[s]
        nz = cz1 - cz0 + 1
        if axis == "y":
            b = y_seams[n]
            data = np.zeros((nz, 2, width + 2), np.uint32)
            for tx, (x0, x1) in enumerate(col_spans):
                above, below = edges.get((s, n, tx)), edges.get((s, n + 1, tx))
                if above is not None:
                    data[:, 0, x0 + 1:x1 + 1] = above[1]
                if below is not None:
                    data[:, 1, x0 + 1:x1 + 1] = below[0]
            if not data.any():
                return None
            return block_surface(data, (cz0, b, 0), cube_shape)
        b = x_seams[n]
        data = np.zeros((nz, height + 2, 2), np.uint32)
        for ty, (y0, y1) in enumerate(row_spans):
            left, right = edges.get((s, ty, n)), edges.get((s, ty, n + 1))
            if left is not None:
                data[:, y0 + 1:y1 + 1, 0] = left[3]
            if right is not None:
                data[:, y0 + 1:y1 + 1, 1] = right[2]
        if not data.any():
            return None
        skip = np.zeros(height + 1, np.bool_)
        skip[np.asarray(y_seams, dtype=np.int64)] = True
        return block_surface(data, (cz0, 0, b), cube_shape, skip_y=skip)

    def run_all(jobs, fn, pool_):
        futures = {}
        for key in jobs:
            futures[pool_.submit(_guarded, fn, key, cancelled)] = key
        for future in as_completed(futures):
            pieces[futures[future]] = future.result()
            tick()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="surface-nets") as pool_:
        try:
            run_all(tiles, run_tile, pool_)
            run_all(seams, run_seam, pool_)
        except BaseException:
            pool_.shutdown(wait=True, cancel_futures=True)
            raise
    ordered = [pieces[key] for key in tiles + seams if pieces.get(key) is not None]
    return ordered, {"blocks": len(tiles), "seams": len(seams), "slabs": len(slabs),
                     "tile": [tile_y, tile_x], "threads": workers, "cube_shape": list(cube_shape)}


def _guarded(fn, key, cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError("cancelled")
    return fn(key)


def _threads(requested=None) -> int:
    if requested:
        return max(1, int(requested))
    try:
        env = int(os.environ.get("PLEXORA_SURFACE_THREADS") or 0)
    except ValueError:
        env = 0
    from plexora._resources import allocated_cpus

    cap = env if env > 0 else MAX_THREADS
    return max(1, min(allocated_cpus(), cap))


# -- assembly --------------------------------------------------------------------------


#: Quad corners, per axis, as offsets in (z, y, x) cubes from the edge's
#: cube, in the order that faces +axis (the low label's outside).
_QUAD = {
    0: ((-1, -1, 0), (-1, 0, 0), (0, 0, 0), (0, -1, 0)),     # x: around y then z
    1: ((-1, 0, -1), (0, 0, -1), (0, 0, 0), (-1, 0, 0)),     # y: around z then x
    2: ((0, -1, -1), (0, -1, 0), (0, 0, 0), (0, 0, -1)),     # z: around x then y
}


def assemble(pieces, cube_shape):
    """One mesh from every block's pieces: `(positions (N, 3) float64 in grid
    voxels, triangles (M, 3) int64, object_id (N,) uint32)`, vertices by
    (label, cube), triangles by (label, cube, axis) -- the same whatever the
    blocks were."""
    pieces = [p for p in pieces if p is not None and len(p.vcube)]
    if not pieces:
        return np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros(0, np.uint32)
    vcube = np.concatenate([p.vcube for p in pieces])
    vlabel = np.concatenate([p.vlabel for p in pieces])
    vpos = np.concatenate([p.vpos for p in pieces])
    qcube = np.concatenate([p.qcube for p in pieces])
    qaxis = np.concatenate([p.qaxis for p in pieces]).astype(np.int64)
    qlow = np.concatenate([p.qlow for p in pieces]).astype(bool)
    qlabel = np.concatenate([p.qlabel for p in pieces])

    labels = np.unique(vlabel)
    cubes = int(np.prod([int(v) for v in cube_shape], dtype=object))
    if len(labels) * cubes >= 2 ** 63:
        raise ValueError("the volume has too many cubes and labels to index in 64 bits")
    ncubes = np.int64(cubes)
    # Every key is unique, so any sort gives the one order.
    vkey = np.searchsorted(labels, vlabel).astype(np.int64) * ncubes + vcube
    order = np.argsort(vkey)
    vkey = vkey[order]
    positions = vpos[order]
    object_id = vlabel[order]

    if not len(qcube):
        return positions, np.zeros((0, 3), np.int64), object_id
    qrank = np.searchsorted(labels, qlabel).astype(np.int64)
    if len(labels) * cubes * 3 < 2 ** 63:
        qorder = np.argsort((qrank * ncubes + qcube) * 3 + qaxis)
    else:
        qorder = np.lexsort((qaxis, qcube, qrank))
    _, cy, cx = (np.int64(int(v)) for v in cube_shape)
    steps = np.array([cy * cx, cx, 1], np.int64)
    offsets = np.zeros((3, 4), np.int64)
    for axis, corners in _QUAD.items():
        offsets[axis] = np.asarray(corners, dtype=np.int64) @ steps
    triangles = np.empty((2 * len(qorder), 3), np.int64)
    ok = _triangulate(vkey, positions, qrank[qorder], qcube[qorder], qaxis[qorder],
                      qlow[qorder].astype(np.int8), offsets, ncubes, triangles)
    if not ok:
        raise RuntimeError("surface nets: a quad names a vertex no block made")
    return positions, triangles, object_id


@njit
def _triangulate(vkey, positions, qrank, qcube, qaxis, qlow, offsets, ncubes,
                 out):  # pragma: no cover - jit
    """Each quad as two triangles of vertex indices, split along its shorter
    diagonal; the high label's quad wound the other way. False when a corner
    names a vertex no block made."""
    n = len(vkey)
    corner = np.empty(4, np.int64)
    # The quads are in key order, so for one axis and one corner offset the
    # keys only grow: each search gallops forward from that pair's last.
    hint = np.zeros((3, 4), np.int64)
    for q in range(len(qcube)):
        axis = qaxis[q]
        base = qrank[q] * ncubes + qcube[q]
        for c in range(4):
            o = c if qlow[q] else 3 - c
            key = base + offsets[axis, o]
            lo = hint[axis, o]
            step = 1
            hi = lo + 1
            while hi < n and vkey[hi] <= key:
                lo = hi
                step *= 2
                hi = lo + step
            if hi > n:
                hi = n
            while lo < hi:                          # the first index whose key >= key
                mid = (lo + hi) >> 1
                if vkey[mid] < key:
                    lo = mid + 1
                else:
                    hi = mid
            if lo >= n or vkey[lo] != key:
                return False
            hint[axis, o] = lo
            corner[c] = lo
        d02 = 0.0
        d13 = 0.0
        for a in range(3):
            u = positions[corner[0], a] - positions[corner[2], a]
            v = positions[corner[1], a] - positions[corner[3], a]
            d02 += u * u
            d13 += v * v
        if d02 <= d13:
            out[2 * q, 0], out[2 * q, 1], out[2 * q, 2] = corner[0], corner[1], corner[2]
            out[2 * q + 1, 0], out[2 * q + 1, 1], out[2 * q + 1, 2] = corner[0], corner[2], corner[3]
        else:
            out[2 * q, 0], out[2 * q, 1], out[2 * q, 2] = corner[1], corner[2], corner[3]
            out[2 * q + 1, 0], out[2 * q + 1, 1], out[2 * q + 1, 2] = corner[1], corner[3], corner[0]
    return True


# -- priming ---------------------------------------------------------------------------


def _prime():
    data = np.zeros((3, 3, 3), np.uint32)
    data[1, 1, 1] = 1
    block_surface(data, (0, 0, 0), (3, 3, 3))
    pool(np.zeros((2, 2, 2), np.uint32), 2)


register_primer(__name__)


def prime():
    _prime()
