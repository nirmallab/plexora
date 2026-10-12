"""A view of a volume, shaped like every other pyramid Plexora serves.

`plane_pyramid(source, spec)` returns something indexed `pyramid[str(level)]
[channel, rows, cols]` -- the contract `data_model.read_tile`, the encoders,
`SourceImage.read` and the node's region route already speak -- for each kind
of view:

- **plane**: the source's own plane pyramid, unwrapped. Zero overhead.
- **slab**: `SlabLevel`s, each computing `op` over planes z0..z1 of the same
  rectangle at the same level, on demand. A level-L tile is the projection of
  level-L planes; see `provenance` for why that is display-only. A *served*
  slab (`served=True`: the tile routes) also bounds what one tile reads -- see
  `SERVED_SLAB_BYTES`.
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
import os
import threading
from typing import Any

import numpy as np

from plexora.server.volume import reduce as _reduce
from plexora.server.volume.model import block_depth, reads_blocks
from plexora.server.volume.viewspec import BadView, ViewSpec

#: Bytes of planes one block read of a slab materializes. A store chunked
#: across z (an NGFF shard ten planes deep, an Imaris chunk of 64) is read a
#: block at a time rather than a plane at a time -- but one block of the whole
#: slab is every plane at once: 1937 planes of one 1024-pixel 16-bit tile is
#: 4 GB, two tiles at a time. Blocks this size keep the peak flat and still
#: decode each chunk once (`_Blocks` rounds them to the source's `z_chunk`).
BLOCK_BYTES = 64 * 2**20

#: Bytes of planes one SERVED slab tile may read. The cost of a projection
#: tile is its depth times its area, so a deep stack's full-depth projection
#: cannot be computed per tile at the finest levels: a light-sheet stack of
#: 1937 planes is 4 GB of decoding for one 1024-pixel tile. Past this budget the
#: tile is computed from the finest coarser level that fits and drawn up to
#: size -- a sharper picture arrives with a thinner slab, and an exact one by
#: pinning the projection (volume/pin.py). Reads that are not served (agent
#: renders, regions, pinning) are never coarsened.
SERVED_SLAB_BYTES = 2**30


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


class _Blocks:
    """`read_plane(z)` for one slab rectangle, served from blocks of `size`
    planes read in order -- what `reduce_planes` asks for, z0 to z1."""

    def __init__(self, source, level, t, channel, rows, cols, z1, size):
        self._source = source
        self._at = (level, t, channel)
        self._rows, self._cols = rows, cols
        self._z1 = z1
        self._size = size
        self._start = 0
        self._block = None

    def __call__(self, z):
        block = self._block
        if block is None or not self._start <= z < self._start + block.shape[0]:
            level, t, channel = self._at
            stop = min(self._z1, z + self._size - 1)
            block = np.asarray(self._source.read_block(level, t, channel, z, stop,
                                                       self._rows, self._cols))
            self._block, self._start = block, z
        return block[z - self._start]


class SlabLevel(_ViewLevel):
    """One level of a slab projection."""

    def __init__(self, source, level: int, spec: ViewSpec, served: bool = False):
        volume = source.volume
        height, width = source.level_shape(level)
        dtype = np.float32 if spec.op == "sum" else volume.dtype
        super().__init__(source, level, spec, (volume.channels, height, width), dtype)
        self._served = bool(served)
        self._itemsize = max(1, np.dtype(volume.dtype).itemsize)
        self._levels = len(volume.levels)

    def __getitem__(self, index):
        channel, rows, cols = _split(index)
        y0, y1 = _bounds(rows, self.shape[1])
        x0, x1 = _bounds(cols, self.shape[2])
        if y1 <= y0 or x1 <= x0:
            return np.zeros((max(0, y1 - y0), max(0, x1 - x0)), dtype=self.dtype)
        steps = self.coarsening(y1 - y0, x1 - x0) if self._served else 0
        if steps:
            # Said (`served_steps`), but final: nothing ever sharpens a drawn-up
            # projection, so it may be kept like any other tile.
            _served.steps = max(getattr(_served, "steps", 0), steps)
            out = self._drawn_up(channel, y0, y1, x0, x1, steps)
        else:
            out = self._reduce(self._level, channel, y0, y1, x0, x1)
        if isinstance(rows, (int, np.integer)):
            out = out[0]
        if isinstance(cols, (int, np.integer)):
            out = out[..., 0]
        return out

    def coarsening(self, height: int, width: int) -> int:
        """How many levels coarser a served `height` x `width` tile is computed
        at: 0 while it fits `SERVED_SLAB_BYTES`, else the fewest that fit (the
        coarsest level when none does)."""
        planes = self.spec.z1 - self.spec.z0 + 1
        steps = 0
        while (planes * math.ceil(height / 2 ** steps) * math.ceil(width / 2 ** steps)
               * self._itemsize > SERVED_SLAB_BYTES
               and self._level + steps + 1 < self._levels):
            steps += 1
        return steps

    def _reduce(self, level, channel, y0, y1, x0, x1):
        spec = self.spec
        source = self._source
        rows_s, cols_s = slice(y0, y1), slice(x0, x1)
        if reads_blocks(source, level):
            size = max(1, BLOCK_BYTES // ((y1 - y0) * (x1 - x0) * self._itemsize))
            chunk = block_depth(source, level)
            size = max(chunk, size - size % chunk)
            read = _Blocks(source, level, spec.t, channel, rows_s, cols_s, spec.z1, size)
        else:
            read = lambda z: source.read_plane(  # noqa: E731
                level, spec.t, z, channel, rows_s, cols_s)
        return _reduce.reduce_planes(read, spec.z0, spec.z1, spec.op, spec.q,
                                     dtype=None if spec.op == "sum" else self.dtype)

    def _drawn_up(self, channel, y0, y1, x0, x1, steps):
        """The rectangle projected `steps` levels coarser, each pixel repeated
        back up to this level's size (what the viewer would draw from a coarse
        tile anyway)."""
        factor = 2 ** steps
        height, width = self._source.level_shape(self._level + steps)
        cy0, cx0 = min(y0 // factor, height - 1), min(x0 // factor, width - 1)
        cy1 = max(cy0 + 1, min(height, -(-y1 // factor)))
        cx1 = max(cx0 + 1, min(width, -(-x1 // factor)))
        small = self._reduce(self._level + steps, channel, cy0, cy1, cx0, cx1)
        big = np.repeat(np.repeat(small, factor, axis=0), factor, axis=1)
        oy, ox = y0 - cy0 * factor, x0 - cx0 * factor
        out = big[oy:oy + (y1 - y0), ox:ox + (x1 - x0)]
        short = (y1 - y0 - out.shape[0], x1 - x0 - out.shape[1])
        if short[0] > 0 or short[1] > 0:
            out = np.pad(out, ((0, max(0, short[0])), (0, max(0, short[1]))), mode="edge")
        return np.ascontiguousarray(out)


def section_rows(depth: int, anisotropy: float, level: int) -> int:
    """How many rows a section has at `level` (z drawn in proportion)."""
    full = max(1, int(round(depth * max(anisotropy, 1e-6))))
    return max(1, math.ceil(full / (2 ** level)))


def section_plane_of(row: int, depth: int, anisotropy: float, level: int) -> int:
    """Which plane row `row` of a level-`level` section shows (nearest)."""
    z = int(math.floor((row + 0.5) * (2 ** level) / max(anisotropy, 1e-6)))
    return max(0, min(depth - 1, z))


#: What one SERVED section tile may decode, in bytes
#: (`PLEXORA_SECTION_READ_BUDGET_MB`). A section through a stack stored plane
#: by plane decodes a chunk-row of every plane it crosses -- a gigabyte for
#: one tile through 1937 light-sheet planes at full resolution. Past the
#: budget the tile is computed at the finest coarser level that fits (the 3-D
#: copy's levels are cheap) and drawn up; it says so (`served_steps`), and the
#: tile route keeps it out of every cache, so the sharp one replaces it as
#: soon as the area is prepared in 3-D.
SECTION_READ_BUDGET = max(8, int(os.environ.get("PLEXORA_SECTION_READ_BUDGET_MB", "") or 256)) * 2**20

_served = threading.local()


def served_steps(reset: bool = True) -> int:
    """How many levels coarser the last served tile on this thread was
    computed (0: at its own level)."""
    steps = getattr(_served, "steps", 0)
    if reset:
        _served.steps = 0
        _served.provisional = False
    return steps


def served_final() -> bool:
    """Whether the last served tile on this thread, if it was drawn up, is
    final: a projection's is (nothing sharpens it), a section's is not (it is
    replaced once the area is prepared in 3-D) and is kept out of every
    cache."""
    return not getattr(_served, "provisional", False)


def decode_shape(source, level: int) -> tuple[int, int, int]:
    """(z, y, x) of what one decode of `level` yields (a chunk, a brick, a tile)."""
    at = getattr(source, "decode_chunk", None)
    if callable(at):
        try:
            shape = at(level)
            if shape:
                return tuple(max(1, int(v)) for v in shape)
        except Exception:  # noqa: BLE001 -- a guess is good enough for a budget
            pass
    return (1, 512, 512)


class SectionLevel(_ViewLevel):
    """One level of an XZ (rows = z, cols = x) or YZ (rows = z, cols = y) section."""

    def __init__(self, source, level: int, spec: ViewSpec, served: bool = False):
        self._served = bool(served)
        self._levels = len(source.volume.levels)
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

    def read_cost(self, r0: int, r1: int, c0: int, c1: int) -> int:
        """Bytes decoded to read rows r0..r1, cols c0..c1 at this level."""
        volume = self._source.volume
        planes = len({section_plane_of(r, volume.depth, self._anisotropy, self._level)
                      for r in range(r0, r1)})
        _cz, cy, cx = decode_shape(self._source, self._level)
        across = cx if self.spec.axis == "xz" else cy
        chunks = -(-max(1, c1 - c0) // max(1, across))
        # A chunk-row of every plane the rows cross: a chunk deeper than one
        # plane is decoded once for all of its planes, so per plane it is the
        # same bytes either way.
        return planes * chunks * cy * cx * max(1, np.dtype(volume.dtype).itemsize)

    def coarsening(self, r0: int, r1: int, c0: int, c1: int) -> int:
        steps = 0
        while self._level + steps + 1 < self._levels:
            factor = 2 ** steps
            level = self if not steps else SectionLevel(self._source, self._level + steps, self.spec)
            if level.read_cost(r0 // factor, max(r0 // factor + 1, -(-r1 // factor)),
                               c0 // factor, max(c0 // factor + 1, -(-c1 // factor))) \
                    <= SECTION_READ_BUDGET:
                break
            steps += 1
        return steps

    def __getitem__(self, index):
        channel, rows, cols = _split(index)
        r0, r1 = _bounds(rows, self.shape[1])
        c0, c1 = _bounds(cols, self.shape[2])
        if r1 <= r0 or c1 <= c0:
            return np.zeros((max(0, r1 - r0), max(0, c1 - c0)), dtype=self.dtype)
        steps = self.coarsening(r0, r1, c0, c1) if self._served else 0
        if steps:
            _served.steps = max(getattr(_served, "steps", 0), steps)
            _served.provisional = True
            out = self._drawn_up(channel, r0, r1, c0, c1, steps)
        else:
            out = self._read(channel, r0, r1, c0, c1)
        if isinstance(rows, (int, np.integer)):
            out = out[0]
        if isinstance(cols, (int, np.integer)):
            out = out[..., 0]
        return out

    def _drawn_up(self, channel, r0, r1, c0, c1, steps):
        """The section `steps` levels coarser, each pixel repeated back up."""
        factor = 2 ** steps
        coarse = SectionLevel(self._source, self._level + steps, self.spec)
        rows, cols = coarse.shape[1], coarse.shape[2]
        cr0, cc0 = min(r0 // factor, rows - 1), min(c0 // factor, cols - 1)
        cr1 = max(cr0 + 1, min(rows, -(-r1 // factor)))
        cc1 = max(cc0 + 1, min(cols, -(-c1 // factor)))
        small = coarse._read(channel, cr0, cr1, cc0, cc1)
        big = np.repeat(np.repeat(small, factor, axis=0), factor, axis=1)
        oy, ox = r0 - cr0 * factor, c0 - cc0 * factor
        out = big[oy:oy + (r1 - r0), ox:ox + (c1 - c0)]
        short = (r1 - r0 - out.shape[0], c1 - c0 - out.shape[1])
        if short[0] > 0 or short[1] > 0:
            out = np.pad(out, ((0, max(0, short[0])), (0, max(0, short[1]))), mode="edge")
        return np.ascontiguousarray(out)

    def _read(self, channel, r0, r1, c0, c1):
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
        rows_s, cols_s = (at, span) if spec.axis == "xz" else (span, at)
        if reads_blocks(self._source, self._level):
            # One read across the planes: a store chunked across z decodes each
            # chunk once, where a line per plane decodes it once per plane (an
            # Imaris chunk is 64 planes deep).
            first, last = min(planes), max(planes)
            block = np.asarray(self._source.read_block(self._level, spec.t, channel, first,
                                                       last, rows_s, cols_s))
            for z in sorted(set(planes)):
                lines[z] = block[z - first].reshape(-1)
        else:
            for z in sorted(set(planes)):
                line = self._source.read_plane(self._level, spec.t, z, channel, rows_s, cols_s)
                lines[z] = np.asarray(line).reshape(-1)
        for i, z in enumerate(planes):
            line = lines[z]
            out[i, :line.shape[0]] = line
        return out


def plane_pyramid(source, spec: ViewSpec, served: bool = False) -> Any:
    """The pyramid serving `spec` from `source` (see module docstring).

    `served`: for the tile routes, whose slab tiles keep to
    `SERVED_SLAB_BYTES`; everything else reads exactly.
    """
    volume = source.volume
    spec = volume.default_view if spec.default else spec.clipped(volume)
    if spec.kind == "plane":
        return source.plane_pyramid(spec.t, spec.z)
    levels = len(volume.levels)
    if spec.kind == "slab":
        return DerivedPyramid([SlabLevel(source, L, spec, served=served)
                               for L in range(levels)], spec)
    if spec.kind == "section":
        return DerivedPyramid([SectionLevel(source, L, spec, served=served)
                               for L in range(levels)], spec)
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
