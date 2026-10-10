"""Decoded chunks of a volume, kept once for the whole process.

A stored chunk -- an Imaris chunk 64 planes deep, a zarr chunk, a brick -- is
decompressed whole to give up any voxel of it. A plane step, a section, a 3-D
level, a window scan and a projection each used to decompress the same chunks
again, and the readers that hold a lock across the decompression (h5py's
global lock, a file's own) did it one chunk at a time for every thread.

`ChunkCache` keeps decoded chunks by what they are --
`(source, level, t, channel, chunk coordinates)` -- in one byte-budgeted LRU
(`PLEXORA_CHUNK_CACHE_MB`, default a quarter of the machine's memory up to
2 GB), read-only, with ONE load per chunk however many threads want it at
once. A reader supplies `load_many(keys) -> {key: array}`; the cache asks it
only for what nobody holds or is loading. `DECODE_POOL` is where readers
decompress, outside their locks (`PLEXORA_DECODE_THREADS`, default up to 8):
zlib and libdeflate release the GIL.

A read too big to be worth keeping (a whole-stack scan, a projection over
every plane) is decoded and handed back without evicting what a viewer is
using (`keep` false): such reads see the cache and add nothing to it.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _default_budget_mb() -> int:
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        total = 8 * 2**30
    return int(max(256, min(2048, total // 4 // 2**20)))


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
        thread has under way, or from `load_many(missing)` here."""
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
        return found

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
                    "waits": self.waits, "loading": len(self._loading)}


CACHE = ChunkCache(_env_int("PLEXORA_CHUNK_CACHE_MB", _default_budget_mb()) * 2**20)
DECODE_POOL = ThreadPoolExecutor(
    max_workers=max(1, _env_int("PLEXORA_DECODE_THREADS", min(8, os.cpu_count() or 4))),
    thread_name_prefix="plexora-decode")


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
