"""The website's chunk cache, kept in the site's own storage.

On the desktop every value the chunk cache keeps (remote_store.CacheIndex) is
one file under the data root. On the website the data root, /data, is held in
the worker's memory and copied into the user's project store
(workers/storeSync.js): a cache there was capped at what a tab can hold, and
it was written into the projects folder besides. So here the values live in
the site's origin-private storage (OPFS plexora/cache/), which holds as much
as the browser grants the site, and only the SQLite index is kept in memory.

Python can only read and write OPFS through sync access handles, and a handle
is opened with a promise, which Python cannot wait on in every browser. So the
page opens a fixed set of files before Python starts (workers/cacheFiles.js)
and Python packs values into them:

- **Segments.** `count` files, written in turn. A value is appended to the
  current one with a header (`HEADER`: its length, a hash of which value it
  is, a CRC of its bytes). When the current segment is full the next is taken
  over: its values that are pinned (a store kept offline), or read since that
  segment was last filled, are copied into the one just filled, and the rest
  are evicted together. Least recently read goes first, as on the desktop, a
  segment at a time; the disk never holds more than the budget plus one
  segment.
- **The index.** The same SQLite tables as the desktop's, in memory, plus
  where each value is (`packed`). A snapshot of it is written to one of two
  index files in turn (`keep`), a few seconds after a change and when the
  page goes idle; a reload starts from the newer one that reads whole. Every
  read checks the value's header, so a row a snapshot remembered for bytes
  that have since been overwritten reads as a miss, never as the wrong bytes.

The budget is the desktop's setting (Settings > Web data, 10 GB unless
changed), capped at a share of what the browser lets the site store. A
second tab of the site cannot open the files (a sync access handle is
exclusive) and caches in memory instead (`MEMORY_BUDGET`), as does a browser
without sync access handles.

    install(files=None, quota=None) -> str   # what the cache is, said once
    keep(force=False) -> bool                # write the index snapshot now
"""

from __future__ import annotations

import errno
import hashlib
import logging
import struct
import time
import zlib
from pathlib import Path
from typing import Optional

from plexora.server.utils import remote_store
from plexora.server.utils.remote_store import CacheIndex

_log = logging.getLogger(__name__)

#: Where the index lives in memory, outside /data (which is copied into the
#: user's project store).
ROOT = "/cache"
#: The index snapshots: files 0 and 1. Segments are files 2..count+1.
INDEX_FILES = 2
#: The budget when the cache is kept in memory (another tab holds the files).
MEMORY_BUDGET = 256 * 1024 ** 2
#: The share of what the browser grants the site that the cache may use: the
#: site's copies of flat images and masks (OPFS plexora/derived) need the rest.
QUOTA_SHARE = 0.4
#: Of a segment being taken over, how much may be carried forward because it
#: was read since (pinned values are always carried).
CARRY_SHARE = 0.5
#: How long after a change the index snapshot is written.
KEEP_AFTER_S = 3.0
#: The smallest segment: a budget too small for every segment at this size
#: (the browser refused more than it had granted) uses fewer segments rather
#: than ones too small to hold a block.
MIN_SEGMENT = 1024 ** 2

MAGIC = b"PXC1"
#: magic, length, which value (8 bytes of blake2b), crc32 of the bytes.
HEADER = struct.Struct("<4sIQI12x")
SNAPSHOT = struct.Struct("<4sQQI12x")
SNAPSHOT_MAGIC = b"PXI1"


def _ident(store_id: str, key: str, rng) -> int:
    digest = hashlib.blake2b(f"{store_id}\0{key}\0{rng[0]}\0{rng[1]}".encode("utf-8"),
                             digest_size=8).digest()
    return int.from_bytes(digest, "little")


