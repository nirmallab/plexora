"""The volume: the canonical image object, of which every 2-D picture is a view.

Plexora's whole tile path, every reader and every plugin speak one contract --
a pyramid whose levels are indexed `[channel, rows, cols]`. A `VolumeSource`
does not change that contract; it PRODUCES it, once per view:

- `plane_pyramid(t, z)` is the reader's own `(C, Y, X)` pyramid for one plane --
  for a planar image exactly the object that was served before this module
  existed, which is what keeps the 2-D hot path byte-identical;
- `plane_view.plane_pyramid(source, spec)` wraps a slab projection or a section
  in the same shape, so `read_tile`, the quantization scan, the encoders,
  `SourceImage` and the node API need no change to serve them.

A source holds file handles and is closed with `close()`. Sources are built by
`plexora.server.volume.sources.open_volume`; nothing here opens a file.
"""

from __future__ import annotations

import os

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from plexora.server.volume.axes import Axes, Spacing
from plexora.server.volume.viewspec import ViewSpec

#: What a volume's z axis MEANS. `depth` is a focal/optical/physical stack;
#: `cycle` is the Akoya/CODEX convention of storing imaging cycles on Z, which
#: Plexora folds into the channel axis (the reading every such project has
#: always had); `unknown` is a stack nobody has described, read as `cycle`
#: until someone says otherwise.
Z_SEMANTICS = ("depth", "cycle", "unknown")
#: The same question for T: a real time series, or cycles stored on T (an
#: ImageJ hyperstack of a CODEX run is TCYX).
T_SEMANTICS = ("time", "cycle", "unknown")

#: How many plane pyramids a source keeps built. Building one is cheap (a
#: handful of page lookups), but scrubbing through a stack asks for the same
#: few planes over and over, and a built pyramid also holds its zarr store.
#: A source keeps up to one per plane of a deep stack, bounded by
#: `PLEXORA_PLANE_CACHE` (default 512) -- 64 was thrashed by any scrub through
#: a stack deeper than that.
PLANE_CACHE = 64
try:
    PLANE_CACHE_MAX = max(PLANE_CACHE, int(os.environ.get("PLEXORA_PLANE_CACHE", "") or 512))
except ValueError:
    PLANE_CACHE_MAX = 512


@dataclass(frozen=True)
class LevelInfo:
    """One resolution level of the XY pyramid (z is never halved here)."""

    index: int
    height: int
    width: int
    chunks: tuple[int, int] | None = None
    #: Planes the level stores, when fewer than the stack's (an Imaris file
    #: pools z in its coarse levels): its plane z is then a pooled plane, so
    #: neighbouring planes read back alike. None: every plane.
    depth: int | None = None

    def to_dict(self) -> dict:
        out = {"level": self.index, "height": self.height, "width": self.width}
        if self.chunks:
            out["chunks"] = list(self.chunks)
        if self.depth is not None:
            out["depth"] = self.depth
        return out


