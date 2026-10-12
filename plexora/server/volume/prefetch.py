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
and nothing at all where the chunks are one plane deep (the browser's own
neighbour prefetch already covers those) or the chunk cache is too small to
hold a block beside the one on screen (`MIN_BUDGET`).
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


class PlanePrefetcher:
    def __init__(self, workers: int = WORKERS, min_budget: int = MIN_BUDGET):
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._jobs: OrderedDict = OrderedDict()
        self._done: OrderedDict = OrderedDict()
        self._last: OrderedDict = OrderedDict()
        self._threads: list[threading.Thread] = []
        self._workers = max(1, int(workers))
        self.min_budget = int(min_budget)
        self.stats = {"queued": 0, "read": 0, "dropped": 0, "failed": 0}

    def after_tile(self, region, z: int, depth: int, block: int, read) -> list[int]:
        """A tile of `region` was served at plane `z`: queue the block ahead.

        `region` names one tile of one channel at one level of one source --
        anything hashable; `depth` is how many planes there are; `block` how
        many one stored chunk spans; `read(z)` reads the same tile at plane
        `z`. Returns the planes queued (for a test to read)."""
        z, depth, block = int(z), int(depth), int(block)
        if block <= 1 or depth <= block or chunk_cache.CACHE.budget < self.min_budget:
            return []
        with self._lock:
            last = self._last.pop(region, None)
            self._last[region] = z
            while len(self._last) > REGIONS_MAX:
                self._last.popitem(last=False)
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

    def _submit(self, key, job) -> bool:
        with self._lock:
            read_at = self._done.get(key)
            if key in self._jobs or (read_at is not None
                                     and time.monotonic() - read_at < DONE_SECONDS):
                return False
            self._jobs[key] = job
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
                key, job = self._jobs.popitem(last=True)
                # Marked before the read, so a tile served while it runs does
                # not queue the same block again.
                self._done.pop(key, None)
                self._done[key] = time.monotonic()
                while len(self._done) > DONE_MAX:
                    self._done.popitem(last=False)
            try:
                with chunk_cache.background():
                    job()
                self.stats["read"] += 1
            except Exception:  # noqa: BLE001 -- a read ahead is never anybody's error
                self.stats["failed"] += 1
                with self._lock:
                    self._done.pop(key, None)

    def forget(self) -> None:
        """Drop everything queued and remembered (a source closed)."""
        with self._lock:
            self._jobs.clear()
            self._done.clear()
            self._last.clear()

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.stats, "waiting": len(self._jobs)}


#: The process's one prefetcher: the node's tile route and the primary's both
#: feed it, and the chunk cache it fills is process-wide too.
PREFETCHER = PlanePrefetcher()
