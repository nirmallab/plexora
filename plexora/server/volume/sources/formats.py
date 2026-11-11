"""The z-stacks and time series of the files `server/utils/formats` reads.

Plane (t, z) is the same levels read at that plane on the one open file
(`FormatPyramid.plane`), so moving through z opens nothing; the default plane
is the 2-D reader's own pyramid -- the middle of the stack, the first frame.

Every plane has the default plane's levels. Where the file has its own
pyramid (an Olympus VSI, a CZI slide scan) or its planes are small enough that
every level is a bounded read of the plane (up to 4096 px a side, every
confocal and widefield camera frame), each plane reads them all from the file.
A larger stack written without a pyramid -- a stitched mosaic in z -- has its
default plane's coarse levels derived at import; any other plane resamples
them from its own level above as they are read (`base.build(derive=True)`), so
the first zoomed-out view of a plane reads that plane once at full
resolution. Until the import has derived the default plane's levels, the
stack is shown as that plane alone.

Float, signed and 32-bit planes (DeltaVision writes signed 16-bit; deconvolved
stacks are float) are drawn through one value map (`value_range`), chosen from
the default plane and applied to every plane, so contrast holds through z.
"""
from __future__ import annotations

import numpy as np

from plexora.server.volume.axes import MISSING, AxisSpacing, Spacing
from plexora.server.volume.model import Volume, VolumeSource, level_infos


class FormatVolumeSource(VolumeSource):
    def __init__(self, pyramid, volume: Volume):
        super().__init__(volume)
        self._pyramid = pyramid
        from plexora.server.utils import value_range

        self._mapped = None
        if value_range.needs_view(pyramid["0"].dtype):
            self._mapped, _overview = value_range.present(pyramid, None)

    def _build_plane(self, t: int, z: int):
        plane = self._pyramid.plane(z, t)
        if self._mapped is None:
            return plane
        if plane is self._pyramid:
            return self._mapped
        from plexora.server.utils import value_range

        return value_range.map_as(plane, self._mapped)

    def overview(self, channels):
        from plexora.server.utils import formats

        return formats.overview_plane(channels)

    def physical_metadata(self) -> dict:
        from plexora.server.utils import formats

        return formats.physical_metadata(self._pyramid)

    def close(self) -> None:
        with self._lock:
            self._planes.clear()
        self._pyramid.close()


def describe_volume(pyramid, *, color: bool | None = None) -> Volume | None:
    """The `Volume` an opened pyramid's planes make, or None for one plane or a
    stack whose planes are not all servable from the file (see above)."""
    from plexora.server.utils import brightfield

    depth, frames = int(pyramid.z_count), int(pyramid.t_count)
    if depth < 2 and frames < 2:
        return None
    if len(pyramid) < len(brightfield._dyadic_shapes(*pyramid.level_shapes[0])):
        # The default plane's coarse levels are not derived yet: every other
        # plane takes its levels' shapes from it.
        return None
    level0 = pyramid["0"]
    from plexora.server.utils import value_range

    # The planes as drawn: through the value map for float, signed and 32-bit.
    dtype = np.uint16 if value_range.needs_view(level0.dtype) else level0.dtype
    mpp = pyramid.mpp
    xy = (AxisSpacing(value=float(mpp[0]), unit="µm", source="metadata")
          if mpp and mpp[0] else MISSING)
    yy = (AxisSpacing(value=float(mpp[1] or mpp[0]), unit="µm", source="metadata")
          if mpp and mpp[0] else MISSING)
    z = (AxisSpacing(value=round(float(pyramid.z_step), 6), unit="µm", source="metadata")
         if pyramid.z_step else MISSING)
    volume = Volume(
        channels=int(level0.shape[0]), depth=depth, frames=frames,
        height=int(level0.shape[1]), width=int(level0.shape[2]),
        dtype=str(np.dtype(dtype)), spacing=Spacing(x=xy, y=yy, z=z),
        levels=level_infos(pyramid.level_shapes, (brightfield.TILE_SIZE, brightfield.TILE_SIZE)),
        source_kind="vendor", z_semantics="depth", t_semantics="time",
        channel_names=tuple(pyramid.channel_names) if pyramid.channel_names else None,
        provenance={"z_spacing_from": pyramid.format_name if pyramid.z_step else None},
        stored_axes="tczyx",
        color=bool(pyramid.is_color) and (color is None or bool(color)))
    default = volume.default_view
    if (default.z, default.t) != (pyramid.z, pyramid.t):
        raise ValueError("the volume's default plane disagrees with the 2-D reader's")
    return volume


def open_format_volume(path, *, extension=None, rgb: bool = False, index: int = 0):
    """A `FormatVolumeSource` of image `index` of the file, or None for one
    plane or a stack served as its middle plane."""
    from plexora.server.utils import formats

    pyramid = formats.open_image(path, extension=extension, index=index)
    try:
        volume = describe_volume(pyramid, color=rgb)
    except Exception:
        pyramid.close()
        raise
    if volume is None:
        pyramid.close()
        return None
    return FormatVolumeSource(pyramid, volume)
