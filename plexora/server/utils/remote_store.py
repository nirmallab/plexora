"""Reading zarr stores from a web address, through a chunk cache on this disk.

An OME-Zarr image on IDR, a public S3 bucket or a lab's own HTTPS server is
the same tree of small files a local store is, so zarr can read it directly --
`FsspecStore` turns every key into a request. What zarr does not do is keep
anything: every tile the viewer asks for refetches the whole chunk it sits in.
Measured against IDR, forty 256-pixel tiles cost 123 s uncached and 0.12 s
cached, so the cache below is what makes a remote image usable at all, not an
optimisation on top of it.

Three pieces:

**URLs.** `canonical_url` is the one spelling a project records, and
`split_store_url` finds the store root in it (the last segment ending in
`.zarr`). One cache tree and one store object serve a root, so `x.zarr` and
`x.zarr/0` never cache a chunk twice and a plate's fields share a connection.

**`ChunkCacheStore`**, a zarr `WrapperStore` over `FsspecStore`. A read is
answered from `<data root>/.remote_cache` when it can be, fetched once
otherwise (concurrent readers of one key share one request), and written
back. Missing keys are remembered too, briefly: zarr probes several metadata
names for every node, and over HTTP each wrong guess is a round trip. 403 is
read as missing, because a bucket without listing permission answers 403 for
a key that is not there.

**`CacheIndex`**, one SQLite file beside the cached bytes that knows what is
there, how big it is and when it was last read. The byte budget is enforced
across every store and across restarts, least recently read first, and a store
somebody asked to keep offline is never evicted.

Why not zarr's own `CacheStore` (zarr.experimental): its accounting is per
instance and in memory, so the budget is neither global nor persistent; byte
ranges (sharded arrays) are cached only in memory; misses are not cached; and
there is no single-flight. Each of those is a failure mode above.

All reads run on zarr's own IO loop -- `zarr.core.sync.sync` is how a
Waitress thread gets there. Nothing here may call `sync()` from a coroutine
already on that loop. fsspec, aiohttp and s3fs are imported inside functions
so that building the app does not import them.

Credentials are never stored. The options a URL is opened with come from the
address book in `models/remote_sources.py`, which keeps an endpoint, an
anonymity flag and a profile name -- keys come from the environment, as they
do for every other cloud tool.
"""

from __future__ import annotations

import asyncio
import atexit
import email.utils
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from zarr.abc.store import (
    OffsetByteRequest,
    RangeByteRequest,
    SuffixByteRequest,
)
from zarr.storage import WrapperStore

from plexora.server.providers.base import RemoteUnreachable, is_remote_locator

#: Extra packages a scheme needs, beyond fsspec and aiohttp (https) and s3fs.
_SCHEME_MODULES = {"gs": "gcsfs", "abfs": "adlfs"}

#: A missing chunk is remembered this long. Long enough that a viewer panning
#: over the empty edge of a sparse array does not re-ask for every tile;
#: short enough that a store being written catches up within a session.
NEGATIVE_TTL_S = 600

#: A missing metadata document is remembered for a minute: these are the
#: probes zarr makes for every node (`zarr.json`, `.zgroup`, `.zattrs`, ...),
#: and a store being fixed up should not stay "not a zarr node" for long.
NEGATIVE_META_TTL_S = 60

_METADATA_NAMES = ("zarr.json", ".zarray", ".zgroup", ".zattrs", ".zmetadata")

#: How long `probe` answers from memory. The same ten seconds a recorded
#: image failure answers for (`data_model.IMAGE_STATUS_TTL_S`).
PROBE_TTL_S = 10

#: After a fetch finds the host unreachable, further misses fail at once for
#: this long rather than each waiting out a connect timeout. Cached reads are
#: unaffected -- that is what keeps a viewer drawing through a dropped link.
_DOWN_BACKOFF_S = 10

#: Retries for a failed fetch that could succeed on a second try (a dropped
#: connection, a 5xx), and the pause before each. Never for a 4xx.
_RETRY_DELAYS_S = (0.5, 1.5)

#: Last-read times are written in batches: a tile burst would otherwise be one
#: UPDATE per chunk, all of them contending for the same file.
_TOUCH_FLUSH_S = 30
_TOUCH_FLUSH_COUNT = 512

#: How many rows one eviction pass considers.
_EVICT_BATCH = 64

#: How long startup reconciliation may spend walking cache directories.
_RECONCILE_BUDGET_S = 20

#: A `.partial` file older than this is a write that died; younger ones may be
#: somebody's write in progress.
_PARTIAL_STALE_S = 3600

_SCHEMA_VERSION = "1"

_STORE_MARKER = ".plexora-store.json"


class RemoteSupportMissing(ImportError):
    """A scheme whose filesystem package is not installed.

    `INSTALL` is the line to show somebody; `layer_jobs._finish` and the
    import page both look for it by name.
    """

    INSTALL = "pip install 'plexora[remote]'"

    def __init__(self, scheme, module):
        super().__init__(
            f"Reading {scheme}:// addresses needs the {module} package, which is "
            f"not installed. Install it with: {self.INSTALL}")
        self.scheme = scheme
        self.module = module


# -- URLs ------------------------------------------------------------------


def canonical_url(raw) -> str:
    """The one spelling of a web address a project records.

    Quotes and whitespace stripped (a pasted path often carries both), scheme
    and host lower-cased, `gcs://` read as `gs://` and `abfss://` as
    `abfs://`, the fragment dropped, repeated slashes in the path collapsed and
    a trailing slash removed. The query is kept: a presigned URL is nothing
    without it.
    """
    text = str(raw or "").strip().strip("'\"").strip()
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    scheme = {"gcs": "gs", "abfss": "abfs"}.get(scheme, scheme)
    netloc = parts.netloc.lower()
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def split_store_url(url) -> tuple[str, str]:
    """(store root, path inside it) for a canonical URL.

    The root is everything up to and including the LAST path segment ending
    in `.zarr` -- a SpatialData store's image is `store.zarr/images/x`, and a
    bioformats2raw series is `x.zarr/0`. A URL with no such segment is its own
    root, which is right for a store somebody named without the suffix.
    """
    url = canonical_url(url)
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    last = None
    for index, segment in enumerate(segments):
        if segment.lower().endswith(".zarr"):
            last = index
    if last is None:
        return url, ""
    root_path = "/" + "/".join(segments[:last + 1])
    root = urlunsplit((parts.scheme, parts.netloc, root_path, parts.query, ""))
    return root, "/".join(segments[last + 1:])


def url_name(url) -> str:
    """The last non-empty path segment, query excluded."""
    segments = [s for s in urlsplit(canonical_url(url)).path.split("/") if s]
    return segments[-1] if segments else urlsplit(canonical_url(url)).netloc


