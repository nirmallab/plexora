"""Reading OME-Zarr (NGFF) images, and topping up the ones that arrive without
enough pyramid to zoom out of.

Plexora never converts an OME-TIFF at import: `convertOmeTiff` wraps the file in
tifffile's lazy `.aszarr()` view and the tile route slices a *virtual* 1024x1024
grid out of whatever chunking the file happens to have. An OME-Zarr store is
already that shape, so it is served the same way -- `zarr.open_group`, index the
level, hand the numbers to the encoder -- with a cheaper decode than TIFF's.
`LocalSegmentationProvider.open()` has served `.zarr` masks like this all along;
this module is the same idea for the channel image, plus the NGFF metadata the
mask path never needed.

Two things NGFF makes harder than a pyramidal TIFF, and both are handled here
rather than at the call sites:

**Level naming is not positional.** `multiscales[0].datasets[i].path` is the
authoritative mapping from level index to array name. Assuming "0".."n" works
for most writers and silently mis-scales the ones that use "s0"/"s1" or skip a
number, which draws the wrong resolution at the wrong zoom with no error.

**A store need not be a pyramid at all.** A single-level 40000x40000 image is a
perfectly legal OME-Zarr, and serving it means decoding the full-resolution
plane for every zoomed-out tile. `build_extension` derives the missing coarse
levels once at import and stores them beside the project -- only the levels that
are missing, never a second copy of level 0, which would double the store's disk
for tiles that already decode fast.

What the rest of the codebase sees is `NgffPyramid`, shaped like the zarr
*group* tifffile hands back for a pyramidal TIFF: `group[str(level)]` gives a
level, `len(group)` gives the level count, and each level indexes as
`[channel, rows, cols]`. That shape is load-bearing in three places --
`data_model._zarr_level`, the `isinstance(pyramid, zarr.Array)` branches in
`read_tile`/`quantization_window_of`, and `node/api.py`'s `hasattr(pyramid,
"shape")` test for a single-level array -- so `NgffPyramid` deliberately is not
a `zarr.Array` and deliberately has no `.shape`.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import zarr

from plexora.server.utils import segmentation_pyramid

#: Name of the derived store `build_extension` writes into a project directory.
EXTENSION_NAME = "image_pyramid.zarr"

#: A store whose coarsest level is still bigger than this on its long side gets
#: an extension pyramid. Two 1024 tiles across: below it, a fully zoomed-out
#: view is a handful of tiles and decoding them from the coarsest level is
#: already cheap.
EXTENSION_THRESHOLD = 2048

#: How small `build_extension` keeps going until -- the long side of the last
#: derived level. One tile.
EXTENSION_TARGET = 1024

#: Chunking of the derived levels: the virtual tile grid the viewer requests, so
#: one tile request is one chunk read.
_EXTENSION_CHUNKS = (1, 1024, 1024)

#: Bounded slab for the downsample pass, so deriving a level from a 50000-row
#: plane does not need the plane in RAM.
_DOWNSAMPLE_SLAB_BYTES = 256 * 1024 * 1024

# NGFF axis units, mapped onto the four the viewer's scale bar understands
# (imageViewer.js's pixelsPerMeter conversion). Anything else means no scale
# bar rather than a wrong one.
_UNIT_ALIASES = {
    "micrometer": "µm", "micron": "µm", "um": "µm", "µm": "µm",
    "nanometer": "nm", "nm": "nm",
    "centimeter": "cm", "cm": "cm",
    "meter": "m", "m": "m",
}

# Files that mark a directory as a zarr node, v2 and v3. Checked by name
# because a resolved element path ("store.zarr/images/morphology") does not end
# in ".zarr" and would otherwise not look like zarr at all.
_ZARR_MARKERS = ("zarr.json", ".zgroup", ".zarray", ".zattrs")


# -- shape ---------------------------------------------------------------


class _LevelView:
    """One pyramid level presented as (channel, y, x).

    NGFF stores axes in canonical t,c,z,y,x order but omits the ones an image
    does not have, so a level's real array is anywhere from 2-D to 5-D. Every
    consumer in Plexora indexes a level as `[channel, rows, cols]`; this maps
    that onto the real array, pinning t and z to 0 (the same plane the TIFF path
    takes from `series[0]`).

    Only built when the mapping is not already the identity -- a plain (c,y,x)
    array is handed through as itself, so the hot tile path keeps zero Python
    overhead on the common case.
    """

    __slots__ = ("_array", "_leading", "shape", "ndim", "dtype", "chunks")

    def __init__(self, array, axes: Sequence[str]):
        self._array = array
        # Everything before (y, x): some subset of (t, c, z), in that order.
        self._leading = tuple(axes[:-2])
        channels = int(array.shape[axes.index("c")]) if "c" in axes else 1
        self.shape = (channels, int(array.shape[-2]), int(array.shape[-1]))
        self.ndim = 3
        self.dtype = array.dtype
        chunks = tuple(getattr(array, "chunks", ()) or (1,) * array.ndim)
        self.chunks = (1, int(chunks[-2]), int(chunks[-1]))

    def _index(self, channel, rows, cols):
        prefix = tuple(channel if name == "c" else 0 for name in self._leading)
        return prefix + (rows, cols)

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)
        channel = index[0]
        if not isinstance(channel, (int, np.integer)):
            raise TypeError(
                "an OME-Zarr level is indexed as [channel, rows, cols] with an "
                f"integer channel, not {channel!r}")
        rows = index[1] if len(index) > 1 else slice(None)
        cols = index[2] if len(index) > 2 else slice(None)
        return self._array[self._index(int(channel), rows, cols)]

    def __array__(self, dtype=None, copy=None):
        stack = np.stack([np.asarray(self[c]) for c in range(self.shape[0])])
        return stack.astype(dtype) if dtype is not None else stack


def _make_level(array, axes: Sequence[str]):
    if tuple(axes) == ("c", "y", "x"):
        return array
    return _LevelView(array, axes)


class NgffPyramid:
    """An OME-Zarr image's resolution levels, shaped like the zarr group
    tifffile produces for a pyramidal TIFF.

    See the module docstring for why it is not a `zarr.Array` and has no
    `.shape`: both are how existing code tells a pyramid from a single plane.
    """

    def __init__(self, levels, *, multiscale: Optional[Mapping[str, Any]] = None,
                 axes: Sequence[str] = ("c", "y", "x"), path=None,
                 extension=None, base_levels: Optional[int] = None):
        self._levels = list(levels)
        self.multiscale = dict(multiscale or {})
        self.axes = tuple(axes)
        self.path = str(path) if path is not None else None
        self.extension = str(extension) if extension is not None else None
        #: How many of the levels came from the source store; the rest are
        #: derived. Only interesting to diagnostics and the tests.
        self.base_levels = len(self._levels) if base_levels is None else base_levels

    def __len__(self) -> int:
        return len(self._levels)

    def __iter__(self):
        return iter(str(index) for index in range(len(self._levels)))

    def __contains__(self, key) -> bool:
        try:
            index = int(key)
        except (TypeError, ValueError):
            return False
        return 0 <= index < len(self._levels)

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
        """[[height, width], ...] finest first -- every level's own dimensions,
        not width >> level. Real pyramids are not all exact halvings."""
        return [[int(level.shape[-2]), int(level.shape[-1])] for level in self._levels]


# -- store inspection ----------------------------------------------------


def _has_zarr_metadata(path: Path) -> bool:
    return any((path / marker).exists() for marker in _ZARR_MARKERS)


def is_zarr_image_path(path) -> bool:
    """Whether `path` should be read as zarr rather than handed to tifffile.

    A directory either named `*.zarr` or carrying zarr metadata. The second half
    matters for a resolved element path -- `store.zarr/images/morphology` is a
    zarr group whose name says nothing.
    """
    if not path:
        return False
    candidate = Path(path)
    try:
        if not candidate.is_dir():
            return False
    except OSError:
        return False
    if candidate.name.lower().endswith(".zarr"):
        return True
    return _has_zarr_metadata(candidate)


#: Group names that are store structure rather than anything a user named.
_STRUCTURAL_GROUPS = {"images", "labels"}


def suggest_name(path) -> Optional[str]:
    """A project name for a path that points *inside* a zarr store, or None.

    A plate's field of view is called "0", and so is the field next to it in
    the next well over -- named on its own the project says nothing and the
    second one collides with the first. What identifies it is the store it came
    out of and the well it sat in, so the name carries both:
    `screen.zarr/B/2/0` becomes "screen_B_2_0".

    None for anything that is not inside a store, the store root included: its
    own name is already the right answer, and quick view deliberately names a
    dropped store for what the user pointed at rather than for the element it
    resolved to.

    Only quick view uses this. The import wizard has a name field the user can
    see and type into, which is why `_derive_dataset_name_from_path` and its JS
    twin can stay the plain suffix-stripping pair they are.
    """
    if not path:
        return None
    node = Path(path)
    if node.name.lower().endswith(".zarr"):
        return None
    interior: list[str] = []
    while node.parent != node:
        interior.append(node.name)
        parent = node.parent
        if parent.name.lower().endswith(".zarr"):
            stem = re.sub(r"\.(ome\.)?zarr$", "", parent.name, flags=re.IGNORECASE)
            inside = [name for name in reversed(interior)
                      if name.lower() not in _STRUCTURAL_GROUPS]
            return "_".join([stem, *inside]) if inside else stem
        node = parent
    return None


def _open_group(path):
    return zarr.open_group(str(path), mode="r")


def _ome_attrs(node) -> dict:
    """A node's NGFF attributes, normalized across the two layouts.

    NGFF 0.4 and earlier put `multiscales`/`omero` at the top of `.zattrs`;
    0.5 nests them under an `"ome"` key in `zarr.json`. Every reader below wants
    the same dict either way.
    """
    try:
        attrs = dict(node.attrs)
    except Exception:
        return {}
    nested = attrs.get("ome")
    return dict(nested) if isinstance(nested, Mapping) else attrs


def _multiscale_of(node) -> Optional[dict]:
    multiscales = _ome_attrs(node).get("multiscales")
    if isinstance(multiscales, Sequence) and len(multiscales):
        first = multiscales[0]
        if isinstance(first, Mapping):
            return dict(first)
    return None


def _zarr_children(path: Path) -> list[str]:
    """Names of `path`'s child zarr nodes, read off the filesystem.

    Not `Group.group_keys()`, which needs the child's metadata to be in the same
    zarr format as the parent's and silently returns nothing when it is not.
    Enumerating a store is a directory listing, so it may as well be one.
    """
    try:
        entries = list(path.iterdir())
    except OSError:
        return []
    return sorted(entry.name for entry in entries
                  if entry.is_dir() and _has_zarr_metadata(entry))


def _is_multiscale(path: Path) -> bool:
    try:
        return _multiscale_of(_open_group(path)) is not None
    except Exception:
        return False


def _plate_fields(root: Path) -> list[str]:
    """Every field of view in an HCS plate store, as paths relative to `root`.

    A plate is a store of stores: `plate.wells[].path` names a well group
    ("B/2"), and that well's own attrs name its fields ("0".."3"). The images
    themselves are ordinary multiscale groups -- only the two index layers above
    them are plate-specific, which is why this resolves to a field path and
    nothing downstream needs to know a plate was involved.
    """
    plate = _ome_attrs(_open_group(root)).get("plate")
    if not isinstance(plate, Mapping):
        return []
    wells = plate.get("wells")
    if not isinstance(wells, Sequence) or isinstance(wells, (str, bytes)):
        return []
    fields: list[str] = []
    for well in wells:
        if not isinstance(well, Mapping):
            continue
        well_path = str(well.get("path", "")).strip("/")
        if not well_path or not (root / well_path).is_dir():
            continue
        images = _ome_attrs(_open_group(root / well_path)).get("well", {})
        images = images.get("images") if isinstance(images, Mapping) else None
        if isinstance(images, Sequence) and not isinstance(images, (str, bytes)):
            names = [str(image.get("path", "")) for image in images
                     if isinstance(image, Mapping) and image.get("path") is not None]
        else:
            names = _zarr_children(root / well_path)
        fields.extend(f"{well_path}/{name}" for name in names if name)
    return fields


def resolve_image_path(path):
    """The multiscale group inside `path`, when `path` is a store that holds one.

    Identity for anything that is not a zarr directory, and for a directory that
    already carries `multiscales` -- so registration can call this on every
    image path it is given without caring which kind it has.

    Three container layouts resolve, each only when the answer is unambiguous: a
    bioformats2raw store (numbered series under the root), a SpatialData store
    (elements under `images/`), and an HCS plate (fields under `<row>/<col>/`).
    More than one candidate raises, naming them, so the user can re-run against
    `<store>/images/<name>` -- the same shape of answer the import wizard
    already gives for a store with several tables.
    """
    original = Path(path)
    try:
        is_directory = original.is_dir()
    except OSError:
        is_directory = False
    if not is_directory:
        return original
    if not is_zarr_image_path(original):
        raise ValueError(
            f"{original.name} is a folder, not an image file. Plexora reads a "
            "folder as an image only when it is an OME-Zarr (.zarr) store.")

    if _is_multiscale(original):
        return original

    # HCS plate: a screen's worth of images, indexed by well and field. Checked
    # before the numbered-series branch because a plate is written *by*
    # bioformats2raw and carries its layout stamp too -- the rows are letters,
    # so the series branch would find nothing and report the wrong reason.
    fields = _plate_fields(original)
    if len(fields) == 1:
        return original / fields[0]
    if fields:
        wells = len({field.rsplit("/", 1)[0] for field in fields})
        raise ValueError(
            f"{original.name} is a high-content screening plate: {wells} wells, "
            f"{len(fields)} images. Plexora opens one field of view at a time -- "
            f"point it at one, e.g. {original.name}/{fields[0]}.")

    # bioformats2raw: the series are numbered groups at the root.
    series = sorted((name for name in _zarr_children(original) if name.isdigit()),
                    key=int)
    candidates = [name for name in series if _is_multiscale(original / name)]
    if candidates:
        if len(candidates) == 1:
            return original / candidates[0]
        raise ValueError(
            f"{original.name} holds {len(candidates)} images "
            f"({', '.join(candidates)}). Point Plexora at one of them, "
            f"e.g. {original.name}/{candidates[0]}.")

    # SpatialData: images live under `images/`, one group each.
    images = original / "images"
    if images.is_dir():
        elements = _zarr_children(images)
        if len(elements) == 1:
            return images / elements[0]
        if len(elements) > 1:
            raise ValueError(
                f"{original.name} holds {len(elements)} images "
                f"({', '.join(elements)}). Point Plexora at one of them, "
                f"e.g. {original.name}/images/{elements[0]}.")

    raise ValueError(
        f"{original.name} is a zarr store but has no OME-Zarr image in it "
        "(no `multiscales` metadata, no numbered series, no `images/` group).")


# -- reading -------------------------------------------------------------


def _axes_of(multiscale: Mapping[str, Any], ndim: int) -> tuple[str, ...]:
    """The axis names of a multiscale's arrays, lowercased, one per dimension.

    NGFF 0.4+ lists them as dicts, 0.3 as bare strings, and earlier versions not
    at all -- in which case the shape is the only evidence, and the canonical
    t,c,z,y,x ordering says which axes a given ndim must have.
    """
    raw = multiscale.get("axes")
    names: list[str] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for entry in raw:
            if isinstance(entry, Mapping):
                names.append(str(entry.get("name", "")).lower())
            else:
                names.append(str(entry).lower())
    if len(names) == ndim:
        return tuple(names)
    fallback = {2: ("y", "x"), 3: ("c", "y", "x"), 4: ("c", "z", "y", "x"),
                5: ("t", "c", "z", "y", "x")}
    if ndim in fallback:
        return fallback[ndim]
    raise ValueError(f"Cannot interpret a {ndim}-dimensional OME-Zarr image.")


def _dataset_paths(multiscale: Mapping[str, Any]) -> list[str]:
    datasets = multiscale.get("datasets")
    if not isinstance(datasets, Sequence) or not len(datasets):
        raise ValueError("OME-Zarr multiscales metadata lists no datasets.")
    paths = []
    for entry in datasets:
        if not isinstance(entry, Mapping) or not entry.get("path"):
            raise ValueError("OME-Zarr dataset entry has no `path`.")
        paths.append(str(entry["path"]))
    return paths


def open_image(path, extension=None) -> NgffPyramid:
    """The image at `path` as an `NgffPyramid`, finest level first.

    Only the levels that form a halving chain from level 0 are kept. The
    client's tile source computes a level's size as `size >> level`, so a store
    whose steps are something else -- a 4x pyramid, an arbitrary set of scales
    -- would draw the wrong rectangle at the wrong zoom, silently. Dropping
    those levels rather than serving them is what makes `len(pyramid)` mean "the
    levels that can actually be drawn", which is what `maxLevel` is recorded
    from; `build_extension` then derives proper ones to replace them.

    `extension` is a store written by `build_extension`; its levels are appended
    to the source's, keyed by absolute level index.
    """
    root = _open_group(path)
    multiscale = _multiscale_of(root)
    if multiscale is None:
        raise ValueError(
            f"{Path(path).name} has no OME-Zarr `multiscales` metadata. "
            "Point Plexora at the image group inside the store.")

    names = _dataset_paths(multiscale)
    arrays = [root[name] for name in names]
    axes = _axes_of(multiscale, arrays[0].ndim)
    levels = [_make_level(array, axes) for array in arrays]
    base_levels = dyadic_prefix([[int(level.shape[-2]), int(level.shape[-1])]
                                 for level in levels])
    levels = levels[:base_levels]

    if extension and Path(extension).exists():
        derived = _open_group(extension)
        index = base_levels
        while str(index) in derived:
            levels.append(derived[str(index)])
            index += 1

    return NgffPyramid(levels, multiscale=multiscale, axes=axes, path=path,
                       extension=extension, base_levels=base_levels)


def channel_labels(path, n_channels) -> Optional[list[str]]:
    """Channel names from the store's `omero` metadata, or None.

    Accepted only when there is one label per channel and none of them is blank
    -- the same rule `_channel_names_from_ome_xml` applies to OME-XML, and for
    the same reason: a partial list mislabels channels more convincingly than
    "Channel 3" does.
    """
    try:
        attrs = _ome_attrs(_open_group(path))
    except Exception:
        return None
    omero = attrs.get("omero")
    if not isinstance(omero, Mapping):
        return None
    channels = omero.get("channels")
    if not isinstance(channels, Sequence):
        return None
    labels = []
    for entry in channels:
        if not isinstance(entry, Mapping):
            return None
        labels.append(str(entry.get("label") or "").strip())
    if len(labels) != n_channels or not all(labels):
        return None
    return labels


def physical_metadata(pyramid) -> dict:
    """`{physical_size_x, physical_size_x_unit, ...}` for the viewer's scale bar.

    The same two keys `imageViewer.js` reads off `/get_ome_metadata` for an
    OME-TIFF, so the scale bar needs no knowledge of where the numbers came
    from. An empty dict when the store declares no units, which is the state the
    scale bar already hides itself for.
    """
    multiscale = getattr(pyramid, "multiscale", None) or {}
    axes = multiscale.get("axes")
    if not isinstance(axes, Sequence) or isinstance(axes, (str, bytes)):
        return {}
    try:
        names = [str(a.get("name", "")).lower() if isinstance(a, Mapping) else str(a).lower()
                 for a in axes]
        units = [str(a.get("unit", "")).lower() if isinstance(a, Mapping) else ""
                 for a in axes]
    except Exception:
        return {}

    datasets = multiscale.get("datasets") or []
    scale = [1.0] * len(names)
    for transforms in (multiscale.get("coordinateTransformations"),
                       (datasets[0].get("coordinateTransformations")
                        if datasets and isinstance(datasets[0], Mapping) else None)):
        if not isinstance(transforms, Sequence):
            continue
        for transform in transforms:
            if not isinstance(transform, Mapping) or transform.get("type") != "scale":
                continue
            values = transform.get("scale")
            if isinstance(values, Sequence) and len(values) == len(names):
                scale = [s * float(v) for s, v in zip(scale, values)]

    out = {}
    for axis, key in (("x", "physical_size_x"), ("y", "physical_size_y")):
        if axis not in names:
            continue
        position = names.index(axis)
        unit = _UNIT_ALIASES.get(units[position])
        if not unit or not scale[position]:
            continue
        out[key] = float(scale[position])
        out[f"{key}_unit"] = unit
    return out


# -- extension pyramids --------------------------------------------------


def dyadic_prefix(level_shapes: Sequence[Sequence[int]]) -> int:
    """How many leading levels form a halving chain.

    The client's tile source computes a level's size as `size >> level`, so a
    pyramid whose steps are not halvings draws the wrong rectangle at the wrong
    zoom. When a store's steps are something else (4x, or an arbitrary set of
    scales), only level 0 survives and everything coarser is derived -- which is
    what this counts.

    Tolerant by one pixel per step, because writers disagree about whether an
    odd dimension rounds up or down.
    """
    count = 1
    for index in range(1, len(level_shapes)):
        previous = level_shapes[index - 1]
        current = level_shapes[index]
        if all(abs(int(c) - -(-int(p) // 2)) <= 1 for p, c in zip(previous, current)):
            count += 1
        else:
            break
    return count


def needs_extension(pyramid, threshold: int = EXTENSION_THRESHOLD) -> bool:
    """Whether zooming out of this image would decode more than it should.

    True when the coarsest level is still large. `open_image` has already
    dropped anything past a break in the halving chain, so this is asking about
    the levels that can actually be drawn."""
    return max(pyramid.level_shapes[-1]) > threshold


def extension_path(data_directory) -> Path:
    return Path(data_directory) / EXTENSION_NAME


def _reduce2(block: np.ndarray) -> np.ndarray:
    """A 2x2 mean of `block`, edge-replicating an odd row or column.

    Replication rather than zero padding: a zero-padded final row averages real
    tissue against black and draws a dark seam along the bottom edge of every
    coarse level.
    """
    height, width = block.shape
    if height % 2:
        block = np.concatenate([block, block[-1:]], axis=0)
        height += 1
    if width % 2:
        block = np.concatenate([block, block[:, -1:]], axis=1)
        width += 1
    return block.reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3))


def _cast_like(values: np.ndarray, dtype) -> np.ndarray:
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        return np.clip(np.rint(values), info.min, info.max).astype(dtype)
    return values.astype(dtype)


def build_extension(pyramid, dest, target: int = EXTENSION_TARGET,
                    progress_callback=None) -> Optional[str]:
    """Derive the coarse levels `pyramid` is missing, into a zarr store at `dest`.

    Only the missing ones. Level 0 is never copied: it decodes as fast from the
    source as from a duplicate, and copying it would double the disk a store
    costs to open. The derived arrays are named by their *absolute* level index,
    so `open_image` can append them to the source's levels without renumbering
    anything -- `open_image` having already dropped whatever the source had past
    a break in the halving chain, which is why the numbering lines up.

    Returns the store path, or None when nothing needed deriving.
    """
    shapes = pyramid.level_shapes
    base_levels = len(shapes)
    if max(shapes[-1]) <= target:
        return None

    destination = Path(dest)
    if destination.exists():
        import shutil
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    source_level = pyramid[base_levels - 1]
    dtype = np.dtype(source_level.dtype)
    channels = int(source_level.shape[0])
    height, width = int(source_level.shape[-2]), int(source_level.shape[-1])

    total = 0
    probe_h, probe_w = height, width
    while max(probe_h, probe_w) > target:
        probe_h, probe_w = -(-probe_h // 2), -(-probe_w // 2)
        total += 1
    if total == 0:
        return None

    group = zarr.open_group(str(destination), mode="w")
    written = 0
    index = base_levels
    while max(height, width) > target:
        out_height, out_width = -(-height // 2), -(-width // 2)
        chunks = (1, min(_EXTENSION_CHUNKS[1], out_height),
                  min(_EXTENSION_CHUNKS[2], out_width))
        target_array = group.create_array(
            str(index), shape=(channels, out_height, out_width),
            dtype=dtype, chunks=chunks)
        rows = max(1, _DOWNSAMPLE_SLAB_BYTES // max(1, width * dtype.itemsize * 2))
        for channel in range(channels):
            for start in range(0, out_height, rows):
                stop = min(start + rows, out_height)
                block = np.asarray(source_level[channel, start * 2:min(stop * 2, height)])
                target_array[channel, start:stop, :] = _cast_like(_reduce2(block), dtype)
        written += 1
        if progress_callback is not None:
            progress_callback(written, total)
        source_level = target_array
        height, width = out_height, out_width
        index += 1

    group.attrs.update({
        "plexora_extension": True,
        "base_levels": base_levels,
        "source": str(getattr(pyramid, "path", "") or ""),
        "source_key": segmentation_pyramid.source_fingerprint(
            getattr(pyramid, "path", "") or destination) or "",
    })
    return str(destination)


def extension_source_key(extension) -> Optional[str]:
    """The source fingerprint an extension store was built against, or None."""
    try:
        attrs = dict(_open_group(extension).attrs)
    except Exception:
        return None
    return attrs.get("source_key") or None


def geometry(pyramid) -> dict:
    """Shape facts about an open pyramid, in `local.image_geometry`'s vocabulary."""
    level_shapes = pyramid.level_shapes
    finest = pyramid[0]
    return {
        "levels": len(pyramid),
        "num_channels": int(finest.shape[0]),
        "height": int(finest.shape[-2]),
        "width": int(finest.shape[-1]),
        "tile_height": _EXTENSION_CHUNKS[1],
        "tile_width": _EXTENSION_CHUNKS[2],
        "level_shapes": level_shapes,
    }


