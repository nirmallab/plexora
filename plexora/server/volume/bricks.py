"""A z-chunked copy of a stack, for every read that runs across planes.

Most stacks are stored plane by plane: a TIFF page, a DICOM focal plane, an
OME-Zarr chunk one plane deep. Reading one plane is then cheap -- which is what
the plane tiles do -- but a projection, an XZ/YZ section, the 3-D view's whole
level and the brightness scan all read *across* planes, and pay one decode per
plane for every rectangle: a section row from a 1937-plane light-sheet stack is
1937 chunk decodes to use one row of each.

So a stack read plane-major gets a second copy beside the project, in bricks
32 planes deep (`BRICK`, zarr v3, sharded `SHARD` so a level is a few files
rather than thousands), in two parts:

- **The coarse levels**, built once in the background when a large stack is
  first opened: every level from the finest whose whole stack of one channel
  fits `LEVEL_BUDGET` down to the coarsest. Small (a few hundred MB per channel
  at most) and enough for every zoomed-out view, the 3-D view and the scan.
- **A cache of finer levels**, built on demand for an area
  (`prepare`, `POST /volume/<ds>/prepare`): what a section cut through a
  zoomed-in view, or the 3-D view of it, reads. Budgeted (`CACHE_BUDGET`),
  least-recently-used shards go first.

- **Pooled levels**, built after the coarse levels: a level the 3-D view reads
  max-pooled in z (`model.pool_for`) kept pooled, as an array of its own named
  `"{level}p{pool}"` -- the light-sheet stack's level 2 is 485 pooled planes
  instead of 1937 read and reduced on every request. Only `read_pooled` reads
  them; every other read still gets the source's own pixels. Built for a stack
  chunked across z as well (`worth_pooling`), which needs no copy otherwise.

Reads come through `BrickedSource`, which stands in for the stack's own source:
plane reads go to the source (its own tiles are the right shape for them),
reads across planes go to the bricks when they hold that part, and to the
source otherwise. The bricks are copies of the source's own levels, never
resampled, so a pixel read either way is the same pixel.

Kept honest by a manifest (`build.json`) that says which levels and shards are
complete and which source they were copied from (its size and modification
time): a shard is read only once its write finished, and a changed source
throws the copy away.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from plexora.server.volume import chunk_cache

FORMAT_VERSION = 2
STORE_NAME = "volume_v1"
#: One chunk: planes, rows, columns.
BRICK = (32, 128, 128)
#: One shard file: the same planes over this many pixels square. Small enough
#: that an area is prepared in pieces near its own size -- a stack 1937 planes
#: deep makes one 1024-pixel shard column 4 GB -- and large enough that a
#: coarse level is tens of files, not thousands.
SHARD_XY = 256
#: The finest coarse level is the finest whose stack of one channel fits this.
LEVEL_BUDGET = int(os.environ.get("PLEXORA_VOLUME_LEVEL_BUDGET", 256 * 2**20))
#: The most the coarse copy holds of a stack none of whose levels fits
#: LEVEL_BUDGET (its coarsest level, a channel at a time).
FALLBACK_BUDGET = 4 * LEVEL_BUDGET
#: What the cache of finer levels may hold on disk.
CACHE_BUDGET = int(os.environ.get("PLEXORA_ROI_BRICK_BUDGET", 4 * 2**30))
#: At most this much of a level is prepared by one request.
PREPARE_BUDGET = 1 * 2**30
#: Stacks smaller than this (voxels per channel at full resolution) read fast
#: enough from their own planes, and are never copied.
MIN_VOXELS = 2**28
#: A source whose own chunks span at least this many planes is already
#: z-chunked (Imaris, most OME-Zarr written for 3-D): nothing to gain.
Z_CHUNKED = 8
ENV_SWITCH = "PLEXORA_VOLUME_BRICKS"

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "1") != "0"


def _lock_for(path) -> threading.Lock:
    key = str(path)
    with _LOCKS_LOCK:
        return _LOCKS.setdefault(key, threading.Lock())


def source_key(path) -> str | None:
    """Size and modification time of the stack's file (or folder's newest)."""
    try:
        p = Path(path)
        if p.is_dir():
            newest = max((f.stat().st_mtime_ns for f in p.rglob("*") if f.is_file()),
                         default=p.stat().st_mtime_ns)
            return f"dir:{newest}"
        info = p.stat()
        return f"{info.st_size}:{info.st_mtime_ns}"
    except OSError:
        return None


def z_decode(source) -> int:
    """Planes one of the source's own reads decodes at once."""
    return max(1, int(getattr(source, "z_decode", getattr(source, "z_chunk", 1)) or 1))