def url_join(url, *parts) -> str:
    """`url` with path segments appended, the query kept at the end."""
    base = urlsplit(canonical_url(url))
    extra = "/".join(str(p).strip("/") for p in parts if str(p).strip("/"))
    path = base.path.rstrip("/") + ("/" + extra if extra else "")
    return urlunsplit((base.scheme, base.netloc, path, base.query, ""))


def display_name(url) -> str:
    """A project name for a web address: its last segment, suffix dropped."""
    return re.sub(r"\.(ome\.zarr|h5ad\.zarr|zarr)$", "", url_name(url),
                  flags=re.IGNORECASE)


def host_of(url) -> str:
    return urlsplit(canonical_url(url)).netloc


def _scheme(url) -> str:
    return urlsplit(canonical_url(url)).scheme


# -- options ---------------------------------------------------------------


def _options_for(url) -> dict:
    """The address-book options for `url`, or {}.

    Imported late: the address book lives with the data root, which is models
    territory, and this module is imported by things that must not pull that
    in at import time.
    """
    try:
        from plexora.server.models import remote_sources
    except Exception:  # noqa: BLE001 -- no address book is "no options"
        return {}
    try:
        return dict(remote_sources.options_for(url) or {})
    except Exception:  # noqa: BLE001
        return {}


def storage_options_for(url, options: Optional[Mapping[str, Any]] = None) -> dict:
    """fsspec `storage_options` for `url`'s scheme.

    https: the environment's proxy settings are honoured (`trust_env`), and a
    connection that goes quiet fails after 90 s rather than never. s3: anonymous
    unless the address book says otherwise, in which case boto's own credential
    chain applies -- Plexora never holds a key.
    """
    import aiohttp

    options = dict(options or {})
    scheme = _scheme(url)
    if scheme in ("http", "https"):
        return {
            "asynchronous": True,
            "client_kwargs": {
                "trust_env": True,
                "timeout": aiohttp.ClientTimeout(total=None, sock_connect=5,
                                                 sock_read=90),
            },
        }
    if scheme == "s3":
        out: dict[str, Any] = {
            "anon": bool(options.get("anon", True)),
            "config_kwargs": {
                "connect_timeout": 5,
                "read_timeout": 90,
                "retries": {"max_attempts": 3, "mode": "standard"},
            },
        }
        client_kwargs = {}
        if options.get("endpoint_url"):
            client_kwargs["endpoint_url"] = str(options["endpoint_url"])
        if options.get("region"):
            client_kwargs["region_name"] = str(options["region"])
        if client_kwargs:
            out["client_kwargs"] = client_kwargs
        if options.get("profile") and not out["anon"]:
            out["profile"] = str(options["profile"])
        return out
    if scheme == "gs":
        out = {}
        if options.get("anon", True):
            out["token"] = "anon"
        return out
    if scheme in ("az", "abfs"):
        out = {"anon": bool(options.get("anon", True))}
        if options.get("account_name"):
            out["account_name"] = str(options["account_name"])
        return out
    return {}


def support() -> dict:
    """Which schemes can be read in this install."""
    import importlib.util

    def has(module):
        return importlib.util.find_spec(module) is not None

    return {
        "https": has("aiohttp") and has("fsspec"),
        "s3": has("s3fs"),
        "gs": has("gcsfs"),
        "az": has("adlfs"),
    }


def _require_scheme(url) -> None:
    scheme = _scheme(url)
    scheme = {"az": "abfs"}.get(scheme, scheme)
    module = _SCHEME_MODULES.get(scheme)
    if scheme == "s3":
        module = "s3fs"
    if module is None:
        return
    import importlib.util

    if importlib.util.find_spec(module) is None:
        raise RemoteSupportMissing(scheme, module)


# -- errors ----------------------------------------------------------------


#: What a failed fetch was, as far as a cache is concerned.
_MISSING, _FORBIDDEN, _RETRY, _FATAL = "missing", "forbidden", "retry", "fatal"


def _classify(exc) -> str:
    """Sort a fetch failure into missing / forbidden / worth retrying / other."""
    # fsspec's HTTP `_info` turns EVERY failure into FileNotFoundError -- a
    # dropped connection and a 403 included -- keeping the real one as the
    # cause. Classifying the wrapper would call a dead host "missing".
    cause = exc.__cause__
    if isinstance(exc, FileNotFoundError) and cause is not None and cause is not exc:
        return _classify(cause)
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return _MISSING
    if isinstance(exc, PermissionError):
        return _FORBIDDEN
    if isinstance(status, int):
        if status in (404, 410):
            return _MISSING
        if status in (401, 403):
            return _FORBIDDEN
        if status in (408, 429) or status >= 500:
            return _RETRY
        return _FATAL
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return _RETRY
    name = type(exc).__name__
    if any(word in name for word in ("Connect", "Timeout", "ServerDisconnected",
                                     "ClientOSError", "ClientPayload")):
        return _RETRY
    if isinstance(exc, OSError):
        return _RETRY
    return _FATAL


# -- the index -------------------------------------------------------------


@dataclass
class Entry:
    store_id: str
    key: str
    range_start: int
    range_end: int
    size: int
    missing: bool
    expires_at: Optional[float]
    atime: float


_FULL = (-1, -1)


def _range_of(byte_range) -> tuple[int, int]:
    """A zarr byte request as the (start, end) pair the index keys on."""
    if byte_range is None:
        return _FULL
    if isinstance(byte_range, RangeByteRequest):
        return int(byte_range.start), int(byte_range.end)
    if isinstance(byte_range, OffsetByteRequest):
        return int(byte_range.offset), -1
    if isinstance(byte_range, SuffixByteRequest):
        return -int(byte_range.suffix), -2
    raise ValueError(f"Unexpected byte range {byte_range!r}")


def _slice(data: bytes, byte_range) -> bytes:
    """The part of a whole cached value a byte request asks for."""
    if byte_range is None:
        return data
    if isinstance(byte_range, RangeByteRequest):
        return data[byte_range.start:byte_range.end]
    if isinstance(byte_range, OffsetByteRequest):
        return data[byte_range.offset:]
    if isinstance(byte_range, SuffixByteRequest):
        return data[-byte_range.suffix:] if byte_range.suffix else b""
    raise ValueError(f"Unexpected byte range {byte_range!r}")


_SAFE_KEY = re.compile(r"[^A-Za-z0-9._/\-]")


def _key_path(key: str) -> str:
    """A store key as a relative path that is legal on every filesystem.

    Percent-encoded outside a conservative set, which keeps `:` and `?` off a
    Windows disk and leaves ordinary keys (`0/c/0/1/2`, `.zattrs`) readable.
    """
    parts = [p for p in key.split("/") if p not in ("", ".", "..")]
    return "/".join(_SAFE_KEY.sub(lambda m: quote(m.group(0), safe=""), p)
                    for p in parts)


