"""What the website's browser node may serve for a project, and how.

The page reads a project's tiles itself (services/browserNode.js: the picked
file in workers of the page, P6's three-times-faster path) only where it is
sure to read what Plexora's Python reads: the same file, the same levels, the
same channels, the same tile grid. This says what those are for a loaded
project, or why the page must leave its tiles to Python. The page then opens
the file and checks every level's shape against these before it answers a
single tile.

Served today: a plain (OME-)TIFF the user picked, read as a channel series,
with 8- or 16-bit samples, or the pyramidized copy the page built of one; a
whole-slide image the page itself reads with openslide-js (`kind: "slide"`:
MRXS, NDPI, SVS, Sakura; Python reads the same slide through the page, so the
two agree byte for byte), at the tiles Python would not resample; and a label
mask the user picked that Python serves as it is (a tiled label pyramid), or
the label pyramid the page built of a flat one, at the levels the file holds.
Both copies are the page's files, under /derived/ (providers/browser.py
`page_pyramid`). Everything else -- a z-stack or time series (its planes go
through the volume layer), OME-Zarr, DICOM, any other brightfield RGB image, a
Xenium focus folder, a pyramid Python drew into the project store, a mask
level Python draws from a finer one, a file on a node -- is Python's until its
own check exists.

The desktop app asks the same question (`describe(name, local=True)`, from
`/resource_routing` when its "Browser rendering" setting is on: plexora/
browser_rendering.py). There the page reads the project's own files on this
computer through the shell's `plexora-file` scheme, so "a file the page holds"
becomes "a regular file on a local disk"; every other refusal is the website's.
"""

from __future__ import annotations

import os
import struct

_TIFF = (".tif", ".tiff")


def format_of(path: str) -> str:
    """A word for the source's format, from its name: what the page's readers
    are chosen by (the TIFF reader takes "ome-tiff" and "tiff")."""
    lowered = (path or "").lower().rstrip("/\\")
    if lowered.endswith((".ome.tif", ".ome.tiff")):
        return "ome-tiff"
    if lowered.endswith(_TIFF):
        return "tiff"
    if lowered.endswith(".zarr") or ".zarr/" in lowered.replace("\\", "/"):
        return "ome-zarr"
    ext = os.path.splitext(lowered)[1].lstrip(".")
    return ext or "unknown"


def is_local_file(path: str) -> bool:
    """A regular file on this computer's own disks: not a URL, not a node's
    address, not a network share (`\\\\server\\share`, `//server/share`), not a
    folder."""
    if not path or "://" in path or path.startswith(("\\\\", "//")):
        return False
    return os.path.isabs(path) and os.path.isfile(path)


#: path -> ((size, mtime_ns), offsets): `main_ifds`'s answers, until the file changes.
_IFDS: dict[str, tuple[tuple[int, int], list[int] | None]] = {}
#: More main IFDs than the page's reader opens (node/tiffReader.js).
_MAX_IFDS = 4096


