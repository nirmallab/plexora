"""An Imaris (.ims) file as a volume, read with h5py -- no vendor SDK.

IMS 5.5 is plain HDF5:

    DataSet/ResolutionLevel L/TimePoint T/Channel C/Data      (Z, Y, X), chunked
        Channel C attrs: ImageSizeX/Y/Z -- the real extent; Data is padded to chunks
    DataSetInfo/Image attrs: X, Y, Z, ExtMin0..2, ExtMax0..2, Unit
    DataSetInfo/Channel C attrs: Name
    DataSetInfo/TimeInfo attrs: TimePoint1, TimePoint2, ... (timestamps)

Attributes are stored as arrays of one-byte strings and are decoded with
`_text`. Spacing is extent over size per axis, which is how Imaris defines it.

Imaris halves Z as well as XY at coarse levels once the voxels become
isotropic. The plane pyramid keeps every level whose XY halves, and reads
plane `z` of a level with fewer planes at `z * Z_L / Z_0` -- a pooled plane,
right for a zoomed-out picture, and the reason coarse tiles are display-only.

h5py is not thread-safe under Waitress's thread pool without its global lock
held, and its own lock does not stop two threads interleaving seeks on one
file handle: every read takes this source's lock. `h5py` is imported inside
`open_ims_volume` so a core install that never opens an .ims never loads it
(tests/test_plugin_boundary.py pins that).
"""

from __future__ import annotations

import datetime as _dt
import threading
from pathlib import Path

import numpy as np

from plexora.server.volume.axes import AxisSpacing, Spacing, normalize_unit
from plexora.server.volume.model import Volume, VolumeSource, level_infos
from plexora.server.volume.plane_view import DerivedPyramid

SUFFIXES = (".ims",)


def is_ims_path(path) -> bool:
    try:
        text = str(path)
    except Exception:
        return False
    return text.lower().endswith(SUFFIXES) and Path(text).is_file()