def _range_name(start: int, end: int) -> str:
    return f"{start}_{end}".replace("-", "m")


def store_id_of(root_url: str) -> str:
    return hashlib.sha1(root_url.encode("utf-8")).hexdigest()[:20]


class CacheIndex:
    """What the chunk cache holds, in one SQLite file beside the bytes.

    One per process, over `paths.remote_cache_root()`, opened on first use.
    Every method takes the one lock: SQLite is fast enough that contention is
    not the cost, and one connection with one lock is a lot easier to be sure
    of than a pool.
    """

    def __init__(self, root: Path, budget: Optional[int] = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._budget = budget
        self._touches: dict[tuple, float] = {}
        self._last_flush = time.monotonic()
        #: Bumped by every clear, so a fetch that started before one does not
        #: write its bytes back into a cache somebody just emptied.
        self.generation = 0
        self._db = self._connect()
        self._total = self._read_total()
        self._reconciled = threading.Event()

    # -- connection ------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.root / "index.sqlite"

    def _connect(self) -> sqlite3.Connection:
        try:
            db = self._open_db()
            ok = db.execute("PRAGMA quick_check").fetchone()
            if not ok or ok[0] != "ok":
                raise sqlite3.DatabaseError(f"quick_check: {ok}")
            return db
        except sqlite3.DatabaseError:
            # A damaged index is not a lost cache: the bytes are all still on
            # disk, each directory says which store it is, and reconciliation
            # rebuilds the rows from them.
            try:
                self._db.close()  # type: ignore[has-type]
            except Exception:  # noqa: BLE001
                pass
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(self.db_path) + suffix)
                if path.exists():
                    try:
                        os.replace(path, Path(f"{self.db_path}.corrupt-{stamp}{suffix}"))
                    except OSError:
                        path.unlink(missing_ok=True)
            return self._open_db()

    def _open_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path), check_same_thread=False,
                             isolation_level=None, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stores (
              store_id TEXT PRIMARY KEY,
              root_url TEXT NOT NULL UNIQUE,
              created_at REAL NOT NULL,
              last_opened_at REAL NOT NULL,
              pinned INTEGER NOT NULL DEFAULT 0,
              bytes INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS entries (
              store_id TEXT NOT NULL REFERENCES stores(store_id) ON DELETE CASCADE,
              key TEXT NOT NULL,
              range_start INTEGER NOT NULL DEFAULT -1,
              range_end INTEGER NOT NULL DEFAULT -1,
              size INTEGER NOT NULL,
              missing INTEGER NOT NULL DEFAULT 0,
              expires_at REAL,
              atime REAL NOT NULL,
              PRIMARY KEY (store_id, key, range_start, range_end)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS entries_atime ON entries(atime) WHERE missing = 0;
        """)
        db.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('schema_version', ?)",
                   (_SCHEMA_VERSION,))
        return db

    def close(self) -> None:
        with self._lock:
            try:
                self._flush_touches()
                self._db.close()
            except Exception:  # noqa: BLE001
                pass

    def _read_total(self) -> int:
        row = self._db.execute(
            "SELECT COALESCE(SUM(size), 0) FROM entries WHERE missing = 0").fetchone()
        return int(row[0] or 0)

    # -- budget ----------------------------------------------------------

    @property
    def budget(self) -> int:
        if self._budget is not None:
            return self._budget
        from plexora import paths

        return paths.remote_cache_budget()

    def set_budget(self, nbytes: Optional[int]) -> None:
        """Apply a new budget now, evicting down to it if the cache is over."""
        with self._lock:
            self._budget = int(nbytes) if nbytes is not None else None
            self._evict_to(self.budget)

    # -- stores ----------------------------------------------------------

    def register_store(self, store_id: str, root_url: str) -> None:
        now = time.time()
        with self._lock:
            # Recovered-by-reconcile rows carry a placeholder root; the real
            # one arrives here the first time the store is opened again.
            self._db.execute("DELETE FROM stores WHERE root_url = ? AND store_id != ?",
                             (root_url, store_id))
            self._db.execute(
                "INSERT INTO stores (store_id, root_url, created_at, last_opened_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(store_id) DO UPDATE SET "
                "root_url = excluded.root_url, last_opened_at = excluded.last_opened_at",
                (store_id, root_url, now, now))
        directory = self.root / store_id
        marker = directory / _STORE_MARKER
        if not marker.exists():
            try:
                directory.mkdir(parents=True, exist_ok=True)
                _atomic_write(marker, json.dumps({"root_url": root_url}).encode("utf-8"))
            except OSError:
                pass

    def is_pinned(self, store_id: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT pinned FROM stores WHERE store_id = ?",
                                   (store_id,)).fetchone()
        return bool(row and row[0])

    def pin(self, store_id: str, flag: bool = True) -> None:
        with self._lock:
            self._db.execute("UPDATE stores SET pinned = ? WHERE store_id = ?",
                             (1 if flag else 0, store_id))
            if flag:
                # Remembered misses of a pinned store never expire: "offline"
                # means every answer, including "not there", stays put.
                self._db.execute(
                    "UPDATE entries SET expires_at = NULL "
                    "WHERE store_id = ? AND missing = 1", (store_id,))

    def pinned_bytes(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(e.size), 0) FROM entries e JOIN stores s "
                "ON e.store_id = s.store_id WHERE s.pinned = 1 AND e.missing = 0"
            ).fetchone()
        return int(row[0] or 0)

    # -- entries ---------------------------------------------------------

    def lookup(self, store_id: str, key: str, rng=_FULL) -> Optional[Entry]:
        with self._lock:
            row = self._db.execute(
                "SELECT size, missing, expires_at, atime FROM entries "
                "WHERE store_id = ? AND key = ? AND range_start = ? AND range_end = ?",
                (store_id, key, rng[0], rng[1])).fetchone()
        if row is None:
            return None
        return Entry(store_id, key, rng[0], rng[1], int(row[0]), bool(row[1]),
                     row[2], float(row[3]))

    def value_path(self, store_id: str, key: str, rng=_FULL) -> Path:
        base = self.root / store_id / _key_path(key)
        if rng == _FULL:
            return base
        return base.with_name(base.name + ".ranges") / _range_name(*rng)

    def record(self, store_id: str, key: str, rng, size: int, *,
               missing: bool = False, expires_at: Optional[float] = None) -> None:
        now = time.time()
        with self._lock:
            previous = self._db.execute(
                "SELECT size, missing FROM entries WHERE store_id = ? AND key = ? "
                "AND range_start = ? AND range_end = ?",
                (store_id, key, rng[0], rng[1])).fetchone()
            self._db.execute(
                "INSERT OR REPLACE INTO entries (store_id, key, range_start, range_end, "
                "size, missing, expires_at, atime) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (store_id, key, rng[0], rng[1], int(size), 1 if missing else 0,
                 expires_at, now))
            delta = (0 if missing else int(size)) - (
                int(previous[0]) if previous and not previous[1] else 0)
            if delta:
                self._total += delta
                self._db.execute("UPDATE stores SET bytes = bytes + ? WHERE store_id = ?",
                                 (delta, store_id))

    def forget(self, store_id: str, key: str, rng=_FULL) -> None:
        """Drop one row whose file has gone."""
        with self._lock:
            row = self._db.execute(
                "SELECT size, missing FROM entries WHERE store_id = ? AND key = ? "
                "AND range_start = ? AND range_end = ?",
                (store_id, key, rng[0], rng[1])).fetchone()
            if row is None:
                return
            self._db.execute(
                "DELETE FROM entries WHERE store_id = ? AND key = ? "
                "AND range_start = ? AND range_end = ?",
                (store_id, key, rng[0], rng[1]))
            if not row[1]:
                self._total -= int(row[0])
                self._db.execute("UPDATE stores SET bytes = bytes - ? WHERE store_id = ?",
                                 (int(row[0]), store_id))

    def touch(self, store_id: str, key: str, rng=_FULL) -> None:
        with self._lock:
            self._touches[(store_id, key, rng[0], rng[1])] = time.time()
            if (len(self._touches) >= _TOUCH_FLUSH_COUNT
                    or time.monotonic() - self._last_flush > _TOUCH_FLUSH_S):
                self._flush_touches()

    def _flush_touches(self) -> None:
        if not self._touches:
            self._last_flush = time.monotonic()
            return
        rows = [(atime, *key) for key, atime in self._touches.items()]
        self._touches.clear()
        self._last_flush = time.monotonic()
        try:
            self._db.executemany(
                "UPDATE entries SET atime = ? WHERE store_id = ? AND key = ? "
                "AND range_start = ? AND range_end = ?", rows)
        except sqlite3.Error:
            pass

    def flush(self) -> None:
        with self._lock:
            self._flush_touches()

    # -- room ------------------------------------------------------------

    def accommodate(self, nbytes: int) -> bool:
        """Make room for `nbytes` more, least recently read first.

        False when the value cannot be cached at all -- bigger than the budget,
        or the unpinned part of the cache is already empty -- in which case the
        caller serves it without keeping it.
        """
        budget = self.budget
        if nbytes > budget:
            return False
        with self._lock:
            if self._total + nbytes <= budget:
                return True
            self._evict_to(budget - nbytes)
            return self._total + nbytes <= budget

    def _evict_to(self, target: int) -> None:
        """Evict unpinned entries until the total is at most `target`. Locked."""
        if self._total <= target:
            return
        self._flush_touches()
        while self._total > target:
            victims = self._db.execute(
                "SELECT e.store_id, e.key, e.range_start, e.range_end, e.size "
                "FROM entries e JOIN stores s ON e.store_id = s.store_id "
                "WHERE e.missing = 0 AND s.pinned = 0 "
                # Stores nobody has opened since a rebuild go first.
                "ORDER BY (s.root_url LIKE 'unknown:%') DESC, e.atime ASC LIMIT ?",
                (_EVICT_BATCH,)).fetchall()
            if not victims:
                return
            for store_id, key, start, end, size in victims:
                if self._total <= target:
                    break
                path = self.value_path(store_id, key, (start, end))
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                self._db.execute(
                    "DELETE FROM entries WHERE store_id = ? AND key = ? "
                    "AND range_start = ? AND range_end = ?", (store_id, key, start, end))
                self._total -= int(size)
                self._db.execute("UPDATE stores SET bytes = bytes - ? WHERE store_id = ?",
                                 (int(size), store_id))

    # -- summaries -------------------------------------------------------

    def usage(self) -> dict:
        with self._lock:
            self._flush_touches()
            rows = self._db.execute(
                "SELECT s.store_id, s.root_url, s.pinned, s.last_opened_at, "
                "COALESCE(SUM(CASE WHEN e.missing = 0 THEN e.size END), 0), "
                "COALESCE(SUM(CASE WHEN e.missing = 0 THEN 1 END), 0) "
                "FROM stores s LEFT JOIN entries e ON e.store_id = s.store_id "
                "GROUP BY s.store_id ORDER BY s.last_opened_at DESC").fetchall()
            total = self._total
        stores = [{
            "id": row[0],
            "url": None if row[1].startswith("unknown:") else row[1],
            "orphaned": row[1].startswith("unknown:"),
            "pinned": bool(row[2]),
            "last_opened_at": row[3],
            "bytes": int(row[4] or 0),
            "entries": int(row[5] or 0),
        } for row in rows]
        return {
            "used_bytes": int(total),
            "pinned_bytes": sum(s["bytes"] for s in stores if s["pinned"]),
            "budget_bytes": self.budget,
            "stores": stores,
        }

    def store_row(self, store_id: str) -> Optional[dict]:
        for row in self.usage()["stores"]:
            if row["id"] == store_id:
                return row
        return None

    # -- clearing --------------------------------------------------------

    def clear(self, store_id: Optional[str] = None) -> None:
        """Empty the cache, or one store's part of it.

        Directories are renamed out of the way first and deleted on a thread,
        so a large cache clears at once from the caller's point of view, and a
        store that is open keeps working -- its next read simply fetches.
        """
        with self._lock:
            self.generation += 1
            self._touches.clear()
            if store_id is None:
                ids = [row[0] for row in self._db.execute("SELECT store_id FROM stores")]
                ids += [p.name for p in self.root.iterdir()
                        if p.is_dir() and not p.name.startswith(".trash-")
                        and p.name not in ids] if self.root.exists() else []
                self._db.execute("DELETE FROM entries")
                self._db.execute("UPDATE stores SET bytes = 0, pinned = 0")
                self._db.execute("DELETE FROM stores WHERE root_url LIKE 'unknown:%'")
                self._total = 0
            else:
                ids = [store_id]
                row = self._db.execute(
                    "SELECT COALESCE(SUM(size), 0) FROM entries "
                    "WHERE store_id = ? AND missing = 0", (store_id,)).fetchone()
                self._db.execute("DELETE FROM entries WHERE store_id = ?", (store_id,))
                self._db.execute("DELETE FROM stores WHERE store_id = ?", (store_id,))
                self._total -= int(row[0] or 0)
            trash = []
            for sid in ids:
                directory = self.root / sid
                if directory.is_dir():
                    target = self.root / f".trash-{uuid.uuid4().hex}"
                    try:
                        os.replace(directory, target)
                        trash.append(target)
                    except OSError:
                        trash.append(directory)
        if trash:
            threading.Thread(target=_remove_all, args=(trash,), daemon=True,
                             name="remote-cache-clear").start()

    # -- reconciliation --------------------------------------------------

    def reconcile(self) -> None:
        """Make the rows agree with the files, once per process.

        Adds rows for files nobody recorded (a crash between write and record,
        or a rebuilt index), drops rows whose file has gone, removes stale
        partial writes and leftover trash, then evicts down to the budget. Time
        bounded: a huge cache is reconciled as far as it gets in twenty
        seconds, which is still right for everything it reached.
        """
        if self._reconciled.is_set():
            return
        self._reconciled.set()
        started = time.monotonic()
        try:
            for leftover in self.root.glob(".trash-*"):
                shutil.rmtree(leftover, ignore_errors=True)
            with self._lock:
                known = dict(self._db.execute("SELECT store_id, root_url FROM stores"))
            directories = [p for p in self.root.iterdir()
                           if p.is_dir() and not p.name.startswith(".")]
            for directory in directories:
                if time.monotonic() - started > _RECONCILE_BUDGET_S:
                    break
                store_id = directory.name
                if store_id not in known:
                    root_url = f"unknown:{store_id}"
                    try:
                        marker = json.loads((directory / _STORE_MARKER).read_text("utf-8"))
                        root_url = str(marker.get("root_url") or root_url)
                    except (OSError, ValueError):
                        pass
                    with self._lock:
                        now = time.time()
                        self._db.execute(
                            "INSERT OR IGNORE INTO stores (store_id, root_url, created_at, "
                            "last_opened_at) VALUES (?, ?, ?, ?)",
                            (store_id, root_url, now, 0.0))
                self._reconcile_store(directory, started)
            with self._lock:
                # Rows for stores whose directory is gone entirely.
                present = {p.name for p in directories}
                for (store_id,) in self._db.execute(
                        "SELECT store_id FROM stores").fetchall():
                    if store_id not in present:
                        self._db.execute(
                            "DELETE FROM entries WHERE store_id = ? AND missing = 0",
                            (store_id,))
                self._db.execute(
                    "UPDATE stores SET bytes = (SELECT COALESCE(SUM(size), 0) FROM entries "
                    "e WHERE e.store_id = stores.store_id AND e.missing = 0)")
                self._total = self._read_total()
                self._evict_to(self.budget)
        except Exception as exc:  # noqa: BLE001 -- reconciliation is best effort
            print(f"remote cache: reconcile stopped early -- {exc}")

    def _reconcile_store(self, directory: Path, started: float) -> None:
        store_id = directory.name
        on_disk: dict[tuple, tuple[int, float]] = {}
        now = time.time()
        for path in directory.rglob("*"):
            if time.monotonic() - started > _RECONCILE_BUDGET_S:
                return
            if not path.is_file() or path.name == _STORE_MARKER:
                continue
            if ".partial-" in path.name:
                try:
                    if now - path.stat().st_mtime > _PARTIAL_STALE_S:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            parsed = self._parse_value_path(directory, path)
            if parsed is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            on_disk[parsed] = (stat.st_size, stat.st_mtime)
        with self._lock:
            rows = self._db.execute(
                "SELECT key, range_start, range_end FROM entries "
                "WHERE store_id = ? AND missing = 0", (store_id,)).fetchall()
            recorded = {(k, s, e) for k, s, e in rows}
            for key, start, end in recorded - set(on_disk):
                self._db.execute(
                    "DELETE FROM entries WHERE store_id = ? AND key = ? "
                    "AND range_start = ? AND range_end = ?", (store_id, key, start, end))
            for (key, start, end) in set(on_disk) - recorded:
                size, mtime = on_disk[(key, start, end)]
                self._db.execute(
                    "INSERT OR REPLACE INTO entries (store_id, key, range_start, range_end, "
                    "size, missing, expires_at, atime) VALUES (?, ?, ?, ?, ?, 0, NULL, ?)",
                    (store_id, key, start, end, size, mtime))

    @staticmethod
    def _parse_value_path(directory: Path, path: Path) -> Optional[tuple]:
        from urllib.parse import unquote

        relative = path.relative_to(directory).as_posix()
        parent = path.parent.name
        if parent.endswith(".ranges"):
            match = re.fullmatch(r"(m?\d+)_(m?\d+)", path.name)
            if not match:
                return None
            start = int(match.group(1).replace("m", "-"))
            end = int(match.group(2).replace("m", "-"))
            key_rel = relative[: -(len(path.name) + 1)]
            key_rel = key_rel[: -len(".ranges")]
            return unquote(key_rel), start, end
        if any(part.endswith(".ranges") for part in Path(relative).parts[:-1]):
            return None
        return unquote(relative), -1, -1


def _remove_all(directories: Iterable[Path]) -> None:
    for directory in directories:
        shutil.rmtree(directory, ignore_errors=True)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.partial-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


_index: Optional[CacheIndex] = None
_index_lock = threading.Lock()
_index_root_override: Optional[Path] = None


def cache_root() -> Path:
    if _index_root_override is not None:
        return _index_root_override
    from plexora import paths

    return paths.remote_cache_root()


def cache_index() -> CacheIndex:
    """The process's index over the cache root, opened (and reconciled) lazily."""
    global _index
    root = cache_root()
    with _index_lock:
        if _index is not None and _index.root == Path(root):
            return _index
        if _index is not None:
            _index.close()
        _index = CacheIndex(Path(root))
        index = _index
    threading.Thread(target=index.reconcile, daemon=True,
                     name="remote-cache-reconcile").start()
    return index


# -- the store -------------------------------------------------------------


def _default_prototype():
    from zarr.core.buffer import default_buffer_prototype

    return default_buffer_prototype()


class ChunkCacheStore(WrapperStore):
    """A remote zarr store read through the on-disk chunk cache.

    Read-only. Every read goes: fresh remembered miss -> None; cached ->
    the file; otherwise one fetch per key however many readers want it, then
    the bytes are written back. A range request is served from a cached whole
    value when there is one, so a store made available offline answers the
    shard-index reads of a sharded array without the network.
    """

    supports_writes = False
    supports_deletes = False
    supports_partial_writes = False

    def __init__(self, store, root_url: str, index: Optional[CacheIndex] = None):
        super().__init__(store)
        self.root_url = root_url
        self.store_id = store_id_of(root_url)
        self._index = index
        #: True turns every miss into `RemoteUnreachable` without trying.
        self.offline = False
        #: Whether a listing of this store has ever returned anything. An
        #: HTTPS gateway answers every listing with nothing, and discovery
        #: needs to know not to believe it.
        self.supports_listing_confirmed = False
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._down_until = 0.0
        self._last_status: dict[str, str] = {}
        self._listings: dict[str, list[str]] = {}
        self._registered = False
        self.stats = {"hits": 0, "misses": 0, "fetches": 0, "negative_hits": 0,
                      "bytes_fetched": 0}

    def _with_store(self, store):
        return type(self)(store, self.root_url, self._index)

    def __eq__(self, other) -> bool:
        return isinstance(other, ChunkCacheStore) and other.root_url == self.root_url

    def __hash__(self) -> int:
        return hash(self.root_url)

    def __str__(self) -> str:
        return self.root_url

    def __repr__(self) -> str:
        return f"ChunkCacheStore({self.root_url!r})"

    @property
    def index(self) -> CacheIndex:
        index = self._index or cache_index()
        if not self._registered:
            index.register_store(self.store_id, self.root_url)
            self._registered = True
        return index

    @property
    def _supports_sync_io(self) -> bool:
        # zarr's sync fast path would call the wrapped store directly and skip
        # the cache entirely. Everything goes through `get`.
        return False

    @property
    def read_only(self) -> bool:
        return True

    @property
    def supports_listing(self) -> bool:
        return self._store.supports_listing

    # -- reads -----------------------------------------------------------

    def last_status(self, key: str) -> Optional[str]:
        """How the last fetch of `key` went: missing / forbidden / None."""
        return self._last_status.get(key)

    def _read_cached(self, key: str, byte_range):
        """(state, bytes): 'hit', 'missing' or None. Runs on a thread."""
        index = self.index
        rng = _range_of(byte_range)
        now = time.time()
        full = index.lookup(self.store_id, key, _FULL)
        if full is not None:
            if full.missing:
                if full.expires_at is None or full.expires_at > now:
                    return "missing", None
                index.forget(self.store_id, key, _FULL)
            else:
                path = index.value_path(self.store_id, key, _FULL)
                try:
                    data = path.read_bytes()
                except OSError:
                    index.forget(self.store_id, key, _FULL)
                else:
                    index.touch(self.store_id, key, _FULL)
                    return "hit", _slice(data, byte_range)
        if rng != _FULL:
            entry = index.lookup(self.store_id, key, rng)
            if entry is not None and not entry.missing:
                path = index.value_path(self.store_id, key, rng)
                try:
                    data = path.read_bytes()
                except OSError:
                    index.forget(self.store_id, key, rng)
                else:
                    index.touch(self.store_id, key, rng)
                    return "hit", data
        return None, None

    def _write_cached(self, key: str, byte_range, data: Optional[bytes],
                      generation: int) -> None:
        """Record a fetched value (or a miss). Runs on a thread."""
        index = self.index
        if index.generation != generation:
            return
        rng = _range_of(byte_range)
        if data is None:
            pinned = index.is_pinned(self.store_id)
            ttl = NEGATIVE_META_TTL_S if key.rsplit("/", 1)[-1] in _METADATA_NAMES \
                else NEGATIVE_TTL_S
            index.record(self.store_id, key, _FULL, 0, missing=True,
                         expires_at=None if pinned else time.time() + ttl)
            return
        if not index.accommodate(len(data)):
            return
        path = index.value_path(self.store_id, key, rng)
        try:
            _atomic_write(path, data)
        except OSError:
            return
        if index.generation != generation:
            path.unlink(missing_ok=True)
            return
        index.record(self.store_id, key, rng, len(data))

    async def _fetch(self, key: str, byte_range) -> Optional[bytes]:
        """One fetch from the wrapped store, retried when it might help."""
        if self.offline or time.monotonic() < self._down_until:
            raise RemoteUnreachable(
                f"{host_of(self.root_url)} cannot be reached right now", self.root_url)
        prototype = _default_prototype()
        last_exc: Optional[BaseException] = None
        for attempt in range(len(_RETRY_DELAYS_S) + 1):
            try:
                self.stats["fetches"] += 1
                buffer = await self._store.get(key, prototype, byte_range)
            except Exception as exc:  # noqa: BLE001 -- classified below
                kind = _classify(exc)
                if kind in (_MISSING, _FORBIDDEN):
                    self._last_status[key] = kind
                    return None
                if kind == _FATAL:
                    raise
                last_exc = exc
                if attempt < len(_RETRY_DELAYS_S):
                    await asyncio.sleep(_RETRY_DELAYS_S[attempt])
                continue
            if buffer is None:
                self._last_status[key] = _MISSING
                return None
            self._last_status.pop(key, None)
            data = buffer.to_bytes()
            self.stats["bytes_fetched"] += len(data)
            _count_fetched(len(data))
            return data
        self._down_until = time.monotonic() + _DOWN_BACKOFF_S
        raise RemoteUnreachable(
            f"{host_of(self.root_url)} cannot be reached right now ({last_exc})",
            self.root_url) from last_exc

    async def _get_bytes(self, key: str, byte_range=None) -> Optional[bytes]:
        state, data = await asyncio.to_thread(self._read_cached, key, byte_range)
        if state == "hit":
            self.stats["hits"] += 1
            return data
        if state == "missing":
            self.stats["negative_hits"] += 1
            return None
        self.stats["misses"] += 1
        flight = (key, _range_of(byte_range))
        pending = self._inflight.get(flight)
        if pending is not None:
            return await asyncio.shield(pending)
        future = asyncio.get_running_loop().create_future()
        self._inflight[flight] = future
        try:
            generation = self.index.generation
            try:
                data = await self._fetch(key, byte_range)
            except RemoteUnreachable:
                # A store made available offline holds everything it has; a
                # key it does not hold is not there. Answering "missing" is
                # what lets zarr's optional metadata probes succeed offline.
                if await asyncio.to_thread(self.index.is_pinned, self.store_id):
                    future.set_result(None)
                    return None
                raise
            await asyncio.to_thread(self._write_cached, key, byte_range, data, generation)
            future.set_result(data)
            return data
        except BaseException as exc:
            future.set_exception(exc)
            # Consumed here so an unawaited future does not log a warning.
            future.exception()
            raise
        finally:
            self._inflight.pop(flight, None)

    async def get(self, key, prototype=None, byte_range=None):
        data = await self._get_bytes(key, byte_range)
        if data is None:
            return None
        prototype = prototype or _default_prototype()
        return prototype.buffer.from_bytes(data)

    async def get_partial_values(self, prototype, key_ranges):
        return list(await asyncio.gather(
            *(self.get(key, prototype, byte_range) for key, byte_range in key_ranges)))

    async def get_ranges(self, key, byte_ranges, *, prototype, max_concurrency=None,
                         max_gap_bytes=None, max_coalesced_bytes=None):
        # Not coalesced: each range is its own cache entry, which is what lets
        # a second open of a sharded array cost nothing.
        for index, byte_range in enumerate(byte_ranges):
            buffer = await self.get(key, prototype, byte_range)
            if buffer is None:
                raise FileNotFoundError(key)
            yield [(index, buffer)]

    async def exists(self, key) -> bool:
        state, _ = await asyncio.to_thread(self._read_cached, key, None)
        if state == "hit":
            return True
        if state == "missing":
            return False
        if key.rsplit("/", 1)[-1] in _METADATA_NAMES:
            # Small, and read right after anyway: fetching settles both.
            return (await self._get_bytes(key)) is not None
        try:
            found = await self._store.exists(key)
        except Exception as exc:  # noqa: BLE001
            if _classify(exc) in (_MISSING, _FORBIDDEN):
                found = False
            else:
                raise RemoteUnreachable(
                    f"{host_of(self.root_url)} cannot be reached right now",
                    self.root_url) from exc
        if not found:
            await asyncio.to_thread(self._write_cached, key, None, None,
                                    self.index.generation)
        return bool(found)

    async def getsize(self, key) -> int:
        entry = await asyncio.to_thread(self.index.lookup, self.store_id, key, _FULL)
        if entry is not None and not entry.missing:
            return entry.size
        return await self._store.getsize(key)

    async def list(self):
        try:
            async for key in self._store.list():
                yield key
        except Exception:  # noqa: BLE001 -- a host that will not list lists nothing
            return

    async def list_prefix(self, prefix):
        try:
            async for key in self._store.list_prefix(prefix):
                yield key
        except Exception:  # noqa: BLE001
            return

    async def list_dir(self, prefix):
        prefix = prefix.strip("/")
        if prefix in self._listings:
            for name in self._listings[prefix]:
                yield name
            return
        names: list[str] = []
        try:
            async for name in self._store.list_dir(prefix):
                # An HTTP index links a directory with a trailing slash.
                name = name.strip("/").rsplit("/", 1)[-1]
                if name and name not in names:
                    names.append(name)
        except Exception:  # noqa: BLE001
            names = []
        if names:
            self.supports_listing_confirmed = True
            self._listings[prefix] = names
        for name in names:
            yield name

    async def set(self, key, value):
        raise PermissionError("a remote store is read-only")

    async def delete(self, key):
        raise PermissionError("a remote store is read-only")

    # -- driving from a thread -------------------------------------------

    def read(self, key: str) -> Optional[bytes]:
        """One whole value through the cache, from any thread but zarr's loop."""
        from zarr.core.sync import sync

        return sync(self._get_bytes(key))

    def list_children(self, prefix: str = "") -> Optional[list[str]]:
        """Names directly under `prefix`, or None when the host will not say."""
        from zarr.core.sync import sync

        async def collect():
            return [name async for name in self.list_dir(prefix)]

        try:
            names = sync(collect())
        except Exception:  # noqa: BLE001
            return None
        return sorted(names) if names else None

    def read_many(self, keys: Iterable[str], concurrency: int = 8) -> dict:
        """{key: bytes or None} for several whole values, fetched concurrently."""
        from zarr.core.sync import sync

        keys = list(keys)

        async def run():
            gate = asyncio.Semaphore(concurrency)

            async def one(key):
                async with gate:
                    return key, await self._get_bytes(key)

            return dict(await asyncio.gather(*(one(k) for k in keys)))

        return sync(run())

    def fetch_keys(self, keys: Iterable[str], *, concurrency: int = 4,
                   cancel: Optional[threading.Event] = None,
                   progress: Optional[Callable[[int, int], None]] = None,
                   batch: int = 64) -> int:
        """Bring `keys` into the cache; returns how many bytes are now held.

        What the warm and offline jobs drive. Its own small gate, under zarr's
        own concurrency ceiling, so a background fill never starves the tile
        reads the viewer is making at the same time. Checked for cancellation
        between batches.
        """
        from zarr.core.sync import sync

        keys = list(keys)
        done = 0
        held = 0

        async def run(chunk):
            gate = asyncio.Semaphore(concurrency)

            async def one(key):
                async with gate:
                    data = await self._get_bytes(key)
                    return len(data) if data is not None else 0

            return sum(await asyncio.gather(*(one(k) for k in chunk)))

        for start in range(0, len(keys), batch):
            if cancel is not None and cancel.is_set():
                break
            chunk = keys[start:start + batch]
            held += sync(run(chunk))
            done += len(chunk)
            if progress is not None:
                progress(done, len(keys))
        return held


# -- the registry ----------------------------------------------------------

_stores: dict[str, ChunkCacheStore] = {}
_stores_lock = threading.Lock()


def open_store(url, options: Optional[Mapping[str, Any]] = None) -> tuple[ChunkCacheStore, str]:
    """(cached store for `url`'s root, path of `url` inside it).

    One store object per root per process, so every image, label and table of
    a store shares its connection pool and its in-flight fetches.
    """
    url = canonical_url(url)
    if not is_remote_locator(url):
        raise ValueError(f"{url!r} is not a web address")
    root, subpath = split_store_url(url)
    with _stores_lock:
        store = _stores.get(root)
        if store is not None:
            return store, subpath
    _require_scheme(root)
    from zarr.storage import FsspecStore
    from zarr.storage._fsspec import ALLOWED_EXCEPTIONS

    if options is None:
        options = _options_for(root)
    inner = FsspecStore.from_url(
        root, storage_options=storage_options_for(root, options), read_only=True,
        allowed_exceptions=tuple(ALLOWED_EXCEPTIONS))
    store = ChunkCacheStore(inner, root)
    with _stores_lock:
        return _stores.setdefault(root, store), subpath


#: Bytes that came over the network, across every store this process opened.
#: Only ever added to, so a caller measures a stretch of work as the
#: difference between two readings (data_model's load progress does).
_fetched_total = 0
_fetched_lock = threading.Lock()


def _count_fetched(nbytes: int) -> None:
    global _fetched_total
    with _fetched_lock:
        _fetched_total += nbytes


def bytes_fetched() -> int:
    """Bytes downloaded so far by this process, from every remote store."""
    return _fetched_total


def open_stores() -> list[ChunkCacheStore]:
    with _stores_lock:
        return list(_stores.values())


def forget(root_url: Optional[str] = None, prefix: Optional[str] = None) -> None:
    """Drop cached store objects so the next open rebuilds them.

    After the address book changes: the options a store was built with are
    part of its filesystem object.
    """
    with _stores_lock:
        for root in list(_stores):
            if (root_url is None and prefix is None) or root == root_url or (
                    prefix and root.startswith(prefix)):
                _stores.pop(root, None)


def close_all() -> None:
    forget()
    global _index
    with _index_lock:
        if _index is not None:
            _index.close()
            _index = None


def _reset_for_tests(root: Optional[Path] = None) -> None:
    """Forget every store, the index, probes and fingerprints; optionally
    repoint the cache root."""
    global _index_root_override
    close_all()
    _index_root_override = Path(root) if root is not None else None
    _probes.clear()
    _fingerprints.clear()


# -- probing and identity --------------------------------------------------


@dataclass
class ProbeResult:
    reachable: bool
    status: str  # ok | offline | missing | inaccessible
    detail: str = ""
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    size: Optional[int] = None
    key: Optional[str] = None
    at: float = field(default_factory=time.time)


_probes: dict[str, ProbeResult] = {}
_fingerprints: dict[str, Any] = {}

_PROBE_KEYS = ("zarr.json", ".zattrs", ".zgroup", ".zarray")


def probe(url, timeout: float = 2.0, *, fresh: bool = False) -> ProbeResult:
    """Whether `url` is a zarr node that answers, bypassing the cache.

    What a stat is to a file. A GET-or-HEAD of the node's metadata document
    (fsspec falls back to GET when HEAD is refused), bounded by `timeout` so a
    dead host costs two seconds and not a connect timeout per probe.
    Remembered for ten seconds.
    """
    url = canonical_url(url)
    remembered = _probes.get(url)
    if not fresh and remembered is not None and time.time() - remembered.at < PROBE_TTL_S:
        return remembered
    try:
        store, subpath = open_store(url)
    except RemoteSupportMissing as exc:
        result = ProbeResult(False, "inaccessible", str(exc))
        _probes[url] = result
        return result
    from zarr.core.sync import sync

    inner = store._store
    fs = inner.fs
    base = inner.path.rstrip("/")

    async def look():
        statuses = []
        for name in _PROBE_KEYS:
            path = "/".join(p for p in (base, subpath, name) if p)
            try:
                info = await asyncio.wait_for(fs._info(path), timeout)
            except Exception as exc:  # noqa: BLE001 -- classified
                kind = _classify(exc)
                if kind in (_MISSING, _FORBIDDEN):
                    statuses.append(kind)
                    continue
                return ProbeResult(False, "offline", _describe(exc, url))
            etag = info.get("ETag") or info.get("etag")
            modified = info.get("Last-Modified") or info.get("LastModified") \
                or info.get("last_modified")
            return ProbeResult(True, "ok", "", str(etag).strip('"') if etag else None,
                               str(modified) if modified else None,
                               info.get("size"), name)
        if statuses and all(s == _FORBIDDEN for s in statuses):
            return ProbeResult(True, "inaccessible",
                               f"{host_of(url)} refused access to {url_name(url)}")
        return ProbeResult(True, "missing",
                           f"No zarr metadata at {url}")

    try:
        result = sync(look(), timeout=timeout * len(_PROBE_KEYS) + 5)
    except Exception as exc:  # noqa: BLE001
        result = ProbeResult(False, "offline", _describe(exc, url))
    _probes[url] = result
    return result


def _describe(exc, url) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    where = f"{host_of(url)} (through the proxy {proxy})" if proxy else host_of(url)
    return f"Could not reach {where}: {text}"


@dataclass(frozen=True)
class RemoteFingerprint:
    size: Optional[int]
    mtime_ns: Optional[int]
    identity: Mapping[str, Any]
    key: Optional[str]


def fingerprint(url) -> Optional[RemoteFingerprint]:
    """Identity of the bytes at `url`, from its metadata document's headers.

    Length plus Last-Modified (as nanoseconds) or, when a host sends only an
    ETag, a stable number derived from it. Offline, the last value this process
    saw -- else None, which never matches anything.
    """
    url = canonical_url(url)
    result = probe(url)
    if result.status != "ok":
        return _fingerprints.get(url)
    mtime_ns = None
    if result.last_modified:
        try:
            parsed = email.utils.parsedate_to_datetime(result.last_modified)
            mtime_ns = int(parsed.timestamp() * 1e9)
        except (TypeError, ValueError):
            try:
                from datetime import datetime

                mtime_ns = int(datetime.fromisoformat(
                    str(result.last_modified)).timestamp() * 1e9)
            except ValueError:
                mtime_ns = None
    if mtime_ns is None and result.etag:
        mtime_ns = int(hashlib.sha1(result.etag.encode("utf-8")).hexdigest()[:15], 16)
    if result.etag:
        key = f"etag:{result.etag}"
    elif result.last_modified:
        key = f"lm:{result.last_modified}"
    elif result.size is not None:
        key = f"size:{result.size}"
    else:
        key = None
    found = RemoteFingerprint(
        size=result.size if result.size is not None else 0,
        mtime_ns=mtime_ns,
        identity={"url": url, "etag": result.etag,
                  "last_modified": result.last_modified},
        key=key)
    _fingerprints[url] = found
    return found


def source_signature(url) -> dict:
    """`{csv_path, csv_size, csv_mtime_ns}` for a table at a web address -- the
    shape the ball-tree and centroid-tile caches record for a local file, with
    the metadata document's length and Last-Modified/ETag standing in for the
    stat."""
    found = fingerprint(url)
    return {"csv_path": canonical_url(url),
            "csv_size": found.size if found else None,
            "csv_mtime_ns": found.mtime_ns if found else None}


def fingerprint_key(url) -> Optional[str]:
    """`etag:<v>` / `lm:<v>` / `size:<n>`, or None -- for the places that key
    staleness on one string (`imagePyramidKey`, `segmentationSourceKey`)."""
    found = fingerprint(url)
    return found.key if found is not None else None


# -- lifecycle -------------------------------------------------------------


def _at_exit() -> None:
    with _index_lock:
        if _index is not None:
            _index.flush()


def _after_fork() -> None:
    global _index
    _stores.clear()
    _index = None
    try:
        import fsspec

        fsspec.AbstractFileSystem.clear_instance_cache()
    except Exception:  # noqa: BLE001
        pass


atexit.register(_at_exit)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


__all__ = [
    "CacheIndex",
    "ChunkCacheStore",
    "NEGATIVE_META_TTL_S",
    "NEGATIVE_TTL_S",
    "ProbeResult",
    "RemoteSupportMissing",
    "cache_index",
    "cache_root",
    "canonical_url",
    "display_name",
    "fingerprint",
    "fingerprint_key",
    "forget",
    "host_of",
    "open_store",
    "probe",
    "split_store_url",
    "storage_options_for",
    "support",
    "url_join",
    "url_name",
]
