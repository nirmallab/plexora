"""A view of a volume, shaped like every other pyramid Plexora serves.

`plane_pyramid(source, spec)` returns something indexed `pyramid[str(level)]
[channel, rows, cols]` -- the contract `data_model.read_tile`, the encoders,
`SourceImage.read` and the node's region route already speak -- for each kind
of view:

- **plane**: the source's own plane pyramid, unwrapped. Zero overhead.
- **slab**: `SlabLevel`s, each computing `op` over planes z0..z1 of the same
  rectangle at the same level, on demand. A level-L tile is the projection of
  level-L planes; see `provenance` for why that is display-only.
- **section**: `SectionLevel`s, one row (XZ) or column (YZ) of every plane,
  stacked so that rows are z. Row height follows the z spacing, so a section
  is drawn in proportion: a level-0 XZ section of a 40-plane stack at 2.5 µm
  over 0.5 µm pixels is 200 rows tall. An unknown z spacing draws one row per
  plane (`assumed`, which provenance says).

Like `ome_zarr.NgffPyramid`, the derived pyramid deliberately has no `.shape`:
that attribute is how existing code tells a single array from a pyramid.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from plexora.server.volume import reduce as _reduce
from plexora.server.volume.viewspec import BadView, ViewSpec


class DerivedPyramid:
    """Levels computed from a volume, presented like a zarr group."""

    def __init__(self, levels, spec: ViewSpec | None = None):
        self._levels = list(levels)
        self.spec = spec

    def __len__(self) -> int:
        return len(self._levels)

    def __iter__(self):
        return iter(str(index) for index in range(len(self._levels)))

    def __contains__(self, key) -> bool:
        try:
            return 0 <= int(key) < len(self._levels)
        except (TypeError, ValueError):
            return False

    def __getitem__(self, key):
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise KeyError(key) from None
        if not 0 <= index < len(self._levels):
            raise KeyError(key)
        return self._levels[index]

    @property
    def level_shapes(self) -> list[list[int]]:
        return [[int(level.shape[-2]), int(level.shape[-1])] for level in self._levels]

    def close(self) -> None:  # the source owns every handle
        pass


def _split(index) -> tuple[int, slice, slice]:
    if not isinstance(index, tuple):
        index = (index,)
    channel = index[0]
    if not isinstance(channel, (int, np.integer)):
        raise TypeError("a view level is indexed as [channel, rows, cols] with an "
                        f"integer channel, not {channel!r}")
    rows = index[1] if len(index) > 1 else slice(None)
    cols = index[2] if len(index) > 2 else slice(None)
    return int(channel), rows, cols


def _bounds(part, size: int) -> tuple[int, int]:
    if isinstance(part, (int, np.integer)):
        start = int(part) + (size if part < 0 else 0)
        return start, start + 1
    start, stop, step = part.indices(size)
    if step != 1:
        raise TypeError("view levels support contiguous slices only")
    return start, max(start, stop)


class _ViewLevel:
    ndim = 3

    def __init__(self, source, level: int, spec: ViewSpec, shape, dtype):
        self._source = source
        self._level = int(level)
        self.spec = spec
        self.shape = tuple(int(s) for s in shape)
        self.dtype = np.dtype(dtype)
        chunk = 1024
        self.chunks = (1, min(chunk, self.shape[1]) or 1, min(chunk, self.shape[2]) or 1)

    def __array__(self, dtype=None, copy=None):
        stack = np.stack([np.asarray(self[c]) for c in range(self.shape[0])])
        return stack.astype(dtype) if dtype is not None else stack


class SlabLevel(_ViewLevel):
    """One level of a slab projection."""

    def __init__(self, source, level: int, spec: ViewSpec):
        volume = source.volume
        height, width = source.level_shape(level)
        dtype = np.float32 if spec.op == "sum" else volume.dtype
        super().__init__(source, level, spec, (volume.channels, height, width), dtype)

    def __getitem__(self, index):
        channel, rows, cols = _split(index)
        y0, y1 = _bounds(rows, self.shape[1])
        x0, x1 = _bounds(cols, self.shape[2])
        if y1 <= y0 or x1 <= x0:
            return np.zeros((max(0, y1 - y0), max(0, x1 - x0)), dtype=self.dtype)
        spec = self.spec
        source = self._source
        rows_s, cols_s = slice(y0, y1), slice(x0, x1)
        if getattr(source, "prefers_block", False):
            block = source.read_block(self._level, spec.t, channel, spec.z0, spec.z1,
                                      rows_s, cols_s)
            read = lambda z: block[z - spec.z0]  # noqa: E731
        else:
            read = lambda z: source.read_plane(  # noqa: E731
                self._level, spec.t, z, channel, rows_s, cols_s)
        out = _reduce.reduce_planes(read, spec.z0, spec.z1, spec.op, spec.q,
                                    dtype=None if spec.op == "sum" else self.dtype)
        if isinstance(rows, (int, np.integer)):
            out = out[0]
        if isinstance(cols, (int, np.integer)):
            out = out[..., 0]
        return out


def section_rows(depth: int, anisotropy: float, level: int) -> int:
    """How many rows a section has at `level` (z drawn in proportion)."""
    full = max(1, int(round(depth * max(anisotropy, 1e-6))))
    return max(1, math.ceil(full / (2 ** level)))


def section_plane_of(row: int, depth: int, anisotropy: float, level: int) -> int:
    """Which plane row `row` of a level-`level` section shows (nearest)."""
    z = int(math.floor((row + 0.5) * (2 ** level) / max(anisotropy, 1e-6)))
    return max(0, min(depth - 1, z))


class SectionLevel(_ViewLevel):
    """One level of an XZ (rows = z, cols = x) or YZ (rows = z, cols = y) section."""

    def __init__(self, source, level: int, spec: ViewSpec):
        volume = source.volume
        height, width = source.level_shape(level)
        anisotropy = volume.spacing.anisotropy()
        rows = section_rows(volume.depth, anisotropy, level)
        cols = width if spec.axis == "xz" else height
        super().__init__(source, level, spec, (volume.channels, rows, cols), volume.dtype)
        self._anisotropy = anisotropy
        full_h, full_w = volume.height, volume.width
        if spec.axis == "xz":
            self._at = min(height - 1, int(spec.index * height / max(1, full_h)))
        else:
            self._at = min(width - 1, int(spec.index * width / max(1, full_w)))

    def __getitem__(self, index):
        channel, rows, cols = _split(index)
        r0, r1 = _bounds(rows, self.shape[1])
        c0, c1 = _bounds(cols, self.shape[2])
        out = np.zeros((max(0, r1 - r0), max(0, c1 - c0)), dtype=self.dtype)
        if out.size == 0:
            return out
        volume = self._source.volume
        planes = [section_plane_of(r, volume.depth, self._anisotropy, self._level)
                  for r in range(r0, r1)]
        spec = self.spec
        at = slice(self._at, self._at + 1)
        span = slice(c0, c1)
        lines = {}
        for z in sorted(set(planes)):
            if spec.axis == "xz":
                line = self._source.read_plane(self._level, spec.t, z, channel, at, span)
                lines[z] = np.asarray(line).reshape(-1)
            else:
                line = self._source.read_plane(self._level, spec.t, z, channel, span, at)
                lines[z] = np.asarray(line).reshape(-1)
        for i, z in enumerate(planes):
            line = lines[z]
            out[i, :line.shape[0]] = line
        if isinstance(rows, (int, np.integer)):
            out = out[0]
        if isinstance(cols, (int, np.integer)):
            out = out[..., 0]
        return out


def plane_pyramid(source, spec: ViewSpec) -> Any:
    """The pyramid serving `spec` from `source` (see module docstring)."""
    volume = source.volume
    spec = volume.default_view if spec.default else spec.clipped(volume)
    if spec.kind == "plane":
        return source.plane_pyramid(spec.t, spec.z)
    levels = len(volume.levels)
    if spec.kind == "slab":
        return DerivedPyramid([SlabLevel(source, L, spec) for L in range(levels)], spec)
    if spec.kind == "section":
        return DerivedPyramid([SectionLevel(source, L, spec) for L in range(levels)], spec)
    raise BadView(f"unknown view kind {spec.kind!r}")


def section_geometry(volume, spec: ViewSpec) -> dict:
    """What a section's tile source needs: width, height, level shapes."""
    anisotropy = volume.spacing.anisotropy()
    shapes = []
    for info in volume.levels:
        rows = section_rows(volume.depth, anisotropy, info.index)
        cols = info.width if spec.axis == "xz" else info.height
        shapes.append([rows, cols])
    return {"width": shapes[0][1], "height": shapes[0][0], "level_shapes": shapes,
            "anisotropy": anisotropy,
            "anisotropy_source": volume.spacing.anisotropy_source}
