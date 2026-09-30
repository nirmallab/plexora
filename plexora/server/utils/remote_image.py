"""What kind of image a web address holds, decided once, by name first.

Every ladder that opens an image -- the provider, registration, the import
wizard, the thumbnailer -- used to ask "is this zarr?" and then treat the
answer "no" as "this is a TIFF file on disk". For a web address that second
step is wrong twice over: nothing can be tested for existence with `Path()`,
and each format has its own reader for streamed bytes. So a URL is sorted here
into one of four kinds, and each ladder branches on the kind before it builds
a single path:

* ``zarr`` -- an OME-Zarr store, read by `ome_zarr` exactly as before;
* ``dicom`` -- a DICOM slide: one `.dcm` or a folder of instances, read by
  `dicom_wsi` through `remote_store.RemoteFile`;
* ``tiff`` -- an OME-TIFF, TIFF or TIFF-based slide (`.svs`, `.ndpi`, ...),
  read by `tiff_region` without tifffile's zarr view (that view reads the
  file on zarr's IO loop, where a streamed read cannot run);
* ``picture`` -- a PNG or JPEG, read whole.

The suffix answers without a request. Only a name that says nothing -- a
folder, a store named without `.zarr` -- is probed: the zarr metadata first,
then a listing for DICOM instances.
"""

from __future__ import annotations

from pathlib import Path

ZARR = "zarr"
DICOM = "dicom"
TIFF = "tiff"
PICTURE = "picture"

TIFF_SUFFIXES = (".tif", ".tiff", ".qptiff", ".svs", ".ndpi", ".scn", ".bif")
PICTURE_SUFFIXES = (".png", ".jpg", ".jpeg")
DICOM_SUFFIXES = (".dcm", ".dicom")


def suffix_of(path) -> str:
    """The lower-cased suffix of a path or a web address, query excluded."""
    from plexora.server.providers.base import is_remote_locator

    if is_remote_locator(path):
        from plexora.server.utils import remote_store

        name = remote_store.url_name(path)
        dot = name.rfind(".")
        return name[dot:].lower() if dot > 0 else ""
    return Path(path).suffix.lower()


def kind_of(url, *, probe: bool = True) -> str:
    """ZARR / DICOM / TIFF / PICTURE for a web address, or ValueError.

    `probe=False` answers from the name alone and raises when the name does
    not say -- what a caller on a keystroke path wants.
    """
    from plexora.server.utils import brightfield, remote_store

    url = remote_store.canonical_url(url)
    name = remote_store.url_name(url)
    lowered = name.lower()
    text = url.lower().split("?", 1)[0]
    if text.endswith(".zarr") or ".zarr/" in text:
        return ZARR
    suffix = suffix_of(url)
    if suffix in DICOM_SUFFIXES or name.upper() == "DICOMDIR":
        return DICOM
    if lowered.endswith(brightfield.OPENSLIDE_ONLY_SUFFIXES):
        raise ValueError(
            f"{name} needs a local copy: {suffix} slides are opened by OpenSlide, "
            "which reads files by path and cannot stream from a web address. "
            "Download it, or open it through a data node next to the data.")
    if suffix in TIFF_SUFFIXES:
        return TIFF
    if suffix in PICTURE_SUFFIXES:
        return PICTURE
    if not probe:
        raise ValueError(f"The name {name} does not say what kind of image it is.")

    from plexora.server.utils import dicom_wsi, ome_zarr

    if ome_zarr.is_zarr_image_path(url):
        return ZARR
    if dicom_wsi.is_dicom_path(url):
        return DICOM
    raise ValueError(
        f"Nothing at {url} answers as an OME-Zarr store, a folder of DICOM "
        "instances, or a TIFF, PNG or JPEG file. A DICOM folder can only be "
        "found when its host lists folders (s3://, gs://, az:// do); on an "
        "https:// gateway, paste the address of one .dcm file instead.")


def kind_or_none(url, *, probe: bool = True):
    """`kind_of`, with None for "cannot tell" instead of an exception."""
    try:
        return kind_of(url, probe=probe)
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "DICOM",
    "DICOM_SUFFIXES",
    "PICTURE",
    "PICTURE_SUFFIXES",
    "TIFF",
    "TIFF_SUFFIXES",
    "ZARR",
    "kind_of",
    "kind_or_none",
    "suffix_of",
]