def main_ifds(path: str) -> list[int] | None:
    """The offsets of a TIFF's main IFDs, in the order the file chains them,
    or None. The page's reader walks that chain one read after another -- forty
    reads through the desktop shell's scheme for a forty-channel OME-TIFF whose
    IFDs sit beside each plane's gigabytes -- and with these it reads them all
    at once (node/tiffReader.js `mainIfdsAt`, which takes them only when they
    ARE the chain it finds). Kept per file until its size or time changes."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    stamp = (stat.st_size, stat.st_mtime_ns)
    held = _IFDS.get(path)
    if held is not None and held[0] == stamp:
        return held[1]
    try:
        with open(path, "rb") as fh:
            offsets = _walk_ifds(fh)
    except (OSError, struct.error):  # only a hint: the page walks the chain itself
        offsets = None
    if len(_IFDS) >= 64:
        _IFDS.clear()
    _IFDS[path] = (stamp, offsets)
    return offsets


def _walk_ifds(fh) -> list[int] | None:
    """The main IFD chain of an open TIFF, as a reader walks it: the header's
    first offset, then each IFD's next, until 0. None for anything that is not
    a classic or Big TIFF, a chain that loops, or one longer than the page's
    reader opens."""
    head = fh.read(16)
    order = {b"II": "<", b"MM": ">"}.get(head[:2])
    if order is None or len(head) < 8:
        return None
    version = struct.unpack(order + "H", head[2:4])[0]
    if version == 42:
        at = struct.unpack(order + "I", head[4:8])[0]
        count, entry, offset = "H", 12, "I"
    elif version == 43 and len(head) == 16:
        at = struct.unpack(order + "Q", head[8:16])[0]
        count, entry, offset = "Q", 20, "Q"
    else:
        return None
    count_size = struct.calcsize(count)
    out = []
    seen = set()
    while at:
        if at in seen or len(out) >= _MAX_IFDS:
            return None
        seen.add(at)
        out.append(int(at))
        fh.seek(at)
        entries = struct.unpack(order + count, fh.read(count_size))[0]
        fh.seek(at + count_size + entries * entry)
        at = struct.unpack(order + offset, fh.read(struct.calcsize(offset)))[0]
    return out or None


def describe(name: str, local: bool = False) -> dict:
    """For project `name`, loading it if need be: the image as `{served,
    reason?, path, format, levels: [[c, h, w], ...], dtype, channels: [name,
    ...]}`, with `tile: [w, h]` (the grid of every tile route), `generation`
    and `labels` (the mask, `_labels`) beside it. `local`: the desktop app's
    question -- a file on this computer instead of one the website's page
    holds -- whose served image and mask also carry `ifds` (`main_ifds`)."""
    from plexora.server.models import data_model
    from plexora.server.models.project import IMAGE_TYPE_BRIGHTFIELD, Project
    from plexora.server.providers.browser import PAGE_ROOTS
    from plexora.server.providers.local import LocalImageProvider

    data_model.ensure_loaded(name)
    project = Project.load(name)
    config = data_model.config[name]
    provider = getattr(data_model._providers, "image", None)
    # The grid `generate_zarr_png` cuts tiles on, and the load the answers
    # belong to: a reload (a new generation) is a new spec for the page.
    out = {"served": False, "name": name, "generation": data_model.load_generation,
           "tile": [int(config["tileWidth"]), int(config["tileHeight"])],
           "labels": _labels(data_model, config, local)}

    def refuse(reason):
        out["reason"] = reason
        return out

    if not isinstance(provider, LocalImageProvider):
        return refuse("the image is not a local file")
    path = getattr(provider, "_path", None) or ""
    out["path"] = path
    out["format"] = format_of(path)
    if local:
        if not is_local_file(path):
            return refuse("not a file on this computer's own disks")
    elif not path.startswith(PAGE_ROOTS):
        return refuse("the file is not one the page holds")
    if getattr(provider, "volume_source", None) is not None or data_model.volume_source is not None:
        return refuse("a z-stack or time series is served through the volume layer")
    if project.image.kind == IMAGE_TYPE_BRIGHTFIELD:
        from plexora.server.utils import brightfield

        slide = brightfield.page_tile_layout(data_model.channels)
        if slide is None:
            return refuse("a brightfield slide's colour tiles are Python's")
        # A slide the page reads with openslide-js (MRXS, NDPI, SVS, Sakura):
        # the page draws a tile itself where `read_rgb` would not resample it.
        out.update(served=True, kind="slide", slide=slide, dtype="uint8",
                   levels=[[3, level["height"], level["width"]] for level in slide["levels"]],
                   channels=[brightfield.RGB_CHANNEL_KEY])
        return out
    if not path.lower().endswith(_TIFF) or os.path.isdir(path):
        return refuse("not a TIFF file")
    group = data_model.channels
    if not hasattr(group, "array_keys"):
        return refuse("one level only: Python serves the coarser ones")
    try:
        keys = sorted((k for k in group.array_keys() if str(k).isdigit()), key=int)
        levels = [list(group[k].shape) for k in keys]
        dtype = str(group[keys[0]].dtype)
    except Exception as exc:  # noqa: BLE001 -- not the shape this reads
        return refuse(f"the pyramid is not a level group ({exc})")
    if dtype not in ("uint8", "uint16") or any(len(shape) != 3 for shape in levels):
        return refuse(f"{dtype} samples in {len(levels[0])} dimensions")
    out.update(served=True, levels=levels, dtype=dtype,
               channels=[c.get("fullname") or c.get("name") for c in config["imageData"]])
    if local:
        out["ifds"] = main_ifds(path)
    return out


def _labels(data_model, config, local: bool = False) -> dict:
    """The mask as `{served, reason?, pending?, path, format, levels: [[level,
    h, w], ...], dtype}`: the levels the file holds, which `label_region` reads
    as they are (a level it draws from a finer one stays Python's). `local` as
    in `describe`."""
    from plexora.server.providers.local import LocalSegmentationProvider

    out = {"served": False}

    def refuse(reason):
        out["reason"] = reason
        return out

    provider = getattr(data_model._providers, "segmentation", None)
    if provider is None or not getattr(provider, "path", None):
        if config.get("segmentationSource") and config.get("segmentation_status") == "pending":
            # Being converted (or checked) by a job: the page asks again when
            # a mask tile is wanted, since the viewer takes the mask on
            # without a new page.
            out["pending"] = True
            return refuse("the mask is being prepared")
        return refuse("no mask")
    if not isinstance(provider, LocalSegmentationProvider):
        return refuse("the mask is not a local file")
    from plexora.server.providers.browser import PAGE_ROOTS

    path = provider.path
    out["path"] = path
    out["format"] = format_of(path)
    if local:
        if not is_local_file(path):
            return refuse("not a file on this computer's own disks")
    elif not path.startswith(PAGE_ROOTS):
        return refuse("the mask is not a file the page holds (Python drew its pyramid)")
    if not path.lower().endswith(_TIFF) or os.path.isdir(path):
        return refuse("not a TIFF file")
    if config.get("segmentationScale"):
        return refuse("a mask finer than the image")
    seg = data_model.seg
    if seg is None or hasattr(seg, "plane"):
        return refuse("a label volume's planes are Python's")
    if not hasattr(seg, "array_keys"):
        return refuse("one level only: Python draws the coarser ones")
    try:
        keys = sorted(int(k) for k in seg.array_keys() if str(k).isdigit())
        levels = [[k, *seg[str(k)].shape] for k in keys]
        dtype = str(seg[str(keys[0])].dtype)
    except Exception as exc:  # noqa: BLE001 -- not the shape this reads
        return refuse(f"the mask is not a level group ({exc})")
    if dtype not in ("uint8", "uint16", "uint32", "int32") or any(len(level) != 3 for level in levels):
        return refuse(f"{dtype} labels in {len(levels[0]) - 1} dimensions")
    out.update(served=True, levels=levels, dtype=dtype)
    if local:
        out["ifds"] = main_ifds(path)
    return out


def describe_json(name: str) -> str:
    """`describe`, as the worker hands it to the page."""
    import json

    return json.dumps(describe(name))
