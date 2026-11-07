"""What the website's browser node may serve for a project, and how.

The page reads a project's image tiles itself (services/browserNode.js: the
picked file in workers of the page, P6's three-times-faster path) only where
it is sure to read what Plexora's Python reads: the same file, the same levels,
the same channels, the same tile grid. This says what those are for a loaded
project, or why the page must leave its tiles to Python. The page then opens
the file and checks every level's shape against these before it answers a
single tile.

Served today: a plain (OME-)TIFF the user picked, read as a channel series,
with 8- or 16-bit samples. Everything else -- a z-stack or time series (its
planes go through the volume layer), OME-Zarr, DICOM, a brightfield RGB slide,
a Xenium focus folder, a pyramid Plexora built into its own store, a file on a
node -- is Python's until its own check exists.
"""

from __future__ import annotations

import os


def describe(name: str) -> dict:
    """`{served, reason?, path, levels: [[c, h, w], ...], dtype, tile: [w, h],
    channels: [name, ...], generation}` for project `name`, loading it if need
    be."""
    from plexora.server.models import data_model
    from plexora.server.models.project import IMAGE_TYPE_BRIGHTFIELD, Project
    from plexora.server.providers.local import LocalImageProvider

    data_model.ensure_loaded(name)
    project = Project.load(name)
    provider = getattr(data_model._providers, "image", None)
    out = {"served": False, "name": name}

    def refuse(reason):
        out["reason"] = reason
        return out

    if not isinstance(provider, LocalImageProvider):
        return refuse("the image is not a local file")
    path = getattr(provider, "_path", None) or ""
    out["path"] = path
    if not path.startswith("/picked/"):
        return refuse("the file is not one the page holds")
    if getattr(provider, "volume_source", None) is not None or data_model.volume_source is not None:
        return refuse("a z-stack or time series is served through the volume layer")
    if project.image.kind == IMAGE_TYPE_BRIGHTFIELD:
        return refuse("a brightfield slide's colour tiles are Python's")
    if not path.lower().endswith((".tif", ".tiff")) or os.path.isdir(path):
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
    config = data_model.config[name]
    # The grid `generate_zarr_png` cuts tiles on, and the load the answers
    # belong to: a reload (a new generation) is a new spec for the page.
    out.update(served=True, levels=levels, dtype=dtype,
               tile=[int(config["tileWidth"]), int(config["tileHeight"])],
               channels=[c.get("fullname") or c.get("name") for c in config["imageData"]],
               generation=data_model.load_generation)
    return out


def describe_json(name: str) -> str:
    """`describe`, as the worker hands it to the page."""
    import json

    return json.dumps(describe(name))
