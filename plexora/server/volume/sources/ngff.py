"""An OME-Zarr image as a volume.

NGFF names its axes and their types (`space`, `time`, `channel`), so unlike a
TIFF there is no cycles-on-Z ambiguity to resolve: a `z` axis is depth and a
`t` axis is time. Each plane is the store's own arrays seen through
`ome_zarr._LevelView` pinned to (t, z) -- the same object `open_image` hands
the tile path for a planar store, with a different pin.

A level whose chunks are more than one plane deep reads its planes through
the decoded-chunk cache instead (`_CachedPlane`): a plane's chunk holds the
next planes too, so read directly it was decoded again at every step and
kept by nothing, and the block the plane prefetcher read ahead
(volume/prefetch.py) was thrown away. Through the cache, a step inside a
chunk decodes nothing and the step across its edge finds the next block read.

A store missing coarse levels gets them derived once (`build_volume_extension`)
for EVERY plane, into arrays shaped (T, C, Z, h, w): the 2-D extension
`ome_zarr.build_extension` writes is one plane's, and a z-stack served from
it would show plane 0 at every zoom-out. "Missing" is the 2-D rule (a coarsest
level over 2048 px a side) or the stack rule (a coarsest level whose stack is
over `STACK_LEVEL_VOXELS` a channel: no 3-D texture could hold it).

A small derivation runs while the project opens. A large one runs in the
background (`data_model._with_volume_extension`); until it is done the source
serves the levels it will have as subsamples of the store's coarsest level
(`plan_extension`), and `adopt_extension` swaps the real ones in.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Optional

import numpy as np
import zarr

from plexora._transient_locks import past_transient_locks
from plexora.server.utils import ome_zarr
from plexora.server.volume.axes import spacing_from
from plexora.server.volume import chunk_cache
from plexora.server.volume.model import (Volume, VolumeSource, level_infos, stored_depth,
                                         stored_plane)

VOLUME_EXTENSION_NAME = "image_pyramid_volume.zarr"

#: Above this many bytes to read, the coarse levels are derived in the
#: background rather than while the project opens.
FOREGROUND_EXTENSION_BYTES = 256 * 2**20


class NgffVolumeSource(VolumeSource):
    def __init__(self, path, arrays, axes, multiscale, base_levels, volume: Volume,
                 extension=None):
        super().__init__(volume)
        self.path = str(path)
        self._arrays = arrays
        self._axes = tuple(axes)
        self._multiscale = multiscale
        self._base_levels = base_levels
        self._extension = extension
        #: The levels a pending derivation will add, served meanwhile as
        #: subsamples of the coarsest stored level (`plan_extension`).
        self._interim: list = []
        #: Where that derivation writes (None: no derivation is pending).
        self.pending_extension: Optional[str] = None
        z_chunks = 1
        if "z" in self._axes:
            chunks = tuple(getattr(arrays[0], "shards", None) or arrays[0].chunks)
            z_chunks = int(chunks[self._axes.index("z")])
        #: A store chunked across z serves a slab in one read per chunk rather
        #: than one per plane (see plane_view.SlabLevel).
        self.prefers_block = z_chunks > 1
        self.z_chunk = z_chunks
        #: Planes one decode yields: the inner chunk's depth inside a shard
        #: (volume/bricks.py copies a stack whose decodes are one plane deep).
        self.z_decode = int(arrays[0].chunks[self._axes.index("z")]) if "z" in self._axes else 1
        #: What the decoded-chunk cache knows this source's chunks by: one
        #: open of the store, forgotten when it closes.
        self.cache_id = ("ngff", self.path, id(self))

    def _build_plane(self, t: int, z: int):
        # A coarse level that halves z holds plane z of the stack at its own
        # plane `stored_plane(level, z)`, as Imaris's do.
        levels = []
        for level, array in enumerate(self._arrays[:self._base_levels]):
            stored = stored_plane(self.volume, level, z)
            if "z" in self._axes and int(array.chunks[self._axes.index("z")]) > 1:
                levels.append(_CachedPlane(self, level, array, self._axes, t, stored))
            else:
                levels.append(ome_zarr._make_level(array, self._axes, t=t, z=stored))
        if self._extension is not None:
            index = self._base_levels
            while str(index) in self._extension:
                levels.append(ome_zarr._LevelView(
                    self._extension[str(index)], ome_zarr.VOLUME_EXTENSION_AXES, t=t, z=z))
                index += 1
        else:
            coarsest = levels[-1]
            for step, shape in enumerate(self._interim, start=1):
                levels.append(_Subsampled(coarsest, 2 ** step, shape))
        return ome_zarr.NgffPyramid(levels, multiscale=self._multiscale, axes=self._axes,
                                    path=self.path, base_levels=self._base_levels)

    def decode_chunk(self, level):
        """(z, y, x) of one decode at `level`: the inner chunk."""
        if level >= self._base_levels:
            return None
        chunks = self._arrays[level].chunks
        pick = lambda name, default: int(chunks[self._axes.index(name)]) if name in self._axes else default  # noqa: E731
        return (pick("z", 1), pick("y", 512), pick("x", 512))

    def read_block(self, level, t, channel, z0, z1, rows, cols):
        if level >= self._base_levels or "z" not in self._axes:
            return super().read_block(level, t, channel, z0, z1, rows, cols)
        if stored_depth(self.volume, level) < self.volume.depth:
            # Planes z0..z1 of the stack from the fewer planes the level
            # stores: the span read once, each stored plane repeated for the
            # stack planes it stands for.
            stored = [stored_plane(self.volume, level, z) for z in range(z0, z1 + 1)]
            span = self.read_stored(level, t, channel, stored[0], stored[-1], rows, cols)
            return span[[s - stored[0] for s in stored]]
        return self.read_stored(level, t, channel, z0, z1, rows, cols)

    def read_stored(self, level, t, channel, s0, s1, rows, cols):
        """Stored planes s0..s1 (inclusive) of a base level, each once, in
        whole chunks through the decoded-chunk cache (`chunk_cache.
        read_through`): a section's next tile, or a slab's next rectangle,
        reuses the chunks its neighbour decoded."""
        array = self._arrays[level]

        def read_box(z0, z1, y0, y1, x0, x1):
            index = []
            for name in self._axes[:-2]:
                if name == "t":
                    index.append(int(t))
                elif name == "c":
                    index.append(int(channel))
                elif name == "z":
                    index.append(slice(int(z0), int(z1)))
                else:
                    index.append(0)
            return np.asarray(array[tuple(index) + (slice(y0, y1), slice(x0, x1))])

        if not (isinstance(rows, slice) and isinstance(cols, slice)):
            return np.asarray(read_box(int(s0), int(s1) + 1, None, None, None, None)
                              [:, rows, cols])
        height, width = (int(v) for v in array.shape[-2:])
        r0, r1 = rows.indices(height)[:2]
        c0, c1 = cols.indices(width)[:2]
        chunks = array.chunks
        chunk = (int(chunks[self._axes.index("z")]), int(chunks[-2]), int(chunks[-1]))
        return chunk_cache.read_through(
            self.cache_id, int(level), t, channel, read_box, chunk,
            ((int(s0), int(s1) + 1), (r0, r1), (c0, c1)), array.dtype)

    def close(self) -> None:
        super().close()
        chunk_cache.CACHE.forget(self.cache_id)

    # -- deriving the coarse levels it lacks ------------------------------------

    def plan_extension(self, dest=None) -> None:
        """Serve the levels `build_volume_extension` would derive before they
        exist, each a subsample of the coarsest stored level, so a zoomed-out
        tile reads that level rather than failing. `dest` is where a pending
        derivation writes them; None when none is coming."""
        self._interim = [tuple(shape) for shape in extension_shapes(self)]
        self.pending_extension = str(dest) if dest else None
        self._forget_planes()

    def adopt_extension(self, path) -> bool:
        """Take up the levels a derivation wrote at `path`; False when it holds
        none."""
        group = zarr.open_group(str(path), mode="r")
        if not dict(group.attrs).get("plexora_volume_extension"):
            return False
        shapes, depths = _level_shapes_with(self._arrays, self._axes, self._base_levels,
                                            group, self.volume.depth)
        self.volume = replace(self.volume, levels=level_infos(
            shapes, tuple(self._arrays[0].chunks)[-2:], depths))
        self._extension = group
        self._interim = []
        self.pending_extension = None
        self._forget_planes()
        return True

    def _forget_planes(self) -> None:
        with self._lock:
            self._planes.clear()


class _CachedPlane(ome_zarr._LevelView):
    """One base level of one plane, `[channel, rows, cols]`, of a store whose
    chunks are deeper than a plane: a rectangle is read in whole chunks
    through the decoded-chunk cache (`NgffVolumeSource.read_stored`), so the
    planes beside it come out of the cache. The same voxels as reading the
    array directly; anything but a plain rectangle -- a stride, an empty or
    reversed range -- is read directly, as a `_LevelView` reads it. Everything
    that looks inside a level view (`ome_zarr._storage_keys`) sees one."""

    __slots__ = ("_source", "_level", "_t", "_stored")

    def __init__(self, source, level, array, axes, t, stored):
        super().__init__(array, axes, t=t, z=stored)
        self._source = source
        self._level = int(level)
        self._t = int(t)
        self._stored = int(stored)

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)
        channel = index[0]
        rows = index[1] if len(index) > 1 else slice(None)
        cols = index[2] if len(index) > 2 else slice(None)
        if (isinstance(channel, (int, np.integer)) and _plain(rows, self.shape[1])
                and _plain(cols, self.shape[2])):
            return self._source.read_stored(self._level, self._t, int(channel), self._stored,
                                            self._stored, rows, cols)[0]
        return super().__getitem__(index)


def _plain(part, size) -> bool:
    """Whether `part` is a rectangle's side a chunked read gives exactly: a
    slice, unit step, that covers at least one pixel of `size`."""
    if not isinstance(part, slice) or part.step not in (None, 1):
        return False
    start, stop, _ = part.indices(size)
    return stop > start


class _Subsampled:
    """A level a derivation has yet to write, read as every `step`-th row and
    column of a finer one: `[channel, rows, cols]` like the level it stands
    in for. Labels are not involved, so a subsample is only a coarser look at
    the same pixels; the derived level replaces it with 2x2 means."""

    ndim = 3

    def __init__(self, finer, step: int, shape):
        self._finer = finer
        self._step = int(step)
        self.shape = (int(finer.shape[0]), int(shape[0]), int(shape[1]))
        self.dtype = finer.dtype
        self.chunks = (1, min(1024, self.shape[1]), min(1024, self.shape[2]))

    def _scaled(self, index, size):
        if isinstance(index, slice):
            start, stop, stride = index.indices(size)
            return slice(start * self._step, stop * self._step, stride * self._step)
        return int(index) * self._step

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)
        channel = index[0]
        rows = index[1] if len(index) > 1 else slice(None)
        cols = index[2] if len(index) > 2 else slice(None)
        return np.asarray(self._finer[channel, self._scaled(rows, self.shape[1]),
                                      self._scaled(cols, self.shape[2])])

    def __array__(self, dtype=None, copy=None):
        whole = np.stack([self[c] for c in range(self.shape[0])])
        return whole.astype(dtype) if dtype is not None else whole


def _level_shapes_with(arrays, axes, base_levels, extension, depth):
    """`(shapes, depths)` of a store's levels: its own dyadic ones, then those
    a derivation added. Each base level's planes, since a store may halve z in
    its coarse levels as well as x and y; the derived levels hold every plane."""
    shapes = [[int(a.shape[-2]), int(a.shape[-1])] for a in arrays[:base_levels]]
    if extension is not None:
        index = base_levels
        while str(index) in extension:
            shapes.append([int(extension[str(index)].shape[-2]),
                           int(extension[str(index)].shape[-1])])
            index += 1
    depths = None
    if "z" in axes:
        z_axis = axes.index("z")
        depths = ([int(a.shape[z_axis]) for a in arrays[:base_levels]]
                  + [depth] * (len(shapes) - base_levels))
    return shapes, depths

    @property
    def physical(self) -> dict:
        return ome_zarr.physical_metadata(self)

    @property
    def multiscale(self):  # what ome_zarr.physical_metadata reads
        return self._multiscale


def open_ngff_volume(path, extension: Optional[str] = None):
    """An `NgffVolumeSource` for the store at `path`, or None when it is planar."""
    root = ome_zarr._open_group(path)
    multiscale = ome_zarr._multiscale_of(root)
    if multiscale is None:
        return None
    names = ome_zarr._dataset_paths(multiscale)
    arrays = [root[name] for name in names]
    axes = ome_zarr._axes_of(multiscale, arrays[0].ndim)
    shape = dict(zip(axes, (int(s) for s in arrays[0].shape)))
    depth = shape.get("z", 1)
    frames = shape.get("t", 1)
    if depth <= 1 and frames <= 1:
        return None
    level_shapes = [[int(a.shape[-2]), int(a.shape[-1])] for a in arrays]
    base_levels = ome_zarr.dyadic_prefix(level_shapes)
    derived = None
    if extension and Path(extension).exists():
        group = zarr.open_group(str(extension), mode="r")
        if dict(group.attrs).get("plexora_volume_extension"):
            derived = group
    shapes, depths = _level_shapes_with(arrays, axes, base_levels, derived, depth)
    physical = ome_zarr.physical_metadata(_Multiscale(multiscale))
    spacing = spacing_from(physical, depth=depth, frames=frames)
    chunks = tuple(arrays[0].chunks)
    names_meta = ome_zarr.channel_labels(path, shape.get("c", 1))
    volume = Volume(
        channels=shape.get("c", 1), depth=depth, frames=frames,
        height=shapes[0][0], width=shapes[0][1], dtype=str(np.dtype(arrays[0].dtype)),
        spacing=spacing, levels=level_infos(shapes, chunks[-2:], depths),
        source_kind="ome_zarr",
        z_semantics="depth", t_semantics="time",
        channel_names=tuple(names_meta) if names_meta else None,
        provenance={"planes_from": "NGFF axes " + "".join(axes),
                    **({"z_spacing_from": "NGFF scale"} if spacing.z.source == "metadata"
                       else {})},
        stored_axes="".join(axes))
    return NgffVolumeSource(path, arrays, axes, multiscale, base_levels, volume,
                            extension=derived)


class _Multiscale:
    """What `ome_zarr.physical_metadata` reads off a pyramid: `.multiscale`."""

    def __init__(self, multiscale):
        self.multiscale = multiscale


def _stack_planes(volume) -> int:
    return max(1, int(volume.depth)) * max(1, int(volume.frames))


def extension_shapes(source: NgffVolumeSource, target: int = ome_zarr.EXTENSION_TARGET,
                     voxels: Optional[int] = None) -> list:
    """`(h, w)` of each level `build_volume_extension` derives, finest first:
    halvings of the store's coarsest level until one is at most `target` px a
    side and its stack (every plane) at most `voxels` a channel."""
    from plexora.server.utils.image_pyramid import STACK_LEVEL_VOXELS

    voxels = STACK_LEVEL_VOXELS if voxels is None else int(voxels)
    planes = _stack_planes(source.volume)
    height, width = (int(s) for s in source._arrays[source._base_levels - 1].shape[-2:])
    shapes = []
    while max(height, width) > 1 and (
            max(height, width) > target or (planes > 1 and planes * height * width > voxels)):
        height, width = -(-height // 2), -(-width // 2)
        shapes.append((height, width))
    return shapes


def needs_volume_extension(source: NgffVolumeSource,
                           threshold: int = ome_zarr.EXTENSION_THRESHOLD,
                           voxels: Optional[int] = None) -> bool:
    """Whether the store's coarsest level is too large to zoom out to (the 2-D
    rule) or too large a stack for any 3-D view (the stack rule)."""
    from plexora.server.utils.image_pyramid import STACK_LEVEL_VOXELS

    voxels = STACK_LEVEL_VOXELS if voxels is None else int(voxels)
    volume = source.volume
    info = volume.levels[-1]
    if max(info.height, info.width) > threshold:
        return True
    planes = stored_depth(volume, len(volume.levels) - 1) * max(1, int(volume.frames))
    return planes > 1 and planes * info.height * info.width > voxels


def extension_read_bytes(source: NgffVolumeSource) -> int:
    """What deriving the coarse levels reads: the coarsest stored level, every
    plane of every channel and frame."""
    volume = source.volume
    height, width = (int(s) for s in source._arrays[source._base_levels - 1].shape[-2:])
    return (_stack_planes(volume) * max(1, int(volume.channels)) * height * width
            * np.dtype(volume.dtype).itemsize)


def build_volume_extension(source: NgffVolumeSource, dest,
                           target: int = ome_zarr.EXTENSION_TARGET,
                           progress_callback=None, cancel=None) -> Optional[str]:
    """Derive the coarse levels a volumetric store lacks, for every plane.

    `ome_zarr.build_extension`'s rule (2x2 mean, edge-replicated, named by
    absolute level index, level 0 never copied), applied per (t, c, z) plane
    into arrays shaped (T, C, Z, h, w), down to `extension_shapes`. Returns
    the path, or None when the store's own levels already reach far enough
    (or `cancel` was set). Written beside `dest` and renamed into place, so a
    reader never opens a half-written store.
    """
    volume = source.volume
    base = source._base_levels
    shapes = extension_shapes(source, target)
    if not shapes:
        return None
    destination = Path(dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    building = destination.with_name(destination.name + ".building")
    if building.exists():
        shutil.rmtree(building)
    dtype = np.dtype(volume.dtype)
    group = zarr.open_group(str(building), mode="w")

    planes = [(t, z) for t in range(volume.frames) for z in range(volume.depth)]
    total = len(shapes) * len(planes)
    done = 0
    previous = None  # the array just written, (T, C, Z, h, w)
    for index, (out_h, out_w) in enumerate(shapes, start=base):
        array = group.create_array(
            str(index), shape=(volume.frames, volume.channels, volume.depth, out_h, out_w),
            dtype=dtype, chunks=(1, 1, 1, min(1024, out_h), min(1024, out_w)))
        for t, z in planes:
            if cancel is not None and cancel.is_set():
                shutil.rmtree(building, ignore_errors=True)
                return None
            for channel in range(volume.channels):
                if previous is None:
                    level = source.level_array(t, z, base - 1)
                    block = np.asarray(level[channel])
                else:
                    block = np.asarray(previous[t, channel, z])
                array[t, channel, z] = ome_zarr._cast_like(ome_zarr._reduce2(block), dtype)
            done += 1
            if progress_callback is not None:
                progress_callback(done, total)
        previous = array
    group.update_attributes({
        "plexora_extension": True,
        "plexora_volume_extension": True,
        "base_levels": base,
        "source": source.path,
        "source_key": ome_zarr._source_key(source.path) or "",
    })
    if destination.exists():
        shutil.rmtree(destination)
    # Moved past Windows' brief refusals while a scanner reads the new chunks.
    past_transient_locks(lambda: os.replace(building, destination))
    return str(destination)
