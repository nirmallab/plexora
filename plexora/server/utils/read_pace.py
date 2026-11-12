"""How long reads wait, per source, told to the page as it happens.

The page says when something is slow because of the network or a drive
(services/handoffNotice.js `pace`, docs/internal/WEBSITE_TAB_LIMITS.md "When
to tell the user something is slow"). It can only say so with a measured
number, and the number is here. Each read is bracketed by `began`/`ended`:

- **network**, per host: every fetch from a remote host
  (`remote_store.ChunkCacheStore._fetch` -- https, s3, gs and sftp alike);
- **drive**, per file, on the desktop only (`time_tifffile`): tifffile's reads
  of a file it opened from a path. On the website the worker times a picked
  file's reads itself (workers/pythonWorker.js `drivePace`).

They are reported as intervals, `{cause, source, start, end, busy, bytes}`:
`start`/`end` in ms since the epoch, `busy` the ms of it during which at
least one read from that source was in flight -- the waiting, not the work
done between reads.

An interval closes when the source has been quiet for `GAP_S` or has been
busy for `REPORT_EVERY_S`, so a long wait is reported every second rather
than at its end, and a burst of small reads is one interval, not hundreds.

Where they go:
- the website: posted to the page as they close (plexora/browser/boot.py);
- the desktop: kept (`keep`) for the page to ask for (`since`, the
  `/read_pace` route, which turns keeping on with its first call).
Before either, `began`/`ended` return at once.
"""

from __future__ import annotations

import collections
import os
import threading
import time
from typing import Callable, Optional

#: The longest an interval runs before it is reported.
REPORT_EVERY_S = 1.0
#: Quiet this long between fetches ends an interval.
GAP_S = 0.25

#: The most intervals kept for the desktop's page (a minute of one busy
#: source is 60).
KEEP = 400

_reporter: Optional[Callable[[dict], None]] = None
_lock = threading.Lock()
#: (cause, source) -> {"inflight", "start", "last", "bytes", "busy", "since"}
#: (times from `_clock`; `since`: when the source last went from idle to in
#: flight).
_hosts: dict = {}
_clock = time.time
#: The desktop's kept intervals, each with its `seq`.
_kept: collections.deque = collections.deque(maxlen=KEEP)
_seq = 0


def set_reporter(reporter: Optional[Callable[[dict], None]]) -> None:
    """Who is told (None: nobody, and nothing is measured)."""
    global _reporter
    _reporter = reporter
    with _lock:
        _hosts.clear()


def _report(event: dict) -> None:
    reporter = _reporter
    if reporter is None:
        return
    try:
        reporter(event)
    except Exception:  # noqa: BLE001 -- a measurement is never worth a failure
        pass


def _keep(event: dict) -> None:
    global _seq
    with _lock:
        _seq += 1
        _kept.append({**event, "seq": _seq})


def keep() -> None:
    """Keep intervals for the page to ask for (the desktop). A reporter
    already set (the website's) is left as it is."""
    if _reporter is None:
        set_reporter(_keep)


def keeping() -> bool:
    return _reporter is _keep


def since(after: int = 0) -> tuple[list, int]:
    """The kept intervals after `seq` `after`, and the last `seq`. An `after`
    past the last (a page older than this process) gets them all."""
    with _lock:
        if after > _seq:
            after = 0
        return [event for event in _kept if event["seq"] > after], _seq


def _closed(key: tuple, state: dict, until: float) -> Optional[dict]:
    """The interval `state` holds, as an event ending at `until`, or None."""
    if state["start"] is None or until <= state["start"]:
        return None
    busy = state["busy"]
    if state["inflight"] > 0 and state["since"] is not None:
        busy += until - state["since"]
        state["since"] = until
    cause, source = key
    event = {"cause": cause, "source": source, "start": round(state["start"] * 1000),
             "end": round(until * 1000), "busy": round(busy * 1000), "bytes": int(state["bytes"])}
    state["start"], state["bytes"], state["busy"] = None, 0, 0.0
    return event


