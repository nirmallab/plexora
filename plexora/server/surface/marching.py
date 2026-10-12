"""Marching cubes over a volume read in z-strips, welded across the seams.

One pipeline for every surface Plexora extracts. The volume is read
`STRIP` planes at a time plus the next strip's first plane, so every cube
between two planes is marched exactly once and the plane two strips share
is marched from both sides. scikit-image's Lewiner marching cubes places an
edge's vertex from that edge's two voxels alone, and a strip's offset is a
whole number of planes, so the shared plane's vertices come out bitwise
identical in both strips: `mesh.weld` joins them exactly and the surface is
closed across every seam, with no tolerance to tune.

**The grid.** Marching runs on an extraction grid -- a level of the pyramid,
an area of it, every plane or every few planes averaged -- and `Grid` maps
its voxels back onto level-0 voxels: `level0 = scale * grid + offset` per
axis, with the grid voxel's centre landing on the centre of the level-0
voxels it covers.

**Closing at the volume's edge.** With `close`, the strips are padded with a
value far below any isovalue, so a structure cut by the edge of the volume
(or of the area asked for) is capped flat on the outermost voxels' centres
instead of left open; the measurement then says it touches the bounds.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from plexora.server.surface import mesh as mesh_mod

#: Planes per strip. Small enough that a strip of a 4k x 4k area at float32 is
#: 2 GB at most; the strips are the unit of progress and cancel too.
STRIP = 32
#: Below any isovalue: the padding that closes surfaces at the volume's edge.
PAD = np.float32(-1e30)


class NoSurface(ValueError):
    """Nothing in the volume crosses the isovalue."""


@dataclass(frozen=True)
class Grid:
    """An extraction grid: `shape` (Z, H, W), and per axis (x, y, z) how many
    level-0 voxels one grid voxel spans (`scale`) and where grid voxel 0's
    centre sits in level-0 voxels (`offset`)."""

    shape: tuple
    scale: tuple = (1.0, 1.0, 1.0)
    offset: tuple = (0.0, 0.0, 0.0)

    @classmethod
    def of(cls, shape, *, factor_xy=(1.0, 1.0), z_stride=1, origin=(0, 0, 0)):
        """The grid of a pyramid level read from level-k column `origin[0]`,
        row `origin[1]` and level-0 plane `origin[2]`, every `z_stride` planes
        averaged."""
        fx, fy = (float(f) for f in factor_xy)
        c0, r0, z0 = origin
        s = float(z_stride)
        return cls(shape=tuple(int(v) for v in shape), scale=(fx, fy, s),
                   offset=((c0 + 0.5) * fx - 0.5, (r0 + 0.5) * fy - 0.5, z0 + (s - 1.0) / 2.0))

    def to_level0(self, positions) -> np.ndarray:
        p = np.asarray(positions, dtype=np.float64)
        return (p * np.asarray(self.scale) + np.asarray(self.offset)).astype(np.float32)

    def to_dict(self) -> dict:
        return {"shape": list(self.shape), "scale": list(self.scale), "offset": list(self.offset)}


def _strips(depth: int, strip: int):
    if depth <= 1:
        return [(0, 0)]
    return [(s, min(depth - 1, s + strip)) for s in range(0, depth - 1, strip)]


def march(read, shape, level, *, strip=STRIP, close=True, pad_value=PAD, progress=None,
          cancelled=None):
    """Marching cubes over `read(z0, z1) -> float32 (z1-z0+1, H, W)`.

    Returns `(positions (N,3) float32 in GRID voxels as (x, y, z), triangles
    (M,3) uint32)`, welded, outward-facing (normals point from above the
    isovalue to below). Raises NoSurface when nothing crosses `level`.

    `pad_value` is what lies beyond the edge when `close`: the default caps a
    surface on the outermost voxels' centres; a binary mask's 0 caps it on
    their outer faces, so a cell cut by the edge keeps its voxels' volume."""
    depth = int(shape[0])
    level = float(level)
    pieces, total = [], 0
    strips = _strips(depth, int(strip))
    for index, (z0, z1) in enumerate(strips):
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")
        block = np.asarray(read(z0, z1), dtype=np.float32)
        lead = bool(close and z0 == 0)
        tail = bool(close and z1 == depth - 1)
        if close:
            block = np.pad(block, ((int(lead), int(tail)), (1, 1), (1, 1)),
                           constant_values=np.float32(pad_value))
        if progress is not None:
            progress(index + 1, len(strips))
        if min(block.shape) < 2:
            continue
        hi, lo = float(block.max()), float(block.min())
        if not (lo < level < hi):
            continue
        try:
            from skimage import measure

            verts, faces, _, _ = measure.marching_cubes(block, level, allow_degenerate=False)
        except (ValueError, RuntimeError):
            continue
        if not len(faces):
            continue
        # (z, y, x) -> (x, y, z), back out of the padding, onto the strip's
        # planes. Whole numbers added to float32: exact, so the seam welds.
        pad = np.float32(1.0 if close else 0.0)
        positions = np.empty_like(verts)
        positions[:, 0] = verts[:, 2] - pad
        positions[:, 1] = verts[:, 1] - pad
        positions[:, 2] = verts[:, 0] - np.float32(int(lead)) + np.float32(z0)
        pieces.append((positions, faces.astype(np.int64) + total))
        total += len(positions)
    if not pieces:
        raise NoSurface(f"nothing in the volume crosses {level:g}")
    positions = np.concatenate([p for p, _ in pieces])
    triangles = np.concatenate([t for _, t in pieces])
    positions, triangles, _ = mesh_mod.weld(positions, triangles)
    return positions, triangles
