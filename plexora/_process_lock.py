"""A read-modify-write lock that holds across processes, not just threads.

The viewer server and `plexora mcp serve` are two processes on one data root,
and the MCP server runs the same capabilities in-process: an agent's ROI edit,
gate or classifier change is the same load-change-store the browser's route
does. A `threading.Lock` around that section serializes the threads of ONE
process only, so the two processes could both read revision N, both write
N+1, and one side's edit vanished without either being told it conflicted.

`ProcessLock` began as the QC store's own lock (plugins/qc/server/results.py,
which now uses this one): a re-entrant thread lock, plus an advisory lock on a small file
for as long as the outermost hold lasts -- `fcntl.flock` on POSIX,
`msvcrt.locking` on Windows. The file is named by a callable resolved at each
outermost acquire, because the data root can change under a running process
(tests, a remote switch). When the file cannot be had (no folder for it, a
filesystem without locks) the thread lock alone is kept, which is what every
caller had before.

One lock object per key per process (`lock_for`): `flock` locks belong to an
open file description, so two objects on one file in one process would
block each other.

Stdlib only.
"""

from __future__ import annotations

import logging
import os
import threading
import time

try:
    import fcntl as _fcntl
    _msvcrt = None
except ImportError:  # Windows
    _fcntl = None
    try:
        import msvcrt as _msvcrt
    except ImportError:
        _msvcrt = None

log = logging.getLogger(__name__)

#: How long a Windows caller waits for another process's hold before going on
#: with the thread lock alone. POSIX `flock` simply blocks, as a thread lock
#: does; `msvcrt.locking` has no blocking form, so it is polled.
WINDOWS_WAIT_SECONDS = 120.0


class ProcessLock:
    """Re-entrant within a process; exclusive across processes for the outermost hold."""

    def __init__(self, path_fn):
        self._path_fn = path_fn
        self._lock = threading.RLock()
        self._depth = 0
        self._handle = None

    def _path(self):
        try:
            return self._path_fn()
        except Exception:  # no folder for it (a test double, a remote project)
            return None

    def _take_file(self):
        path = self._path()
        if path is None:
            return
        try:
            handle = open(os.fspath(path), "a+b")
        except OSError:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            elif _msvcrt is not None:
                deadline = time.monotonic() + WINDOWS_WAIT_SECONDS
                while True:
                    handle.seek(0)
                    try:
                        _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.02)
        except OSError:
            log.warning("could not lock %s; going on with this process's lock alone", path)
            handle.close()
            return
        self._handle = handle

    def _drop_file(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                handle.seek(0)
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            handle.close()

    def acquire(self, blocking=True, timeout=-1):
        if not self._lock.acquire(blocking, timeout):
            return False
        self._depth += 1
        if self._depth == 1:
            try:
                self._take_file()
            except BaseException:
                self._depth -= 1
                self._lock.release()
                raise
        return True

    def release(self):
        if self._depth == 1:
            self._drop_file()
        self._depth -= 1
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


_LOCKS: dict = {}
_GUARD = threading.Lock()


def lock_for(key, path_fn) -> ProcessLock:
    """The one `ProcessLock` of this process for `key` (any hashable naming
    what it guards). `path_fn() -> path` names its lock file; the first call
    for a key decides it."""
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = ProcessLock(path_fn)
        return lock
