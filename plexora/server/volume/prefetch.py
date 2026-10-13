"""Reading ahead of a hand moving through a stack's planes.

A stack stored in chunks many planes deep -- an Imaris file's are 8 or 64 --
is read a block of planes at a time: the first plane of a block decodes every
chunk under the tile, and the next seven are then in the chunk cache
(chunk_cache.py) and cost nothing. So stepping through the planes is smooth
inside a block and stalls at every boundary, where a whole block of chunks
under every tile on screen is read cold -- measured on a 70-channel stack on
HMS O2 at 70-290 ms a tile, every eighth step.

`PlanePrefetcher` reads the NEXT block -- in the direction the planes are
moving, both ways until a direction is known -- right after a tile is served,
on its own threads and in the chunk cache's background lane
(`chunk_cache.background`), so the step across the boundary finds the chunks
decoded. What it reads is exactly what a tile read would read (the caller's
`read(z)`), so it warms nothing a tile will not use.

Bounded every way that matters: newest first (where the hand is now, not
where it was), at most `QUEUE_MAX` waiting (older ones are dropped, the hand
has moved on), a block read once until it has had time to leave the cache,
and nothing at all where the chunks are one plane deep or the chunk cache is
too small to hold a block beside the one on screen (`MIN_BUDGET`).

PLANES DECODED ONE AT A TIME -- a TIFF stored plane by plane, a zarr store
chunked one plane deep -- have no block to read: nothing a plane's read
decodes holds the next plane, and no decoded cache keeps it. There the TILE
is made ahead instead (`after_plane_tile`, `TILE_PREFETCHER`): the same tile
of the next `TILE_AHEAD` planes in the direction of travel, read and encoded
into the tile cache the route answers from, under the key the route would
file it under -- so the browser's standby request for the next plane is a
cache hit. One worker, newest first, a tile the cache already holds skipped,
and never anything a request waits on.

A read ahead never reads ahead of itself: a tile read in the background lane
(`chunk_cache.background`) moves no hand and queues nothing.

`snapshot()` says how much of it landed: of the blocks a hand stepped into,
how many the read-ahead had read (`entered`, `warmed`); of the planes' tiles
served, how many were cache hits the tile read-ahead had made (`served`,
`warmed`).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from plexora.server.volume import chunk_cache

#: Reads waiting; past this the oldest are dropped.
QUEUE_MAX = 192
#: Blocks remembered as read, so a plane served again does not read again --
#: for this long, after which a block may have left the chunk cache and is
#: worth reading again.
DONE_MAX = 4096
DONE_SECONDS = 60.0
#: Tile regions whose last plane is remembered, for the direction of travel.
REGIONS_MAX = 4096
#: A chunk cache smaller than this has no room for a block ahead: reading one
#: would evict the plane on screen.
MIN_BUDGET = 1 << 30
#: Threads reading ahead. Each read fans its chunks out over the background
#: pool, so two keep it busy without crowding a tile anybody is waiting on.
WORKERS = 2
#: Planes whose tiles are made ahead of a tile served from a stack decoded a
#: plane at a time: the browser's standby planes are the next two.
TILE_AHEAD = 2
#: Tiles made ahead that are remembered, for the hit rate.
MADE_MAX = 8192


class PlanePrefetcher:
    def __init__(self, workers: int = WORKERS, min_budget: int = MIN_BUDGET):
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._jobs: OrderedDict = OrderedDict()
        self._done: OrderedDict = OrderedDict()
        self._last: OrderedDict = OrderedDict()
        #: (region, z) of the tiles `after_plane_tile` made, for the hit rate.
        self._made: OrderedDict = OrderedDict()
        self._threads: list[threading.Thread] = []
        self._workers = max(1, int(workers))
        #: Jobs a worker has taken and not finished (`drain`).
        self._running = 0
        self.min_budget = int(min_budget)
        self.stats = {"queued": 0, "read": 0, "dropped": 0, "failed": 0,
                      # blocks: stepped into, and of those read ahead first
                      "entered": 0, "warmed": 0,
                      # tiles: served, made ahead, skipped as already held,
                      # and served from the cache having been made ahead
                      "served": 0, "made": 0, "skipped": 0, "tiles_warmed": 0}

    def _moved(self, region, z):
        """The plane `region` was last served at, now that it is `z`."""
        with self._lock:
            last = self._last.pop(region, None)
            self._last[region] = z
            while len(self._last) > REGIONS_MAX:
                self._last.popitem(last=False)
        return last

    def after_tile(self, region, z: int, depth: int, block: int, read) -> list[int]:
        """A tile of `region` was served at plane `z`: queue the block ahead.

        `region` names one tile of one channel at one level of one source --
        anything hashable; `depth` is how many planes there are; `block` how
        many one stored chunk spans; `read(z)` reads the same tile at plane
        `z`. Returns the planes queued (for a test to read)."""
        z, depth, block = int(z), int(depth), int(block)
        if block <= 1 or depth <= block or chunk_cache.CACHE.budget < self.min_budget:
            return []
        if chunk_cache.in_background():
            return []
        last = self._moved(region, z)
        if last is None or last // block != z // block:
            # Stepped into a block: was it read ahead (or being read)?
            with self._lock:
                self.stats["entered"] += 1
                if (region, z // block) in self._done:
                    self.stats["warmed"] += 1
        direction = 0 if last is None or last == z else (1 if z > last else -1)
        start = z // block * block
        targets = []
        if direction >= 0 and start + block < depth:
            targets.append(start + block)
        if direction <= 0 and start > 0:
            targets.append(start - 1)
        queued = []
        for target in targets:
            if self._submit((region, target // block), lambda target=target: read(target)):
                queued.append(target)
        return queued

    def after_plane_tile(self, region, z: int, depth: int, encode, held=None, hit=None,
                         ahead: int = TILE_AHEAD) -> list[int]:
        """A tile of `region` was served at plane `z` of a stack decoded a plane
        at a time: queue the same tile, encoded, at the next `ahead` planes in
        the direction of travel (the planes either side until one is known).

        `encode(z)` reads and encodes the tile at plane `z` into the cache the
        route answers from, under the key the route would use, and returns
        whether it did (False when it was already there, or the project has
        changed since). `held(z)`, when given, says whether the cache already
        has it, so a tile held is never queued. `hit` is whether this tile
        came from that cache, for the hit rate. Returns the planes queued
        nearest first (for a test to read)."""
        z, depth = int(z), int(depth)
        if depth <= 1 or chunk_cache.in_background():
            return []
        last = self._moved(region, z)
        with self._lock:
            self.stats["served"] += 1
            if hit and (region, z) in self._made:
                self.stats["tiles_warmed"] += 1
        direction = 0 if last is None or last == z else (1 if z > last else -1)
        if direction:
            targets = [z + direction * step for step in range(1, max(1, int(ahead)) + 1)]
        else:
            targets = [z + 1, z - 1]
        targets = [target for target in targets if 0 <= target < depth
                   and not (held is not None and held(target))]
        queued = []
        # Farthest first: the newest job runs first, so the nearest plane is
        # made before the one after it.
        for target in reversed(targets):
            def job(target=target):
                made = encode(target)
                with self._lock:
                    if made:
                        self.stats["made"] += 1
                        self._made.pop((region, target), None)
                        self._made[(region, target)] = True
                        while len(self._made) > MADE_MAX:
                            self._made.popitem(last=False)
                    else:
                        self.stats["skipped"] += 1

            if self._submit((region, "tile", target), job, remember=False):
                queued.append(target)
        return queued[::-1]

    def _submit(self, key, job, remember: bool = True) -> bool:
        """Queue `job` under `key`. `remember`: refuse it again for
        DONE_SECONDS once read (a block); a tile is asked again whenever its
        cache does not hold it."""
        with self._lock:
            read_at = self._done.get(key) if remember else None
            if key in self._jobs or (read_at is not None
                                     and time.monotonic() - read_at < DONE_SECONDS):
                return False
            self._jobs[key] = (job, remember)
            self.stats["queued"] += 1
            while len(self._jobs) > QUEUE_MAX:
                self._jobs.popitem(last=False)
                self.stats["dropped"] += 1
            self._start_workers()
            self._wake.notify()
        return True

    def _start_workers(self):
        """Called with the lock held."""
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        while len(self._threads) < self._workers:
            thread = threading.Thread(target=self._run, name="plexora-plane-prefetch",
                                      daemon=True)
            thread.start()
            self._threads.append(thread)

    def _run(self):
        while True:
            with self._lock:
                while not self._jobs:
                    self._wake.wait()
                key, (job, remember) = self._jobs.popitem(last=True)
                if remember:
                    # Marked before the read, so a tile served while it runs
                    # does not queue the same block again.
                    self._done.pop(key, None)
                    self._done[key] = time.monotonic()
                    while len(self._done) > DONE_MAX:
                        self._done.popitem(last=False)
                self._running += 1
            outcome = "read"
            try:
                with chunk_cache.background():
                    job()
            except Exception:  # noqa: BLE001 -- a read ahead is never anybody's error
                outcome = "failed"
            finally:
                with self._lock:
                    self._running -= 1
                    self.stats[outcome] += 1
                    if outcome == "failed":
                        self._done.pop(key, None)

    def forget(self) -> None:
        """Drop everything queued and remembered (a source closed)."""
        with self._lock:
            self._jobs.clear()
            self._done.clear()
            self._last.clear()
            self._made.clear()

    def snapshot(self) -> dict:
        with self._lock:
            stats = dict(self.stats)
            waiting = len(self._jobs)
        stats["waiting"] = waiting
        # None until there is something to divide.
        stats["hit_rate"] = (round(stats["warmed"] / stats["entered"], 3)
                             if stats["entered"] else None)
        stats["tile_hit_rate"] = (round(stats["tiles_warmed"] / stats["served"], 3)
                                  if stats["served"] else None)
        return stats

    def drain(self, seconds: float = 10.0) -> bool:
        """Wait until nothing is queued or running (tests). True if it got there."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            with self._lock:
                if not self._jobs and not self._running:
                    return True
            time.sleep(0.005)
        return False


#: The process's one block prefetcher: the node's tile route and the
#: primary's both feed it, and the chunk cache it fills is process-wide too.
PREFETCHER = PlanePrefetcher()
#: The process's one tile prefetcher (`after_plane_tile`): one worker, since
#: each job reads and encodes on its own thread rather than fanning out.
TILE_PREFETCHER = PlanePrefetcher(workers=1, min_budget=0)
