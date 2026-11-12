"""Microscope and slide-scanner files that are not plain TIFF.

Zeiss CZI, ZVI and LSM, Nikon ND2, Leica LIF/LOF/XLEF, Olympus VSI (or one of
its ETS files alone), OIR and OIB/OIF, Hamamatsu NDPIS, DeltaVision, Hyperion MCD, KFBio KFB and 3DHISTECH
MRXS fluorescence slides, and Plexora's own smaller images (`.approx`): each read by a reader that knows
its format (one module per format here, most of them over a maintained
permissively licensed package), and every one of them presented as the same
pyramid (`base.FormatPyramid`), so the rest of Plexora asks the same questions
of all of them through the functions below -- one branch in each place that
opens an image, not one per format.

What the readers have in common is the contract in `base.Reader`; what they do
not is told in each module: whether the file has its own pyramid (VSI, CZI
slide scans) or the coarse levels are derived once at import (ND2, LIF, OIR,
DV), whether its samples are a colour picture (a brightfield slide scan), and
what counts as an image in a file holding several.

A file holding several images (a CZI's scenes, an ND2's positions, a LIF's
images, an MCD's acquisitions, a VSI's scan areas) is read one image at a
time: every function below that takes a path takes `index`, the image's
position in `describe(path).images` (0, the first, when not given). A project
records it as `ImageSpec.image_index` (`imageIndex`), and the import offers
each image as a sample of its own.
"""
from __future__ import annotations

import importlib
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from plexora.server.utils.formats.base import (  # noqa: F401 -- the package's vocabulary
    Description, FormatPyramid, FormatSupportMissing, FormatUnreadable, Reader)

#: suffix -> (module, what the format is called, the package its reader needs
#: or None when the reader here needs nothing beyond Plexora's own dependencies)
FORMATS = {
    ".vsi": ("vsi", "Olympus VSI", None),
    # One scan's pixels without its .vsi (an H&E scan sent alone).
    ".ets": ("ets", "Olympus ETS", None),
    ".ndpis": ("ndpis", "Hamamatsu NDPIS", None),
    ".dv": ("dv", "DeltaVision", None),
    ".r3d": ("dv", "DeltaVision", None),
    ".czi": ("czi", "Zeiss CZI", "czifile"),
    ".nd2": ("nd2file", "Nikon ND2", "nd2"),
    ".lif": ("lif", "Leica LIF", "liffile"),
    ".lof": ("lif", "Leica LOF", "liffile"),
    ".xlef": ("lif", "Leica XLEF", "liffile"),
    ".oir": ("oir", "Olympus OIR", "oirfile"),
    ".oib": ("oib", "Olympus OIB", "oiffile"),
    ".oif": ("oib", "Olympus OIF", "oiffile"),
    ".mcd": ("mcd", "Hyperion MCD", "readimc"),
    ".zvi": ("zvi", "Zeiss ZVI", None),
    ".lsm": ("lsm", "Zeiss LSM", None),
    ".kfb": ("kfb", "KFBio KFB", None),
    # Only a fluorescence slide (`mrxs.claims`): a brightfield MRXS stays
    # OpenSlide's, as it always was (brightfield.py).
    ".mrxs": ("mrxs", "3DHISTECH MRXS", None),
    # A smaller image a .plexora file brought (server/utils/approximate_image.py).
    ".approx": ("approx", "Plexora smaller image", None),
}

SUFFIXES = tuple(FORMATS)

#: Where a pyramid's derived coarse levels live in a project folder.
EXTENSION_NAME = "format_pyramid.zarr"

#: `image_kind` for one of these files read as channels -- a new kind for the
#: reason `dicom_wsi.IMAGE_KIND` is one: the path is not a TIFF. The browser
#: only ever compares `image_kind` with "brightfield" and "rgb", so it draws
#: these as it draws an OME-TIFF; a colour scan read as brightfield records
#: "brightfield".
IMAGE_KIND = "vendor"


def _suffix(path) -> str:
    return Path(str(path)).suffix.lower()


def handles(path) -> bool:
    """Whether `path` is a local file of a format this package reads. A format
    whose module has `claims(path)` shares its suffix with another reader (an
    MRXS: fluorescence here, brightfield OpenSlide's) and says which files
    are its own; the answer is kept per version of the file."""
    suffix = _suffix(path) if path else ""
    if suffix not in FORMATS:
        return False
    from plexora.server.providers.base import is_remote_locator

    if is_remote_locator(str(path)) or not Path(str(path)).is_file():
        return False
    module = importlib.import_module(f"plexora.server.utils.formats.{FORMATS[suffix][0]}")
    claims = getattr(module, "claims", None)
    return claims is None or _claimed(claims, path)


