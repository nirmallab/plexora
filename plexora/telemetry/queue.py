"""The local queue: hourly aggregates and records in one SQLite file.

`<data_root>/.telemetry/queue.sqlite`, written only by the telemetry writer
thread and read only by it and the uploader thread -- never by a request.

- **Rollback journal, not WAL.** Data roots live on NFS home directories on
  clusters, and WAL's shared-memory index does not work there. Every write
  here is a small transaction every thirty seconds, which a rollback journal
  handles without noticing.
- **Aggregated before it arrives.** `counters` is keyed by
  `(hour window, event, key, dims)` and upserts add, so an hour of panning is
  a few dozen rows however many tiles it drew.
- **Batches are frozen when built.** `take_batch` moves rows into `outbox`
  as one gzipped payload with its own id, in one `BEGIN IMMEDIATE`, so a
  retry resends byte-identical content under the same id (the server drops
  duplicates) and two processes sharing a data root never send a row twice.
- **Bounded.** `trim` keeps the file under `max_bytes` and drops anything
  older than `max_age_days`, oldest window first.
- **Never raises to its caller.** A file that cannot be opened makes `open`
  return False; a corrupt file is moved aside and a fresh one started.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plexora.telemetry.schema import HIST_BINS

FILENAME = "queue.sqlite"
DIRNAME = ".telemetry"
MAX_BYTES = 10 * 1024 * 1024
MAX_AGE_DAYS = 30
CLAIM_SECONDS = 120

_BINS = ", ".join(f"b{i}" for i in range(HIST_BINS))
_BIN_COLUMNS = ", ".join(f"b{i} INTEGER NOT NULL DEFAULT 0" for i in range(HIST_BINS))
_BIN_UPSERT = ", ".join(f"b{i} = b{i} + excluded.b{i}" for i in range(HIST_BINS))

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS counters (
    window TEXT NOT NULL,
    event TEXT NOT NULL,
    key TEXT NOT NULL,
    dims TEXT NOT NULL,
    agg TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    {_BIN_COLUMNS},
    s REAL NOT NULL DEFAULT 0,
    mx REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (window, event, key, dims)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window TEXT NOT NULL,
    event TEXT NOT NULL,
    props TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5
);
CREATE TABLE IF NOT EXISTS outbox (
    batch_id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    payload BLOB NOT NULL,
    events INTEGER NOT NULL,
    raw_bytes INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
"""


