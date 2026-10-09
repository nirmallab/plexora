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
PLANE_CACHE = 64


@dataclass(frozen=True)
class LevelInfo:
    """One resolution level of the XY pyramid (z is never halved here)."""

    index: int
    height: int
    width: int
    chunks: tuple[int, int] | None = None

    def to_dict(self) -> dict:
        out = {"level": self.index, "height": self.height, "width": self.width}
        if self.chunks:
            out["chunks"] = list(self.chunks)
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
        }


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
            while len(self._planes) > PLANE_CACHE:
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
                chunks: Sequence[int] | None = None) -> tuple[LevelInfo, ...]:
    return tuple(LevelInfo(index=i, height=int(h), width=int(w),
                           chunks=tuple(int(c) for c in chunks[-2:]) if chunks else None)
                 for i, (h, w) in enumerate(level_shapes))
