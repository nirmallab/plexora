"""Intensity isosurfaces: the boundary where a channel crosses one raw value.

`iso` is always in the channel's RAW units (or, with background subtraction,
raw units above the local background) -- never a display window's 0..1 --
so the record says exactly which value the surface is, and moving a contrast
slider never moves a surface.

Optional preprocessing runs per strip with a halo of planes, so a strip's
filtered edge planes are what filtering the whole volume would give:

* `sigma_um` -- a Gaussian, its sigma in microns turned into voxels per axis
  (an anisotropic stack is blurred as the tissue was);
* `background_um` -- an object diameter: the local background, a Gaussian of
  half that diameter, is subtracted (clipped at zero), Imaris's "local
  contrast", so a dim structure on a bright bed thresholds like a bright one.

Pieces are the mesh's connected components, numbered by size (1 = largest);
`min_size` drops the pieces that enclose less than it (µm³ when the spacing
is known, voxels³ otherwise) before anything is smoothed or written.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from plexora.server.surface import marching
from plexora.server.surface import mesh as mesh_mod


@dataclass(frozen=True)
class Preprocess:
    sigma_um: float = 0.0
    background_um: float = 0.0

    @classmethod
    def from_dict(cls, raw):
        raw = raw if isinstance(raw, dict) else {}

        def number(key):
            try:
                value = float(raw.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0
            return value if math.isfinite(value) and value > 0 else 0.0

        return cls(sigma_um=number("sigma_um"), background_um=number("background_um"))

    @property
    def active(self) -> bool:
        return self.sigma_um > 0 or self.background_um > 0

    def to_dict(self) -> dict | None:
        return ({"sigma_um": self.sigma_um, "background_um": self.background_um}
                if self.active else None)

    def sigmas(self, voxel_um):
        """`(blur, background)` sigmas in grid voxels, (z, y, x)."""
        vx, vy, vz = (max(1e-9, float(v)) for v in voxel_um)
        blur = (self.sigma_um / vz, self.sigma_um / vy, self.sigma_um / vx) \
            if self.sigma_um > 0 else None
        radius = self.background_um / 2.0
        background = (radius / vz, radius / vy, radius / vx) if radius > 0 else None
        return blur, background

    def halo(self, voxel_um) -> int:
        """Planes read either side of a strip so its filtered planes are exact."""
        blur, background = self.sigmas(voxel_um)
        widest = max((s[0] for s in (blur, background) if s), default=0.0)
        return min(64, int(math.ceil(4.0 * widest)))

    def apply(self, block, voxel_um):
        from scipy import ndimage

        block = np.asarray(block, dtype=np.float32)
        blur, background = self.sigmas(voxel_um)
        if blur:
            block = ndimage.gaussian_filter(block, blur, mode="nearest")
        if background:
            block = np.clip(block - ndimage.gaussian_filter(block, background, mode="nearest"),
                            0, None).astype(np.float32)
        return block


def preprocessed_reader(read, depth, pre: Preprocess | None, voxel_um):
    """`read(z0, z1)` with `pre` applied over a halo of planes."""
    if pre is None or not pre.active:
        return read
    halo = pre.halo(voxel_um)

    def reader(z0, z1):
        a, b = max(0, z0 - halo), min(depth - 1, z1 + halo)
        block = pre.apply(read(a, b), voxel_um)
        return block[z0 - a:z0 - a + (z1 - z0 + 1)]

    return reader


def extract(read, grid: marching.Grid, iso, *, pre: Preprocess | None = None,
            voxel_um=(1.0, 1.0, 1.0), scale=(1.0, 1.0, 1.0), min_size=0.0,
            smoothing: mesh_mod.Smoothing | None = None, strip=marching.STRIP,
            keep_raw=True, progress=None, cancelled=None):
    """The isosurface of `read` over `grid` at `iso`.

    `read(z0, z1)` returns grid planes z0..z1 as float32 (Z, H, W).
    `voxel_um` is one GRID voxel's size (x, y, z), for preprocessing; `scale`
    is one LEVEL-0 voxel's physical size (x, y, z), for sizes and smoothing.
    Returns `(Mesh, report)`; raises `marching.NoSurface`."""
    depth = int(grid.shape[0])
    reader = preprocessed_reader(read, depth, pre, voxel_um)
    positions, triangles = marching.march(reader, grid.shape, iso, strip=strip, close=True,
                                          progress=progress, cancelled=cancelled)
    positions = grid.to_level0(positions)
    surface = mesh_mod.Mesh(positions=positions, triangles=triangles,
                            object_id=np.zeros(len(positions), np.uint32))
    count, labels = mesh_mod.components(len(positions), triangles)
    surface = mesh_mod.renumber_by_size(surface, labels)
    surface = mesh_mod.sort_by_object(surface)
    report = {"pieces": int(count), "dropped_small": 0}
    if min_size and min_size > 0 and surface.n_objects:
        volumes = np.abs(_object_volumes(surface, scale))
        keep_ids = surface.objects[volumes >= float(min_size), 0]
        report["dropped_small"] = int(surface.n_objects - len(keep_ids))
        if not len(keep_ids):
            raise marching.NoSurface(
                f"every piece at {iso:g} is smaller than the minimum size ({min_size:g})")
        surface = mesh_mod.subset(surface, keep_ids)
        count, labels = mesh_mod.components(surface.n_vertices, surface.triangles)
        surface = mesh_mod.sort_by_object(
            mesh_mod.renumber_by_size(replace(surface, objects=None), labels))
    report["pieces_kept"] = surface.n_objects
    surface = finish(surface, smoothing, scale=scale, voxel=grid.scale, keep_raw=keep_raw,
                     report=report)
    return surface, report


def _object_volumes(surface, scale):
    t = surface.triangles.astype(np.int64)
    p = surface.positions.astype(np.float64) * np.asarray(scale, dtype=np.float64)
    per = np.einsum("ij,ij->i", p[t[:, 0]], np.cross(p[t[:, 1]], p[t[:, 2]])) / 6.0
    return np.add.reduceat(per, surface.objects[:, 1].astype(np.int64))


def finish(surface, smoothing, *, scale, voxel, keep_raw=True, report=None, inset=0.0):
    """Smooth (Taubin, constrained), compute normals, inset; fills `report`."""
    report = report if report is not None else {}
    before = float(np.abs(_object_volumes(surface, scale)).sum()) if surface.n_objects else 0.0
    if smoothing is not None and smoothing.iterations > 0 and surface.n_triangles:
        raw = surface.positions
        smoothed, moved = mesh_mod.taubin(raw, surface.triangles, smoothing, scale=scale,
                                          voxel=voxel)
        surface = replace(surface, positions=smoothed, positions_raw=raw if keep_raw else None)
        report["smoothing"] = {**smoothing.to_dict(), **moved}
    normals = mesh_mod.vertex_normals(surface.positions, surface.triangles)
    if inset:
        surface = replace(surface, positions=mesh_mod.inset(surface.positions, normals, inset))
        report["inset_vox"] = float(inset)
    surface = replace(surface, normals=normals)
    after = float(np.abs(_object_volumes(surface, scale)).sum()) if surface.n_objects else 0.0
    if before > 0:
        report["volume_change_pct"] = round(100.0 * (after - before) / before, 3)
    return surface
