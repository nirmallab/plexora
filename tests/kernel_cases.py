"""Inputs for every compiled kernel, and a runner that times whichever form runs.

One case per kernel (plexora/server/utils/jit.py), built from numpy only so the
same inputs can be made anywhere -- this interpreter, a subprocess with numba
switched off, or Pyodide in the website's worker. Two sizes: `test` (small
enough for the plain-Python form to take milliseconds; the agreement tests)
and `bench` (what one real call sees: a 1024 x 1024 tile, a 32 x 256 x 256
label block, a million-triangle mesh, 200,000 cells; the cost table in
docs/internal/bench/browser_node/L5.md).

    python -m tests.kernel_cases build --size bench --out inputs.pkl
    python -m tests.kernel_cases run --inputs inputs.pkl --out outputs.pkl [--repeat 3]

`run` calls each kernel as the process has it -- the compiled port
(plexora-kernels), numba, or the Python it is written in, as jit.py chose from
PLEXORA_KERNELS and PLEXORA_NO_NUMBA -- on fresh copies of the inputs, and
keeps what it returned and every array it wrote into.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import pickle
import sys
import time

import numpy as np

# -- inputs ---------------------------------------------------------------------------


def cells_2d(height, width, n, rng, radius=(4, 11)):
    """A label plane of `n` disc-shaped cells (uint32, later cells on top)."""
    labels = np.zeros((height, width), np.uint32)
    for label in range(1, n + 1):
        r = rng.uniform(*radius)
        cy, cx = rng.uniform(0, height), rng.uniform(0, width)
        y0, y1 = max(0, int(cy - r)), min(height, int(cy + r) + 1)
        x0, x1 = max(0, int(cx - r)), min(width, int(cx + r) + 1)
        if y1 <= y0 or x1 <= x0:
            continue
        yy, xx = np.ogrid[y0:y1, x0:x1]
        labels[y0:y1, x0:x1][(yy - cy) ** 2 + (xx - cx) ** 2 <= r * r] = label
    return labels


def cells_3d(depth, height, width, n, rng):
    """A label volume of `n` ellipsoids (uint32), as tests/test_surface_perf.py packs them."""
    labels = np.zeros((depth, height, width), np.uint32)
    zz, yy, xx = np.ogrid[:depth, :height, :width]
    for label in range(1, n + 1):
        c = rng.uniform([2, 8, 8], [depth - 2, height - 8, width - 8])
        r = rng.uniform([2, 4, 4], [5, 9, 9])
        z0, z1 = int(max(0, c[0] - r[0])), int(min(depth, c[0] + r[0] + 1))
        y0, y1 = int(max(0, c[1] - r[1])), int(min(height, c[1] + r[1] + 1))
        x0, x1 = int(max(0, c[2] - r[2])), int(min(width, c[2] + r[2] + 1))
        inside = (((zz[z0:z1] - c[0]) / r[0]) ** 2 + ((yy[:, y0:y1] - c[1]) / r[1]) ** 2
                  + ((xx[:, :, x0:x1] - c[2]) / r[2]) ** 2) <= 1.0
        box = labels[z0:z1, y0:y1, x0:x1]
        box[inside & (box == 0)] = label
    return labels


def sphere_meshes(k, rng, lat=12, lon=24):
    """`k` closed latitude-longitude spheres: positions (N, 3) float64,
    triangles (M, 3) int64 grouped by object, and `objects` [id, first, count]."""
    ring = np.linspace(0, np.pi, lat + 1)[1:-1]
    turn = np.linspace(0, 2 * np.pi, lon, endpoint=False)
    unit = np.concatenate([[[0, 0, 1]],
                           np.stack([np.outer(np.sin(ring), np.cos(turn)).ravel(),
                                     np.outer(np.sin(ring), np.sin(turn)).ravel(),
                                     np.repeat(np.cos(ring), lon)], axis=1),
                           [[0, 0, -1]]])
    nv = len(unit)
    tris = []
    for j in range(lon):                                  # the caps
        tris.append([0, 1 + j, 1 + (j + 1) % lon])
        base = 1 + (lat - 2) * lon
        tris.append([nv - 1, base + (j + 1) % lon, base + j])
    for i in range(lat - 2):                              # the bands
        for j in range(lon):
            a, b = 1 + i * lon + j, 1 + i * lon + (j + 1) % lon
            c, d = a + lon, b + lon
            tris += [[a, c, b], [b, c, d]]
    tris = np.asarray(tris, np.int64)
    positions, triangles, objects = [], [], []
    for o in range(k):
        centre = rng.uniform(0, 1000, 3)
        radius = rng.uniform([3, 3, 1.5], [8, 8, 4])
        positions.append(centre + unit * radius + rng.normal(0, 0.05, unit.shape))
        triangles.append(tris + o * nv)
        objects.append([o + 1, o * len(tris), len(tris)])
    return (np.concatenate(positions), np.concatenate(triangles),
            np.asarray(objects, np.int64))


def lzw_encode(data: bytes) -> bytes:
    """TIFF's LZW (MSB-first codes, early change), in Python: an input for
    `lzw_decode` that needs no imagecodecs (the website has none)."""
    out = bytearray()
    acc = nbits = 0

    def put(code, width):
        nonlocal acc, nbits
        acc = (acc << width) | code
        nbits += width
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
            acc &= (1 << nbits) - 1

    table = {bytes((i,)): i for i in range(256)}
    width, nxt = 9, 258
    put(256, width)
    w = b""
    for value in data:
        wc = w + bytes((value,))
        if wc in table:
            w = wc
            continue
        put(table[w], width)
        table[wc] = nxt
        nxt += 1
        if nxt >= (1 << width) and width < 12:
            width += 1
        if nxt >= 4094:
            put(256, width)
            table = {bytes((i,)): i for i in range(256)}
            width, nxt = 9, 258
        w = bytes((value,))
    if w:
        put(table[w], width)
        nxt += 1
        if nxt >= (1 << width) and width < 12:
            width += 1
    put(257, width)
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)
    return bytes(out)


def _triangulate_inputs(labels):
    """`nets.assemble`'s arguments to `_triangulate`, for `labels` as one block."""
    from plexora.server.surface import nets

    padded = np.pad(labels, 1)
    cube_shape = tuple(int(v) + 1 for v in labels.shape)
    piece = nets.block_surface(padded, (0, 0, 0), cube_shape)
    vlabel, vcube = piece.vlabel, piece.vcube
    labels_ = np.unique(vlabel)
    ncubes = np.int64(int(np.prod(cube_shape)))
    vkey = np.searchsorted(labels_, vlabel).astype(np.int64) * ncubes + vcube
    order = np.argsort(vkey)
    vkey, positions = vkey[order], piece.vpos[order]
    qrank = np.searchsorted(labels_, piece.qlabel).astype(np.int64)
    qaxis = piece.qaxis.astype(np.int64)
    qorder = np.argsort((qrank * ncubes + piece.qcube) * 3 + qaxis)
    _, cy, cx = (np.int64(v) for v in cube_shape)
    steps = np.array([cy * cx, cx, 1], np.int64)
    offsets = np.zeros((3, 4), np.int64)
    for axis, corners in nets._QUAD.items():
        offsets[axis] = np.asarray(corners, dtype=np.int64) @ steps
    out = np.zeros((2 * len(qorder), 3), np.int64)
    return (vkey, positions, qrank[qorder], piece.qcube[qorder], qaxis[qorder],
            piece.qlow[qorder].astype(np.int8), offsets, ncubes, out)


