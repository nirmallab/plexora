"""Which level a surface is extracted at, and what it will cost.

A browser holds about three to five million triangles comfortably; a
full-resolution isosurface of a large stack, or of every cell in it, is
many times that. So a surface is extracted at the finest pyramid level
whose grid fits the QUALITY it was asked at, and a request that cannot fit
even its quality's ceiling is refused up front with the estimate and what
to change (a smaller area, fewer planes, fewer objects, a lower quality),
rather than built and then not drawable.

The grid of a level averages `z_stride` planes into one voxel, as many as
fit in one of the level's pixels when the z step is known -- the level
route's `z_pool`, by mean instead of maximum, since an isosurface of
max-pooled planes swells every bright structure.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

import numpy as np

from plexora.server.surface.marching import Grid


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


#: Triangles a surface may have at most, whatever the quality.
RESIDENT_TRIANGLES = max(100_000, _env_int("PLEXORA_SURFACE_BUDGET_TRIANGLES", 5_000_000))

#: quality -> (grid voxels at most for an isosurface, triangles at most)
QUALITY = {
    "preview": (4_000_000, 300_000),
    "standard": (48_000_000, 3_000_000),
    "full": (512_000_000, RESIDENT_TRIANGLES),
}
QUALITIES = tuple(QUALITY)

#: Cell surfaces (surface nets, surface/nets.py) make exactly two triangles
#: per exposed voxel face at the level they are extracted at.
TRIANGLES_PER_FACE = 2.0
#: A voxelized object exposes about this many voxel faces per unit of its
#: smooth surface's area (1.5 for large spheres; more for small cells).
FACES_PER_AREA = 1.55
#: An isosurface at a level k makes about this many times fewer triangles
#: than at level k-1 (its area in grid voxels; between 2 and 4).
ISO_LEVEL_RATIO = 3.0


class SurfaceRefused(ValueError):
    """A surface that will not be built; the message says why and what to change.

    `code` maps onto an agent error code; `estimate` carries the numbers."""

    def __init__(self, message, *, code="invalid_input", estimate=None):
        super().__init__(message)
        self.code = code
        self.estimate = estimate or {}


@dataclass(frozen=True)
class Region:
    """What part of the volume: a level-0 box (x0, y0, x1, y1), exclusive, and
    planes (z0, z1), inclusive. None for the whole extent."""

    box: tuple | None = None
    planes: tuple | None = None

    @classmethod
    def from_dict(cls, raw, *, width, height, depth):
        raw = raw if isinstance(raw, dict) else {}
        box = None
        if raw.get("box") is not None:
            try:
                bx, by, bw, bh = (float(v) for v in raw["box"])
            except (TypeError, ValueError):
                raise SurfaceRefused("region.box is [x, y, width, height] in pixels") from None
            x0, y0 = max(0, int(bx)), max(0, int(by))
            x1, y1 = min(width, int(math.ceil(bx + bw))), min(height, int(math.ceil(by + bh)))
            if x1 - x0 < 2 or y1 - y0 < 2:
                raise SurfaceRefused("the region is outside the image")
            if (x0, y0, x1, y1) != (0, 0, width, height):
                box = (x0, y0, x1, y1)
        planes = None
        if raw.get("z") is not None:
            try:
                z0, z1 = sorted(int(v) for v in raw["z"])
            except (TypeError, ValueError):
                raise SurfaceRefused("region.z is [z0, z1], planes inclusive") from None
            z0, z1 = max(0, z0), min(depth - 1, z1)
            if z1 < z0:
                raise SurfaceRefused("the region's planes are outside the stack")
            if (z0, z1) != (0, depth - 1):
                planes = (z0, z1)
        return cls(box=box, planes=planes)

    def to_dict(self) -> dict | None:
        if self.box is None and self.planes is None:
            return None
        out = {}
        if self.box is not None:
            x0, y0, x1, y1 = self.box
            out["box"] = [x0, y0, x1 - x0, y1 - y0]
        if self.planes is not None:
            out["z"] = list(self.planes)
        return out


def z_stride(volume, level: int) -> int:
    """Planes averaged into one grid voxel at `level`: as many as fit in one of
    the level's pixels when the z step is known, else 1."""
    spacing = volume.spacing
    sx, sz = spacing.x.microns(), spacing.z.microns()
    if not (sx and sz) or spacing.z.source not in ("metadata", "manual"):
        return 1
    info = volume.levels[level]
    pixel = float(sx) * volume.width / max(1, info.width)
    return max(1, int(pixel // float(sz)))


@dataclass(frozen=True)
class Plan:
    """Where an isosurface is extracted: the level, its area there, the planes
    and how many are averaged, and the grid that maps it to level 0."""

    level: int
    rows: slice
    cols: slice
    z0: int
    z1: int
    stride: int
    grid: Grid

    @property
    def voxels(self) -> int:
        return int(np.prod(self.grid.shape))

    def voxel_um(self, scale):
        """One grid voxel's physical size (x, y, z)."""
        return tuple(float(a) * float(b) for a, b in zip(self.grid.scale, scale))

    def to_dict(self) -> dict:
        return {"level": self.level, "z_stride": self.stride, "grid": self.grid.to_dict(),
                "voxels": self.voxels}


def plan_at(volume, level: int, region: Region) -> Plan:
    from plexora.server.volume.serve import area_at

    rows, cols, _covered = area_at(volume, level, region.box)
    z0, z1 = region.planes or (0, volume.depth - 1)
    stride = min(z_stride(volume, level), z1 - z0 + 1)
    depth = -(-(z1 - z0 + 1) // stride)
    info = volume.levels[level]
    fx = volume.width / max(1, info.width)
    fy = volume.height / max(1, info.height)
    grid = Grid.of((depth, rows.stop - rows.start, cols.stop - cols.start), factor_xy=(fx, fy),
                   z_stride=stride, origin=(cols.start, rows.start, z0))
    return Plan(level=level, rows=rows, cols=cols, z0=z0, z1=z1, stride=stride, grid=grid)


def plan_isosurface(volume, region: Region, *, quality="standard", level=None) -> Plan:
    """The finest level whose grid fits `quality` (or `level`, checked)."""
    if quality not in QUALITY:
        raise SurfaceRefused(f"quality is one of {', '.join(QUALITIES)}")
    budget = QUALITY[quality][0]
    if level is not None:
        if not 0 <= int(level) < len(volume.levels):
            raise SurfaceRefused(f"level {level} does not exist")
        plan = plan_at(volume, int(level), region)
        if plan.voxels > QUALITY["full"][0]:
            raise SurfaceRefused(
                f"level {level} of this region is {plan.voxels / 1e6:.0f} M voxels, more than "
                f"one extraction reads ({QUALITY['full'][0] / 1e6:.0f} M); choose a coarser "
                "level or a smaller region", estimate={"voxels": plan.voxels})
        return plan
    for info in volume.levels:
        plan = plan_at(volume, info.index, region)
        if plan.voxels <= budget:
            return plan
    return plan_at(volume, volume.levels[-1].index, region)


def iso_estimate(preview_triangles: int, preview_level: int, level: int) -> int:
    """Triangles at `level` from a preview's count at `preview_level`."""
    return int(preview_triangles * ISO_LEVEL_RATIO ** max(0, preview_level - level))


def label_triangles(counts, faces=None, *, level=0, level_factor=1.0, z_stride=1) -> int:
    """Estimated triangles for label surfaces of objects with voxel `counts`
    at level 0 (or exact face counts `faces` = (fx, fy, fz) arrays), at a
    level whose XY voxels are `level_factor` level-0 voxels wide, read every
    `z_stride` planes (`label_z_stride`)."""
    f = float(level_factor)
    s = float(max(1, int(z_stride)))
    if faces is not None:
        fx, fy, fz = (np.asarray(a, dtype=np.float64) for a in faces)
        # A face across x or y spans one pixel and one plane; across z, two
        # pixels: at a coarser grid they are that many times fewer.
        total = (fz / (f * f) + (fx + fy) / (f * s)).sum()
    else:
        counts = np.asarray(counts, dtype=np.float64)
        # A sphere's area per V^(2/3): (36 pi)^(1/3); about a third faces z.
        area = (36 * math.pi) ** (1 / 3) * np.cbrt(counts) ** 2 * FACES_PER_AREA
        total = (area / 3.0 / (f * f) + 2.0 * area / 3.0 / (f * s)).sum()
    return int(total * TRIANGLES_PER_FACE)


def label_z_stride(level_factor, scale=None, depth=None) -> int:
    """How many planes one grid plane of a cell surface pools at a level
    whose XY voxels are `level_factor` level-0 voxels wide: as many as make
    the grid's voxels about as deep as they are wide.

    A label volume's levels are XY-only, so without this a coarse level of a
    finely stepped stack keeps every plane: cells stay hundreds of voxels
    tall and their sides alone outgrow any triangle budget (a 194-plane stack
    at 0.28 um under 0.14 um pixels was 7 M triangles at its coarsest level).
    1 while the pixel is no wider than the z step -- every stack whose z step
    is the coarser one, at the levels it is drawn at today -- and 1 when the
    spacing is not known (`scale` None)."""
    if not scale:
        return 1
    sx, sy, sz = (float(v) for v in scale)
    if not sz > 0:
        return 1
    stride = int(round(float(level_factor) * min(sx, sy) / sz))
    stride = max(1, stride)
    return min(stride, int(depth)) if depth else stride


def plan_labels(label_volume, counts, *, faces=None, quality="standard", level=None,
                image_width=None, scale=None):
    """The finest level of the label volume whose surfaces fit `quality`:
    `(level, estimated triangles)`, or SurfaceRefused. `scale` (level-0
    voxel size, µm) lets a coarse level pool planes (`label_z_stride`)."""
    if quality not in QUALITY:
        raise SurfaceRefused(f"quality is one of {', '.join(QUALITIES)}")
    ceiling = QUALITY[quality][1]
    shapes = label_volume.level_shapes()
    width0 = image_width or shapes[0][1]
    candidates = [int(level)] if level is not None else list(range(len(shapes)))
    best = None
    for k in candidates:
        if not 0 <= k < len(shapes):
            raise SurfaceRefused(f"level {k} does not exist")
        factor = width0 / max(1, shapes[k][1])
        stride = label_z_stride(factor, scale, getattr(label_volume, "depth", None))
        estimate = label_triangles(counts, faces, level=k, level_factor=factor,
                                   z_stride=stride)
        best = (k, estimate)
        if estimate <= ceiling:
            return best
    k, estimate = best
    if level is not None or estimate > RESIDENT_TRIANGLES:
        raise SurfaceRefused(
            f"surfaces of {len(counts)} objects would be about {estimate / 1e6:.1f} M "
            f"triangles at level {k}, more than a browser can hold "
            f"({RESIDENT_TRIANGLES / 1e6:.0f} M); choose fewer objects or a smaller region",
            estimate={"triangles": estimate, "level": k, "objects": int(len(counts))})
    return best


def plan_reader(source, plan: Plan, *, t: int, channel: int):
    """`read(g0, g1)` -> float32 grid planes g0..g1 of `plan`: the level's
    rows and columns, `plan.stride` planes averaged per grid plane."""
    stride = plan.stride

    def read(g0, g1):
        z_start = plan.z0 + g0 * stride
        z_stop = min(plan.z1, plan.z0 + (g1 + 1) * stride - 1)
        block = np.asarray(source.read_block(plan.level, t, channel, z_start, z_stop,
                                             plan.rows, plan.cols), dtype=np.float32)
        if stride == 1:
            return block
        planes = g1 - g0 + 1
        out = np.empty((planes,) + block.shape[1:], np.float32)
        for k in range(planes):
            part = block[k * stride:(k + 1) * stride]
            out[k] = part.mean(axis=0) if len(part) else 0.0
        return out

    return read