def _text(value) -> str:
    """An IMS attribute (an array of single bytes, usually) as a string."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    try:
        array = np.asarray(value)
        if array.dtype.kind in ("S", "O"):
            return b"".join(bytes(v) if not isinstance(v, bytes) else v
                            for v in array.ravel()).decode("utf-8", "replace")
        return str(array.item() if array.size == 1 else array.tolist())
    except Exception:
        return str(value)


def _number(value, default=None):
    try:
        return float(_text(value).strip())
    except (TypeError, ValueError):
        return default


class _ImsLevel:
    """One XY level of one plane: indexed `[channel, rows, cols]`."""

    ndim = 3

    def __init__(self, source, level: int, t: int, z: int):
        self._source = source
        self._level = level
        self._t = t
        info = source._levels[level]
        self._z = min(info["depth"] - 1, int(z * info["depth"] / max(1, source._depth0)))
        self.shape = (source.volume.channels, info["height"], info["width"])
        self.dtype = np.dtype(source.volume.dtype)
        self.chunks = (1,) + tuple(info["chunks"][-2:])

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)
        channel = int(index[0])
        rows = index[1] if len(index) > 1 else slice(None)
        cols = index[2] if len(index) > 2 else slice(None)
        rows = _clip(rows, self.shape[1])
        cols = _clip(cols, self.shape[2])
        return self._source._read(self._level, self._t, channel, self._z, rows, cols)

    def __array__(self, dtype=None, copy=None):
        stack = np.stack([np.asarray(self[c]) for c in range(self.shape[0])])
        return stack.astype(dtype) if dtype is not None else stack


def _clip(part, size):
    if isinstance(part, slice):
        start, stop, _ = part.indices(size)
        return slice(start, max(start, stop))
    return part


class ImsVolumeSource(VolumeSource):
    prefers_block = True

    def __init__(self, path, handle, levels, volume: Volume):
        super().__init__(volume)
        self.path = str(path)
        self._file = handle
        self._levels = levels
        self._depth0 = levels[0]["depth"]
        self._io = threading.Lock()
        #: Planes per stored chunk: a slab is read in whole chunks of them.
        self.z_chunk = int(levels[0]["chunks"][0]) if levels[0].get("chunks") else 1

    def _dataset(self, level, t, channel):
        return self._file[f"DataSet/ResolutionLevel {level}/TimePoint {t}/Channel {channel}/Data"]

    def _read(self, level, t, channel, z, rows, cols):
        with self._io:
            return np.asarray(self._dataset(level, t, channel)[z, rows, cols])

    def _build_plane(self, t: int, z: int):
        return DerivedPyramid([_ImsLevel(self, level, t, z) for level in range(len(self._levels))])

    def read_block(self, level, t, channel, z0, z1, rows, cols):
        info = self._levels[level]
        rows = _clip(rows, info["height"])
        cols = _clip(cols, info["width"])
        if info["depth"] != self._depth0:
            # A level with fewer planes: plane z is its plane z * Z_L / Z_0
            # (as `_ImsLevel` reads it), and the span is read once -- plane by
            # plane would decompress each chunk, 64 planes deep, once per plane.
            stored = [min(info["depth"] - 1, int(z * info["depth"] / max(1, self._depth0)))
                      for z in range(z0, z1 + 1)]
            with self._io:
                span = np.asarray(self._dataset(level, t, channel)[
                    stored[0]:stored[-1] + 1, rows, cols])
            return span[[z - stored[0] for z in stored]]
        with self._io:
            return np.asarray(self._dataset(level, t, channel)[z0:z1 + 1, rows, cols])

    def close(self) -> None:
        super().close()
        try:
            self._file.close()
        except Exception:
            pass


def _levels_of(handle) -> list[dict]:
    """Every resolution level whose XY halves, with its real (unpadded) extent."""
    levels = []
    index = 0
    while f"DataSet/ResolutionLevel {index}" in handle:
        group = handle[f"DataSet/ResolutionLevel {index}/TimePoint 0/Channel 0"]
        data = group["Data"]
        width = int(_number(group.attrs.get("ImageSizeX"), data.shape[2]))
        height = int(_number(group.attrs.get("ImageSizeY"), data.shape[1]))
        depth = int(_number(group.attrs.get("ImageSizeZ"), data.shape[0]))
        levels.append({"index": index, "width": width, "height": height,
                       "depth": depth, "chunks": tuple(data.chunks or data.shape),
                       "dtype": str(data.dtype)})
        index += 1
    if not levels:
        raise ValueError("this .ims file has no DataSet/ResolutionLevel 0")
    kept = [levels[0]]
    for level in levels[1:]:
        previous = kept[-1]
        if (abs(level["width"] - -(-previous["width"] // 2)) <= 1
                and abs(level["height"] - -(-previous["height"] // 2)) <= 1):
            kept.append(level)
        else:
            break
    return kept


def _count(handle, prefix: str, kind: str) -> int:
    count = 0
    while f"{prefix}/{kind} {count}" in handle:
        count += 1
    return count


def _spacing(handle, levels, frames) -> tuple[Spacing, dict]:
    image = handle["DataSetInfo/Image"].attrs if "DataSetInfo/Image" in handle else {}
    unit = normalize_unit(_text(image.get("Unit")) or "um")
    sizes = (levels[0]["width"], levels[0]["height"], levels[0]["depth"])
    steps = []
    for axis, size in enumerate(sizes):
        low = _number(image.get(f"ExtMin{axis}"))
        high = _number(image.get(f"ExtMax{axis}"))
        if low is None or high is None or size <= 0 or high <= low:
            steps.append(None)
        else:
            steps.append((high - low) / size)
    x, y, z = (AxisSpacing(value=s, unit=unit, source="metadata") if s else AxisSpacing()
               for s in steps)
    if levels[0]["depth"] <= 1:
        z = AxisSpacing()
    t = None
    if frames > 1:
        t = AxisSpacing(unit="s")
        info = handle["DataSetInfo/TimeInfo"].attrs if "DataSetInfo/TimeInfo" in handle else {}
        stamps = [_parse_time(_text(info.get(f"TimePoint{i}"))) for i in (1, 2)]
        if all(stamps) and stamps[1] > stamps[0]:
            t = AxisSpacing(value=(stamps[1] - stamps[0]).total_seconds(), unit="s",
                            source="metadata")
    provenance = {"planes_from": "IMS DataSet",
                  "z_spacing_from": "IMS ExtMin/ExtMax" if z.value else None}
    return Spacing(x=x, y=y, z=z, t=t), {k: v for k, v in provenance.items() if v}


def _parse_time(text: str):
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


#: What Imaris writes as a channel's Name when nobody named it. Six channels
#: all called this are six unnamed channels, not one name six times.
UNNAMED = "(name not specified)"


def channel_names(handle, count: int) -> list[str] | None:
    """Every channel's Name, repeats suffixed `_1`, `_2` (AnnData's
    convention); None unless every channel has one."""
    names = []
    for channel in range(count):
        key = f"DataSetInfo/Channel {channel}"
        name = _text(handle[key].attrs.get("Name")).strip() if key in handle else ""
        names.append("" if name == UNNAMED else name)
    if not all(names):
        return None
    seen: dict[str, int] = {}
    unique = []
    for name in names:
        repeat = seen.get(name, 0)
        seen[name] = repeat + 1
        unique.append(f"{name}_{repeat}" if repeat else name)
    return unique


def open_ims_volume(path):
    """An `ImsVolumeSource` for the file at `path` (planar files included:
    no other reader opens an .ims)."""
    import h5py

    handle = h5py.File(str(path), "r")
    try:
        levels = _levels_of(handle)
        frames = max(1, _count(handle, "DataSet/ResolutionLevel 0", "TimePoint"))
        channels = max(1, _count(handle, "DataSet/ResolutionLevel 0/TimePoint 0", "Channel"))
        spacing, provenance = _spacing(handle, levels, frames)
        names = channel_names(handle, channels)
        volume = Volume(
            channels=channels, depth=levels[0]["depth"], frames=frames,
            height=levels[0]["height"], width=levels[0]["width"],
            dtype=levels[0]["dtype"], spacing=spacing,
            levels=level_infos([(lv["height"], lv["width"]) for lv in levels],
                               levels[0]["chunks"][-2:]),
            source_kind="ims", z_semantics="depth", t_semantics="time",
            channel_names=tuple(names) if names else None, provenance=provenance,
            stored_axes=("t" if frames > 1 else "") + "c"
            + ("z" if levels[0]["depth"] > 1 else "") + "yx")
    except Exception:
        handle.close()
        raise
    return ImsVolumeSource(path, handle, levels, volume)


def physical_metadata(source: ImsVolumeSource) -> dict:
    """The `/get_ome_metadata` payload: the scale bar's keys and z/t."""
    spacing = source.volume.spacing
    out = {}
    for axis in ("x", "y", "z"):
        step = getattr(spacing, axis)
        if step.value:
            out[f"physical_size_{axis}"] = step.value
            out[f"physical_size_{axis}_unit"] = step.unit
    if spacing.t is not None and spacing.t.value:
        out["time_increment"] = spacing.t.value
        out["time_increment_unit"] = spacing.t.unit
    return out
