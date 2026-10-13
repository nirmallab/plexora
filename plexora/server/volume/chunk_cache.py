"""Decoded chunks of a volume, kept once for the whole process.

A stored chunk -- an Imaris chunk 64 planes deep, a zarr chunk, a brick -- is
decompressed whole to give up any voxel of it. A plane step, a section, a 3-D
level, a window scan and a projection each used to decompress the same chunks
again, and the readers that hold a lock across the decompression (h5py's
global lock, a file's own) did it one chunk at a time for every thread.

`ChunkCache` keeps decoded chunks by what they are --
`(source, level, t, channel, chunk coordinates)` -- in one byte-budgeted LRU
(`PLEXORA_CHUNK_CACHE_MB`, default an eighth of the memory this process was
given, 256 MB to 8 GB), read-only, with ONE load per chunk however many
threads want it at once. A reader supplies `load_many(keys) -> {key: array}`;
the cache asks it only for what nobody holds or is loading. `DECODE_POOL` is
where readers read and decompress, outside their locks
(`PLEXORA_DECODE_THREADS`, default twice the allocated cores, 4 to 32): zlib,
libdeflate and a file read all release the GIL, and a read from a cluster's
network filesystem spends most of its time waiting rather than computing.

Sized from the ALLOCATION, not the machine (plexora/_resources.py): a 64 GB
job on a cluster node of several hundred must not plan for the node's memory,
and an 8-plane-deep Imaris chunk under every 256 px of a plane means a
viewport of four channels holds about 750 MB of chunks per block of planes --
2 GB, the old ceiling, held barely two blocks.

A read too big to be worth keeping (a whole-stack scan, a projection over
every plane) is decoded and handed back without evicting what a viewer is
using (`keep` false): such reads see the cache and add nothing to it.

BACKGROUND WORK HAS ITS OWN LANE. A window scan, a plane prefetch and a
node's warm-up read through `background()`, which sends their chunks to
`BACKGROUND_POOL` (a quarter of the threads) instead of `DECODE_POOL`: a tile
somebody is waiting for never queues behind work nobody is.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter

import numpy as np

from plexora.telemetry import performance as _perf


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _allocated_cpus() -> int:
    try:
        from plexora._resources import allocated_cpus

        return allocated_cpus()
    except Exception:  # noqa: BLE001 -- sizing only; never fatal
        return os.cpu_count() or 4


def _default_budget_mb() -> int:
    total = None
    try:
        from plexora._resources import allocated_memory_bytes

        total = allocated_memory_bytes()
    except Exception:  # noqa: BLE001 -- sizing only; never fatal
        total = None
    if not total:
        total = 8 * 2**30
    return int(max(256, min(8192, total // 8 // 2**20)))


def _default_decode_threads() -> int:
    return max(4, min(32, 2 * _allocated_cpus()))


class ChunkCache:
    def __init__(self, budget_bytes: int):
        self.budget = int(budget_bytes)
        self._entries: OrderedDict = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self._loading: dict = {}
        self.hits = 0
        self.misses = 0
        self.loads = 0
        self.waits = 0

    def get(self, key):
        with self._lock:
            array = self._entries.get(key)
            if array is not None:
                self._entries.move_to_end(key)
                self.hits += 1
            return array

    def put(self, key, array) -> None:
        array = np.asarray(array)
        array.setflags(write=False)
        with self._lock:
            self._put(key, array)

    def _put(self, key, array):
        if key in self._entries:
            return
        if array.nbytes > self.budget:
            return
        self._entries[key] = array
        self._bytes += array.nbytes
        while self._bytes > self.budget and self._entries:
            _, dropped = self._entries.popitem(last=False)
            self._bytes -= dropped.nbytes

    def fetch(self, keys, load_many, *, keep: bool = True) -> dict:
        """`{key: array}` for every key: from the cache, from a load another
        thread has under way, or from `load_many(missing)` here.

        On a tile's thread the time is the read's `chunk` part (`Server-
        Timing`), a hit when every chunk was held and a miss when any had to
        be loaded or waited for."""
        phases = _perf.current()
        if phases is None:
            return self._fetch(keys, load_many, keep)[0]
        started = perf_counter()
        found, cold = self._fetch(keys, load_many, keep)
        _perf.part("chunk", (perf_counter() - started) * 1000.0, phases)
        if cold or phases.notes.get("chunk") != "miss":
            phases.notes["chunk"] = "miss" if cold else "hit"
        return found

    def _fetch(self, keys, load_many, keep):
        """`(found, cold)`: `fetch`'s answer, and whether any of it was not
        already held."""
        keys = list(dict.fromkeys(keys))
        found = {}
        mine = []
        theirs = {}
        with self._lock:
            for key in keys:
                array = self._entries.get(key)
                if array is not None:
                    self._entries.move_to_end(key)
                    self.hits += 1
                    found[key] = array
                elif key in self._loading:
                    theirs[key] = self._loading[key]
                else:
                    flight = {"event": threading.Event(), "array": None, "error": None}
                    self._loading[key] = flight
                    mine.append((key, flight))
            self.misses += len(mine)
        if mine:
            try:
                loaded = load_many([key for key, _ in mine]) or {}
                self.loads += 1
            except BaseException as error:
                with self._lock:
                    for key, flight in mine:
                        flight["error"] = error
                        self._loading.pop(key, None)
                        flight["event"].set()
                raise
            with self._lock:
                for key, flight in mine:
                    array = loaded.get(key)
                    if array is not None:
                        array = np.asarray(array)
                        array.setflags(write=False)
                        if keep:
                            self._put(key, array)
                    flight["array"] = array
                    self._loading.pop(key, None)
                    flight["event"].set()
                    found[key] = array
        for key, flight in theirs.items():
            self.waits += 1
            flight["event"].wait()
            if flight["error"] is not None:
                raise flight["error"]
            found[key] = flight["array"]
        return found, bool(mine or theirs)

    def forget(self, source_id) -> None:
        """Drop every chunk of `source_id` (a source closed or found changed)."""
        with self._lock:
            for key in [k for k in self._entries if k and k[0] == source_id]:
                self._bytes -= self._entries.pop(key).nbytes

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._entries), "bytes": self._bytes, "budget": self.budget,
                    "hits": self.hits, "misses": self.misses, "loads": self.loads,
                    "waits": self.waits, "loading": len(self._loading),
                    "decode_threads": DECODE_THREADS,
                    "background_threads": BACKGROUND_THREADS}


CACHE = ChunkCache(_env_int("PLEXORA_CHUNK_CACHE_MB", _default_budget_mb()) * 2**20)
DECODE_THREADS = max(1, _env_int("PLEXORA_DECODE_THREADS", _default_decode_threads()))
DECODE_POOL = ThreadPoolExecutor(max_workers=DECODE_THREADS,
                                 thread_name_prefix="plexora-decode")
BACKGROUND_THREADS = max(2, DECODE_THREADS // 4)
BACKGROUND_POOL = ThreadPoolExecutor(max_workers=BACKGROUND_THREADS,
                                     thread_name_prefix="plexora-decode-bg")

_lane = threading.local()


@contextlib.contextmanager
def background():
    """Read at background priority for the duration: this thread's chunk
    loads go to `BACKGROUND_POOL`, behind nothing a viewer is waiting on."""
    previous = getattr(_lane, "background", False)
    _lane.background = True
    try:
        yield
    finally:
        _lane.background = previous


def in_background() -> bool:
    return bool(getattr(_lane, "background", False))


def pool() -> ThreadPoolExecutor:
    """Where this thread's chunk reads run (see `background`)."""
    return BACKGROUND_POOL if in_background() else DECODE_POOL