@dataclass(frozen=True)
class Volume:
    """What a volume is: its extent, its spacing and what its axes mean.

    `depth` and `frames` are the planes and time points that are SERVED as
    such; an axis folded into channels (`z_semantics == "cycle"`) contributes
    to `channels` instead and has depth 1 here.
    """

    channels: int
    depth: int
    frames: int
    height: int
    width: int
    dtype: str
    spacing: Spacing
    levels: tuple[LevelInfo, ...]
    source_kind: str
    z_semantics: str = "depth"
    t_semantics: str = "time"
    channel_names: tuple[str, ...] | None = None
    #: Where the numbers came from, for provenance and the import proposal:
    #: e.g. {"z_spacing_from": "OME PhysicalSizeZ", "planes_from": "OME SizeZ"}.
    provenance: Mapping[str, Any] = field(default_factory=dict)
    #: Axes this source stored natively before any folding, e.g. "tczyx".
    stored_axes: str = ""
    #: The XY translation of level 0, in spacing units, when the file states one.
    translation: Mapping[str, float] = field(default_factory=dict)
    #: A colour (brightfield) stack: each plane is one RGB picture, served
    #: whole. Its planes are shown one at a time -- a maximum or a mean of
    #: colour samples is not a picture anybody stained, and a section through
    #: three interleaved samples is not one either (viewspec refuses both).
    color: bool = False

    @property
    def is_planar(self) -> bool:
        return self.depth <= 1 and self.frames <= 1

    @property
    def axes(self) -> Axes:
        return Axes.of(self.depth, self.frames, self.channels)

    @property
    def default_view(self) -> ViewSpec:
        """The middle plane of the first frame -- the plane every reader
        that collapsed a stack has always shown (TIFF, DICOM)."""
        return ViewSpec(kind="plane", t=0, z=self.depth // 2)

    @property
    def shape(self) -> dict:
        return {"t": self.frames, "c": self.channels, "z": self.depth,
                "y": self.height, "x": self.width}

    def to_dict(self) -> dict:
        """The `/volume/<ds>/info` payload and the agent's `inspect_volume`."""
        spacing = self.spacing
        return {
            "volumetric": not self.is_planar,
            "axes": self.axes.spelled,
            "stored_axes": self.stored_axes,
            "shape": self.shape,
            "depth": self.depth,
            "frames": self.frames,
            "channels": self.channels,
            "height": self.height,
            "width": self.width,
            "dtype": self.dtype,
            "spacing": spacing.to_dict(),
            "anisotropy": spacing.anisotropy(),
            "anisotropy_source": spacing.anisotropy_source,
            "volumetric_ok": spacing.volumetric_ok(),
            "z_semantics": self.z_semantics,
            "t_semantics": self.t_semantics,
            "levels": [level.to_dict() for level in self.levels],
            "default_view": self.default_view.token(),
            "source_kind": self.source_kind,
            "provenance": dict(self.provenance),
            "color": self.color,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Volume":
        """`to_dict()` read back -- how a data node's volume reaches the primary.

        The primary never reads a node's planes; it needs the extent, the
        spacing and the levels to parse a view token, word a caption and size
        a section's tile source, which is everything this payload carries.
        """
        levels = tuple(
            LevelInfo(index=int(entry.get("level", index)), height=int(entry["height"]),
                      depth=int(entry["depth"]) if entry.get("depth") else None,
                      width=int(entry["width"]),
                      chunks=tuple(int(c) for c in entry["chunks"]) if entry.get("chunks")
                      else None)
            for index, entry in enumerate(raw.get("levels") or ()))
        return cls(
            channels=int(raw["channels"]), depth=int(raw.get("depth") or 1),
            frames=int(raw.get("frames") or 1), height=int(raw["height"]),
            width=int(raw["width"]), dtype=str(raw.get("dtype") or "uint16"),
            spacing=Spacing.from_dict(raw.get("spacing")), levels=levels,
            source_kind=str(raw.get("source_kind") or ""),
            z_semantics=str(raw.get("z_semantics") or "depth"),
            t_semantics=str(raw.get("t_semantics") or "time"),
            provenance=dict(raw.get("provenance") or {}),
            stored_axes=str(raw.get("stored_axes") or ""),
            color=bool(raw.get("color")))


class VolumeSource:
    """A readable volume. Subclasses implement `_build_plane`.

    The generic reads here go through plane pyramids, which is right for every
    format whose storage is plane-major (TIFF pages, DICOM focal planes, NGFF
    with z-chunks of 1). A format whose chunks span z (IMS, a z-chunked NGFF
    store) overrides `read_block` to take one read instead of Z.
    """

    volume: Volume

    def __init__(self, volume: Volume):
        self.volume = volume
        self._planes: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    # -- the one hook ----------------------------------------------------

    def _build_plane(self, t: int, z: int):
        """The `(C, Y, X)` pyramid of plane (t, z): a zarr group, an
        `NgffPyramid`, anything indexable by `str(level)`."""
        raise NotImplementedError

    # -- planes ----------------------------------------------------------

    def plane_pyramid(self, t: int = 0, z: int | None = None):
        if z is None:
            z = self.volume.default_view.z
        key = (int(t), int(z))
        with self._lock:
            cached = self._planes.get(key)
            if cached is not None:
                self._planes.move_to_end(key)
                return cached
        built = self._build_plane(*key)
        with self._lock:
            self._planes[key] = built
            self._planes.move_to_end(key)
            keep = min(PLANE_CACHE_MAX, max(PLANE_CACHE, self.volume.depth * self.volume.frames))
            while len(self._planes) > keep:
                self._planes.popitem(last=False)
        return built

    def level_array(self, t: int, z: int, level: int):
        """Level `level` of plane (t, z), indexed `[channel, rows, cols]`."""
        pyramid = self.plane_pyramid(t, z)
        if is_single_array(pyramid):
            return pyramid
        return pyramid[str(level)]

    def read_plane(self, level: int, t: int, z: int, channel: int,
                   rows: slice, cols: slice) -> np.ndarray:
        """One channel's rectangle of one plane, at one level."""
        return np.asarray(self.level_array(t, z, level)[channel, rows, cols])

    def read_block(self, level: int, t: int, channel: int, z0: int, z1: int,
                   rows: slice, cols: slice) -> np.ndarray:
        """`(Z, H, W)` for planes z0..z1 inclusive of one channel."""
        planes = [self.read_plane(level, t, z, channel, rows, cols)
                  for z in range(z0, z1 + 1)]
        return np.stack(planes) if planes else np.zeros((0, 0, 0))

    def level_shape(self, level: int) -> tuple[int, int]:
        info = self.volume.levels[min(level, len(self.volume.levels) - 1)]
        return info.height, info.width

    def close(self) -> None:
        with self._lock:
            planes = list(self._planes.values())
            self._planes.clear()
        for pyramid in planes:
            closer = getattr(pyramid, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


def reads_blocks(source, level: int) -> bool:
    """Whether a read across planes at `level` is best one block read (a store
    chunked across z, or the stack's z-chunked copy of that level) rather
    than a read per plane. Per level: a copy holds some levels and not others,
    and a block read of a level stored plane by plane is a read per plane cut
    into thin bands -- each TIFF tile strip decoded over and over."""
    at = getattr(source, "prefers_block_at", None)
    if callable(at):
        return bool(at(level))
    return bool(getattr(source, "prefers_block", False))


def block_depth(source, level: int) -> int:
    """Planes one block read of `level` should span (whole chunks of them)."""
    at = getattr(source, "z_chunk_at", None)
    value = at(level) if callable(at) else getattr(source, "z_chunk", 1)
    return max(1, int(value or 1))


def is_single_array(obj) -> bool:
    """Whether `obj` is one array rather than a pyramid of levels -- the same
    test `data_model._zarr_level` makes, extended to numpy for in-memory
    volumes."""
    if isinstance(obj, np.ndarray):
        return True
    try:
        import zarr
    except Exception:  # pragma: no cover
        return False
    return isinstance(obj, zarr.Array)


def level_infos(level_shapes: Sequence[Sequence[int]],
                chunks: Sequence[int] | None = None,
                depths: Sequence[int] | None = None) -> tuple[LevelInfo, ...]:
    """`depths`: each level's planes, recorded only where fewer than level 0's."""
    full = int(depths[0]) if depths else None
    return tuple(LevelInfo(index=i, height=int(h), width=int(w),
                           chunks=tuple(int(c) for c in chunks[-2:]) if chunks else None,
                           depth=(int(depths[i]) if depths and int(depths[i]) < full
                                  else None))
                 for i, (h, w) in enumerate(level_shapes))