SIZES = {
    #          plane  cells  volume         vcells  spheres  points   lzw
    "test":  dict(plane=(64, 80), cells=40, volume=(6, 24, 28), vcells=6, spheres=6,
                  points=800, lzw=(48, 40)),
    "bench": dict(plane=(1024, 1024), cells=3000, volume=(32, 256, 256), vcells=300,
                  spheres=2000, points=200_000, lzw=(1024, 1024)),
}

#: Each kernel: (case name, kernel key, the product feature it serves).
FEATURES = {
    "label_kernels._boundary": "cell outlines drawn on a picture (label_overlay.boundary_mask)",
    "label_kernels._bboxes": "where each cell's mask is, for gating evidence (label_bboxes)",
    "measure._bbox_kernel": "3-D objects measured from a label volume (measure_objects)",
    "cell_features._background_blocks": "the classifier's per-cell pixel features: local background",
    "cell_features._tile_kernel": "the classifier's per-cell pixel features: one tile's sums",
    "cell_features._run_labels": "the classifier's per-cell pixel features: labels in a tile",
    "segqc.label_stats": "segmentation QC: per-cell area, DNA, perimeter",
    "segqc.count_pairs": "segmentation QC: touching cell pairs (count)",
    "segqc.fill_pairs": "segmentation QC: touching cell pairs (fill)",
    "autogate._count": "automatic gating: neighbours within a radius (density, spill)",
    "nets._pool_runs": "3-D cell surfaces at a coarse z (planes pooled)",
    "nets._corners": "3-D cell surfaces: one cube's corners (inlined in _count/_fill)",
    "nets._distinct": "3-D cell surfaces: one cube's labels (inlined in _count/_fill)",
    "nets._count": "3-D cell surfaces (surface nets): counting vertices and quads",
    "nets._fill": "3-D cell surfaces (surface nets): making vertices and quads",
    "nets._triangulate": "3-D cell surfaces: quads to triangles",
    "mesh._face_normals_into": "surface normals (every surface drawn)",
    "mesh._adjacency": "surface smoothing (Taubin): the vertex graph",
    "mesh._umbrella_step": "surface smoothing (Taubin): one step",
    "mesh._signed_volumes": "surface volumes per object",
    "mesh._object_sums": "surface measurement: area, volume, centroid, box",
    "mesh._object_moments": "surface measurement: elongation",
    "lzw_decode": "LZW-compressed TIFF tiles (imagecodecs_shim on the website)",
}

