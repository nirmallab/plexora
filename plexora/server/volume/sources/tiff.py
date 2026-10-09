"""A TIFF's planes as a volume: OME-TIFF, ImageJ hyperstacks, Plexora's own copies.

A TIFF stores one 2-D page per (t, c, z) combination, in an order the file
states: OME-XML's `DimensionOrder` (`XYZCT` = z fastest, then c, then t), an
ImageJ hyperstack's fixed `TZCYX`, or -- for a pyramidized copy Plexora wrote
of a flat stack -- the `page_axes` recorded in the copy's own description.
`page_layout` turns that into `[(axis, size), ...]` slowest first, which is
all the indexing below needs: page index = row-major ravel of (t, c, z) in
that order.

**What gets served as an axis is a decision, not a fact.** An Akoya/CODEX
export stores imaging CYCLES on Z; a CODEX ImageJ hyperstack stores them on T.
Both are channels to the person who made them, and every existing Plexora
project reads them so (`tiff_series.channel_series` folds the leading axes
into the channel axis, in page order). So each of z and t is either SERVED
(`depth`/`time`) or FOLDED into channels (`cycle`), and folding keeps the
exact channel order today's reader produces: folded axes enumerate in page
order, slowest first.

`semantics(...)` makes the decision. With nothing recorded on the project it
reproduces today's behaviour exactly -- a single-channel Z-stack is a depth
stack (whose middle plane is what was always shown), anything else folds --
so opening an existing project under this module changes no channel list.
A new import records the choice (`imageZSemantics`) and is read by it.

A plane is served as the reader's own object: a `TiffPageSeries` over that
plane's pages at every pyramid level, chained like tifffile chains a
pyramidal series, opened through `aszarr()`. The tile path then takes exactly
the zarr branch it takes for a planar OME-TIFF.
"""

from __future__ import annotations

import itertools
import json
from typing import Any, Mapping
from xml.etree import ElementTree

import numpy as np
import tifffile as tf
import zarr

from plexora.server.volume.axes import spacing_from
from plexora.server.volume.model import Volume, VolumeSource, level_infos

#: How much of the first page's description is scanned for OME markers.
_OME_WINDOW = 4096

_IMAGEJ_ORDER = "TZC"  # slowest first, before YX


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def ome_pixels(tiff) -> dict | None:
    """What the file's OME-XML says about its planes, or None.

    `{SizeC, SizeZ, SizeT, DimensionOrder, physical_size_x/_y/_z (+ _unit),
    time_increment (+ _unit), channel_names, description}` -- read with
    ElementTree (a few microseconds) rather than ome_types, because it runs on
    every open.
    """
    try:
        xml = tiff.pages[0].tags["ImageDescription"].value
    except Exception:
        return None
    if not isinstance(xml, str):
        return None
    head = xml[:_OME_WINDOW]
    if "openmicroscopy" not in head and "<OME" not in head:
        return None
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    image = next((el for el in root.iter() if _local(el.tag) == "Image"), None)
    pixels = next((el for el in (image.iter() if image is not None else root.iter())
                   if _local(el.tag) == "Pixels"), None)
    if pixels is None:
        return None
    attrib = pixels.attrib
    out: dict[str, Any] = {}
    for key in ("SizeC", "SizeZ", "SizeT"):
        try:
            out[key] = int(attrib.get(key, 1))
        except (TypeError, ValueError):
            out[key] = 1
    out["DimensionOrder"] = str(attrib.get("DimensionOrder") or "XYZCT").upper()
    for axis in ("X", "Y", "Z"):
        raw = attrib.get(f"PhysicalSize{axis}")
        try:
            value = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            value = None
        if value and value > 0:
            out[f"physical_size_{axis.lower()}"] = value
            out[f"physical_size_{axis.lower()}_unit"] = attrib.get(
                f"PhysicalSize{axis}Unit") or "µm"
    raw = attrib.get("TimeIncrement")
    try:
        increment = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        increment = None
    if increment and increment > 0:
        out["time_increment"] = increment
        out["time_increment_unit"] = attrib.get("TimeIncrementUnit") or "s"
    names = []
    for element in pixels:
        if _local(element.tag) == "Channel":
            names.append(str(element.attrib.get("Name") or "").strip())
    out["channel_names"] = names
    if image is not None:
        for element in image:
            if _local(element.tag) == "Description" and element.text:
                out["description"] = element.text
    return out


