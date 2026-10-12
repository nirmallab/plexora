"""What a preset needs to know about an image, without loading it into the viewer.

A preset turns facts -- the image's size, its planes and timepoints, its
spacing, its channels -- into tracks. In the browser those facts come from the
live viewer and are posted with the request (the viewer knows best what is on
screen). An agent creating a video from Python has no viewer to ask, so this
module reads them from the project record and the image's own volume
description (`api.project_data(...).image.volume()`), which reads the file and
never touches the datasource the viewer has loaded.
"""

from __future__ import annotations

import math

from plexora import api


def channel_key(channel) -> str:
    """A channel's stable id: the last segment of its tile URL, as Figure
    Builder keys channels -- it survives renaming."""
    src = (channel or {}).get("src") or ""
    return src.strip("/").rsplit("/", 1)[-1]


def fingerprint(datasource, *, handle=None, volume=None) -> dict:
    handle = handle or api.project_data(datasource)
    width, height = handle.image.size
    out = {
        "image_width": int(width or 0),
        "image_height": int(height or 0),
        "channel_keys": [channel_key(c) for c in handle.image.channels if channel_key(c)],
    }
    if volume:
        out["depth"] = int(volume.get("depth") or 1)
        out["frames"] = int(volume.get("frames") or 1)
    return out


def dataset_facts(datasource) -> dict:
    """The facts for `datasource`. Raises KeyError when there is no such
    project."""
    handle = api.project_data(datasource)
    width, height = handle.image.size
    try:
        volume = handle.image.volume()
    except Exception:  # noqa: BLE001 - a stack that will not describe itself is planar here
        volume = None
    spacing = (volume or {}).get("spacing") or {}
    pixel_size = None
    x = spacing.get("x") if isinstance(spacing, dict) else None
    if isinstance(x, dict) and x.get("value") and x.get("unit") in ("µm", "um", "micron"):
        pixel_size = float(x["value"])
    channels = [{"key": channel_key(c), "name": c.get("fullname") or c.get("name") or ""}
                for c in handle.image.channels if channel_key(c)]
    return {
        "datasource": datasource,
        "image_size": [int(width or 0), int(height or 0)],
        "depth": int((volume or {}).get("depth") or 1),
        "frames": int((volume or {}).get("frames") or 1),
        "volumetric": bool(volume) and int((volume or {}).get("depth") or 1) > 1,
        "spacing": {axis: spacing[axis] for axis in ("x", "y", "z", "t")
                    if isinstance(spacing, dict) and isinstance(spacing.get(axis), dict)},
        "pixel_size": pixel_size,
        "channels": channels,
        "fingerprint": fingerprint(datasource, handle=handle, volume=volume),
    }


def merge(server: dict | None, client: dict | None) -> dict:
    """Server facts, overridden field by field by what the live viewer said.

    The viewer is believed on what it shows (a remote image's depth as the
    viewer has it, the channels as named on screen); the server fills in what
    the viewer did not send."""
    out = dict(server or {})
    for key, value in (client or {}).items():
        if value not in (None, "", [], {}):
            out[key] = value
    out.setdefault("image_size", [0, 0])
    out.setdefault("depth", 1)
    out.setdefault("frames", 1)
    out.setdefault("channels", [])
    out.setdefault("spacing", {})
    out["volumetric"] = int(out.get("depth") or 1) > 1
    return out


def anisotropy(facts: dict) -> float:
    """z step over x step (1 when either is unknown) -- what the renderer uses
    to draw the box in physical proportion."""
    spacing = facts.get("spacing") or {}
    try:
        sx = float(spacing["x"]["value"])
        sz = float(spacing["z"]["value"])
    except (KeyError, TypeError, ValueError):
        return 1.0
    if not (sx > 0 and sz > 0 and math.isfinite(sx) and math.isfinite(sz)):
        return 1.0
    return sz / sx
