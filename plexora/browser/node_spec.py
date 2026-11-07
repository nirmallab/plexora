"""What the website's browser node may serve for a project, and how.

The page reads a project's tiles itself (services/browserNode.js: the picked
file in workers of the page, P6's three-times-faster path) only where it is
sure to read what Plexora's Python reads: the same file, the same levels, the
same channels, the same tile grid. This says what those are for a loaded
project, or why the page must leave its tiles to Python. The page then opens
the file and checks every level's shape against these before it answers a
single tile.

Served today: a plain (OME-)TIFF the user picked, read as a channel series,
with 8- or 16-bit samples; and a label mask the user picked that Python serves
as it is (a tiled label pyramid), at the levels the file holds. Everything
else -- a z-stack or time series (its planes go through the volume layer),
OME-Zarr, DICOM, a brightfield RGB slide, a Xenium focus folder, a pyramid
Plexora built into its own store, a mask level Python draws from a finer one,
a file on a node -- is Python's until its own check exists.
"""

from __future__ import annotations

import os

_TIFF = (".tif", ".tiff")


def describe(name: str) -> dict:
    """For project `name`, loading it if need be: the image as `{served,
    reason?, path, levels: [[c, h, w], ...], dtype, channels: [name, ...]}`,
    with `tile: [w, h]` (the grid of every tile route), `generation` and
    `labels` (the mask, `_labels`) beside it."""
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
           "labels": _labels(data_model, config)}

    def refuse(reason):
        out["reason"] = reason
        return out

    if not isinstance(provider, LocalImageProvider):
        return refuse("the image is not a local file")
    path = getattr(provider, "_path", None) or ""
    out["path"] = path
    if not path.startswith(PAGE_ROOTS):
        return refuse("the file is not one the page holds")
    if getattr(provider, "volume_source", None) is not None or data_model.volume_source is not None:
        return refuse("a z-stack or time series is served through the volume layer")
    if project.image.kind == IMAGE_TYPE_BRIGHTFIELD:
        return refuse("a brightfield slide's colour tiles are Python's")
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
    return out


def _labels(data_model, config) -> dict:
    """The mask as `{served, reason?, pending?, path, levels: [[level, h, w],
    ...], dtype}`: the levels the file holds, which `label_region` reads as they
    are (a level it draws from a finer one stays Python's)."""
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
    if not path.startswith(PAGE_ROOTS):
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
    return out


def describe_json(name: str) -> str:
    """`describe`, as the worker hands it to the page."""
    import json

    return json.dumps(describe(name))