def began(source: str, cause: str = "network") -> None:
    """A read from `source` (a host, or a file for "drive") started."""
    if _reporter is None or not source:
        return
    now = _clock()
    event = None
    key = (cause, source)
    with _lock:
        state = _hosts.setdefault(key, {"inflight": 0, "start": None, "last": now, "bytes": 0,
                                        "busy": 0.0, "since": None})
        if state["inflight"] == 0 and state["start"] is not None and now - state["last"] > GAP_S:
            # Quiet since the last read ended: what was open is one interval.
            event = _closed(key, state, state["last"])
        if state["start"] is None:
            state["start"] = now
        if state["inflight"] == 0:
            state["since"] = now
        state["inflight"] += 1
    if event:
        _report(event)


def ended(source: str, nbytes: int = 0, cause: str = "network") -> None:
    """A read from `source` ended, having brought `nbytes`."""
    if _reporter is None or not source:
        return
    now = _clock()
    event = None
    key = (cause, source)
    with _lock:
        state = _hosts.get(key)
        if state is None:
            return
        state["inflight"] = max(0, state["inflight"] - 1)
        if state["inflight"] == 0 and state["since"] is not None:
            state["busy"] += now - state["since"]
            state["since"] = None
        state["bytes"] += max(0, int(nbytes or 0))
        state["last"] = now
        if state["start"] is not None and now - state["start"] >= REPORT_EVERY_S:
            event = _closed(key, state, now)
            if state["inflight"] > 0:
                # Still waiting: the next interval starts where this one ended.
                state["start"] = now
    if event:
        _report(event)


def flush() -> None:
    """Report every interval still open (tests; a page going idle)."""
    events = []
    with _lock:
        for key, state in _hosts.items():
            event = _closed(key, state, state["last"] if state["inflight"] == 0 else _clock())
            if event:
                events.append(event)
    for event in events:
        _report(event)


# -- drive: tifffile's reads of a file it opened from a path (the desktop) ------

_depth = threading.local()


def _drive_source(handle) -> Optional[str]:
    """The file name a tifffile FileHandle reads, if it opened it from a path
    (a stream it was given -- a remote file -- is the network's)."""
    file = getattr(handle, "_file", None)
    while file is not None and hasattr(file, "_file") and hasattr(file, "_fh"):
        file = file._file  # a FileHandle around another
    if isinstance(file, (str, os.PathLike)):
        return os.path.basename(os.fspath(file)) or None
    return None


def _size(result) -> int:
    if isinstance(result, int):
        return max(0, result)
    nbytes = getattr(result, "nbytes", None)
    if isinstance(nbytes, int):
        return nbytes
    try:
        return len(result)
    except TypeError:
        return 0


def _timed(original):
    def timed(self, *args, **kwargs):
        source = _drive_source(self) if _reporter is not None else None
        if source is None or getattr(_depth, "n", 0):
            return original(self, *args, **kwargs)
        _depth.n = 1
        began(source, "drive")
        nbytes = 0
        try:
            result = original(self, *args, **kwargs)
            nbytes = _size(result)
            return result
        finally:
            _depth.n = 0
            ended(source, nbytes, "drive")
    timed.__wrapped__ = original
    return timed


def _timed_segments(original):
    def read_segments(self, *args, **kwargs):
        source = _drive_source(self) if _reporter is not None else None
        segments = original(self, *args, **kwargs)
        if source is None:
            yield from segments
            return
        while True:
            # Each pass reads, then yields: the time to the yield is the
            # read's, and decoding happens in the caller, after it.
            began(source, "drive")
            nbytes = 0
            try:
                item = next(segments)
                for data, _index in (item if isinstance(item, list) else [item]):
                    nbytes += len(data) if data is not None else 0
            except StopIteration:
                return
            finally:
                ended(source, nbytes, "drive")
            yield item
    read_segments.__wrapped__ = original
    return read_segments


def time_tifffile() -> bool:
    """Time tifffile's reads of files it opened from a path, as "drive"
    intervals (the desktop: TIFF, OME-TIFF, SVS, NDPI and the other TIFF-based
    formats). Once; nothing is measured while no reporter is set. HDF5
    (.ims), zarr folders and the other formats' own readers are not timed."""
    try:
        import tifffile
    except ImportError:
        return False
    handle = tifffile.FileHandle
    if getattr(handle, "_plexora_paced", False):
        return True
    for name in ("read", "readinto", "read_array"):
        setattr(handle, name, _timed(getattr(handle, name)))
    handle.read_segments = _timed_segments(handle.read_segments)
    handle._plexora_paced = True
    return True
