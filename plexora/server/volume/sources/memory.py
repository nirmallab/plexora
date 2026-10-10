"""A notebook's array as a volume: a z-stack or time series held in memory.

`plexora.view(array, axes="czyx")` and `register_memory_datasource` snapshot an
array onto the kernel's own data node (plexora/memory.py). A (C, Y, X) array
is served as it always was; an array with z or t is wrapped here. Nothing is
copied: the source keeps the caller's array, transposed to (T, C, Z, Y, X) as
a view, and a plane's pyramid is built only when that plane is asked for --
its level 0 is the plane itself, its coarser levels the halving
`memory_pyramid.image_pyramid` derives for any in-memory image.

An array carries no header, so its axes are stated by whoever passes it.
Without `axes`, a 4-D array is read as (C, Z, Y, X) and a 5-D one as
(T, C, Z, Y, X) -- the OME order -- and a 3-D one stays a channel stack, the
reading it had before volumes existed.
"""

from __future__ import annotations

import numpy as np

from plexora.server.volume.axes import MISSING, AxisSpacing, Spacing
from plexora.server.volume.model import Volume, VolumeSource, level_infos

CANONICAL = "tczyx"
DEFAULT_AXES = {2: "yx", 3: "cyx", 4: "czyx", 5: "tczyx"}


def normalize_axes(axes, ndim: int) -> str:
    """`axes` lower-cased and checked against the array, or the default."""
    if axes is None:
        if ndim not in DEFAULT_AXES:
            raise ValueError(f"an image array is 2- to 5-D; this one is {ndim}-D")
        return DEFAULT_AXES[ndim]
    spelled = str(axes).lower()
    if len(spelled) != ndim:
        raise ValueError(f"axes {axes!r} names {len(spelled)} axes; the array has {ndim}")
    if len(set(spelled)) != len(spelled) or set(spelled) - set(CANONICAL):
        raise ValueError(f"axes are some of t, c, z, y, x, each once; not {axes!r}")
    if not spelled.endswith("yx"):
        raise ValueError(f"the last two axes must be y and x; {axes!r} ends otherwise")
    return spelled


def is_volume_axes(axes: str) -> bool:
    return "z" in axes or "t" in axes


def as_tczyx(array, axes: str):
    """A (T, C, Z, Y, X) view of `array` (no copy)."""
    view = np.asarray(array)
    for name in CANONICAL:
        if name not in axes:
            view = view[np.newaxis, ...]
            axes = name + axes
    order = [axes.index(name) for name in CANONICAL]
    return np.transpose(view, order)


class MemoryVolumeSource(VolumeSource):
    def __init__(self, array, volume: Volume):
        super().__init__(volume)
        self._array = array            # (T, C, Z, Y, X), the caller's memory

    def _build_plane(self, t: int, z: int):
        from plexora.server.utils import memory_pyramid

        return memory_pyramid.image_pyramid(self._array[t, :, z])

    def read_plane(self, level, t, z, channel, rows, cols):
        if level == 0:
            # Level 0 is the array itself; no pyramid needed to read it.
            return np.asarray(self._array[t, channel, z, rows, cols])
        return super().read_plane(level, t, z, channel, rows, cols)

    def read_block(self, level, t, channel, z0, z1, rows, cols):
        if level == 0:
            return np.asarray(self._array[t, channel, z0:z1 + 1, rows, cols])
        return super().read_block(level, t, channel, z0, z1, rows, cols)


def _step(value, unit="µm") -> AxisSpacing:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return MISSING
    return AxisSpacing(value=number, unit=unit, source="metadata") if number > 0 else MISSING


def volume_from_array(array, axes=None, *, pixel_size=None, z_step=None,
                      frame_interval=None, channel_names=None):
    """A `MemoryVolumeSource` over `array`, or None when it has no z or t."""
    spelled = normalize_axes(axes, np.ndim(array))
    if not is_volume_axes(spelled):
        return None
    data = as_tczyx(array, spelled)
    frames, channels, depth, height, width = (int(side) for side in data.shape)
    from plexora.server.utils import memory_pyramid

    plane = memory_pyramid.image_pyramid(data[0, :, depth // 2])
    shapes = [list(plane[key].shape[-2:]) for key in sorted(list(plane), key=int)]
    xy = _step(pixel_size)
    t = _step(frame_interval, "s") if frames > 1 else None
    names = tuple(str(name) for name in channel_names) if channel_names else None
    volume = Volume(
        channels=channels, depth=depth, frames=frames, height=height, width=width,
        dtype=str(data.dtype), spacing=Spacing(x=xy, y=xy, z=_step(z_step), t=t),
        levels=level_infos(shapes, (1024, 1024)), source_kind="memory",
        z_semantics="depth", t_semantics="time",
        channel_names=names if names and len(names) == channels else None,
        provenance={"axes_from": "stated" if axes else "default"},
        stored_axes=spelled)
    source = MemoryVolumeSource(data, volume)
    # The default plane's pyramid, already built: the snapshot serves it.
    source._planes[(0, depth // 2)] = plane
    return source