_CLAIMED: OrderedDict = OrderedDict()


def _claimed(claims, path) -> bool:
    try:
        key = _identity(path)
    except OSError:
        return False
    with _DESCRIBED_LOCK:
        if key in _CLAIMED:
            return _CLAIMED[key]
    try:
        answer = bool(claims(str(path)))
    except Exception:  # noqa: BLE001 -- a file it cannot read is not its own
        answer = False
    with _DESCRIBED_LOCK:
        _CLAIMED[key] = answer
        while len(_CLAIMED) > 256:
            _CLAIMED.popitem(last=False)
    return answer


def format_name(path) -> str:
    entry = FORMATS.get(_suffix(path))
    return entry[1] if entry else ""


def _index(index) -> int:
    try:
        return int(index or 0)
    except (TypeError, ValueError):
        raise FormatUnreadable(f"{index!r} is not an image number") from None


def _takes_index(make) -> bool:
    """Whether a module's `reader` takes an image's position after the path
    (a format whose files hold one image may not)."""
    import inspect

    try:
        parameters = list(inspect.signature(make).parameters.values())
    except (TypeError, ValueError):
        return False
    return len(parameters) >= 2 or any(
        parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters)


def open_reader(path, index=0) -> Reader:
    """The reader for `path`, opened at image `index` -- a position in what the
    file holds (`Reader.images`: a CZI's scenes, an ND2's positions, a LIF's
    images, an MCD's acquisitions, a VSI's scan areas). Raises
    `FormatSupportMissing` when the package its format needs is not installed,
    and `FormatUnreadable` for a position the file does not have."""
    module_name, name, package = FORMATS[_suffix(path)]
    index = _index(index)
    try:
        module = importlib.import_module(f"plexora.server.utils.formats.{module_name}")
        if not index:
            return module.reader(str(path))
        if not _takes_index(module.reader):
            raise FormatUnreadable(
                f"{Path(str(path)).name} holds one image; there is no image {index + 1}.")
        return module.reader(str(path), index)
    except ImportError as exc:
        if package and package.split("[")[0].replace("-", "_") in str(exc).replace("-", "_"):
            raise FormatSupportMissing(name, package) from exc
        raise


# -- descriptions, cached by the file's identity -----------------------------

_DESCRIBED: OrderedDict = OrderedDict()
_DESCRIBED_LOCK = threading.Lock()


def _identity(path):
    stat = os.stat(path)
    return (os.path.abspath(str(path)), stat.st_size, stat.st_mtime_ns)


def describe(path, index=0) -> Description:
    """What image `index` of the file is, read once per version of the file
    and image. `images` lists every image the file holds, whichever is asked."""
    index = _index(index)
    key = _identity(path) + (index,)
    with _DESCRIBED_LOCK:
        found = _DESCRIBED.get(key)
        if found is not None:
            _DESCRIBED.move_to_end(key)
            return found
    reader = open_reader(path, index)
    try:
        description = Description.of(reader)
    finally:
        reader.close()
    with _DESCRIBED_LOCK:
        _DESCRIBED[key] = description
        while len(_DESCRIBED) > 64:
            _DESCRIBED.popitem(last=False)
    return description


def image_names(path) -> list:
    """Every image the file holds, by name, in the order `index` counts them;
    one entry for a file of one image."""
    return list(describe(path).images)


# -- the pyramid and what is read off it -------------------------------------


def open_image(path, extension=None, rgb: bool = False, index=0) -> FormatPyramid:
    """Image `index` of the file as a pyramid at its default plane (the middle
    of a z-stack, the first time point). `rgb` is accepted for parity with the
    other readers: a file says itself whether its samples are a colour
    picture. `extension` is the derived coarse levels of the same image."""
    from plexora.server.utils.formats import base

    reader = open_reader(path, index)
    try:
        return base.build(reader, path, extension=extension)
    except Exception:
        reader.close()
        raise


def geometry(pyramid) -> dict:
    from plexora.server.utils import dicom_wsi

    return dicom_wsi.geometry(pyramid)