def worth_it(source) -> bool:
    """Whether a stack should be copied into bricks at all."""
    volume = source.volume
    if not enabled() or volume.depth < 8 or volume.is_planar:
        return False
    if z_decode(source) >= Z_CHUNKED:
        return False
    return volume.depth * volume.height * volume.width >= MIN_VOXELS


def pooled_levels(volume) -> list[tuple[int, int]]:
    """`(level, pool)` of each level the store keeps max-pooled in z: every
    level that keeps every plane, whose pool (`model.pool_for`) is at least 2,
    and whose pooled stack of one channel fits LEVEL_BUDGET. Needs a stated z
    step: without one nothing is pooled."""
    from plexora.server.volume.model import pool_for

    if volume.is_planar or volume.depth < 8 or not volume.spacing.z.known:
        return []
    itemsize = np.dtype(volume.dtype).itemsize
    out = []
    for info in volume.levels:
        if info.depth is not None and info.depth < volume.depth:
            continue
        pool = pool_for(volume, info.index)
        if pool < 2:
            continue
        if -(-volume.depth // pool) * info.height * info.width * itemsize <= LEVEL_BUDGET:
            out.append((info.index, pool))
    return out


def pooled_name(level: int, pool: int) -> str:
    return f"{int(level)}p{int(pool)}"


def worth_pooling(source) -> bool:
    """Whether a stack should get pooled levels: a large deep local stack with
    a stated z step, read plane-major or chunked across z alike."""
    volume = source.volume
    if not enabled() or volume.depth < 8 or volume.is_planar:
        return False
    if volume.depth * volume.height * volume.width < MIN_VOXELS:
        return False
    return bool(pooled_levels(volume))


def coarse_levels(volume) -> list[int]:
    """The levels the coarse copy holds: from the finest whose stack of one
    channel fits LEVEL_BUDGET down to the coarsest.

    When none fits, the coarsest alone if it is at most FALLBACK_BUDGET, and
    nothing past that: a single-level 4096 x 4096 x 200 stack would otherwise
    be copied whole, 6.7 GB a channel, for views that cannot draw it anyway.
    Such a stack is given a pyramid at import (`image_pyramid.pyramid_gaps`);
    areas of it are still prepared on demand (`prepare`)."""
    itemsize = np.dtype(volume.dtype).itemsize
    for info in volume.levels:
        if volume.depth * info.height * info.width * itemsize <= LEVEL_BUDGET:
            return [level.index for level in volume.levels if level.index >= info.index]
    coarsest = volume.levels[-1]
    if volume.depth * coarsest.height * coarsest.width * itemsize <= FALLBACK_BUDGET:
        return [coarsest.index]
    return []


def layout(volume, level: int):
    """(chunks, shards) of a level's brick array, (t, c, z, y, x) each."""
    info = volume.levels[level]
    chunks = (1, 1, min(BRICK[0], volume.depth), min(BRICK[1], info.height),
              min(BRICK[2], info.width))
    sy = -(-max(chunks[3], min(SHARD_XY, info.height)) // chunks[3]) * chunks[3]
    sx = -(-max(chunks[4], min(SHARD_XY, info.width)) // chunks[4]) * chunks[4]
    return chunks, (1, 1, chunks[2], sy, sx)


def prepare_cost(volume, level: int, box, channels: int) -> int:
    """Bytes `prepare` would write for `box` (that level's pixels) at
    `level`: whole shards, through every plane, for `channels` channels."""
    _chunks, (_t, _c, sz, sy, sx) = layout(volume, level)
    x0, y0, x1, y1 = box
    rows = (max(0, int(y1) - 1) // sy) - (max(0, int(y0)) // sy) + 1
    cols = (max(0, int(x1) - 1) // sx) - (max(0, int(x0)) // sx) + 1
    blocks = -(-volume.depth // sz)
    return int(blocks * rows * cols * sz * sy * sx * channels
               * np.dtype(volume.dtype).itemsize)


# -- the store ---------------------------------------------------------------------


class Store:
    """The bricks of one stack: a zarr v3 group and its manifest."""

    def __init__(self, path, volume, key):
        self.path = Path(path)
        self.volume = volume
        self.key = key
        self.lock = _lock_for(self.path)
        self._arrays: dict[int, object] = {}
        self._manifest = None
        self._touched: dict[str, float] = {}
        #: "level/shard" -> readers using it now: eviction passes these by.
        self._pins: dict[str, int] = {}
        #: What the decoded-chunk cache knows this store's bricks by.
        self.cache_id = ("bricks", str(self.path), key)

    # -- manifest ------------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.path / "build.json"

    def manifest(self) -> dict:
        if self._manifest is None:
            try:
                raw = json.loads(self.manifest_path.read_text())
            except (OSError, ValueError):
                raw = None
            if (not isinstance(raw, dict) or raw.get("version") != FORMAT_VERSION
                    or raw.get("source_key") != self.key):
                raw = {"version": FORMAT_VERSION, "source_key": self.key,
                       "coarse": {}, "shards": {}, "bytes": 0}
            raw.setdefault("pooled", {})
            self._manifest = raw
        return self._manifest

    def _save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        part = self.manifest_path.with_suffix(".json.part")
        part.write_text(json.dumps(self._manifest))
        os.replace(part, self.manifest_path)

    def reset_if_stale(self) -> None:
        """A store copied from another version of the source is thrown away."""
        with self.lock:
            try:
                raw = json.loads(self.manifest_path.read_text())
            except (OSError, ValueError):
                raw = None
            if raw is not None and (raw.get("version") != FORMAT_VERSION
                                    or raw.get("source_key") != self.key):
                shutil.rmtree(self.path, ignore_errors=True)
                self._arrays.clear()
                self._manifest = None

    # -- arrays ----------------------------------------------------------------

    def array(self, level: int, create: bool = False):
        array = self._arrays.get(level)
        if array is not None:
            return array
        import zarr

        name = str(level)
        try:
            group = zarr.open_group(str(self.path), mode="a", zarr_format=3)
        except Exception:
            if not create:
                return None
            raise
        if name in group:
            array = group[name]
        elif create:
            from zarr.codecs import BloscCodec

            volume = self.volume
            info = volume.levels[level]
            shape = (volume.frames, volume.channels, volume.depth, info.height, info.width)
            chunks, shards = layout(volume, level)
            array = group.create_array(
                name, shape=shape, dtype=volume.dtype, chunks=chunks, shards=shards,
                compressors=BloscCodec(cname="zstd", clevel=3, shuffle="bitshuffle"),
                fill_value=0)
        else:
            return None
        self._arrays[level] = array
        return array

    def pooled_array(self, level: int, pool: int, create: bool = False):
        """Level `level` max-pooled `pool` planes at a time, (T, C, ceil(Z /
        pool), H, W): a separately named array, read only by `read_pooled`."""
        name = pooled_name(level, pool)
        array = self._arrays.get(name)
        if array is not None:
            return array
        import zarr

        try:
            group = zarr.open_group(str(self.path), mode="a", zarr_format=3)
        except Exception:
            if not create:
                return None
            raise
        if name in group:
            array = group[name]
        elif create:
            from zarr.codecs import BloscCodec

            volume = self.volume
            info = volume.levels[level]
            depth = -(-volume.depth // pool)
            chunks = (1, 1, min(BRICK[0], depth), min(BRICK[1], info.height),
                      min(BRICK[2], info.width))
            sy = -(-max(chunks[3], min(SHARD_XY, info.height)) // chunks[3]) * chunks[3]
            sx = -(-max(chunks[4], min(SHARD_XY, info.width)) // chunks[4]) * chunks[4]
            array = group.create_array(
                name, shape=(volume.frames, volume.channels, depth, info.height, info.width),
                dtype=volume.dtype, chunks=chunks, shards=(1, 1, chunks[2], sy, sx),
                compressors=BloscCodec(cname="zstd", clevel=3, shuffle="bitshuffle"),
                fill_value=0)
        else:
            return None
        self._arrays[name] = array
        return array

    def has_pooled(self, level: int, pool: int) -> bool:
        return bool(self.manifest()["pooled"].get(pooled_name(level, pool)))

    def pooled_held(self, level: int) -> list[int]:
        """The pools level `level` is held at, finished ones only, largest first."""
        out = []
        with self.lock:
            held = list(self.manifest()["pooled"].items())
        for name, done in held:
            stem, _, pool = name.partition("p")
            if done and stem == str(int(level)) and pool.isdigit():
                out.append(int(pool))
        return sorted(out, reverse=True)

    def shard_shape(self, level: int) -> tuple[int, int, int]:
        _chunks, shards = layout(self.volume, level)
        return int(shards[2]), int(shards[3]), int(shards[4])

    # -- what is held ---------------------------------------------------------------

    def has_coarse(self, level: int) -> bool:
        return bool(self.manifest()["coarse"].get(str(level)))

    def coarse_complete(self) -> bool:
        return all(self.has_coarse(level) for level in coarse_levels(self.volume))

    def pooled_complete(self) -> bool:
        return all(self.has_pooled(level, pool) for level, pool in pooled_levels(self.volume))

    @staticmethod
    def shard_id(t, c, zb, sy, sx) -> str:
        return f"{t}.{c}.{zb}.{sy}.{sx}"

    def shards_for(self, level, t, channel, z0, z1, rows, cols):
        """The shard ids covering planes z0..z1 of a rectangle at a level."""
        sz, sy, sx = self.shard_shape(level)
        y0, y1 = rows.start or 0, rows.stop
        x0, x1 = cols.start or 0, cols.stop
        return [self.shard_id(t, channel, zb, by, bx)
                for zb in range(z0 // sz, z1 // sz + 1)
                for by in range(y0 // sy, max(y0, y1 - 1) // sy + 1)
                for bx in range(x0 // sx, max(x0, x1 - 1) // sx + 1)]

    def holds(self, level, t, channel, z0, z1, rows, cols) -> bool:
        if self.has_coarse(level):
            return True
        held = self.manifest()["shards"].get(str(level))
        if not held:
            return False
        wanted = self.shards_for(level, t, channel, z0, z1, rows, cols)
        if not all(shard in held for shard in wanted):
            return False
        now = time.time()
        for shard in wanted:
            self._touched[f"{level}/{shard}"] = now
        return True

    def read(self, level, t, channel, z0, z1, rows, cols, *, pool=None) -> np.ndarray:
        """Planes z0..z1 of a rectangle, brick by brick through the
        decoded-chunk cache (volume/chunk_cache.py): a brick decoded for one
        section tile is not decoded again for its neighbour. `pool`: of the
        level's pooled array, z0..z1 then being pooled planes."""
        array = self.array(level) if pool is None else self.pooled_array(level, pool)
        name = int(level) if pool is None else pooled_name(level, pool)
        _t, _c, cz, cy, cx = (int(v) for v in array.chunks)
        dtype = np.dtype(array.dtype)
        keys = [(self.cache_id, name, int(t), int(channel), zc, yc, xc)
                for zc in chunk_cache.chunk_ranges(z0, z1 + 1, cz)
                for yc in chunk_cache.chunk_ranges(rows.start, rows.stop, cy)
                for xc in chunk_cache.chunk_ranges(cols.start, cols.stop, cx)]

        def load_many(missing):
            # One zarr read over the missing bricks' extent (zarr decodes its
            # chunks in parallel), cut back into bricks.
            zcs = [key[4] for key in missing]
            ycs = [key[5] for key in missing]
            xcs = [key[6] for key in missing]
            bz0, by0, bx0 = min(zcs) * cz, min(ycs) * cy, min(xcs) * cx
            bz1 = min(array.shape[2], (max(zcs) + 1) * cz)
            by1 = min(array.shape[3], (max(ycs) + 1) * cy)
            bx1 = min(array.shape[4], (max(xcs) + 1) * cx)
            block = np.asarray(array[t, channel, bz0:bz1, by0:by1, bx0:bx1])
            out = {}
            for key in missing:
                zc, yc, xc = key[4:7]
                piece = np.zeros((cz, cy, cx), dtype=dtype)
                part = block[zc * cz - bz0:(zc + 1) * cz - bz0, yc * cy - by0:(yc + 1) * cy - by0,
                             xc * cx - bx0:(xc + 1) * cx - bx0]
                piece[:part.shape[0], :part.shape[1], :part.shape[2]] = part
                out[key] = piece
            return out

        nbytes = len(keys) * cz * cy * cx * dtype.itemsize
        chunks = chunk_cache.CACHE.fetch(keys, load_many, keep=chunk_cache.keep_for(nbytes))
        return chunk_cache.assemble(((z0, z1 + 1), (rows.start, rows.stop), (cols.start, cols.stop)),
                                    (cz, cy, cx), chunks, dtype, coords_of=lambda key: key[4:7])

    def pin(self, names) -> None:
        """Keep shards `names` ("level/shard") from eviction (lock held)."""
        for name in names:
            self._pins[name] = self._pins.get(name, 0) + 1

    def unpin(self, names) -> None:
        with self.lock:
            for name in names:
                left = self._pins.get(name, 0) - 1
                if left > 0:
                    self._pins[name] = left
                else:
                    self._pins.pop(name, None)

    def pieces(self, level, t, channel, z0, z1, rows, cols):
        """The rectangle cut on shard lines: `[(rows, cols, held)]`, `held`
        when every plane z0..z1 of that piece is in the cache. Empty when the
        level holds nothing for this channel (the caller reads the source)."""
        held = self.manifest()["shards"].get(str(level))
        if not held:
            return []
        sz, sy, sx = self.shard_shape(level)
        zbs = range(z0 // sz, z1 // sz + 1)
        out, any_held = [], False
        for by in range(rows.start // sy, max(rows.start, rows.stop - 1) // sy + 1):
            r = slice(max(rows.start, by * sy), min(rows.stop, (by + 1) * sy))
            for bx in range(cols.start // sx, max(cols.start, cols.stop - 1) // sx + 1):
                c = slice(max(cols.start, bx * sx), min(cols.stop, (bx + 1) * sx))
                ok = all(self.shard_id(t, channel, zb, by, bx) in held for zb in zbs)
                any_held |= ok
                out.append((r, c, ok))
        if not any_held:
            return []
        now = time.time()
        for r, c, ok in out:
            if ok:
                for zb in zbs:
                    shard = self.shard_id(t, channel, zb, r.start // sy, c.start // sx)
                    self._touched[f"{level}/{shard}"] = now
        return out


# -- building --------------------------------------------------------------------------


def store_for(project_name, source) -> Store:
    from plexora import paths

    image = getattr(source, "path", None)
    return Store(paths.derived_root(project_name) / STORE_NAME, source.volume,
                 source_key(image) if image else None)


def _free_bytes(directory: Path):
    from plexora.server.utils.image_pyramid import _free_bytes as free

    return free(directory)


def build_coarse(store: Store, source, report=None, cancel=None, *, copy=True) -> None:
    """Copy every coarse level (unless `copy` is False: a stack chunked across
    z needs no copy), a block of BRICK[0] planes of one channel at a time,
    then build the pooled levels (`pooled_levels`) the same way. Resumable: a
    finished block is never copied again."""
    volume = source.volume
    levels = coarse_levels(volume) if copy else []
    pooled = pooled_levels(volume)
    itemsize = np.dtype(volume.dtype).itemsize
    need = sum(volume.frames * volume.channels * volume.depth
               * volume.levels[level].height * volume.levels[level].width * itemsize
               for level in levels)
    need += sum(volume.frames * volume.channels * -(-volume.depth // pool)
                * volume.levels[level].height * volume.levels[level].width * itemsize
                for level, pool in pooled)
    free = _free_bytes(store.path.parent)
    if free is not None and free < need:
        raise OSError(f"Not enough free space for the stack's 3-D copy: about "
                      f"{need / 2**20:.0f} MB is needed and {free / 2**20:.0f} MB is free.")
    units = [(level, t, c, z0) for level in levels for t in range(volume.frames)
             for c in range(volume.channels) for z0 in range(0, volume.depth, BRICK[0])]
    pooled_units = [(level, pool, t, c, p0) for level, pool in pooled
                    for t in range(volume.frames) for c in range(volume.channels)
                    for p0 in range(0, -(-volume.depth // pool), BRICK[0])]
    total = len(units) + len(pooled_units)
    manifest = store.manifest()
    done = set(manifest.setdefault("units", []))
    for count, (level, t, c, z0) in enumerate(units):
        if cancel is not None and cancel.is_set():
            return
        unit = f"{level}.{t}.{c}.{z0}"
        if unit not in done:
            info = volume.levels[level]
            z1 = min(volume.depth, z0 + BRICK[0]) - 1
            block = source.read_block(level, t, c, z0, z1, slice(0, info.height),
                                      slice(0, info.width))
            array = store.array(level, create=True)
            array[t, c, z0:z1 + 1] = np.asarray(block, dtype=volume.dtype)
            with store.lock:
                manifest["units"].append(unit)
                done.add(unit)
                if all(f"{level}.{tt}.{cc}.{zz}" in done for tt in range(volume.frames)
                       for cc in range(volume.channels)
                       for zz in range(0, volume.depth, BRICK[0])):
                    manifest["coarse"][str(level)] = True
                store._save()
        if report is not None:
            report(100.0 * (count + 1) / total, f"Copying the stack in 3-D: level {level}")
    # Read through the copy itself, so a level it already holds is pooled
    # from bricks rather than from the stack.
    reader = BrickedSource(source, store) if levels else source
    for count, (level, pool, t, c, p0) in enumerate(pooled_units, start=len(units)):
        if cancel is not None and cancel.is_set():
            return
        name = pooled_name(level, pool)
        unit = f"{name}.{t}.{c}.{p0}"
        if unit not in done:
            info = volume.levels[level]
            depth = -(-volume.depth // pool)
            p1 = min(depth, p0 + BRICK[0])
            block = _pool_from_planes(reader, level, t, c, p0 * pool,
                                      min(volume.depth, p1 * pool) - 1, pool,
                                      slice(0, info.height), slice(0, info.width))
            array = store.pooled_array(level, pool, create=True)
            array[t, c, p0:p1] = np.asarray(block, dtype=volume.dtype)
            with store.lock:
                manifest["units"].append(unit)
                done.add(unit)
                if all(f"{name}.{tt}.{cc}.{pp}" in done for tt in range(volume.frames)
                       for cc in range(volume.channels)
                       for pp in range(0, depth, BRICK[0])):
                    manifest["pooled"][name] = True
                store._save()
        if report is not None:
            report(100.0 * (count + 1) / total, f"Pooling the stack in depth: level {level}")


def _pool_from_planes(source, level, t, channel, z0, z1, pool, rows, cols):
    """Planes z0..z1 max-pooled `pool` at a time, from the planes themselves
    (never from a pooled array: this is what builds one)."""
    from plexora.server.volume.model import read_pooled

    class _Planes:
        def __init__(self, inner):
            self._inner = inner
            self.volume = inner.volume

        def read_block(self, *args):
            return self._inner.read_block(*args)

    return read_pooled(_Planes(source), level, t, channel, z0, z1, pool, rows, cols)


def prepare(store: Store, source, level: int, box, *, channels=None, t=0, report=None,
            cancel=None) -> dict:
    """Bricks of `level` for the rectangle `box` (x0, y0, x1, y1 in that
    level's pixels) of every plane, for `channels` (default all). Whole shards;
    least-recently-used shards of other areas make room within CACHE_BUDGET.
    Returns {"shards": added, "bytes": written, "skipped": already held}."""
    volume = source.volume
    if store.has_coarse(level):
        return {"shards": 0, "bytes": 0, "skipped": 0}
    info = volume.levels[level]
    x0, y0, x1, y1 = (int(v) for v in box)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(info.width, x1), min(info.height, y1)
    if x1 <= x0 or y1 <= y0:
        return {"shards": 0, "bytes": 0, "skipped": 0}
    channels = list(range(volume.channels)) if channels is None else list(channels)
    sz, sy, sx = store.shard_shape(level)
    itemsize = np.dtype(volume.dtype).itemsize
    wanted = []
    for c in channels:
        for zb in range(0, -(-volume.depth // sz)):
            for by in range(y0 // sy, (y1 - 1) // sy + 1):
                for bx in range(x0 // sx, (x1 - 1) // sx + 1):
                    wanted.append((c, zb, by, bx))
    manifest = store.manifest()
    # {shard id: bytes written}, so eviction gives back what each one took.
    held = manifest["shards"].setdefault(str(level), {})
    todo = [w for w in wanted if store.shard_id(t, *w) not in held]
    cost = len(todo) * sz * sy * sx * itemsize
    if cost > PREPARE_BUDGET:
        raise ValueError(f"That area is {cost / 2**30:.1f} GB at this level; zoom in further "
                         f"or prepare a coarser level.")
    added = written = 0
    for count, (c, zb, by, bx) in enumerate(todo):
        if cancel is not None and cancel.is_set():
            break
        z0, z1 = zb * sz, min(volume.depth, (zb + 1) * sz) - 1
        rows = slice(by * sy, min(info.height, (by + 1) * sy))
        cols = slice(bx * sx, min(info.width, (bx + 1) * sx))
        block = source.read_block(level, t, c, z0, z1, rows, cols)
        array = store.array(level, create=True)
        array[t, c, z0:z1 + 1, rows, cols] = np.asarray(block, dtype=volume.dtype)
        size = int(np.asarray(block).nbytes)
        with store.lock:
            held[store.shard_id(t, c, zb, by, bx)] = size
            manifest["bytes"] = int(manifest.get("bytes", 0)) + size
            store._touched[f"{level}/{store.shard_id(t, c, zb, by, bx)}"] = time.time()
            _evict(store, keep={f"{level}/{store.shard_id(t, *w)}" for w in wanted})
            store._save()
        added += 1
        written += size
        if report is not None:
            report(100.0 * (count + 1) / max(1, len(todo)), "Preparing the area in 3-D")
    return {"shards": added, "bytes": written, "skipped": len(wanted) - len(todo)}


def _evict(store: Store, keep=()) -> None:
    """Drop least-recently-used cached shards until the cache fits its budget
    (called with the store's lock held). Removed from the manifest first, so a
    read never finds a shard whose file is going."""
    manifest = store.manifest()
    while int(manifest.get("bytes", 0)) > CACHE_BUDGET:
        candidates = [(store._touched.get(f"{level}/{shard}", 0.0), level, shard)
                      for level, shards in manifest["shards"].items() for shard in shards
                      if f"{level}/{shard}" not in keep
                      and not store._pins.get(f"{level}/{shard}")]
        if not candidates:
            return
        _, level, shard = min(candidates)
        size = manifest["shards"][level].pop(shard)
        t, c, zb, by, bx = shard.split(".")
        manifest["bytes"] = max(0, int(manifest["bytes"]) - int(size))
        target = store.path / level / "c" / t / c / zb / by / bx
        try:
            target.unlink()
        except OSError:
            pass


# -- reading ---------------------------------------------------------------------------


class BrickedSource:
    """A volume source whose reads across planes come from its bricks where
    they hold them (see module docstring). Everything else is the source's."""

    def __init__(self, source, store: Store, *, copies=True):
        self._source = source
        self.store = store
        self.volume = source.volume
        #: Whether areas of finer levels are copied on demand (`prepare`): not
        #: for a stack chunked across z, which has a store for its pooled
        #: levels only.
        self.copies = copies

    def __getattr__(self, name):
        return getattr(self._source, name)

    @property
    def base(self):
        return self._source

    def _held(self, level) -> bool:
        return self.store.has_coarse(level) or bool(
            self.store.manifest()["shards"].get(str(level)))

    def prefers_block_at(self, level) -> bool:
        """Blocks where the copy holds the level; the stack's own way elsewhere."""
        from plexora.server.volume.model import reads_blocks

        return True if self._held(level) else reads_blocks(self._source, level)

    def decode_chunk(self, level):
        """(z, y, x) of one decode at `level`: a brick where the copy holds it."""
        if self._held(level):
            return BRICK
        at = getattr(self._source, "decode_chunk", None)
        return at(level) if callable(at) else None

    def z_chunk_at(self, level) -> int:
        from plexora.server.volume.model import block_depth

        return BRICK[0] if self._held(level) else block_depth(self._source, level)

    def plane_pyramid(self, t=0, z=None):
        return self._source.plane_pyramid(t, z)

    def level_array(self, t, z, level):
        return self._source.level_array(t, z, level)

    def read_plane(self, level, t, z, channel, rows, cols):
        return self._source.read_plane(level, t, z, channel, rows, cols)

    def level_shape(self, level):
        return self._source.level_shape(level)

    def read_block(self, level, t, channel, z0, z1, rows, cols):
        height, width = self.level_shape(level)
        rows = slice(*rows.indices(height)[:2]) if isinstance(rows, slice) else rows
        cols = slice(*cols.indices(width)[:2]) if isinstance(cols, slice) else cols
        if isinstance(rows, slice) and isinstance(cols, slice):
            # Checked under the store's lock and the shards used PINNED, then
            # read without it: eviction takes a shard out of the manifest and
            # then deletes its file, and a read between the two would get the
            # fill value -- zeros -- for real pixels; a pinned shard is passed
            # over. A coarse level is never evicted and needs no pin. Holding
            # the lock across the read itself serialized every brick read in
            # the process, sections and 3-D levels and scans alike.
            store = self.store
            with store.lock:
                whole = store.holds(level, t, channel, z0, z1, rows, cols)
                pieces = [] if whole else store.pieces(level, t, channel, z0, z1, rows, cols)
                pins = []
                if (whole or pieces) and not store.has_coarse(level):
                    pins = [f"{level}/{shard}"
                            for shard in store.shards_for(level, t, channel, z0, z1, rows, cols)
                            if shard in store.manifest()["shards"].get(str(level), {})]
                    store.pin(pins)
            try:
                if whole:
                    return store.read(level, t, channel, z0, z1, rows, cols)
                held = {(p[0].start, p[1].start): store.read(level, t, channel, z0, z1, p[0], p[1])
                        for p in pieces if p[2]}
            finally:
                if pins:
                    store.unpin(pins)
            if pieces:
                # Part of it prepared (a section tile wider than the area on
                # screen): those pieces from the bricks, the rest from the stack.
                out = np.empty((z1 - z0 + 1, rows.stop - rows.start, cols.stop - cols.start),
                               dtype=self.volume.dtype)
                for r, c, ok in pieces:
                    block = (held[(r.start, c.start)] if ok else
                             self._source.read_block(level, t, channel, z0, z1, r, c))
                    out[:, r.start - rows.start:r.stop - rows.start,
                        c.start - cols.start:c.stop - cols.start] = block
                return out
        return self._source.read_block(level, t, channel, z0, z1, rows, cols)

    def read_pooled(self, level, t, channel, z0, z1, pool, rows, cols):
        """Planes z0..z1 of `level` max-pooled `pool` at a time, from a pooled
        array the store holds -- one at `pool`, or at a pool `pool` is a
        multiple of, pooled further -- or None (the caller pools the planes).
        Only for a span on whole pools: it starts on one and ends on one or
        at the stack's last plane, so a stored pool never runs past it."""
        from plexora.server.volume.model import stored_depth

        volume = self.volume
        if stored_depth(volume, level) < volume.depth or pool < 2 or z0 % pool:
            return None
        end = int(z1) + 1
        if end % pool and end != volume.depth:
            return None
        if not (isinstance(rows, slice) and isinstance(cols, slice)):
            return None
        height, width = self.level_shape(level)
        rows = slice(*rows.indices(height)[:2])
        cols = slice(*cols.indices(width)[:2])
        store = self.store
        for held in store.pooled_held(level):
            if pool % held:
                continue
            q0, q1 = z0 // held, -(-end // held) - 1
            block = store.read(level, t, channel, q0, q1, rows, cols, pool=held)
            factor = pool // held
            if factor == 1:
                return block
            count = -(-(end - z0) // pool)
            out = np.empty((count,) + block.shape[1:], dtype=block.dtype)
            for k in range(count):
                np.max(block[k * factor:(k + 1) * factor], axis=0, out=out[k])
            return out
        return None

    def close(self):
        self._source.close()


def adopt(source, store: Store, *, copies=True):
    """`source`, reading through `store` once it holds anything."""
    if isinstance(source, BrickedSource):
        return source
    return BrickedSource(source, store, copies=copies)


def state(store: Store | None, source=None) -> dict:
    """What `/volume/<ds>/info` and the Depth card say about the copy."""
    if store is None:
        return {"state": "none"}
    manifest = store.manifest()
    copies = getattr(source, "copies", True) if source is not None else True
    levels = coarse_levels(store.volume) if copies else []
    held = [level for level in levels if manifest["coarse"].get(str(level))]
    pooled = [pooled_name(level, pool) for level, pool in pooled_levels(store.volume)]
    pooled_ready = [name for name in pooled if manifest["pooled"].get(name)]
    complete = len(held) == len(levels) and len(pooled_ready) == len(pooled)
    return {"state": "ready" if complete else "partial" if held or pooled_ready else "none",
            "coarse_levels": levels, "coarse_ready": held,
            "pooled_levels": pooled, "pooled_ready": pooled_ready,
            "cache_bytes": int(manifest.get("bytes", 0)), "cache_budget": CACHE_BUDGET,
            "cached_levels": sorted(int(level) for level, shards
                                    in manifest.get("shards", {}).items() if shards)}