def keep_for(nbytes: int) -> bool:
    """Whether a read of `nbytes` of chunks should be kept: not one that would
    push a quarter of the cache out (a scan, a deep projection)."""
    return nbytes <= CACHE.budget // 4


def chunk_ranges(start: int, stop: int, size: int):
    """The chunk indices covering [start, stop) along an axis of chunk `size`."""
    if stop <= start:
        return range(0)
    return range(start // size, (stop - 1) // size + 1)


def assemble(region, chunk_shape, chunks: dict, dtype, coords_of=None):
    """The voxels of `region` -- `((z0, z1), (y0, y1), (x0, x1))`, half-open --
    out of decoded chunks keyed by `(zc, yc, xc)` (or `coords_of(key)`)."""
    (z0, z1), (y0, y1), (x0, x1) = region
    cz, cy, cx = chunk_shape
    out = np.empty((z1 - z0, y1 - y0, x1 - x0), dtype=dtype)
    for key, array in chunks.items():
        zc, yc, xc = coords_of(key) if coords_of else key
        bz, by, bx = zc * cz, yc * cy, xc * cx
        sz0, sz1 = max(z0, bz), min(z1, bz + cz)
        sy0, sy1 = max(y0, by), min(y1, by + cy)
        sx0, sx1 = max(x0, bx), min(x1, bx + cx)
        if sz1 <= sz0 or sy1 <= sy0 or sx1 <= sx0:
            continue
        out[sz0 - z0:sz1 - z0, sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = \
            array[sz0 - bz:sz1 - bz, sy0 - by:sy1 - by, sx0 - bx:sx1 - bx]
    return out


def read_through(source_id, name, t, channel, read_box, chunk_shape, region, dtype):
    """The voxels of `region` -- `((z0, z1), (y0, y1), (x0, x1))`, half-open --
    of one channel's (Z, Y, X) array, in whole chunks through `CACHE`.

    `read_box(z0, z1, y0, y1, x0, x1)` reads a half-open box of the array (one
    store read: zarr decodes its chunks in parallel). Chunks are keyed
    `(source_id, name, t, channel, zc, yc, xc)`, so a chunk decoded for one
    section tile is not decoded again for its neighbour. A read too large to
    keep (`keep_for`) bypasses the cache: cut into chunks and put back
    together, a whole level would be copied twice for nothing."""
    (z0, z1), (y0, y1), (x0, x1) = region
    cz, cy, cx = (max(1, int(v)) for v in chunk_shape)
    dtype = np.dtype(dtype)
    keys = [(source_id, name, int(t), int(channel), zc, yc, xc)
            for zc in chunk_ranges(z0, z1, cz)
            for yc in chunk_ranges(y0, y1, cy)
            for xc in chunk_ranges(x0, x1, cx)]
    nbytes = len(keys) * cz * cy * cx * dtype.itemsize
    if not keep_for(nbytes):
        return np.asarray(read_box(z0, z1, y0, y1, x0, x1))

    def load_many(missing):
        zcs = [key[4] for key in missing]
        ycs = [key[5] for key in missing]
        xcs = [key[6] for key in missing]
        bz0, by0, bx0 = min(zcs) * cz, min(ycs) * cy, min(xcs) * cx
        block = np.asarray(read_box(bz0, (max(zcs) + 1) * cz, by0, (max(ycs) + 1) * cy,
                                    bx0, (max(xcs) + 1) * cx))
        out = {}
        for key in missing:
            zc, yc, xc = key[4:7]
            piece = np.zeros((cz, cy, cx), dtype=dtype)
            part = block[zc * cz - bz0:(zc + 1) * cz - bz0, yc * cy - by0:(yc + 1) * cy - by0,
                         xc * cx - bx0:(xc + 1) * cx - bx0]
            piece[:part.shape[0], :part.shape[1], :part.shape[2]] = part
            out[key] = piece
        return out

    chunks = CACHE.fetch(keys, load_many)
    return assemble(region, (cz, cy, cx), chunks, dtype, coords_of=lambda key: key[4:7])
