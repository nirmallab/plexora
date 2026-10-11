"""A DICOM slide's focal planes as a z-stack.

`dicom_wsi.open_image` reads the middle focal plane and records how many the
slide has. This serves every one of them: plane z is the same levels read at
the z-th focal plane (`dicom_wsi.at_focal_plane`), on the one open handle, so
moving through z opens no file and re-reads no header. The default plane is
the reader's own pyramid object -- the 2-D path, byte for byte.

The z step is the distance between focal planes, which DICOM states (in µm,
`ZOffsetInSlideCoordinateSystem` or `SpacingBetweenSlices`): it is recorded as
read from the file when the planes are evenly spaced, and as missing when they
are not -- a stack whose planes are 1, 2 and 5 µm apart has no single step,
and pretending it had one would put a section's rows at the wrong depths.

A brightfield (colour) slide's focal planes are a stack too (`Volume.color`):
each plane is the RGB picture the scanner recorded at that depth, served
whole and shown one at a time. Projections and sections are refused for it
(viewspec): a maximum of colour samples is not a picture anybody stained.
"""

from __future__ import annotations

import numpy as np

from plexora.server.volume.axes import MISSING, AxisSpacing, Spacing
from plexora.server.volume.model import Volume, VolumeSource, level_infos


class DicomVolumeSource(VolumeSource):
    def __init__(self, pyramid, volume: Volume):
        super().__init__(volume)
        self._pyramid = pyramid
        self._focal = tuple(getattr(pyramid, "focal_planes", ()) or ())

    def _build_plane(self, t: int, z: int):
        from plexora.server.utils import dicom_wsi

        return dicom_wsi.at_focal_plane(self._pyramid, self._focal[z])

    # The 2-D branch's own overview and metadata, so the default plane's
    # project opens exactly as it did before volumes (see
    # LocalImageProvider._open_volume).
    def overview(self, channels):
        from plexora.server.utils import dicom_wsi

        return dicom_wsi.overview_plane(channels)

    def physical_metadata(self) -> dict:
        from plexora.server.utils import dicom_wsi

        out = dict(dicom_wsi.physical_metadata(self._pyramid))
        step = self.volume.spacing.z
        if step.value and step.source == "metadata":
            out["physical_size_z"] = step.value
            out["physical_size_z_unit"] = step.unit
        return out

    def close(self) -> None:
        # Plane copies share the default pyramid's handle; it closes once.
        with self._lock:
            self._planes.clear()
        self._pyramid.close()


def _z_spacing(planes) -> tuple[AxisSpacing, dict]:
    if len(planes) < 2:
        return MISSING, {}
    steps = np.diff(np.asarray(planes, dtype=np.float64))
    step = float(np.median(steps))
    if step > 0 and np.allclose(steps, step, rtol=0.01, atol=1e-6):
        return (AxisSpacing(value=round(step, 6), unit="µm", source="metadata"),
                {"z_spacing_from": "DICOM focal planes"})
    return MISSING, {"z_spacing_from": None,
                     "focal_planes_um": [round(float(p), 4) for p in planes],
                     "note": "focal planes are not evenly spaced"}


def describe_volume(pyramid, *, color: bool | None = None) -> Volume | None:
    """The `Volume` an opened DICOM pyramid's focal planes make, or None for a
    slide with one focal plane. Opens nothing.

    `color` is how the project reads a colour slide: as one RGB picture per
    plane (brightfield, the default for a colour slide) or as three planes of
    samples (a "fluorescence" override, which may be projected like any
    other channels). A monochrome slide is never colour."""
    from plexora.server.utils import dicom_wsi

    planes = tuple(getattr(pyramid, "focal_planes", ()) or ())
    if len(planes) < 2:
        return None
    level0 = pyramid["0"]
    mpp = pyramid.mpp
    xy = (AxisSpacing(value=float(mpp[0]), unit="µm", source="metadata")
          if mpp and mpp[0] else MISSING)
    yy = (AxisSpacing(value=float(mpp[1] or mpp[0]), unit="µm", source="metadata")
          if mpp and mpp[0] else MISSING)
    z, provenance = _z_spacing(planes)
    volume = Volume(
        channels=int(level0.shape[0]), depth=len(planes), frames=1,
        height=int(level0.shape[1]), width=int(level0.shape[2]),
        dtype=str(np.dtype(level0.dtype)), spacing=Spacing(x=xy, y=yy, z=z),
        levels=level_infos(pyramid.level_shapes, (dicom_wsi.TILE_SIZE, dicom_wsi.TILE_SIZE)),
        source_kind="dicom", z_semantics="depth", t_semantics="time",
        channel_names=tuple(pyramid.channel_names) if pyramid.channel_names else None,
        provenance=provenance, stored_axes="czyx",
        color=bool(pyramid.is_color) and (color is None or bool(color)))
    if planes[volume.default_view.z] != pyramid.focal_plane:
        # The volume's default plane must be the one the 2-D reader shows.
        raise ValueError("DICOM default focal plane disagrees with the volume's")
    return volume


def open_dicom_volume(path, *, extension=None, rgb: bool = False):
    """A `DicomVolumeSource`, or None for a slide with one focal plane or one
    this reader cannot open."""
    from plexora.server.utils import dicom_wsi

    pyramid = dicom_wsi.open_image(path, extension=extension)
    try:
        volume = describe_volume(pyramid, color=rgb)
    except Exception:
        pyramid.close()
        raise
    if volume is None:
        pyramid.close()
        return None
    return DicomVolumeSource(pyramid, volume)