def window_of(ts=None) -> str:
    """The UTC hour `ts` falls in: `2026-09-27T14`."""
    moment = datetime.fromtimestamp(time.time() if ts is None else ts, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H")


def _window_cutoff(days) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H")


def default_path() -> Path:
    from plexora import paths

    return paths.data_root() / DIRNAME / FILENAME


class Queue:
    def __init__(self, path=None):
        self._path = Path(path) if path is not None else None
        self._conn = None
        self._lock = threading.RLock()
        self.failed = False
        self.errors = 0
        self.last_error = None

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = default_path()
        return self._path

    # -- opening ------------------------------------------------------------

    def open(self) -> bool:
        """Open (creating if need be). False when this machine cannot keep a
        queue -- read-only root, no space, an unresolvable data root."""
        with self._lock:
            if self._conn is not None:
                return True
            if self.failed:
                return False
            try:
                path = self.path
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self._conn = self._connect(path)
                except sqlite3.DatabaseError:
                    self._move_aside(path)
                    self._conn = self._connect(path)
                return True
            except Exception:
                self._conn = None
                self.failed = True
                return False

    def _connect(self, path):
        conn = sqlite3.connect(str(path), timeout=2.0, isolation_level=None,
                               check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=2000")
            if not path.exists() or path.stat().st_size == 0:
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            ok = conn.execute("PRAGMA quick_check").fetchone()
            if not ok or ok[0] != "ok":
                raise sqlite3.DatabaseError("quick_check failed")
            conn.executescript(_SCHEMA)
        except Exception:
            conn.close()
            raise
        return conn

    def _move_aside(self, path):
        stamp = time.strftime("%Y%m%d%H%M%S")
        for suffix in ("", "-journal"):
            source = Path(str(path) + suffix)
            if source.exists():
                try:
                    source.rename(Path(f"{path}.corrupt-{stamp}{suffix}"))
                except OSError:
                    source.unlink(missing_ok=True)

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _run(self, fn):
        """Run `fn(conn)` under the lock; any sqlite failure is a False/None
        answer, never an exception."""
        with self._lock:
            if not self.open():
                return None
            try:
                return fn(self._conn)
            except Exception as exc:
                self.errors += 1
                self.last_error = type(exc).__name__
                if self._conn is not None and self._conn.in_transaction:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
                return None

    # -- writing ------------------------------------------------------------

    def add_counters(self, window, rows) -> bool:
        """Upsert `[(event, key, dims_json, agg, n, bins|None, s, mx)]`."""
        if not rows:
            return True

        def write(conn):
            params = []
            for event, key, dims, agg, n, bins, s, mx in rows:
                bins = list(bins) if bins else [0] * HIST_BINS
                params.append((window, event, key, dims, agg, int(n), *bins, float(s), float(mx)))
            placeholders = ", ".join("?" * (8 + HIST_BINS))
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    f"INSERT INTO counters (window, event, key, dims, agg, n, {_BINS}, s, mx) "
                    f"VALUES ({placeholders}) ON CONFLICT (window, event, key, dims) DO UPDATE SET "
                    f"n = n + excluded.n, {_BIN_UPSERT}, s = s + excluded.s, "
                    f"mx = max(mx, excluded.mx)", params)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return True

        return bool(self._run(write))

    def add_events(self, window, records) -> bool:
        """Insert `[(event, props_json, priority)]`."""
        if not records:
            return True

        def write(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    "INSERT INTO events (window, event, props, priority) VALUES (?, ?, ?, ?)",
                    [(window, event, props, priority) for event, props, priority in records])
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return True

        return bool(self._run(write))

    # -- state --------------------------------------------------------------

    def get_state(self, key, default=None):
        def read(conn):
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

        try:
            answer = self._run(read)
        except Exception:
            return default
        return default if answer is None else answer

    def set_state(self, key, value) -> bool:
        def write(conn):
            if value is None:
                conn.execute("DELETE FROM state WHERE key = ?", (key,))
            else:
                conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) "
                             "DO UPDATE SET value = excluded.value", (key, json.dumps(value)))
            return True

        try:
            return bool(self._run(write))
        except Exception:
            return False

    def state_items(self, prefix):
        def read(conn):
            rows = conn.execute("SELECT key, value FROM state WHERE key >= ? AND key < ?",
                                (prefix, prefix + "￿")).fetchall()
            return [(k, json.loads(v)) for k, v in rows]

        try:
            return self._run(read) or []
        except Exception:
            return []

    # -- reading and batching -----------------------------------------------

    def pending(self) -> dict:
        """Counts for `plexora telemetry status`."""
        def read(conn):
            counters = conn.execute("SELECT COUNT(*) FROM counters").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            outbox = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            return {"counter_rows": counters, "records": events, "batches_waiting": outbox}

        try:
            return self._run(read) or {}
        except Exception:
            return {}

    def snapshot(self, *, include_open=True, limit=5000):
        """`(counter_rows, records)` as they stand, without claiming anything."""
        current = window_of()

        def read(conn):
            where = "" if include_open else "WHERE window < ?"
            args = () if include_open else (current,)
            counters = conn.execute(
                f"SELECT window, event, key, dims, agg, n, {_BINS}, s, mx FROM counters {where} "
                f"ORDER BY window, event, key, dims LIMIT ?", (*args, limit)).fetchall()
            records = conn.execute(
                f"SELECT id, window, event, props, priority FROM events {where} "
                f"ORDER BY priority, id LIMIT ?", (*args, limit)).fetchall()
            return counters, records

        try:
            return self._run(read) or ([], [])
        except Exception:
            return [], []

    def take_batch(self, build, *, include_open=False, max_rows=2000, max_records=200,
                   now=None):
        """Claim the next batch to send: `(batch_id, payload_gz)` or None.

        An unacknowledged batch from before is returned first (same id, same
        bytes). Otherwise closed-window rows are frozen into a new one by
        `build(batch_id, counter_rows, records) -> (payload_gz | None,
        used_counter_keys, used_record_ids, event_count, raw_bytes)` in the
        same transaction that deletes the rows it used.
        """
        now = time.time() if now is None else now
        current = window_of(now)

        def take(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT batch_id, payload FROM outbox WHERE claimed_until < ? "
                    "ORDER BY created LIMIT 1", (now,)).fetchone()
                if row is not None:
                    conn.execute("UPDATE outbox SET claimed_until = ?, attempts = attempts + 1 "
                                 "WHERE batch_id = ?", (now + CLAIM_SECONDS, row[0]))
                    conn.execute("COMMIT")
                    return row[0], bytes(row[1])
                where = "" if include_open else "WHERE window < ?"
                args = () if include_open else (current,)
                counters = conn.execute(
                    f"SELECT window, event, key, dims, agg, n, {_BINS}, s, mx FROM counters "
                    f"{where} ORDER BY window, event, key, dims LIMIT ?",
                    (*args, max_rows)).fetchall()
                records = conn.execute(
                    f"SELECT id, window, event, props, priority FROM events {where} "
                    f"ORDER BY priority, id LIMIT ?", (*args, max_records)).fetchall()
                if not counters and not records:
                    conn.execute("COMMIT")
                    return None
                batch_id = uuid.uuid4().hex
                built = build(batch_id, counters, records)
                if built is None:
                    conn.execute("COMMIT")
                    return None
                payload, used_counters, used_records, event_count, raw_bytes = built
                if used_counters:
                    conn.executemany("DELETE FROM counters WHERE window = ? AND event = ? "
                                     "AND key = ? AND dims = ?", used_counters)
                if used_records:
                    conn.executemany("DELETE FROM events WHERE id = ?",
                                     [(rid,) for rid in used_records])
                if payload is None:
                    # Everything taken was rejected at build time: dropped.
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    "INSERT INTO outbox (batch_id, created, payload, events, raw_bytes, "
                    "attempts, claimed_until) VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (batch_id, now, payload, event_count, raw_bytes, now + CLAIM_SECONDS))
                conn.execute("COMMIT")
                return batch_id, payload
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return self._run(take)

    def ack(self, batch_id) -> None:
        """Delivered (or undeliverable for good): forget the batch."""
        self._run(lambda conn: conn.execute("DELETE FROM outbox WHERE batch_id = ?",
                                            (batch_id,)))

    def unack(self, batch_id) -> None:
        """Not delivered: release the claim so the next attempt resends it."""
        self._run(lambda conn: conn.execute(
            "UPDATE outbox SET claimed_until = 0 WHERE batch_id = ?", (batch_id,)))

    # -- bounding -----------------------------------------------------------

    def size_bytes(self) -> int:
        def read(conn):
            pages = conn.execute("PRAGMA page_count").fetchone()[0]
            size = conn.execute("PRAGMA page_size").fetchone()[0]
            free = conn.execute("PRAGMA freelist_count").fetchone()[0]
            return (pages - free) * size

        try:
            return int(self._run(read) or 0)
        except Exception:
            return 0

    def trim(self, max_bytes=MAX_BYTES, max_age_days=MAX_AGE_DAYS) -> int:
        """Drop what is too old, then the oldest windows until under
        `max_bytes`. Returns how many rows went."""
        cutoff = _window_cutoff(max_age_days)

        def trim(conn):
            dropped = 0
            dropped += conn.execute("DELETE FROM counters WHERE window < ?", (cutoff,)).rowcount
            dropped += conn.execute("DELETE FROM events WHERE window < ?", (cutoff,)).rowcount
            dropped += conn.execute("DELETE FROM outbox WHERE created < ?",
                                    (time.time() - max_age_days * 86400,)).rowcount
            for _ in range(10_000):
                pages = conn.execute("PRAGMA page_count").fetchone()[0]
                size = conn.execute("PRAGMA page_size").fetchone()[0]
                free = conn.execute("PRAGMA freelist_count").fetchone()[0]
                if (pages - free) * size <= max_bytes:
                    break
                oldest = conn.execute(
                    "SELECT MIN(w) FROM (SELECT MIN(window) AS w FROM counters "
                    "UNION ALL SELECT MIN(window) FROM events)").fetchone()[0]
                if oldest is None:
                    outbox = conn.execute("DELETE FROM outbox WHERE batch_id = (SELECT batch_id "
                                          "FROM outbox ORDER BY created LIMIT 1)").rowcount
                    dropped += outbox
                    if not outbox:
                        break
                    continue
                dropped += conn.execute("DELETE FROM counters WHERE window = ?",
                                        (oldest,)).rowcount
                dropped += conn.execute("DELETE FROM events WHERE window = ?",
                                        (oldest,)).rowcount
            try:
                conn.execute("PRAGMA incremental_vacuum")
            except sqlite3.Error:
                pass
            return dropped

        try:
            return int(self._run(trim) or 0)
        except Exception:
            return 0

    def clear(self) -> None:
        """Drop everything queued (the user turned telemetry off)."""
        def clear(conn):
            conn.execute("DELETE FROM counters")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM outbox")

        try:
            self._run(clear)
        except Exception:
            pass


def gzip_json(obj) -> tuple[bytes, int]:
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return gzip.compress(raw, 6), len(raw)