MODULES = {
    "label_kernels": "plexora.server.utils.label_kernels",
    "measure": "plexora.server.volume.measure",
    "cell_features": "plexora.server.utils.cell_features",
    "segqc": "plexora.plugins.qc.server.segqc.kernels",
    "autogate": "plexora.plugins.gating.server.autogate.kernels",
    "nets": "plexora.server.surface.nets",
    "mesh": "plexora.server.surface.mesh",
}


def build(size: str = "test", seed: int = 7) -> dict:
    """{case name: (kernel name, args)} -- the args every implementation is
    handed (copies of them)."""
    s = SIZES[size]
    rng = np.random.default_rng(seed)
    h, w = s["plane"]
    labels = cells_2d(h, w, s["cells"], rng)
    cases = {}
    cases["label_kernels._boundary"] = (labels, 1, np.zeros(labels.shape, np.bool_))
    cases["label_kernels._boundary/r2-int32"] = (labels.astype(np.int32), 2,
                                                np.zeros(labels.shape, np.bool_))
    ids = np.unique(labels)
    ids = np.ascontiguousarray(ids[ids != 0])
    cases["label_kernels._bboxes"] = (labels, ids, np.zeros((len(ids), 7), np.int64))
    cases["label_kernels._bboxes/u16"] = (labels.astype(np.uint16), ids.astype(np.uint16),
                                         np.zeros((len(ids), 7), np.int64))
    n = int(labels.max()) + 1
    big = np.full(n, np.iinfo(np.int64).max, np.int64)
    small = np.full(n, -1, np.int64)
    cases["measure._bbox_kernel"] = (labels, 17, 3, big.copy(), small.copy(), big.copy(),
                                     small.copy(), big.copy(), small.copy())

    # cell_features: a tile with a 1 px halo, as _tile_sums_compiled hands it over.
    labels_h = np.pad(labels, 1)
    marker_h = (rng.gamma(2.0, 300.0, labels_h.shape) + 50 * (labels_h > 0)).astype(np.float32)
    marker_h[rng.random(labels_h.shape) < 0.001] = np.nan
    dna_h = (rng.normal(100, 40, labels_h.shape)).astype(np.float32)
    core = np.ascontiguousarray(labels_h[1:-1, 1:-1])
    step = 128 if size == "bench" else 16
    cases["cell_features._background_blocks"] = (
        np.ascontiguousarray(marker_h[1:-1, 1:-1]), core, step, 20.0, 64 if size == "bench" else 8)
    uniq = np.unique(core[core > 0])
    rows, cols = -(-core.shape[0] // step), -(-core.shape[1] // step)
    background = rng.normal(10, 2, (rows, cols))
    background[rng.random((rows, cols)) < 0.2] = np.nan
    marker_ok = np.nan_to_num(marker_h).astype(np.float32)
    cases["cell_features._tile_kernel"] = (labels_h, marker_ok, dna_h, True, background, step, uniq,
                                           np.zeros((uniq.size, 10)), np.full(uniq.size, -np.inf))
    cases["cell_features._tile_kernel/no-dna"] = (
        labels_h, marker_ok, np.zeros((1, 1), np.float32), False, background, step, uniq,
        np.zeros((uniq.size, 10)), np.full(uniq.size, -np.inf))
    cases["cell_features._run_labels"] = (core,)

    # segmentation QC: smoothed log DNA, a window inside a halo.
    dna = np.log1p(np.abs(dna_h[1:-1, 1:-1])).astype(np.float32)
    window = (4, h - 4, 6, w - 6)
    stats = tuple(np.zeros(n) for _ in range(7))
    cases["segqc.label_stats"] = (labels, dna, 100, 200, *window, 3.5, *stats)
    cases["segqc.count_pairs"] = (labels, *window)
    from plexora.plugins.qc.server.segqc import kernels as segqc

    pairs = int(segqc.count_pairs(labels, *window))
    cases["segqc.fill_pairs"] = (labels, dna, 100, 200, *window,
                                 np.zeros(pairs, np.int64), np.zeros(pairs, np.int64),
                                 np.zeros(pairs), np.zeros(pairs), np.zeros(pairs))

    # automatic gating: the grid as autogate.kernels.Grid builds it.
    from plexora.plugins.gating.server.autogate import kernels as autogate

    m = s["points"]
    extent = 500.0 if size == "test" else 5000.0
    xs, ys = rng.uniform(0, extent, m), rng.uniform(0, extent, m)
    xs[rng.random(m) < 0.001] = np.nan
    grid = autogate.Grid(xs, ys, 30.0)
    query = np.arange(m, dtype=np.int64)
    flags = rng.random(m) < 0.3
    cases["autogate._count"] = (grid.xs[query], grid.ys[query], query, grid.xs, grid.ys, flags,
                                900.0, grid.order, grid.starts, grid.x0, grid.y0, grid.side,
                                grid.nx, grid.ny, np.zeros(m, np.int64), np.zeros(m, np.int64))

    # surface nets.
    volume = cells_3d(*s["volume"], s["vcells"], rng)
    cases["nets._pool_runs"] = (volume, np.int64(3), np.zeros(
        (-(-volume.shape[0] // 3),) + volume.shape[1:], np.uint32))
    padded = np.pad(volume, 1)
    skip = np.zeros(padded.shape[1] - 1, np.bool_)
    cases["nets._count"] = (padded, skip)
    skip_some = skip.copy()
    skip_some[::7] = True
    cases["nets._count/skip"] = (padded, skip_some)
    from plexora.server.surface import nets

    nv, nq = (int(v) for v in nets._count(padded, skip))
    cz = tuple(int(v) + 1 for v in volume.shape)
    cases["nets._fill"] = (padded, skip, 0, 0, 0, np.int64(cz[2]), np.int64(cz[1] * cz[2]),
                           nets._CORNERS, nets._EDGES, np.zeros(nv, np.int64),
                           np.zeros(nv, np.uint32), np.zeros((nv, 3)), np.zeros(nq, np.int64),
                           np.zeros(nq, np.int8), np.zeros(nq, np.int8), np.zeros(nq, np.uint32))
    cases["nets._triangulate"] = _triangulate_inputs(volume)
    cases["nets._corners"] = (padded, 1, 5, 7, np.zeros(8, np.uint32))
    cases["nets._distinct"] = (np.array([3, 0, 3, 5, 9, 5, 0, 2], np.uint32), np.zeros(8, np.uint32))

    # meshes.
    positions, triangles, objects = sphere_meshes(s["spheres"], rng)
    cases["mesh._face_normals_into"] = (positions, triangles, np.zeros_like(positions))
    cases["mesh._adjacency"] = (len(positions), triangles)
    indptr, indices = _adjacency_plain(len(positions), triangles)
    cases["mesh._umbrella_step"] = (positions, indptr, indices, 0.5, np.zeros_like(positions))
    starts, counts = objects[:, 1].copy(), objects[:, 2].copy()
    scale = np.array([0.65, 0.65, 2.0])
    cases["mesh._signed_volumes"] = (positions, triangles, starts, counts, scale,
                                     np.zeros(len(objects)))
    cases["mesh._object_sums"] = (positions, triangles, starts, counts, scale,
                                  np.zeros((len(objects), 8)), np.zeros((len(objects), 6)))
    centroid = rng.uniform(0, 1000, (len(objects), 3))
    cases["mesh._object_moments"] = (positions, triangles, starts, counts, scale, centroid,
                                     np.zeros((len(objects), 6)))

    # LZW: a smooth 16-bit image with noise, as a microscope tile compresses.
    lh, lw = s["lzw"]
    image = (np.add.outer(np.arange(lh), np.arange(lw)) * 7
             + rng.integers(0, 40, (lh, lw))).astype(np.uint16)
    cases["lzw_decode"] = (lzw_encode(image.tobytes()),)
    return cases


def _adjacency_plain(n, t):
    a = np.concatenate([t[:, 0], t[:, 1], t[:, 2], t[:, 1], t[:, 2], t[:, 0]])
    b = np.concatenate([t[:, 1], t[:, 2], t[:, 0], t[:, 0], t[:, 1], t[:, 2]])
    pairs = np.unique(a * np.int64(n) + b)
    src, dst = pairs // n, pairs % n
    indptr = np.zeros(n + 1, np.int64)
    np.cumsum(np.bincount(src, minlength=n), out=indptr[1:])
    return indptr, dst.astype(np.int64)


# -- running --------------------------------------------------------------------------


def kernel(name: str):
    """The callable this process runs for kernel `name` (as jit.py chose)."""
    if name == "lzw_decode":
        from plexora.browser import imagecodecs_shim

        return imagecodecs_shim._lzw()
    module, function = name.split(".")
    return getattr(importlib.import_module(MODULES[module]), function)


def implementation(fn) -> str:
    from plexora.server.utils import jit

    return jit.implementation(fn)


def _fresh(args):
    return tuple(a.copy() if isinstance(a, np.ndarray) else copy.copy(a) for a in args)


def run(cases: dict, repeat: int = 1, only=None, budget_s: float = 60.0) -> dict:
    """{case: {impl, first_s, best_s, result, arrays}}: each kernel called on
    fresh copies of its args; `arrays` are the args after the call (what it
    wrote). Repeats stop once `budget_s` is spent on a case."""
    out = {}
    for name, args in cases.items():
        base = name.split("/")[0]
        if only and base not in only and name not in only:
            continue
        fn = kernel(base)
        times = []
        result = held = None
        for i in range(max(1, repeat)):
            fresh = _fresh(args)
            started = time.perf_counter()
            got = fn(*fresh)
            times.append(time.perf_counter() - started)
            if i == 0:
                result, held = got, fresh
            if sum(times) > budget_s:
                break
        out[name] = {"kernel": base, "impl": implementation(fn), "first_s": times[0],
                     "best_s": min(times[1:]) if len(times) > 1 else times[0],
                     "calls": len(times), "result": result,
                     "arrays": [a for a in held if isinstance(a, np.ndarray)]}
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--size", default="test", choices=sorted(SIZES))
    b.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--inputs", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--only", default="")
    r.add_argument("--budget", type=float, default=60.0)
    args = parser.parse_args(argv)
    if args.command == "build":
        with open(args.out, "wb") as handle:
            pickle.dump(build(args.size), handle)
        return 0
    with open(args.inputs, "rb") as handle:
        cases = pickle.load(handle)
    only = {name for name in args.only.split(",") if name}
    started = time.perf_counter()
    got = run(cases, args.repeat, only, args.budget)
    with open(args.out, "wb") as handle:
        pickle.dump(got, handle)
    print(json.dumps({name: {k: v for k, v in entry.items() if k.endswith("_s") or k in
                             ("impl", "calls")} for name, entry in got.items()}))
    print(f"total {time.perf_counter() - started:.1f} s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
