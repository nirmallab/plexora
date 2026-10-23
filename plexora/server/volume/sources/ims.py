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
dataset's filters are ones this reads itself (deflate or LZ4, optionally
after shuffle), only the raw chunk bytes are read under the lock
(`read_direct_chunk`, a few ms for a plane's chunks); they are inflated on
`chunk_cache.DECODE_POOL` with the lock released, and the decoded chunks kept
in the process-wide `chunk_cache.CACHE` instead of h5py's per-dataset one. Any
other filter pipeline reads the way it always did.

AND READ OUTSIDE IT. "A few ms" was a local disk. On a cluster's network
filesystem the raw bytes are the slow part -- 16 chunks under a 1024 px tile,
an 8-plane block of a 70-channel stack, measured at 50-300 ms a tile on HMS
O2 -- and every tile of every channel took its turn at them under the one
lock: a node with 16 cores answered about 21 cold tiles a second however many
were asked for. Asking HDF5 only WHERE each chunk is, under the lock, and
reading the bytes by offset outside it (`_RawFile`, on the decode pool) looks
like the cure and is not, for a file on a disk: `get_chunk_info_by_coord`
walks the dataset's whole chunk index on every call -- linear in the chunks,
~10 ms a call on a 24,000-chunk Imaris channel even with the index cached --
so a tile spent longer finding its chunks than h5py took to read them.
Measured on HMS O2 scratch, 16 cold 1024 px tiles: 213-763 ms a tile that
way, 60-110 through h5py, 34-45 with `read_direct_chunk` (a B-tree lookup and
the read) under the lock and the decode outside it -- and 13-14 with every
chunk's offset known beforehand. So a large file on a disk has its chunk
index walked ONCE, in a process of its own, and saved (ims_index.py); with
that index its chunks are read by offset, outside the lock and in parallel,
and until it exists (or for a small file) with `read_direct_chunk`. A file
at a web address, below, is located and read by offset. Either way the first
chunk read by offset from each dataset is compared with h5py's own
`read_direct_chunk` of it; where they differ that dataset reads the old way.

AT A WEB ADDRESS. An .ims at an https/s3/gs/az/sftp address is the same
file, read through `remote_store.RemoteFile`: h5py opens that stream (its
`fileobj` driver), so the superblock, the object headers and the chunk index
are read through the block cache, a 64 KB block at a time and kept on disk.
Chunk bytes go the way they go on a cluster filesystem, by offset -- except
that the chunks under a read are fetched in one batch (`_StreamRaw.read_many`)
rather than a request per chunk on the decode pool, because over a network a
request is a round trip. What h5py still reads itself -- a mask's slab, the
windows that decide whether a file is a mask -- is fetched the same way first
(`_warm`), so h5py's one-chunk-at-a-time reads are answered from the cache
rather than one round trip after another.
"""

from __future__ import annotations

import datetime as _dt
import functools
import os
import threading
from collections import OrderedDict
from pathlib import Path
from time import perf_counter

import numpy as np

from plexora.server.volume import chunk_cache
from plexora.server.volume.sources import ims_index
from plexora.telemetry import performance as _perf
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


#: Most bytes `_warm` asks for in one batch: what is fetched is kept by the
#: cache, not here, so this only bounds what one batch holds in memory.
WARM_BATCH_BYTES = 64 * 2**20


def is_ims_path(path) -> bool:
    """Whether `path` is an .ims: a file on this disk, or -- by its name,
    without a request -- one at a web address."""
    try:
        text = str(path)
    except Exception:
        return False
    if is_remote_ims(text):
        return True
    return text.lower().endswith(SUFFIXES) and Path(text).is_file()


def is_remote_ims(path) -> bool:
    """Whether `path` is a web address naming an .ims (name only)."""
    from plexora.server.providers.base import is_remote_locator

    if not is_remote_locator(path):
        return False
    from plexora.server.utils import remote_image

    return remote_image.suffix_of(path) in SUFFIXES


def _open_file(path, **options):
    """`(h5py.File, stream)` for `path`, read-only. `stream` is the
    `RemoteFile` h5py reads when `path` is a web address, else None; close
    the file first and the stream after it (`_close_file`)."""
    import h5py
    import hdf5plugin  # noqa: F401 -- registers LZ4 et al. (Imaris 10 writes LZ4)

    if not is_remote_ims(path):
        return h5py.File(str(path), "r", **options), None
    from plexora.server.utils import remote_store

    stream = remote_store.open_file(path)
    try:
        # The length first, outside h5py: a missing or forbidden file raises
        # here, where h5py's open would keep asking a stream that cannot say
        # and never return. h5py asks for it next anyway; it is remembered.
        stream.size
        return h5py.File(stream, "r", **options), stream
    except Exception:
        stream.close()
        raise


def _close_file(handle, stream) -> None:
    try:
        handle.close()
    except Exception:  # noqa: BLE001
        pass
    if stream is not None:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def _name(path) -> str:
    """The file's name, for a sentence -- a web address's included."""
    if is_remote_ims(path):
        from plexora.server.utils import remote_store

        return remote_store.url_name(path)
    return Path(str(path)).name


def _chunk_spans(dataset, z0, z1, rows, cols) -> list:
    """`(offset, size)` of each stored chunk under planes z0..z1 (inclusive)
    of the rectangle -- as many as HDF5 can say where they are."""
    chunks = dataset.chunks
    if not chunks or len(chunks) != 3:
        return []
    cz, cy, cx = (int(c) for c in chunks)
    spans = []
    try:
        for zc in chunk_cache.chunk_ranges(int(z0), int(z1) + 1, cz):
            for yc in chunk_cache.chunk_ranges(rows.start, rows.stop, cy):
                for xc in chunk_cache.chunk_ranges(cols.start, cols.stop, cx):
                    info = dataset.id.get_chunk_info_by_coord((zc * cz, yc * cy, xc * cx))
                    if getattr(info, "byte_offset", None) is not None:
                        spans.append((int(info.byte_offset), int(info.size)))
    except Exception:  # noqa: BLE001 -- an h5py too old to say: nothing warmed
        pass
    return spans


def _warm(stream, spans) -> None:
    """Fetch `spans` of a remote file a batch at a time, so h5py's own reads
    of them, one chunk after another, come out of the cache. A no-op on disk."""
    if stream is None or not spans:
        return
    batch, held = [], 0
    for span in spans:
        batch.append(span)
        held += span[1]
        if held >= WARM_BATCH_BYTES:
            stream.pread_many(batch)
            batch, held = [], 0
    if batch:
        stream.pread_many(batch)


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


class _RawFile:
    """A file's bytes at an offset, from any number of threads at once.

    `os.pread` where there is one -- no shared position, so no lock -- and on
    Windows, which has none, a handle per thread. Every handle is closed by
    `close`: a file held open is one Windows will not let anybody delete."""

    def __init__(self, path):
        self.path = str(path)
        self._fd = None
        self._lock = threading.Lock()
        self._local = threading.local()
        self._handles = []
        self._closed = False

    def read(self, offset: int, size: int) -> bytes:
        offset, size = int(offset), int(size)
        if hasattr(os, "pread"):
            fd = self._fd
            if fd is None:
                with self._lock:
                    if self._closed:
                        raise OSError(f"{self.path} is closed")
                    if self._fd is None:
                        self._fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                    fd = self._fd
            parts = []
            got = 0
            while got < size:
                part = os.pread(fd, size - got, offset + got)
                if not part:
                    raise EOFError(f"{self.path} ends before byte {offset + size}")
                parts.append(part)
                got += len(part)
            return parts[0] if len(parts) == 1 else b"".join(parts)
        handle = getattr(self._local, "handle", None)
        if handle is None or handle.closed:
            with self._lock:
                if self._closed:
                    raise OSError(f"{self.path} is closed")
                handle = open(self.path, "rb", buffering=0)  # noqa: SIM115 -- closed in close()
                self._handles.append(handle)
            self._local.handle = handle
        handle.seek(offset)
        parts = []
        got = 0
        while got < size:
            part = handle.read(size - got)
            if not part:
                raise EOFError(f"{self.path} ends before byte {offset + size}")
            parts.append(part)
            got += len(part)
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            fd, self._fd = self._fd, None
            handles, self._handles = self._handles, []
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass


class _StreamRaw:
    """`_RawFile` for a file at a web address: bytes by offset through the
    chunk cache, and `read_many` for a batch fetched together. The stream is
    the h5py file's too, so the source closes it, after the file."""

    def __init__(self, stream):
        self.stream = stream

    def read(self, offset: int, size: int) -> bytes:
        return self.read_many([(offset, size)])[0]

    def read_many(self, spans) -> list:
        spans = [(int(o), int(n)) for o, n in spans]
        out = self.stream.pread_many(spans)
        for (offset, size), data in zip(spans, out):
            if len(data) < size:
                raise EOFError(f"{self.stream.name} ends before byte {offset + size}")
        return out

    def close(self) -> None:
        pass


#: A chunk HDF5 could not say where it is: read it through h5py instead.
_UNLOCATED = object()


class ImsVolumeSource(VolumeSource):
    prefers_block = True

    def __init__(self, path, handle, levels, volume: Volume, stream=None):
        super().__init__(volume)
        self.path = str(path)
        self._file = handle
        #: The `RemoteFile` h5py reads, for a file at a web address.
        self._stream = stream
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
        #: The file's bytes by offset, read in parallel (see the module notes).
        self._raw = _RawFile(self.path) if stream is None else _StreamRaw(stream)
        #: (level, t, channel) -> whether its chunks are read by offset: True
        #: once one read that way matched h5py's own, False when it did not
        #: (or this h5py cannot say where a chunk is); absent until known.
        self._direct = {}
        #: Where every chunk is, from the file's saved index (ims_index.py):
        #: None until there is one, looked for every `ims_index.POLL_S`.
        self._chunk_index = None
        self._index_looked = float("-inf")
        self._index_lock = threading.Lock()
        if stream is not None:
            # h5py asked for the length when it opened the stream.
            self.cache_id = ("ims", self.path, stream.known_size)
            return
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

    def _index_grid(self, dkey):
        """`(offsets, sizes, masks)` of `dkey`'s chunk grid from the file's
        saved index, or None: a file at a web address, or no index yet."""
        if self._stream is not None:
            return None
        with self._index_lock:
            index = self._chunk_index
            if index is None:
                now = perf_counter()
                if now - self._index_looked < ims_index.POLL_S:
                    return None
                self._index_looked = now
                index = self._chunk_index = ims_index.load(self.path)
                if index is None:
                    return None
        return index.grid(*dkey)

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
            # Shuffle then one codec, in the order HDF5 applies them on write.
            if dataset.chunks and _readable(filters):
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
        decoded-chunk cache: raw bytes under the lock, inflated outside it.
        On a tile's thread, the bytes' time is the read's `io` part and their
        decompression its `inflate` (`Server-Timing`), from whichever decode
        thread did each chunk."""
        cz, cy, cx = layout["chunks"]
        phases = _perf.current()
        dtype = layout["dtype"]
        dataset = layout["dataset"]
        filters = layout["filters"]
        codec = next((f for f in filters if f != _SHUFFLE), None)
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
            if codec is not None and not (mask >> filters.index(codec)) & 1:
                data = _decoder(codec)(data)
            if _SHUFFLE in filters and dtype.itemsize > 1 and not (mask >> filters.index(_SHUFFLE)) & 1:
                data = np.frombuffer(data, np.uint8).reshape(dtype.itemsize, -1).T.tobytes()
            return np.frombuffer(data, dtype=dtype, count=cz * cy * cx).reshape(cz, cy, cx)

        def origin_of(key):
            _, _, _, _, zc, yc, xc = key
            return (zc * cz, yc * cy, xc * cx)

        def read_locked(origin):
            with self._io:
                try:
                    mask, raw = dataset.id.read_direct_chunk(origin)
                except Exception:  # noqa: BLE001 -- a chunk never written
                    mask, raw = 0, None
            return raw, mask

        def load(entry, fetched=None):
            key, where = entry
            started = perf_counter()
            if where is _UNLOCATED:
                raw, mask = read_locked(origin_of(key))
            elif where is None:
                raw, mask = None, 0
            else:
                offset, size, mask = where
                raw = fetched if fetched is not None else self._raw.read(offset, size)
            if phases is None:
                return decode(raw, mask)
            read = perf_counter()
            decoded = decode(raw, mask)
            _perf.part("io", (read - started) * 1000.0, phases)
            _perf.part("inflate", (perf_counter() - read) * 1000.0, phases)
            return decoded

        def load_many(missing):
            dkey = (int(level), int(t), int(channel))
            located = []
            grid = self._index_grid(dkey) if self._direct.get(dkey, True) else None
            if grid is not None:
                # Where each chunk is, from the file's saved index: no lock
                # and no HDF5 call (ims_index.py).
                direct = True
                offsets, sizes, masks = grid
                for key in missing:
                    at = tuple(key[4:7])
                    try:
                        offset = int(offsets[at])
                    except IndexError:
                        located.append((key, _UNLOCATED))
                        continue
                    located.append((key, None if offset < 0 else
                                    (offset, int(sizes[at]), int(masks[at]))))
            else:
                # By offset otherwise only at a web address: on a disk,
                # finding a chunk costs more than reading it (the module
                # docstring).
                direct = self._direct.get(dkey, self._stream is not None)
                with self._io:
                    for key in missing:
                        if not direct:
                            located.append((key, _UNLOCATED))
                            continue
                        try:
                            info = dataset.id.get_chunk_info_by_coord(origin_of(key))
                        except AttributeError:
                            # An h5py too old to say where a chunk is.
                            self._direct[dkey] = direct = False
                            located.append((key, _UNLOCATED))
                            continue
                        except Exception:  # noqa: BLE001 -- h5py's own read decides
                            located.append((key, _UNLOCATED))
                            continue
                        offset = getattr(info, "byte_offset", None)
                        located.append((key, None if offset is None else
                                        (int(offset), int(info.size), int(info.filter_mask))))
            if direct and dkey not in self._direct:
                located = self._check_direct(dkey, located, origin_of, read_locked)
            fetched = {}
            read_many = getattr(self._raw, "read_many", None)
            wanted = [(key, where) for key, where in located if isinstance(where, tuple)]
            if read_many is not None and wanted:
                # At a web address: every chunk's bytes in one batch of
                # requests, not a round trip each on the decode pool.
                started = perf_counter()
                datas = read_many([(where[0], where[1]) for _, where in wanted])
                fetched = {key: data for (key, _), data in zip(wanted, datas)}
                _perf.part("io", (perf_counter() - started) * 1000.0, phases)
            work = chunk_cache.pool()
            if len(located) == 1:
                return {located[0][0]: load(located[0], fetched.get(located[0][0]))}
            futures = [(entry[0], work.submit(load, entry, fetched.get(entry[0])))
                       for entry in located]
            return {key: future.result() for key, future in futures}

        chunks = chunk_cache.CACHE.fetch(
            keys, load_many, keep=chunk_cache.keep_for(len(keys) * chunk_bytes))
        return chunk_cache.assemble(((z0, z1 + 1), (rows.start, rows.stop), (cols.start, cols.stop)),
                                    (cz, cy, cx), chunks, dtype,
                                    coords_of=lambda key: key[4:7])

    def _check_direct(self, dkey, located, origin_of, read_locked):
        """Settle whether `dkey`'s chunks can be read by offset: the first
        one located is read both ways and compared. Returns `located`,
        re-pointed at h5py's own read when they differ."""
        for key, where in located:
            if not isinstance(where, tuple):
                continue
            offset, size, mask = where
            try:
                ours = self._raw.read(offset, size)
            except Exception:  # noqa: BLE001 -- unreadable by offset: h5py's way
                ours = None
            theirs, their_mask = read_locked(origin_of(key))
            same = ours is not None and ours == theirs and mask == their_mask
            self._direct[dkey] = same
            if same:
                return located
            return [(k, _UNLOCATED) for k, _ in located]
        return located

    def _span(self, level, t, channel, z0, z1, rows, cols):
        """Stored planes z0..z1 of the rectangle, by whichever path applies.
        Finding the dataset and its layout, the file's lock included, is the
        read's `open` part."""
        started = perf_counter()
        with self._io:
            layout = self._layout(level, t, channel)
            if layout is None:
                return np.asarray(self._dataset(level, t, channel)[z0:z1 + 1, rows, cols])
        _perf.part("open", (perf_counter() - started) * 1000.0)
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

    def read_stored(self, level, t, channel, s0, s1, rows, cols):
        """Stored planes s0..s1 of `level`, each once (`model.read_stored`)."""
        info = self._levels[level]
        return self._span(level, t, channel, int(s0), int(s1),
                          _clip(rows, info["height"]), _clip(cols, info["width"]))

    def close(self) -> None:
        super().close()
        chunk_cache.CACHE.forget(self.cache_id)
        self._layouts.clear()
        self._open.clear()
        self._direct.clear()
        with self._index_lock:
            if self._chunk_index is not None:
                self._chunk_index.close()
                self._chunk_index = None
        self._raw.close()
        _close_file(self._file, self._stream)


#: HDF5 filter ids: byte shuffle, deflate, and LZ4 -- the registered id of
#: hdf5plugin's LZ4 filter, which Imaris 10 can write.
_SHUFFLE, _DEFLATE, _LZ4 = 2, 1, 32004


def _readable(filters) -> bool:
    """Whether `_read_chunks` decodes a dataset with this filter pipeline
    itself: nothing, shuffle, a codec, or shuffle then a codec."""
    codecs = [_DEFLATE] + ([_LZ4] if _lz4_available() else [])
    return filters in ([], [_SHUFFLE], *([c] for c in codecs), *([_SHUFFLE, c] for c in codecs))


def _decoder(codec):
    """The function that decompresses a chunk written through `codec`."""
    return _inflate if codec == _DEFLATE else _unlz4


@functools.cache
def _lz4_available() -> bool:
    """Whether imagecodecs here decodes HDF5's LZ4 framing; without it an LZ4
    dataset reads through h5py (and hdf5plugin) instead."""
    try:
        import imagecodecs

        return bool(imagecodecs.LZ4H5.available)
    except (ImportError, AttributeError):
        return False


def _unlz4(raw: bytes) -> bytes:
    """A chunk through HDF5's LZ4 filter, decompressed: its 12-byte header
    (original size, block size) and length-prefixed LZ4 blocks, decoded by
    imagecodecs, which releases the GIL."""
    import imagecodecs

    return imagecodecs.lz4h5_decode(raw)


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
    no other reader opens an .ims), on this disk or at a web address."""
    # The chunk cache is set on the file and given to every dataset opened
    # from it; `rdcc_nslots` a prime well above the chunks it can hold.
    handle, stream = _open_file(path, rdcc_nbytes=chunk_cache_bytes(),
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
                               levels[0]["chunks"][-2:],
                               depths=[lv["depth"] for lv in levels]),
            source_kind="ims", z_semantics="depth", t_semantics="time",
            channel_names=tuple(names) if names else None, provenance=provenance,
            stored_axes=("t" if frames > 1 else "") + "c"
            + ("z" if levels[0]["depth"] > 1 else "") + "yx")
    except Exception:
        _close_file(handle, stream)
        raise
    if stream is None:
        # A large file's chunk index, walked once in the background.
        ims_index.ensure(path)
    return ImsVolumeSource(path, handle, levels, volume, stream=stream)


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


# -- an .ims as a segmentation mask -------------------------------------------
#
# An .ims is HDF5, and Imaris itself writes no label images into one -- but the
# tools that hand segmentations to Imaris users do (a label TIFF run through
# the Imaris File Converter, PyImarisWriter from a pipeline), so a mask arrives
# as an .ims with one channel of ids, or with that channel beside the stains.
# `ImsLabels` reads one channel as a label array: (Z, Y, X), or (T, Z, Y, X)
# for a time series, which `volume/labels.build_label_volume` stores as a
# label volume exactly as it does a 3-D TIFF; a single plane takes the 2-D
# conversion (`segmentation_pyramid._open_level_zero`).

#: Every whole number up to here survives float32 -- and float32 is what a mask
#: of more than 65,535 cells becomes, because Imaris stores 32-bit data as float.
FLOAT_ID_LIMIT = 2 ** 24

#: Past this many bytes a chunk-deep slab is not read whole, and planes are
#: read one at a time (each re-inflating the chunks it crosses).
SLAB_LIMIT = 1 << 30

#: How a window of ids differs from a window of stain, measured along rows of
#: labelled pixels. A cell is ONE value across its interior, so its runs are
#: as long as it is wide (`_MIN_RUN` excludes a stain's pixel-to-pixel noise),
#: and its id comes back on each row it crosses exactly once -- where a stain's
#: values recur all along a row (`_MAX_REPEATS`). Neither depends on how big
#: the cells are, which a share-of-flat-neighbours threshold did: small nuclei
#: sat right on it. A binary mask fails the second too (every object one
#: value), and it is not a mask of cells. Measured on a real 6-channel confocal
#: .ims: its stains repeat a value 13 to 41 times per row; masks drawn from it
#: 1.0 (round cells) to 1.5 (ragged ones) -- so 3 leaves room both ways.
_MIN_RUN = 2.0
_MAX_REPEATS = 3.0


def _channel_name(handle, channel: int) -> str:
    key = f"DataSetInfo/Channel {channel}"
    name = _text(handle[key].attrs.get("Name")).strip() if key in handle else ""
    return "" if name == UNNAMED else name


def _window_of(levels) -> tuple:
    """(z, rows, cols) of the middle plane's central 512 x 512 at level 0."""
    info = levels[0]
    y0 = max(0, info["height"] // 2 - 256)
    x0 = max(0, info["width"] // 2 - 256)
    return (info["depth"] // 2, slice(y0, min(info["height"], y0 + 512)),
            slice(x0, min(info["width"], x0 + 512)))


def _window_data(handle, channel: int):
    return handle[f"DataSet/ResolutionLevel 0/TimePoint 0/Channel {channel}/Data"]


def _window(handle, channel: int, levels) -> np.ndarray:
    """The middle plane's central 512 x 512 of `channel`, frame 0, level 0."""
    z, rows, cols = _window_of(levels)
    return np.asarray(_window_data(handle, channel)[z, rows, cols])


def looks_like_labels(window: np.ndarray) -> bool:
    """Whether a plane's window is cell ids: whole numbers, a background of
    zero, more than one value, and flat across each cell."""
    if window.size == 0 or window.dtype.kind not in "uif":
        return False
    if window.dtype.kind == "f":
        if not np.isfinite(window).all() or not np.array_equal(window, np.floor(window)):
            return False
    if window.min() < 0 or not (window == 0).any() or np.unique(window).size < 2:
        return False
    values = window.astype(np.int64, copy=False)
    # A run starts wherever a labelled pixel differs from the one to its left.
    starts = values != np.pad(values, ((0, 0), (1, 0)), constant_values=-1)[:, :-1]
    starts &= values != 0
    pixels = int(np.count_nonzero(values))
    runs = int(np.count_nonzero(starts))
    if not runs or pixels / runs < _MIN_RUN:
        return False
    rows = np.nonzero(starts)[0]
    pairs = np.unique(np.stack([values[starts], rows]), axis=1).shape[1]
    return runs / pairs <= _MAX_REPEATS


def _mask_named(name: str) -> bool:
    from plexora.server.models.import_proposal import MASK_HINTS

    lowered = name.lower()
    return any(hint in lowered for hint in MASK_HINTS)


def label_channels(handle, stream=None) -> tuple[list[int], list[str]]:
    """(channels whose middle window reads as ids, every channel's name).

    `stream` is the file's `RemoteFile` at a web address: every channel's
    window is then fetched in one batch first, not a channel at a time."""
    levels = _levels_of(handle)
    count = max(1, _count(handle, "DataSet/ResolutionLevel 0/TimePoint 0", "Channel"))
    names = [_channel_name(handle, c) for c in range(count)]
    if stream is not None:
        z, rows, cols = _window_of(levels)
        _warm(stream, [span for c in range(count)
                       for span in _chunk_spans(_window_data(handle, c), z, z, rows, cols)])
    looks = [c for c in range(count) if looks_like_labels(_window(handle, c, levels))]
    return looks, names


def label_channel(handle, path, stream=None) -> int:
    """The channel of an .ims mask that holds the ids.

    The only channel; else the one channel that reads as ids, preferring one
    NAMED as a mask when several do. Anything else is a sentence naming the
    channels, rather than a guess between a nuclear and a whole-cell mask.
    """
    looks, names = label_channels(handle, stream)
    if len(names) == 1:
        return 0
    named = [c for c in looks if _mask_named(names[c])]
    for pool in (named, looks):
        if len(pool) == 1:
            return pool[0]
    label = lambda c: names[c] or f"channel {c + 1}"  # noqa: E731
    which = (f"{len(looks)} of them hold ids ({', '.join(label(c) for c in looks)})"
             if looks else "none of them holds cell ids")
    raise ValueError(
        f"{_name(path)} has {len(names)} channels and {which}, so which one "
        "is the mask cannot be told. Save the mask channel as an .ims of its "
        "own, or name it (mask, cells, nuclei...).")


def mask_verdict(path):
    """`import_proposal.looks_like_label_image` for an .ims: True, False, or
    None to ask. A file of one channel of ids is a mask; a file of stains with
    one channel of ids beside them is either, and is asked about unless a name
    -- the file's or the channel's -- says mask."""
    try:
        handle, stream = _open_file(path)
    except Exception:
        return None
    try:
        looks, names = label_channels(handle, stream)
    except Exception:
        return None
    finally:
        _close_file(handle, stream)
    if not looks:
        return False
    if len(names) == 1:
        return True
    if len(looks) == 1 and (_mask_named(names[looks[0]]) or _mask_named(_name(path))):
        return True
    return None


class ImsLabels:
    """One channel of an .ims as a label array: `shape` (Z, Y, X), or
    (T, Z, Y, X) for a time series, at the real extent rather than the chunk-
    padded one. Indexed the way `build_label_volume` reads: `[z]` is a plane,
    and for a time series `[t]` is a frame whose `[z]` is a plane.

    A float32 channel is read as uint32, every value checked to be a whole
    number on the way (`FLOAT_ID_LIMIT`) -- a fraction is a stain, not an id.
    Planes come out of a chunk-deep slab read once (`SLAB_LIMIT`), because a
    plane alone would inflate every chunk it crosses for one plane of each.
    """

    def __init__(self, path, channel=None):
        self.path = str(path)
        self._file, self._stream = _open_file(self.path)
        try:
            levels = _levels_of(self._file)
            self.frames = max(1, _count(self._file, "DataSet/ResolutionLevel 0", "TimePoint"))
            self.depth = levels[0]["depth"]
            self.height = levels[0]["height"]
            self.width = levels[0]["width"]
            self.channel = (label_channel(self._file, self.path, self._stream)
                            if channel is None else int(channel))
            stored = np.dtype(self._data(0).dtype)
        except Exception:
            _close_file(self._file, self._stream)
            raise
        if stored.kind not in "uif":
            _close_file(self._file, self._stream)
            raise TypeError(f"{_name(self.path)} holds {stored} values, not cell ids.")
        self._float = stored.kind == "f"
        self.dtype = np.dtype(np.uint32) if self._float else stored
        self.shape = (((self.frames,) if self.frames > 1 else ())
                      + (self.depth, self.height, self.width))
        self.ndim = len(self.shape)
        self._slab = None

    def _data(self, t: int):
        return self._file[f"DataSet/ResolutionLevel 0/TimePoint {int(t)}/Channel {self.channel}/Data"]

    def _ids(self, block: np.ndarray) -> np.ndarray:
        if not self._float:
            return block
        if block.size and (not np.isfinite(block).all()
                           or not np.array_equal(block, np.floor(block))
                           or block.min() < 0 or block.max() > FLOAT_ID_LIMIT):
            raise ValueError(
                f"{_name(self.path)}'s mask channel holds values that are "
                "not whole numbers, so they are not cell ids.")
        return block.astype(np.uint32)

    def _warm(self, data, z0, z1, rows, cols) -> None:
        """At a web address, fetch the chunks under the read a batch at a time
        first (`_warm`). On a disk, nothing -- not even finding the chunks:
        `_chunk_spans` asks HDF5 where each one is, which costs about as much
        as reading it (a whole plane of a 46,000-chunk mask: 0.67 s, for a
        0.74 s read)."""
        if self._stream is not None:
            _warm(self._stream, _chunk_spans(data, z0, z1, rows, cols))

    def plane(self, t: int, z: int, rows: slice = slice(None),
              cols: slice = slice(None)) -> np.ndarray:
        """Frame `t`, plane `z`, `[rows, cols]` of the real extent."""
        rows = _clip(rows, self.height)
        cols = _clip(cols, self.width)
        whole = (rows == slice(0, self.height) and cols == slice(0, self.width))
        if not whole:
            data = self._data(t)
            self._warm(data, z, z, rows, cols)
            return self._ids(np.asarray(data[int(z), rows, cols]))
        data = self._data(t)
        depth = int((data.chunks or data.shape)[0])
        if depth > 1 and depth * self.height * self.width * data.dtype.itemsize <= SLAB_LIMIT:
            z0 = int(z) // depth * depth
            if self._slab is None or self._slab[:2] != (int(t), z0):
                z1 = min(self.depth, z0 + depth)
                self._warm(data, z0, z1 - 1, rows, cols)
                block = np.asarray(data[z0:z1, :self.height, :self.width])
                self._slab = (int(t), z0, block)
            return self._ids(self._slab[2][int(z) - z0])
        self._warm(data, z, z, rows, cols)
        return self._ids(np.asarray(data[int(z), :self.height, :self.width]))

    def __getitem__(self, index):
        if self.frames > 1:
            return _ImsFrame(self, int(index))
        return self.plane(0, int(index))

    def close(self):
        self._slab = None
        _close_file(self._file, self._stream)


class _ImsFrame:
    """One frame of a time series of label volumes: `[z]` is a plane."""

    def __init__(self, labels: ImsLabels, t: int):
        self._labels = labels
        self._t = int(t)
        self.shape = labels.shape[1:]
        self.dtype = labels.dtype

    def __getitem__(self, z):
        return self._labels.plane(self._t, int(z))


class ImsPlane:
    """An .ims mask's one plane as a 2-D label array, `[rows, cols]` -- the
    middle plane of frame 0 when there are more (the plane a Z-stack reads as
    everywhere a 2-D mask is wanted). `close()` closes the file."""

    def __init__(self, path, channel=None):
        self._labels = ImsLabels(path, channel)
        self._z = self._labels.depth // 2
        self.shape = (self._labels.height, self._labels.width)
        self.ndim = 2
        self.dtype = self._labels.dtype

    def __getitem__(self, index):
        rows, cols = index if isinstance(index, tuple) else (index, slice(None))
        return self._labels.plane(0, self._z, rows, cols)

    def __array__(self, dtype=None, copy=None):
        plane = self._labels.plane(0, self._z)
        return plane.astype(dtype) if dtype is not None else plane

    @property
    def oindex(self):
        labels, z = self._labels, self._z

        class _Rows:
            def __getitem__(self, index):
                rows, cols = (np.asarray(part) for part in index)
                if not rows.size or not cols.size:
                    return np.zeros((rows.size, cols.size), dtype=labels.dtype)
                top, left = int(rows.min()), int(cols.min())
                block = labels.plane(0, z, slice(top, int(rows.max()) + 1),
                                     slice(left, int(cols.max()) + 1))
                return block[np.ix_(rows - top, cols - left)]

        return _Rows()

    def close(self):
        self._labels.close()


def label_shape(path):
    """`(Z, Y, X)` or `(T, Z, Y, X)` when the .ims at `path` is a mask with
    depth or frames -- a label volume; None for a single plane, or for a file
    whose ids cannot be found (the conversion says why)."""
    try:
        labels = ImsLabels(path)
    except Exception:
        return None
    try:
        if labels.depth < 2 and labels.frames < 2:
            return None
        return labels.shape
    finally:
        labels.close()