_GEOMETRIES: OrderedDict = OrderedDict()


def geometry_of(path, index=0, extension=None) -> dict:
    """`geometry` of image `index` of `path`. Without derived levels (what
    the import asks of every image of a file, again on every answer) it is
    read once per version of the file; with them it is the project's own
    levels, read each time."""
    import copy

    def read(derived):
        opened = open_image(path, extension=derived, index=index)
        try:
            return geometry(opened)
        finally:
            opened.close()

    if extension:
        return read(extension)
    key = (*_identity(path), _index(index))
    with _DESCRIBED_LOCK:
        found = _GEOMETRIES.get(key)
        if found is not None:
            _GEOMETRIES.move_to_end(key)
            return copy.deepcopy(found)
    found = read(None)
    with _DESCRIBED_LOCK:
        _GEOMETRIES[key] = found
        while len(_GEOMETRIES) > 256:
            _GEOMETRIES.popitem(last=False)
    return copy.deepcopy(found)


def overview_plane(pyramid, minimum: int = 200, maximum: int = 400):
    from plexora.server.utils import dicom_wsi

    return dicom_wsi.overview_plane(pyramid, minimum, maximum)


def _metadata_of(pixel_size, z_step=None) -> dict:
    if not pixel_size or not pixel_size[0]:
        return {}
    out = {"physical_size_x": float(pixel_size[0]), "physical_size_x_unit": "µm",
           "physical_size_y": float(pixel_size[1] or pixel_size[0]),
           "physical_size_y_unit": "µm"}
    if z_step:
        out["physical_size_z"] = float(z_step)
        out["physical_size_z_unit"] = "µm"
    return out


def physical_metadata(source, index=0) -> dict:
    """The scale bar's numbers (`{physical_size_x, physical_size_x_unit, ...}`)
    from an opened pyramid, or image `index` of a path; {} when the file
    states none."""
    if isinstance(source, FormatPyramid):
        return _metadata_of(source.mpp, source.z_step)
    description = describe(source, index)
    return _metadata_of(description.pixel_size, description.z_step)


def pixel_size(path, index=0) -> Optional[tuple]:
    return describe(path, index).pixel_size


def channel_names(path, index=0) -> list:
    return list(describe(path, index).channel_names)


def is_rgb(path, index=0) -> bool:
    return bool(describe(path, index).rgb)


def note(path, index=0) -> str:
    return describe(path, index).note


def detect_image_type(path, index=0):
    """A `brightfield.Detection`: the file says whether the samples of image
    `index` are one colour picture (a brightfield scan) or channels, so the
    answer is certain."""
    from plexora.server.utils import brightfield

    description = describe(path, index)
    if description.rgb:
        return brightfield.Detection(
            verdict=brightfield.BRIGHTFIELD, confidence="high",
            reason=f"the {description.format_name} file stores a colour (RGB) image")
    count = description.channels
    return brightfield.Detection(
        verdict=brightfield.FLUORESCENCE, confidence="high",
        reason=(f"the {description.format_name} file stores {count} "
                f"channel{'s' if count != 1 else ''}"))


def needs_extension(pyramid) -> bool:
    """Whether the levels the file can serve reach out to one tile (the
    question `dicom_wsi.needs_extension` asks, for the same reason)."""
    from plexora.server.utils import brightfield

    height, width = pyramid.level_shapes[0]
    return len(pyramid) < len(brightfield._dyadic_shapes(height, width))


def extension_path(data_directory) -> Path:
    return Path(data_directory) / EXTENSION_NAME


def build_extension(pyramid, dest, progress_callback=None) -> Optional[str]:
    """Derive the coarse levels `pyramid` cannot serve into a zarr store
    (`ome_zarr.build_extension`; the levels here are its input contract)."""
    from plexora.server.utils import ome_zarr

    return ome_zarr.build_extension(pyramid, dest, progress_callback=progress_callback)


__all__ = [
    "Description", "EXTENSION_NAME", "FORMATS", "IMAGE_KIND", "FormatPyramid", "FormatSupportMissing",
    "FormatUnreadable", "Reader", "SUFFIXES", "build_extension", "channel_names",
    "describe", "detect_image_type", "extension_path", "format_name", "geometry",
    "geometry_of",
    "handles", "image_names", "is_rgb", "needs_extension", "note", "open_image",
    "open_reader", "overview_plane", "physical_metadata", "pixel_size",
]
