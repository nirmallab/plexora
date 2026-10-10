"""Opening an image file as a volume, when it is one.

`open_volume(path, ...)` takes the same dispatch order as
`LocalImageProvider.open` (a Xenium focus folder, OME-Zarr, DICOM, colour,
IMS, TIFF) and returns a `VolumeSource` for an image that serves more than one
plane or frame (a DICOM slide's focal planes included), or None for a planar
image -- which every existing reader keeps serving exactly as before. IMS is the exception: no other reader opens one,
so a planar .ims comes back as a one-plane source too.

`z_semantics`/`t_semantics` are the project's recorded choices (see
`sources/tiff.py` for why a TIFF's z axis is a decision); `new_import` applies
the rules for a file being registered now rather than one registered before
volumes existed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from plexora.server.volume.model import VolumeSource


def is_volume_only_path(path) -> bool:
    """Whether only the volume layer can open `path` (no 2-D reader does)."""
    from plexora.server.volume.sources import ims

    return ims.is_ims_path(path)


def open_volume(path, *, extension: Optional[str] = None, rgb: bool = False,
                z_semantics: Optional[str] = None, t_semantics: Optional[str] = None,
                new_import: bool = False) -> Optional[VolumeSource]:
    if not path:
        return None
    from plexora.server.providers.base import is_remote_locator
    from plexora.server.utils import brightfield, dicom_wsi, ome_zarr, xenium_focus
    from plexora.server.volume.sources import ims

    if is_remote_locator(path):
        if ome_zarr.is_zarr_image_path(path):
            from plexora.server.volume.sources.ngff import open_ngff_volume

            return open_ngff_volume(path, extension=extension)
        from plexora.server.utils import remote_image

        if rgb or remote_image.kind_of(path) != remote_image.TIFF:
            return None
        try:
            if brightfield.is_rgb_layout(path):
                return None
            from plexora.server.volume.sources.tiff import open_tiff_volume

            return open_tiff_volume(path, z_choice=z_semantics, t_choice=t_semantics,
                                    new_import=new_import)
        except Exception:
            return None
    if xenium_focus.is_focus_dir(path):
        return None
    if ome_zarr.is_zarr_image_path(path):
        from plexora.server.volume.sources.ngff import open_ngff_volume

        return open_ngff_volume(path, extension=extension)
    if dicom_wsi.is_dicom_path(path):
        if rgb:
            return None
        from plexora.server.volume.sources.dicom import open_dicom_volume

        try:
            return open_dicom_volume(path, extension=extension)
        except Exception:
            # The 2-D reader is the authority on whether the slide opens at
            # all; it is asked next and says why if it does not.
            return None
    if ims.is_ims_path(path):
        return ims.open_ims_volume(path)
    if rgb or not Path(str(path)).is_file():
        return None
    try:
        if brightfield.is_rgb_layout(path):
            return None
    except Exception:
        return None
    from plexora.server.volume.sources.tiff import open_tiff_volume

    try:
        return open_tiff_volume(path, z_choice=z_semantics, t_choice=t_semantics,
                                new_import=new_import)
    except Exception:
        # Not a TIFF this module understands. The 2-D reader is the authority
        # on whether the file opens at all, and it is asked next.
        return None


__all__ = ["open_volume", "is_volume_only_path"]
