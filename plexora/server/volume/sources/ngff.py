"""An OME-Zarr image as a volume.

NGFF names its axes and their types (`space`, `time`, `channel`), so unlike a
TIFF there is no cycles-on-Z ambiguity to resolve: a `z` axis is depth and a
`t` axis is time. Each plane is the store's own arrays seen through
`ome_zarr._LevelView` pinned to (t, z) -- the same object `open_image` hands
the tile path for a planar store, with a different pin.

A store missing coarse levels gets them derived once (`build_volume_extension`)
for EVERY plane, into arrays shaped (T, C, Z, h, w): the 2-D extension
`ome_zarr.build_extension` writes is one plane's, and a z-stack served from
it would show plane 0 at every zoom-out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import zarr

from plexora.server.utils import ome_zarr
from plexora.server.volume.axes import spacing_from
from plexora.server.volume.model import Volume, VolumeSource, level_infos

VOLUME_EXTENSION_NAME = "image_pyramid_volume.zarr"


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

    def _build_plane(self, t: int, z: int):
        levels = [ome_zarr._make_level(array, self._axes, t=t, z=z)
                  for array in self._arrays[:self._base_levels]]
        if self._extension is not None:
            index = self._base_levels
            while str(index) in self._extension:
                levels.append(ome_zarr._LevelView(
                    self._extension[str(index)], ome_zarr.VOLUME_EXTENSION_AXES, t=t, z=z))
                index += 1
        return ome_zarr.NgffPyramid(levels, multiscale=self._multiscale, axes=self._axes,
                                    path=self.path, base_levels=self._base_levels)

    def read_block(self, level, t, channel, z0, z1, rows, cols):
        if level >= self._base_levels or "z" not in self._axes:
            return super().read_block(level, t, channel, z0, z1, rows, cols)
        array = self._arrays[level]
        index = []
        for name in self._axes[:-2]:
            if name == "t":
                index.append(int(t))
            elif name == "c":
                index.append(int(channel))
            elif name == "z":
                index.append(slice(int(z0), int(z1) + 1))
            else:
                index.append(0)
        return np.asarray(array[tuple(index) + (rows, cols)])

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
    shapes = level_shapes[:base_levels]
    if extension and Path(extension).exists():
        group = zarr.open_group(str(extension), mode="r")
        if dict(group.attrs).get("plexora_volume_extension"):
            derived = group
            index = base_levels
            while str(index) in group:
                shapes.append([int(group[str(index)].shape[-2]),
                               int(group[str(index)].shape[-1])])
                index += 1
    physical = ome_zarr.physical_metadata(_Multiscale(multiscale))
    spacing = spacing_from(physical, depth=depth, frames=frames)
    chunks = tuple(arrays[0].chunks)
    names_meta = ome_zarr.channel_labels(path, shape.get("c", 1))
    volume = Volume(
        channels=shape.get("c", 1), depth=depth, frames=frames,
        height=shapes[0][0], width=shapes[0][1], dtype=str(np.dtype(arrays[0].dtype)),
        spacing=spacing, levels=level_infos(shapes, chunks[-2:]), source_kind="ome_zarr",
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


def needs_volume_extension(source: NgffVolumeSource,
                           threshold: int = ome_zarr.EXTENSION_THRESHOLD) -> bool:
    info = source.volume.levels[-1]
    return max(info.height, info.width) > threshold


def build_volume_extension(source: NgffVolumeSource, dest,
                           target: int = ome_zarr.EXTENSION_TARGET,
                           progress_callback=None) -> Optional[str]:
    """Derive the coarse levels a volumetric store lacks, for every plane.

    `ome_zarr.build_extension`'s rule (2x2 mean, edge-replicated, named by
    absolute level index, level 0 never copied), applied per (t, c, z) plane
    into arrays shaped (T, C, Z, h, w). Returns the path, or None when the
    store's own levels already reach `target`.
    """
    volume = source.volume
    base = source._base_levels
    height, width = (int(s) for s in source._arrays[base - 1].shape[-2:])
    if max(height, width) <= target:
        return None
    destination = Path(dest)
    if destination.exists():
        import shutil
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    dtype = np.dtype(volume.dtype)
    group = zarr.open_group(str(destination), mode="w")

    total = 0
    probe_h, probe_w = height, width
    while max(probe_h, probe_w) > target:
        probe_h, probe_w = -(-probe_h // 2), -(-probe_w // 2)
        total += 1
    planes = [(t, z) for t in range(volume.frames) for z in range(volume.depth)]
    done = 0
    previous = None  # the array just written, (T, C, Z, h, w)
    index = base
    while max(height, width) > target:
        out_h, out_w = -(-height // 2), -(-width // 2)
        array = group.create_array(
            str(index), shape=(volume.frames, volume.channels, volume.depth, out_h, out_w),
            dtype=dtype, chunks=(1, 1, 1, min(1024, out_h), min(1024, out_w)))
        for t, z in planes:
            for channel in range(volume.channels):
                if previous is None:
                    level = source.level_array(t, z, base - 1)
                    block = np.asarray(level[channel])
                else:
                    block = np.asarray(previous[t, channel, z])
                array[t, channel, z] = ome_zarr._cast_like(ome_zarr._reduce2(block), dtype)
        previous = array
        height, width = out_h, out_w
        index += 1
        done += 1
        if progress_callback is not None:
            progress_callback(done, total)
    group.update_attributes({
        "plexora_extension": True,
        "plexora_volume_extension": True,
        "base_levels": base,
        "source": source.path,
        "source_key": ome_zarr._source_key(source.path) or "",
    })
    return str(destination)