class PageFiles:
    """The page's files (workers/cacheFiles.js) as Python calls them: bytes
    in and out. Tests hand `PackedCacheIndex` an object with the same five
    methods and `count`."""

    def __init__(self, files):
        self._files = files
        self.count = int(files.count)

    def size(self, i: int) -> int:
        return int(self._files.size(i))

    def read(self, i: int, at: int, length: int) -> bytes:
        return self._files.read(i, at, length).to_bytes()

    def write(self, i: int, at: int, data: bytes) -> None:
        from pyodide.ffi import to_js

        written = int(self._files.write(i, at, to_js(memoryview(data))))
        if written != len(data):
            raise OSError(errno.ENOSPC, f"wrote {written} of {len(data)} bytes")

    def truncate(self, i: int, length: int) -> None:
        self._files.truncate(i, length)

    def flush(self, i: int) -> None:
        self._files.flush(i)


def _io(call, *args):
    """A file call, with a browser's refusal (out of quota, a handle closed)
    as the OSError the cache already handles."""
    try:
        return call(*args)
    except OSError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a JsException
        text = str(exc)
        code = errno.ENOSPC if "Quota" in text else errno.EIO
        raise OSError(code, text) from exc


class PackedCacheIndex(CacheIndex):
    """`CacheIndex` with its values packed into the page's files."""

    def __init__(self, root: Path, files, *, quota: Optional[int] = None,
                 budget: Optional[int] = None):
        self._files = files
        self._segments = int(files.count) - INDEX_FILES
        if self._segments < 2:
            raise ValueError("the cache needs at least two segments")
        self._quota = int(quota) if quota else None
        #: Lowered when the browser refused a write: what it actually let us keep.
        self._ceiling: Optional[int] = None
        self._sizes = [_io(files.size, INDEX_FILES + i) for i in range(self._segments)]
        self._head = 0
        self._dirty = False
        self._changed_at = 0.0
        self._snapshot_seq = 0
        self._snapshot_slot = 0
        self._unflushed: set[int] = set()
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self._restore(root / "index.sqlite")
        super().__init__(root, budget)
        self._settle()

    # -- the index snapshot ----------------------------------------------

    def _restore(self, path: Path) -> None:
        """The newer snapshot that reads whole, as the in-memory index file."""
        best = None
        for slot in range(INDEX_FILES):
            try:
                size = _io(self._files.size, slot)
                if size < SNAPSHOT.size:
                    continue
                head = _io(self._files.read, slot, 0, SNAPSHOT.size)
                magic, length, seq, crc = SNAPSHOT.unpack(head)
                if magic != SNAPSHOT_MAGIC or SNAPSHOT.size + length > size:
                    continue
                if best is not None and seq <= best[0]:
                    continue
                body = _io(self._files.read, slot, SNAPSHOT.size, length)
                if len(body) != length or zlib.crc32(body) != crc:
                    continue
                best = (seq, slot, body)
            except OSError:
                continue
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        if best is not None:
            self._snapshot_seq, slot, body = best
            self._snapshot_slot = 1 - slot
            path.write_bytes(body)

    def keep(self, force: bool = False) -> bool:
        """Write the index snapshot, if it changed (and, unless `force`, a
        few seconds have passed since). True if written."""
        if not self._dirty:
            return False
        if not force and time.monotonic() - self._changed_at < KEEP_AFTER_S:
            return False
        with self._lock:
            self._flush_touches()
            for segment in sorted(self._unflushed):
                try:
                    _io(self._files.flush, INDEX_FILES + segment)
                except OSError:
                    pass
            self._unflushed.clear()
            body = self._db.serialize()
            seq = self._snapshot_seq + 1
            slot = self._snapshot_slot
            head = SNAPSHOT.pack(SNAPSHOT_MAGIC, len(body), seq, zlib.crc32(body))
            try:
                _io(self._files.truncate, slot, 0)
                _io(self._files.write, slot, 0, head + body)
                _io(self._files.flush, slot)
            except OSError as exc:
                _log.warning("remote cache: could not keep the index (%s)", exc)
                return False
            self._snapshot_seq = seq
            self._snapshot_slot = 1 - slot
            self._dirty = False
        return True

    def _changed(self) -> None:
        if not self._dirty:
            self._changed_at = time.monotonic()
        self._dirty = True

    def _maybe_keep(self) -> None:
        if self._dirty and time.monotonic() - self._changed_at >= KEEP_AFTER_S:
            self.keep()

    # -- tables ----------------------------------------------------------

    def _prepare_db(self, db):
        db = super()._prepare_db(db)
        # In memory, and kept by snapshots: a write-ahead log would only be
        # something a snapshot could miss.
        db.execute("PRAGMA journal_mode=MEMORY")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS packed (
              store_id TEXT NOT NULL,
              key TEXT NOT NULL,
              range_start INTEGER NOT NULL,
              range_end INTEGER NOT NULL,
              segment INTEGER NOT NULL,
              offset INTEGER NOT NULL,
              length INTEGER NOT NULL,
              PRIMARY KEY (store_id, key, range_start, range_end)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS packed_segment ON packed(segment);
            CREATE TABLE IF NOT EXISTS segments (
              segment INTEGER PRIMARY KEY,
              filled_at REAL NOT NULL);
        """)
        return db

    def _settle(self) -> None:
        """Make the rows agree with the files: a value the files no longer
        hold whole is forgotten. Cheap (sizes only), so done at once."""
        with self._lock:
            db = self._db
            db.execute("DELETE FROM packed WHERE segment < 0 OR segment >= ?", (self._segments,))
            for segment, size in enumerate(self._sizes):
                db.execute("DELETE FROM packed WHERE segment = ? AND offset + length + ? > ?",
                           (segment, HEADER.size, size))
            db.execute(
                "DELETE FROM entries WHERE missing = 0 AND NOT EXISTS (SELECT 1 FROM packed p "
                "WHERE p.store_id = entries.store_id AND p.key = entries.key "
                "AND p.range_start = entries.range_start AND p.range_end = entries.range_end)")
            db.execute(
                "UPDATE stores SET bytes = (SELECT COALESCE(SUM(size), 0) FROM entries "
                "e WHERE e.store_id = stores.store_id AND e.missing = 0)")
            self._total = self._read_total()
            row = db.execute("SELECT v FROM meta WHERE k = 'head'").fetchone()
            try:
                head = int(row[0]) if row else 0
            except ValueError:
                head = 0
            self._head = head if 0 <= head < self._segments else 0
            self._evict_to(self.budget)
        self._reconciled.set()

    def reconcile(self) -> None:
        """Done when the index opened (`_settle`)."""
        self._reconciled.set()

    def _write_marker(self, store_id: str, root_url: str) -> None:
        """The stores table is kept with the index: no marker files."""

    # -- budget ----------------------------------------------------------

    @property
    def budget(self) -> int:
        wanted = super().budget
        if self._quota:
            wanted = min(wanted, int(self._quota * QUOTA_SHARE))
        if self._ceiling is not None:
            wanted = min(wanted, self._ceiling)
        return max(int(wanted), 0)

    def in_use(self) -> int:
        """How many segments the ring goes round: all of them, unless the
        budget is too small for that many of `MIN_SEGMENT`."""
        return max(2, min(self._segments, self.budget // MIN_SEGMENT))

    def segment_bytes(self) -> int:
        return self.budget // self.in_use()

    def disk_bytes(self) -> int:
        return sum(self._sizes)

    def accommodate(self, nbytes: int) -> bool:
        """Room is made by `write_value`, a segment at a time: anything that
        fits in one is kept."""
        return HEADER.size + int(nbytes) <= self.segment_bytes()

    def _evict_to(self, target: int) -> None:
        """Take over the oldest filled segments until the disk holds at most
        `target` (the budget shrank, or the browser refused a write). Locked."""
        target = max(int(target), 0)
        # Segments the ring no longer goes round are emptied first.
        for segment in range(self.in_use(), self._segments):
            if self._sizes[segment] and segment != self._head:
                self._take_over(segment, into=self._head, carry_read=False)
        while self.disk_bytes() > target:
            row = self._db.execute(
                "SELECT segment FROM segments WHERE segment != ? ORDER BY filled_at ASC LIMIT 1",
                (self._head,)).fetchone()
            candidates = [row[0]] if row else [
                s for s in range(self._segments) if s != self._head and self._sizes[s]]
            if not candidates:
                return
            before = self.disk_bytes()
            self._take_over(candidates[0], into=self._head, carry_read=False)
            if self.disk_bytes() >= before:
                return

    # -- values ----------------------------------------------------------

    def read_value(self, store_id: str, key: str, rng=remote_store._FULL) -> bytes:
        with self._lock:
            row = self._db.execute(
                "SELECT segment, offset, length FROM packed WHERE store_id = ? AND key = ? "
                "AND range_start = ? AND range_end = ?",
                (store_id, key, rng[0], rng[1])).fetchone()
            if row is None:
                raise FileNotFoundError(key)
            data = self._read_at(int(row[0]), int(row[1]), int(row[2]),
                                 _ident(store_id, key, rng))
            if data is None:
                self._db.execute(
                    "DELETE FROM packed WHERE store_id = ? AND key = ? "
                    "AND range_start = ? AND range_end = ?",
                    (store_id, key, rng[0], rng[1]))
                self._changed()
                raise FileNotFoundError(f"{key}: not as it was written")
        self._maybe_keep()
        return data

    def _read_at(self, segment: int, offset: int, length: int, ident: int) -> Optional[bytes]:
        if offset + HEADER.size + length > self._sizes[segment]:
            return None
        try:
            raw = _io(self._files.read, INDEX_FILES + segment, offset, HEADER.size + length)
        except OSError:
            return None
        if len(raw) != HEADER.size + length:
            return None
        magic, size, which, crc = HEADER.unpack_from(raw)
        data = raw[HEADER.size:]
        if magic != MAGIC or size != length or which != ident or zlib.crc32(data) != crc:
            return None
        return bytes(data)

    def write_value(self, store_id: str, key: str, rng, data: bytes) -> None:
        data = bytes(data)
        record = HEADER.pack(MAGIC, len(data), _ident(store_id, key, rng), zlib.crc32(data)) + data
        with self._lock:
            for attempt in range(2):
                if len(record) > self.segment_bytes():
                    raise OSError(errno.EFBIG, "larger than a cache segment")
                if self._sizes[self._head] + len(record) > self.segment_bytes():
                    self._advance()
                segment = self._head
                offset = self._sizes[segment]
                try:
                    _io(self._files.write, INDEX_FILES + segment, offset, record)
                except OSError as exc:
                    if exc.errno != errno.ENOSPC or attempt:
                        raise
                    # The browser keeps less than it said it would: what the
                    # files hold now is the most this cache will use.
                    self._ceiling = max(self.disk_bytes() - self.segment_bytes(), 0)
                    _log.warning("remote cache: the browser refused more (%s); keeping "
                                 "%d MB", exc, self._ceiling // 1024 ** 2)
                    self._evict_to(self._ceiling)
                    continue
                self._sizes[segment] = offset + len(record)
                self._unflushed.add(segment)
                self._db.execute(
                    "INSERT OR REPLACE INTO packed (store_id, key, range_start, range_end, "
                    "segment, offset, length) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (store_id, key, rng[0], rng[1], segment, offset, len(data)))
                self._changed()
                return

    def drop_value(self, store_id: str, key: str, rng=remote_store._FULL) -> None:
        """Its row goes now; its bytes when the segment is taken over."""
        with self._lock:
            self._db.execute(
                "DELETE FROM packed WHERE store_id = ? AND key = ? "
                "AND range_start = ? AND range_end = ?", (store_id, key, rng[0], rng[1]))
            self._changed()

    def _advance(self) -> None:
        """The current segment is full: take over the next. Locked."""
        filled = self._head
        following = (filled + 1) % self.in_use()
        self._db.execute("INSERT OR REPLACE INTO segments (segment, filled_at) VALUES (?, ?)",
                         (filled, time.time()))
        self._take_over(following, into=filled, carry_read=True)
        self._head = following
        self._db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('head', ?)", (str(following),))
        self._changed()

    def _take_over(self, segment: int, *, into: int, carry_read: bool) -> None:
        """Empty `segment`: values pinned -- and, `carry_read`, values read
        since it was filled, up to `CARRY_SHARE` of a segment -- are copied to
        the end of `into`, the rest are evicted. Locked."""
        self._flush_touches()
        row = self._db.execute("SELECT filled_at FROM segments WHERE segment = ?",
                               (segment,)).fetchone()
        filled_at = float(row[0]) if row else 0.0
        rows = self._db.execute(
            "SELECT p.store_id, p.key, p.range_start, p.range_end, p.offset, p.length, "
            "e.atime, e.missing, COALESCE(s.pinned, 0) FROM packed p "
            "LEFT JOIN entries e ON e.store_id = p.store_id AND e.key = p.key "
            "AND e.range_start = p.range_start AND e.range_end = p.range_end "
            "LEFT JOIN stores s ON s.store_id = p.store_id WHERE p.segment = ?",
            (segment,)).fetchall()
        pinned = [r for r in rows if r[6] is not None and not r[7] and r[8]]
        read = sorted((r for r in rows if r[6] is not None and not r[7] and not r[8]
                       and carry_read and r[6] > filled_at), key=lambda r: -r[6])
        allowance = int(self.segment_bytes() * CARRY_SHARE)
        carried = set()
        for r in pinned + read:
            store_id, key, start, end, offset, length = r[:6]
            if not r[8]:
                if allowance < length:
                    continue
                allowance -= length
            data = self._read_at(segment, offset, length, _ident(store_id, key, (start, end)))
            if data is None:
                continue
            record = HEADER.pack(MAGIC, length, _ident(store_id, key, (start, end)),
                                 zlib.crc32(data)) + data
            at = self._sizes[into]
            try:
                _io(self._files.write, INDEX_FILES + into, at, record)
            except OSError:
                break
            self._sizes[into] = at + len(record)
            self._unflushed.add(into)
            self._db.execute(
                "UPDATE packed SET segment = ?, offset = ? WHERE store_id = ? AND key = ? "
                "AND range_start = ? AND range_end = ?", (into, at, store_id, key, start, end))
            carried.add((store_id, key, start, end))
        for store_id, key, start, end, _offset, _length, atime, missing, _pin in rows:
            if (store_id, key, start, end) in carried:
                continue
            self._db.execute(
                "DELETE FROM packed WHERE store_id = ? AND key = ? "
                "AND range_start = ? AND range_end = ?", (store_id, key, start, end))
            if atime is None or missing:
                continue
            size = self._db.execute(
                "SELECT size FROM entries WHERE store_id = ? AND key = ? "
                "AND range_start = ? AND range_end = ?", (store_id, key, start, end)).fetchone()
            self._db.execute(
                "DELETE FROM entries WHERE store_id = ? AND key = ? "
                "AND range_start = ? AND range_end = ?", (store_id, key, start, end))
            if size is not None:
                self._total -= int(size[0])
                self._db.execute("UPDATE stores SET bytes = bytes - ? WHERE store_id = ?",
                                 (int(size[0]), store_id))
        try:
            _io(self._files.truncate, INDEX_FILES + segment, 0)
        except OSError:
            pass
        self._sizes[segment] = 0
        self._unflushed.discard(segment)
        self._db.execute("DELETE FROM segments WHERE segment = ?", (segment,))
        self._changed()

    # -- rows ------------------------------------------------------------

    def record(self, store_id, key, rng, size, *, missing=False, expires_at=None) -> None:
        super().record(store_id, key, rng, size, missing=missing, expires_at=expires_at)
        self._changed()
        self._maybe_keep()

    def forget(self, store_id, key, rng=remote_store._FULL) -> None:
        super().forget(store_id, key, rng)
        self.drop_value(store_id, key, rng)

    def touch(self, store_id, key, rng=remote_store._FULL) -> None:
        super().touch(store_id, key, rng)
        self._changed()

    def pin(self, store_id, flag=True) -> None:
        super().pin(store_id, flag)
        self._changed()

    def register_store(self, store_id, root_url) -> None:
        super().register_store(store_id, root_url)
        self._changed()

    def clear(self, store_id: Optional[str] = None) -> None:
        super().clear(store_id)
        with self._lock:
            if store_id is None:
                self._db.execute("DELETE FROM packed")
                self._db.execute("DELETE FROM segments")
                for segment in range(self._segments):
                    try:
                        _io(self._files.truncate, INDEX_FILES + segment, 0)
                    except OSError:
                        pass
                    self._sizes[segment] = 0
                self._unflushed.clear()
                self._head = 0
                self._db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('head', '0')")
            else:
                self._db.execute("DELETE FROM packed WHERE store_id = ?", (store_id,))
            self._changed()
        self.keep(force=True)

    def usage(self) -> dict:
        found = super().usage()
        found["where"] = "browser"
        found["disk_bytes"] = self.disk_bytes()
        return found

    def close(self) -> None:
        try:
            self.keep(force=True)
        except Exception:  # noqa: BLE001
            pass
        super().close()


# -- installing ----------------------------------------------------------------

_installed: Optional[str] = None


def _given(value) -> bool:
    """Whether the page left its files: without them it sets null (a scratch
    boot, another tab holding them, no sync access handles), which reaches
    Python as pyodide.ffi.jsnull rather than None."""
    if value is None:
        return False
    try:
        from pyodide.ffi import jsnull
    except ImportError:
        return True
    return value is not jsnull


def install(files=None, quota: Optional[int] = None) -> str:
    """Use the page's files for the chunk cache, or memory when there are
    none (another tab holds them, or the browser has no sync access handles).
    `files` and `quota` default to what the page left at
    `self.plexoraCacheFiles` / `self.plexoraCacheQuota`. Says which."""
    global _installed
    why = ""
    if files is None:
        try:
            import js

            found = getattr(js, "plexoraCacheFiles", None)
            files = PageFiles(found) if _given(found) else None
            quota = quota or (int(getattr(js, "plexoraCacheQuota", 0) or 0) or None)
            why = str(getattr(js, "plexoraCacheWhy", "") or "")
        except ImportError:
            files = None
    if files is None:
        remote_store.use_index(lambda root: CacheIndex(root, budget=MEMORY_BUDGET), Path(ROOT))
        _installed = f"memory ({why})" if why else "memory"
        return _installed

    def made(root: Path) -> CacheIndex:
        try:
            return PackedCacheIndex(root, files, quota=quota)
        except Exception as exc:  # noqa: BLE001 -- a cache in memory still works
            _log.warning("remote cache: the site's storage could not be used (%s)", exc)
            return CacheIndex(root, budget=MEMORY_BUDGET)

    remote_store.use_index(made, Path(ROOT))
    _installed = f"browser storage ({files.count - INDEX_FILES} segments)"
    return _installed


def keep(force: bool = False) -> bool:
    """Write the index snapshot if it changed: called by the worker when the
    page goes idle (workers/pythonWorker.js), and before it copies the store."""
    index = remote_store._index
    if isinstance(index, PackedCacheIndex):
        return index.keep(force=force)
    return False


def uninstall() -> None:
    """Back to the desktop's cache (tests)."""
    global _installed
    remote_store.use_index(None, None)
    _installed = None
