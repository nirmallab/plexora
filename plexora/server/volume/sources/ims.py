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

STEPPING TO THE NEXT PLANE IS A CACHE HIT. Imaris chunks run many planes deep
(64 x 128 x 128 is common), so a plane is a thin slice through dozens of
chunks, each decompressed whole to give it up. HDF5 keeps decoded chunks --
but per OPEN dataset, 8 MB by default, and a dataset reopened per read starts
empty. So the datasets in use are kept open (`OPEN_DATASETS`, least recently
used closed first) with room for about a plane's chunks each
(`chunk_cache_bytes`): the next plane, and the one after, are then read out of
memory rather than decompressed again -- 120 ms a tile became a few.

DECOMPRESSED OUTSIDE THE LOCK. h5py serializes every HDF5 call behind one
process-wide lock, and gzip inflate runs inside H5Dread -- so two channels,
a section and a 3-D level, read at once, were one inflate at a time. Where a
dataset's filters are ones this reads itself (deflate, optionally after
shuffle), only the raw chunk bytes are read under the lock
(`read_direct_chunk`, a few ms for a plane's chunks); they are inflated on
`chunk_cache.DECODE_POOL` with the lock released, and the decoded chunks kept
in the process-wide `chunk_cache.CACHE` instead of h5py's per-dataset one. Any
other filter pipeline reads the way it always did.
"""

from __future__ import annotations

import datetime as _dt
import os
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

from plexora.server.volume import chunk_cache
from plexora.server.volume.axes import AxisSpacing, Spacing, normalize_unit
from plexora.server.volume.model import Volume, VolumeSource, level_infos
from plexora.server.volume.plane_view import DerivedPyramid

SUFFIXES = (".ims",)

#: Datasets (one channel of one level of one frame) kept open with their
#: decoded chunks; past this the least recently read is closed.
OPEN_DATASETS = 6
#: Decoded chunks kept per open dataset, in MB: a level-0 plane of a typical
#: confocal stack crosses ~80 chunks of 1 MB. Bounded in all by OPEN_DATASETS.
CHUNK_CACHE_MB = 96
ENV_CHUNK_CACHE = "PLEXORA_IMS_CHUNK_CACHE_MB"


def chunk_cache_bytes() -> int:
    """Decoded chunks kept per open dataset (`PLEXORA_IMS_CHUNK_CACHE_MB`)."""
    try:
        megabytes = float(os.environ.get(ENV_CHUNK_CACHE, "") or CHUNK_CACHE_MB)
    except ValueError:
        megabytes = CHUNK_CACHE_MB
    return int(max(1.0, megabytes) * 2**20)


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
        #: (level, t, channel) -> open h5py dataset, most recently read last.
        self._open = OrderedDict()
        #: Planes per stored chunk: a slab is read in whole chunks of them.
        self.z_chunk = int(levels[0]["chunks"][0]) if levels[0].get("chunks") else 1
        self.z_decode = self.z_chunk
        #: (level, t, channel) -> how its chunks are read here, or None for
        #: h5py's own path (see the module notes).
        self._layouts = {}
        try:
            stat = os.stat(self.path)
            self.cache_id = ("ims", self.path, stat.st_size, stat.st_mtime_ns)
        except OSError:
            self.cache_id = ("ims", self.path)

    def decode_chunk(self, level):
        """(z, y, x) of one decode at `level`: the dataset's chunk."""
        chunks = self._levels[level].get("chunks")
        return tuple(int(c) for c in chunks[-3:]) if chunks else None

    def _dataset(self, level, t, channel):
        """The dataset, held open so its decoded chunks outlive this read.
        Called with `_io` held."""
        key = (int(level), int(t), int(channel))
        dataset = self._open.get(key)
        if dataset is None:
            dataset = self._file[f"DataSet/ResolutionLevel {level}/TimePoint {t}/Channel {channel}/Data"]
            self._open[key] = dataset
            while len(self._open) > OPEN_DATASETS:
                self._open.popitem(last=False)
        else:
            self._open.move_to_end(key)
        return dataset

    def _layout(self, level, t, channel):
        """`{chunks, dtype, fill, shuffle, deflate}` when this reads the
        dataset's chunks itself, else None. Called with `_io` held."""
        key = (int(level), int(t), int(channel))
        if key in self._layouts:
            return self._layouts[key]
        layout = None
        try:
            dataset = self._dataset(level, t, channel)
            plist = dataset.id.get_create_plist()
            filters = [plist.get_filter(i)[0] for i in range(plist.get_nfilters())]
            # 2 = shuffle, 1 = deflate; in the order HDF5 applies them on write.
            if dataset.chunks and filters in ([], [1], [2, 1], [2]):
                layout = {"chunks": tuple(int(c) for c in dataset.chunks),
                          "dtype": np.dtype(dataset.dtype),
                          "fill": dataset.fillvalue,
                          "filters": filters,
                          "dataset": dataset}
        except Exception:  # noqa: BLE001 -- h5py's own path reads anything
            layout = None
        self._layouts[key] = layout
        return layout

    def _read_chunks(self, level, t, channel, layout, z0, z1, rows, cols):
        """Stored planes z0..z1 (inclusive) of the rectangle, through the
        decoded-chunk cache: raw bytes under the lock, inflated outside it."""
        cz, cy, cx = layout["chunks"]
        dtype = layout["dtype"]
        dataset = layout["dataset"]
        filters = layout["filters"]
        keys = [(self.cache_id, int(level), int(t), int(channel), zc, yc, xc)
                for zc in chunk_cache.chunk_ranges(z0, z1 + 1, cz)
                for yc in chunk_cache.chunk_ranges(rows.start, rows.stop, cy)
                for xc in chunk_cache.chunk_ranges(cols.start, cols.stop, cx)]
        chunk_bytes = cz * cy * cx * dtype.itemsize

        def decode(raw, mask):
            if raw is None:
                return np.full((cz, cy, cx), layout["fill"] or 0, dtype=dtype)
            data = raw
            # A set bit in the mask is a filter this chunk skipped.
            if 1 in filters and not (mask >> filters.index(1)) & 1:
                data = _inflate(data)
            if 2 in filters and dtype.itemsize > 1 and not (mask >> filters.index(2)) & 1:
                data = np.frombuffer(data, np.uint8).reshape(dtype.itemsize, -1).T.tobytes()
            return np.frombuffer(data, dtype=dtype, count=cz * cy * cx).reshape(cz, cy, cx)

        def load_many(missing):
            raws = []
            with self._io:
                for key in missing:
                    _, _, _, _, zc, yc, xc = key
                    try:
                        mask, raw = dataset.id.read_direct_chunk((zc * cz, yc * cy, xc * cx))
                    except Exception:  # noqa: BLE001 -- a chunk never written
                        mask, raw = 0, None
                    raws.append((key, raw, mask))
            if len(raws) == 1:
                return {raws[0][0]: decode(raws[0][1], raws[0][2])}
            futures = [(key, chunk_cache.DECODE_POOL.submit(decode, raw, mask))
                       for key, raw, mask in raws]
            return {key: future.result() for key, future in futures}

        chunks = chunk_cache.CACHE.fetch(
            keys, load_many, keep=chunk_cache.keep_for(len(keys) * chunk_bytes))
        return chunk_cache.assemble(((z0, z1 + 1), (rows.start, rows.stop), (cols.start, cols.stop)),
                                    (cz, cy, cx), chunks, dtype,
                                    coords_of=lambda key: key[4:7])

    def _span(self, level, t, channel, z0, z1, rows, cols):
        """Stored planes z0..z1 of the rectangle, by whichever path applies."""
        with self._io:
            layout = self._layout(level, t, channel)
            if layout is None:
                return np.asarray(self._dataset(level, t, channel)[z0:z1 + 1, rows, cols])
        return self._read_chunks(level, t, channel, layout, z0, z1, rows, cols)

    def _read(self, level, t, channel, z, rows, cols):
        info = self._levels[level]
        return self._span(level, t, channel, z, z,
                          _clip(rows, info["height"]), _clip(cols, info["width"]))[0]

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
            span = self._span(level, t, channel, stored[0], stored[-1], rows, cols)
            return span[[z - stored[0] for z in stored]]
        return self._span(level, t, channel, z0, z1, rows, cols)

    def close(self) -> None:
        super().close()
        chunk_cache.CACHE.forget(self.cache_id)
        self._layouts.clear()
        self._open.clear()
        try:
            self._file.close()
        except Exception:
            pass


def _inflate(raw: bytes) -> bytes:
    """A deflated chunk, inflated -- libdeflate when it is installed (several
    times zlib's speed), zlib otherwise; both release the GIL."""
    try:
        import imagecodecs

        return imagecodecs.deflate_decode(raw)
    except ImportError:
        import zlib

        return zlib.decompress(raw)


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

    # The chunk cache is set on the file and given to every dataset opened
    # from it; `rdcc_nslots` a prime well above the chunks it can hold.
    handle = h5py.File(str(path), "r", rdcc_nbytes=chunk_cache_bytes(),
                       rdcc_nslots=100_003, rdcc_w0=0.75)
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