def plexora_copy_info(pixels: Mapping | None) -> dict | None:
    """The JSON a pyramidized copy carries in its OME Image description."""
    if not pixels or not pixels.get("description"):
        return None
    try:
        info = json.loads(pixels["description"])
    except (TypeError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("plexora") != "image-pyramid":
        return None
    return info


def imagej_physical(tiff) -> dict:
    """x/y/z/t steps an ImageJ header states."""
    metadata = getattr(tiff, "imagej_metadata", None)
    if not isinstance(metadata, dict):
        return {}
    out: dict[str, Any] = {}
    unit = str(metadata.get("unit") or "").strip()
    if unit in ("\\u00B5m", "micron"):
        unit = "µm"
    try:
        spacing = float(metadata.get("spacing") or 0)
    except (TypeError, ValueError):
        spacing = 0.0
    if spacing > 0 and unit and unit.lower() not in ("pixel", "pixels"):
        out["physical_size_z"] = spacing
        out["physical_size_z_unit"] = unit
    try:
        page = tiff.pages[0]
        resolution = page.tags.get("XResolution")
        if resolution is not None and unit and unit.lower() not in ("pixel", "pixels"):
            num, den = resolution.value
            if num and den:
                size = float(den) / float(num)
                if size > 0:
                    out["physical_size_x"] = size
                    out["physical_size_y"] = size
                    out["physical_size_x_unit"] = unit
                    out["physical_size_y_unit"] = unit
    except Exception:
        pass
    try:
        interval = float(metadata.get("finterval") or 0)
    except (TypeError, ValueError):
        interval = 0.0
    if interval > 0:
        out["time_increment"] = interval
        out["time_increment_unit"] = "s"
    return out


def page_layout(tiff, series) -> tuple[list[tuple[str, int]], str, dict] | None:
    """`(page_axes, evidence, physical)` for a stack of 2-D pages, or None.

    `page_axes` is `[(axis, size), ...]` slowest first over t/c/z, its sizes
    multiplying to the page count. None for anything that is not a plain stack
    of grey pages (interleaved RGB, a series whose page count disagrees).
    """
    axes = str(getattr(series, "axes", "") or "")
    shape = tuple(int(side) for side in series.shape)
    if not axes.endswith("YX") or len(shape) < 2:
        return None
    pages = getattr(series, "pages", None)
    count = len(pages) if pages is not None else 0
    leading = axes[:-2]
    sizes = shape[:-2]
    pixels = ome_pixels(tiff)
    physical: dict[str, Any] = {}

    copy = plexora_copy_info(pixels)
    if copy and isinstance(copy.get("page_axes"), list):
        layout = [(str(name), int(size)) for name, size in copy["page_axes"]]
        if _product(layout) == count:
            physical = dict(copy.get("physical") or {})
            return layout, "plexora copy", physical

    if len(sizes) > 1:
        # A shaped series (ImageJ hyperstack, a tifffile-shaped stack): the
        # series' own axes ARE the page order.
        layout = [(_axis_name(letter), int(size)) for letter, size in zip(leading, sizes)]
        if _product(layout) == count:
            physical = imagej_physical(tiff)
            return layout, f"series axes {axes}", physical
        return None

    if pixels is not None:
        order = pixels.get("DimensionOrder") or "XYZCT"
        rest = [letter for letter in order if letter in "ZCT"]
        if len(rest) == 3:
            layout = [(letter.lower(), int(pixels.get(f"Size{letter}", 1)))
                      for letter in reversed(rest)]
            if _product(layout) == count:
                physical = {k: v for k, v in pixels.items()
                            if k.startswith("physical_size") or k.startswith("time_increment")}
                return layout, f"OME {order}", physical

    if len(sizes) == 1:
        imagej = getattr(tiff, "imagej_metadata", None)
        if isinstance(imagej, dict):
            channels = int(imagej.get("channels", 1) or 1)
            slices = int(imagej.get("slices", 1) or 1)
            frames = int(imagej.get("frames", 1) or 1)
            layout = [("t", frames), ("z", slices), ("c", channels)]
            if _product(layout) == count:
                return layout, "ImageJ header", imagej_physical(tiff)
        letter = leading[:1] or "C"
        return [(_axis_name(letter), count or int(sizes[0]))], f"series axes {axes}", {}
    return [("c", 1)], "single plane", {}


def _axis_name(letter: str) -> str:
    letter = letter.upper()
    if letter == "Z":
        return "z"
    if letter == "T":
        return "t"
    # C, and anything tifffile could not name (I, Q, ...): a channel-like axis,
    # folded the way it has always been read.
    return "c"


def _product(layout) -> int:
    total = 1
    for _, size in layout:
        total *= int(size)
    return total


def sizes_of(layout) -> dict:
    out = {"t": 1, "c": 1, "z": 1}
    for name, size in layout:
        out[name] = out.get(name, 1) * int(size)
    return out


def semantics(layout, physical: Mapping, *, z_choice: str | None = None,
              t_choice: str | None = None, new_import: bool = False,
              stated: bool = True) -> tuple[str, str]:
    """`(z_semantics, t_semantics)` for this file.

    A recorded choice wins. Otherwise, for a project that predates volumes
    (`new_import=False`), today's reading: only a single-channel, single-
    frame Z-stack is a depth stack. A new import also reads a multi-channel
    Z-stack as depth when the file states a z spacing (OME PhysicalSizeZ,
    ImageJ spacing) -- a stated physical step is what says the planes are
    depths rather than cycles -- and a T axis as time when the file states a
    time increment.

    `stated` is whether the file's OME-XML or ImageJ header itself gave the
    plane counts. Today's reader collapses a single-channel stack only then
    (`tiff_series.focal_planes`); a stack whose layout came from anything
    else keeps its flattened reading unless a choice was recorded.
    """
    sizes = sizes_of(layout)
    if z_choice in ("depth", "cycle"):
        z = z_choice
    elif sizes["z"] < 2:
        z = "depth"
    elif sizes["c"] == 1 and sizes["t"] == 1 and (stated or new_import):
        z = "depth"
    elif new_import and physical.get("physical_size_z"):
        z = "depth"
    else:
        z = "cycle"
    if t_choice in ("time", "cycle"):
        t = t_choice
    elif sizes["t"] < 2:
        t = "time"
    elif new_import and physical.get("time_increment"):
        t = "time"
    else:
        t = "cycle"
    return z, t


def served_extent(layout, z_semantics: str, t_semantics: str) -> tuple[int, int, int]:
    """(channels, depth, frames) once folded axes are counted as channels."""
    sizes = sizes_of(layout)
    depth = sizes["z"] if z_semantics == "depth" else 1
    frames = sizes["t"] if t_semantics == "time" else 1
    channels = _product(layout) // max(1, depth * frames)
    return channels, depth, frames


def page_indices(layout, served: Mapping[str, int]) -> list[int]:
    """Page index of every channel of the plane at `served` ({"t": .., "z": ..}).

    Folded axes enumerate in page order, which is the channel order the
    flattening reader has always produced.
    """
    strides = []
    stride = 1
    for _, size in reversed(layout):
        strides.append(stride)
        stride *= int(size)
    strides.reverse()
    base = 0
    folded = []
    for position, (name, size) in enumerate(layout):
        if name in served:
            base += int(served[name]) * strides[position]
        else:
            folded.append((position, int(size)))
    indices = []
    for combo in itertools.product(*[range(size) for _, size in folded]):
        indices.append(base + sum(c * strides[pos] for c, (pos, _) in zip(combo, folded)))
    return indices


class TiffVolumeSource(VolumeSource):
    source_kind = "ome_tiff"

    def __init__(self, path, tiff, series, layout, volume: Volume):
        super().__init__(volume)
        self.path = str(path)
        self._tiff = tiff
        self._series = series
        self._layout = layout
        self._served = {}
        if volume.depth > 1:
            self._served["z"] = True
        if volume.frames > 1:
            self._served["t"] = True

    def pages_for(self, t: int, z: int) -> list[int]:
        served = {}
        if "z" in self._served:
            served["z"] = z
        if "t" in self._served:
            served["t"] = t
        return page_indices(self._layout, served)

    def _build_plane(self, t: int, z: int):
        return zarr.open(plane_series(self._tiff, self._series, self.pages_for(t, z)).aszarr(),
                         mode="r")

    def close(self) -> None:
        super().close()
        try:
            self._tiff.close()
        except Exception:
            pass


def plane_series(tiff, series, indices):
    """Pages `indices` of `series` as a (C, Y, X) series, keeping every level
    that holds all of them -- `tiff_series.single_plane_series` for C pages."""
    levels = list(getattr(series, "levels", None) or [series])
    built = []
    for level in levels:
        pages = list(level.pages)
        if any(index >= len(pages) or pages[index] is None for index in indices):
            break
        chosen = [pages[index] for index in indices]
        plane = tuple(int(size) for size in chosen[0].shape[-2:])
        built.append(tf.TiffPageSeries(
            chosen, shape=(len(chosen),) + plane, dtype=series.dtype, axes="CYX",
            parent=tiff, name=series.name))
    if not built:
        raise ValueError("the requested plane is not in this file")
    base = built[0]
    base.levels = built
    return base


def describe(tiff, series, *, path=None, z_choice=None, t_choice=None,
             new_import=False) -> tuple[Volume, list] | None:
    """`(Volume, layout)` when this TIFF serves more than one plane, else None."""
    found = page_layout(tiff, series)
    if found is None:
        return None
    layout, evidence, physical = found
    from plexora.server.utils import tiff_series

    stated = tiff_series.plane_sizes(tiff) is not None or evidence == "plexora copy"
    z_sem, t_sem = semantics(layout, physical, z_choice=z_choice, t_choice=t_choice,
                             new_import=new_import, stated=stated)
    channels, depth, frames = served_extent(layout, z_sem, t_sem)
    if depth <= 1 and frames <= 1:
        return None
    levels = list(getattr(series, "levels", None) or [series])
    shapes = []
    for level in levels:
        pages = list(level.pages)
        if len(pages) != len(series.pages) or any(p is None for p in pages):
            break
        shapes.append(tuple(int(s) for s in pages[0].shape[-2:]))
    chunks = None
    try:
        page0 = series.pages[0]
        if getattr(page0, "is_tiled", False):
            chunks = (int(page0.tilelength), int(page0.tilewidth))
    except Exception:
        chunks = None
    spacing = spacing_from(physical, depth=depth, frames=frames)
    sizes = sizes_of(layout)
    provenance = {"planes_from": evidence,
                  "page_axes": [[name, size] for name, size in layout]}
    if spacing.z.source == "metadata":
        provenance["z_spacing_from"] = ("ImageJ spacing" if "ImageJ" in evidence
                                        or "series" in evidence else "OME PhysicalSizeZ")
    stored = "".join(name for name in ("t", "c", "z") if sizes[name] > 1) + "yx"
    volume = Volume(
        channels=channels, depth=depth, frames=frames,
        height=shapes[0][0], width=shapes[0][1], dtype=str(np.dtype(series.dtype)),
        spacing=spacing, levels=level_infos(shapes, chunks), source_kind="ome_tiff",
        z_semantics=z_sem if sizes["z"] > 1 else "depth",
        t_semantics=t_sem if sizes["t"] > 1 else "time",
        provenance=provenance, stored_axes=stored)
    return volume, layout


def open_tiff_volume(path, *, z_choice=None, t_choice=None, new_import=False):
    """A `TiffVolumeSource` for the file at `path`, or None when it is planar."""
    tiff = tf.TiffFile(str(path), is_ome=False)
    try:
        series = tiff.series[0]
        described = describe(tiff, series, path=path, z_choice=z_choice,
                              t_choice=t_choice, new_import=new_import)
    except Exception:
        tiff.close()
        raise
    if described is None:
        tiff.close()
        return None
    volume, layout = described
    return TiffVolumeSource(path, tiff, series, layout, volume)