def overview_plane(pyramid, minimum: int = 200, maximum: int = 400):
    """A materialized coarse level, mean-pooled toward `minimum` px.

    The zarr counterpart of `LocalImageProvider.open`'s TIFF branch, and the
    same heuristic: the smallest level with both dimensions >= `minimum`, pooled
    down when it is still well above it. Bounded regardless of the image's full
    resolution, which is what makes materializing it safe.
    """
    from skimage.measure import block_reduce

    candidates = [index for index in range(len(pyramid))
                  if all(d >= minimum for d in pyramid[index].shape[-2:])]
    index = candidates[-1] if candidates else 0
    array = np.asarray(pyramid[index])
    if array.shape[-2] > maximum or array.shape[-1] > maximum:
        factor = int(min(array.shape[-2] // minimum, array.shape[-1] // minimum))
        if factor > 1:
            array = block_reduce(array, (1, factor, factor), np.mean)
    return array


__all__ = [
    "EXTENSION_NAME",
    "EXTENSION_TARGET",
    "EXTENSION_THRESHOLD",
    "NgffPyramid",
    "build_extension",
    "channel_labels",
    "dyadic_prefix",
    "extension_path",
    "extension_source_key",
    "geometry",
    "is_zarr_image_path",
    "needs_extension",
    "open_image",
    "overview_plane",
    "physical_metadata",
    "resolve_image_path",
]
