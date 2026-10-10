"""Measuring 3-D objects: one row per cell of a label volume.

What a 3-D segmentation is for. A 2-D per-plane table counts one cell once per
plane it crosses and measures each slice of it; this measures each object once,
whole: its voxel count and physical volume, centroid in pixels and microns,
bounding box, surface area and sphericity, and every channel's mean and
integrated intensity over its voxels.

Streamed plane by plane in row strips, so a whole-slide stack never has to fit
in memory: every statistic is a sum (voxels, coordinates, intensities, faces)
or a min/max (bounding box), and both merge across strips and planes exactly.

**Surface** is counted as exposed voxel faces -- a face between an object's
voxel and a voxel of anything else (background, another object, outside the
stack) -- each weighted by its physical area (a face across x is
`sy * sz`). That over-estimates a smooth surface by up to about 1.5x on
voxelized spheres, the same bias every face-count method has; sphericity
`pi^(1/3) (6V)^(2/3) / A` is therefore below 1 even for a sphere, and is for
ranking objects, not for an absolute shape claim. Stated in the table's
manifest.

**Physical columns need a known z step.** Without one (`spacing.z.source` not
`metadata`/`manual`) `measure_objects` refuses unless `units="voxel"`, and then
writes voxel columns only -- a volume in µm³ from a guessed z step is a number
that looks right and is not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from plexora.server.utils.jit import njit, register_primer

#: Rows read at once per plane.
STRIP_ROWS = 1024


class SpacingMissing(ValueError):
    """A physical measurement was asked for and the z step is not known."""


@njit
def _bbox_kernel(labels, y0, z, minx, maxx, miny, maxy, minz, maxz):  # pragma: no cover - jit
    height, width = labels.shape
    for r in range(height):
        for c in range(width):
            label = labels[r, c]
            if label == 0:
                continue
            y = y0 + r
            if c < minx[label]:
                minx[label] = c
            if c > maxx[label]:
                maxx[label] = c
            if y < miny[label]:
                miny[label] = y
            if y > maxy[label]:
                maxy[label] = y
            if z < minz[label]:
                minz[label] = z
            if z > maxz[label]:
                maxz[label] = z


def _prime():
    labels = np.zeros((2, 2), dtype=np.uint32)
    labels[0, 0] = 1
    big = np.full(2, 2**31 - 1, dtype=np.int64)
    small = np.full(2, -1, dtype=np.int64)
    _bbox_kernel(labels, 0, 0, big.copy(), small.copy(), big.copy(), small.copy(),
                 big.copy(), small.copy())


register_primer(__name__)


def prime():
    _prime()


@dataclass
class _Acc:
    n: int
    channels: int
    count: np.ndarray = None
    sx: np.ndarray = None
    sy: np.ndarray = None
    sz: np.ndarray = None
    faces_x: np.ndarray = None
    faces_y: np.ndarray = None
    faces_z: np.ndarray = None
    sums: np.ndarray = None
    bbox: list = field(default_factory=list)

    def __post_init__(self):
        n = self.n
        self.count = np.zeros(n, np.int64)
        self.sx = np.zeros(n, np.float64)
        self.sy = np.zeros(n, np.float64)
        self.sz = np.zeros(n, np.float64)
        self.faces_x = np.zeros(n, np.int64)
        self.faces_y = np.zeros(n, np.int64)
        self.faces_z = np.zeros(n, np.int64)
        self.sums = np.zeros((self.channels, n), np.float64)
        big = np.iinfo(np.int64).max
        self.bbox = [np.full(n, big, np.int64), np.full(n, -1, np.int64),
                     np.full(n, big, np.int64), np.full(n, -1, np.int64),
                     np.full(n, big, np.int64), np.full(n, -1, np.int64)]


def _boundary_counts(a, b, n):
    """Faces between voxel arrays `a` and `b` (neighbours along one axis):
    every label on either side of a change of label gets one face."""
    differ = a != b
    out = np.zeros(n, np.int64)
    if differ.any():
        left = a[differ].ravel()
        right = b[differ].ravel()
        out += np.bincount(left, minlength=n)[:n]
        out += np.bincount(right, minlength=n)[:n]
    out[0] = 0
    return out


def measure(label_volume, read_channel_plane, *, channel_names, spacing=None,
            units="physical", t=0, progress=None):
    """Measure every object of `label_volume` (a `labels.LabelVolume`).

    `read_channel_plane(channel_index, z, rows, cols)` returns one channel's
    level-0 pixels for a strip of plane z. `spacing` is a `Spacing`; physical
    columns need its z step to be known (see module docstring).

    Returns `(columns: dict of numpy arrays, manifest: dict)`.
    """
    physical = units == "physical"
    if physical and (spacing is None or not spacing.volumetric_ok()):
        raise SpacingMissing(
            "measuring objects in microns needs the image's pixel size and z step; "
            "set the z step (set_z_spacing), or measure in voxels (units='voxel')")
    depth, height, width = label_volume.depth, label_volume.height, label_volume.width
    channels = len(channel_names)

    # The largest id decides the accumulators' size; read it plane by plane.
    top = 0
    for z in range(depth):
        for y0 in range(0, height, STRIP_ROWS):
            block = label_volume.read(0, z, z, slice(y0, y0 + STRIP_ROWS), slice(None))
            if block.size:
                top = max(top, int(block.max()))
    n = top + 1
    acc = _Acc(n=n, channels=channels)
    xs_cache = {}
    previous_plane_rows = None
    total = depth * max(1, math.ceil(height / STRIP_ROWS))
    done = 0
    for z in range(depth):
        previous_strip_last = None
        plane_rows = []
        for y0 in range(0, height, STRIP_ROWS):
            rows = slice(y0, min(height, y0 + STRIP_ROWS))
            labels = label_volume.read(0, z, z, rows, slice(None))[0].astype(np.int64)
            h, w = labels.shape
            flat = labels.ravel()
            acc.count += np.bincount(flat, minlength=n)[:n]
            if w not in xs_cache:
                xs_cache[w] = np.arange(w, dtype=np.float64)
            xs = np.broadcast_to(xs_cache[w], (h, w)).ravel()
            ys = np.repeat(np.arange(y0, y0 + h, dtype=np.float64), w)
            acc.sx += np.bincount(flat, weights=xs, minlength=n)[:n]
            acc.sy += np.bincount(flat, weights=ys, minlength=n)[:n]
            acc.sz += np.bincount(flat, minlength=n)[:n] * float(z)
            _bbox_kernel(labels.astype(np.uint32), y0, z, *acc.bbox)
            # Faces across x within the strip, and the stack's own x edges.
            acc.faces_x += _boundary_counts(labels[:, 1:], labels[:, :-1], n)
            acc.faces_x += np.bincount(labels[:, 0], minlength=n)[:n]
            acc.faces_x += np.bincount(labels[:, -1], minlength=n)[:n]
            # Faces across y within the strip, against the previous strip, and
            # the stack's own y edges.
            acc.faces_y += _boundary_counts(labels[1:, :], labels[:-1, :], n)
            if previous_strip_last is not None:
                acc.faces_y += _boundary_counts(labels[:1, :], previous_strip_last[None, :], n)
            else:
                acc.faces_y += np.bincount(labels[0], minlength=n)[:n]
            if rows.stop >= height:
                acc.faces_y += np.bincount(labels[-1], minlength=n)[:n]
            previous_strip_last = labels[-1].copy()
            # Faces across z: against the same strip of the previous plane.
            if previous_plane_rows is not None:
                above = previous_plane_rows[len(plane_rows)]
                acc.faces_z += _boundary_counts(labels, above, n)
            else:
                acc.faces_z += np.bincount(flat, minlength=n)[:n]
            if z == depth - 1:
                acc.faces_z += np.bincount(flat, minlength=n)[:n]
            plane_rows.append(labels)
            for index in range(channels):
                values = np.asarray(read_channel_plane(index, z, rows, slice(None)),
                                    dtype=np.float64)
                acc.sums[index] += np.bincount(flat, weights=values.ravel(), minlength=n)[:n]
            acc.faces_z[0] = acc.faces_x[0] = acc.faces_y[0] = 0
            done += 1
            if progress is not None:
                progress(done, total)
        previous_plane_rows = plane_rows
    return _columns(acc, channel_names, spacing, physical, depth, height, width, t)


def _columns(acc, channel_names, spacing, physical, depth, height, width, t):
    present = np.nonzero(acc.count)[0]
    present = present[present > 0]
    count = acc.count[present].astype(np.float64)
    # `label`, not `id`: the table adapters build an `id` column of their own
    # from whichever column is the cell id, and two `id` columns collide.
    out = {"label": present.astype(np.int64)}
    out["X"] = acc.sx[present] / count
    out["Y"] = acc.sy[present] / count
    out["Z"] = acc.sz[present] / count
    out["t"] = np.full(len(present), int(t), np.int64)
    minx, maxx, miny, maxy, minz, maxz = (b[present] for b in acc.bbox)
    out.update({"bbox_x0": minx, "bbox_x1": maxx, "bbox_y0": miny, "bbox_y1": maxy,
                "bbox_z0": minz, "bbox_z1": maxz})
    out["n_planes"] = (maxz - minz + 1).astype(np.int64)
    out["touches_z_bounds"] = (minz == 0) | (maxz == depth - 1)
    out["touches_xy_bounds"] = (minx == 0) | (miny == 0) | (maxx == width - 1) \
        | (maxy == height - 1)
    out["volume_vox"] = acc.count[present]
    manifest = {"objects": int(len(present)), "units": "physical" if physical else "voxel",
                "surface_method": "exposed voxel faces (over-estimates smooth surfaces)",
                "percentile_method": None, "depth": depth}
    if physical:
        sx = spacing.x.microns()
        sy = spacing.y.microns()
        sz = spacing.z.microns()
        out["x_um"] = out["X"] * sx
        out["y_um"] = out["Y"] * sy
        out["z_um"] = out["Z"] * sz
        volume = count * sx * sy * sz
        area = (acc.faces_x[present] * sy * sz + acc.faces_y[present] * sx * sz
                + acc.faces_z[present] * sx * sy)
        out["volume_um3"] = volume
        out["surface_area_um2"] = area
        with np.errstate(divide="ignore", invalid="ignore"):
            out["sphericity"] = np.where(area > 0, (math.pi ** (1 / 3))
                                         * (6 * volume) ** (2 / 3) / area, np.nan)
        manifest["spacing_um"] = {"x": sx, "y": sy, "z": sz,
                                  "z_source": spacing.z.source}
    for index, name in enumerate(channel_names):
        sums = acc.sums[index][present]
        out[name] = sums / count
        out[f"{name}__integrated"] = sums
    return out, manifest
