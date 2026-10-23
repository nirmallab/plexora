"""Where every chunk of a large local Imaris file is, worked out once and kept.

WHY. The fastest way through an .ims on a cluster filesystem is to read its
chunks by offset, outside the file's lock and many at once (ims.py) -- but
HDF5 says where a chunk is only by walking the dataset's chunk index: one
chunk at a time (`get_chunk_info_by_coord`) that walk is linear in the chunks,
~10 ms a call on a 24,000-chunk Imaris channel; the whole dataset at once
(`chunk_iter`) about a second, cold. Measured on HMS O2 scratch, 16 cold
1024 px tiles: 37-58 ms a tile read with `read_direct_chunk` under the lock,
13-14 ms with every chunk's offset already known.

So the file's chunk index is walked ONCE, in a separate process -- an HDF5 of
its own, so the walk never holds the viewer's lock -- and saved; every later
open of the file reads it in milliseconds. Until it exists (the first minute
or so with a new file) the reader reads the way it would without one, and
looks again every `POLL_S`.

KEPT under the user's data root (`paths.ims_index_root()`), one `.npz` a file,
named for the file's path, size and modification time: a file rewritten in
place gets a new index, and one whose file has changed is never read. Each
dataset's chunk grid is three arrays -- byte offset (-1 for a chunk never
written), stored size, filter mask -- 16 bytes a chunk before compression.
The reader still compares the first chunk it reads by offset from each
dataset with h5py's own read of it (ims.py `_check_direct`), so an index that
is wrong for any reason costs speed, never a wrong picture.

ONLY FOR LARGE FILES ON A DISK (`MIN_BYTES`): a file at a web address would
need its whole index fetched to build one, and a small file reads quickly
either way. `PLEXORA_IMS_INDEX=0` turns building off.

h5py is imported inside the functions that need it, as ims.py does, so a
core install that never opens an .ims never loads it.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

#: Bumped when the file's layout changes; an index of another version is
#: built again.
VERSION = 1

#: Files smaller than this are not indexed: their chunks are few, and found
#: quickly enough one at a time.
MIN_BYTES = 1 << 30

#: How often a reader without an index looks for one again, in seconds.
POLL_S = 2.0

#: A builder's lock older than this is a builder that died: taken over.
STALE_LOCK_S = 3600.0

#: Worker processes one build walks the datasets with.
BUILD_WORKERS = 8

ENV_SWITCH = "PLEXORA_IMS_INDEX"

#: Files this process has started a builder for: asked once each.
_started: set = set()
_started_lock = threading.Lock()


def _root() -> Path:
    from plexora import paths

    return paths.ims_index_root()


def identity(path):
    """`(absolute path, size, mtime_ns)` of a local file, or None."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (os.path.abspath(str(path)), int(stat.st_size), int(stat.st_mtime_ns))


def _stem(ident) -> str:
    return hashlib.sha1(json.dumps([VERSION, *ident]).encode()).hexdigest()[:24]


def index_path(ident) -> Path:
    return _root() / f"{_stem(ident)}.npz"


def _lock_path(ident) -> Path:
    return _root() / f"{_stem(ident)}.building"


def _failed_path(ident) -> Path:
    return _root() / f"{_stem(ident)}.failed"


def _key(level, t, channel) -> str:
    return f"L{int(level)}_T{int(t)}_C{int(channel)}"


class ChunkIndex:
    """One file's saved index, each dataset's arrays read on first use."""

    def __init__(self, npz):
        self._npz = npz
        self._grids = {}
        self._lock = threading.Lock()

    def grid(self, level, t, channel):
        """`(offsets, sizes, masks)` over the dataset's chunk grid, or None
        when the index does not hold it."""
        key = _key(level, t, channel)
        with self._lock:
            if key in self._grids:
                return self._grids[key]
            try:
                grid = (self._npz[f"{key}_offsets"], self._npz[f"{key}_sizes"],
                        self._npz[f"{key}_masks"])
            except (KeyError, ValueError, OSError):
                grid = None
            self._grids[key] = grid
            return grid

    def close(self) -> None:
        with self._lock:
            self._grids.clear()
            try:
                self._npz.close()
            except Exception:  # noqa: BLE001 -- closing is best effort
                pass


def load(path):
    """The saved index of the file at `path`, or None: none built, the file
    changed since, or the index unreadable."""
    ident = identity(path)
    if ident is None:
        return None
    where = index_path(ident)
    if not where.exists():
        return None
    try:
        npz = np.load(where, allow_pickle=False)
    except Exception:  # noqa: BLE001 -- a damaged index is no index
        return None
    try:
        meta = json.loads(str(npz["meta"]))
        if [meta.get("version"), meta.get("path"), meta.get("size"), meta.get("mtime_ns")] \
                != [VERSION, *ident]:
            npz.close()
            return None
    except Exception:  # noqa: BLE001
        npz.close()
        return None
    return ChunkIndex(npz)


# -- building ------------------------------------------------------------------

def _datasets(handle) -> list:
    """`(level, t, channel)` of every chunked dataset in an open .ims."""
    found = []
    top = handle.get("DataSet")
    if top is None:
        return found
    for level_name in top:
        if not level_name.startswith("ResolutionLevel "):
            continue
        for t_name in top[level_name]:
            if not t_name.startswith("TimePoint "):
                continue
            for c_name in top[level_name][t_name]:
                if not c_name.startswith("Channel "):
                    continue
                data = top[level_name][t_name][c_name].get("Data")
                if data is None or not data.chunks or len(data.chunks) != 3:
                    continue
                found.append((int(level_name.split()[-1]), int(t_name.split()[-1]),
                              int(c_name.split()[-1])))
    return found


_worker_file = None


def _worker_open(path) -> None:
    global _worker_file
    import h5py

    _worker_file = h5py.File(path, "r")


def walk(handle, level, t, channel):
    """`(offsets, sizes, masks)` of one dataset, from one pass over its chunk
    index. Raises AttributeError where h5py cannot walk one (`chunk_iter`)."""
    data = handle[f"DataSet/ResolutionLevel {level}/TimePoint {t}/Channel {channel}/Data"]
    cz, cy, cx = (int(c) for c in data.chunks)
    grid = tuple(-(-int(size) // chunk) for size, chunk in zip(data.shape, (cz, cy, cx)))
    offsets = np.full(grid, -1, dtype=np.int64)
    sizes = np.zeros(grid, dtype=np.uint32)
    masks = np.zeros(grid, dtype=np.uint32)

    def put(info):
        z, y, x = info.chunk_offset
        at = (z // cz, y // cy, x // cx)
        offsets[at] = info.byte_offset
        sizes[at] = info.size
        masks[at] = info.filter_mask

    data.id.chunk_iter(put)
    return offsets, sizes, masks


def _walk_in_worker(dataset):
    return dataset, walk(_worker_file, *dataset)


def build(path, *, workers=None) -> Path:
    """Walk every dataset of the .ims at `path` and save its index; returns
    where. The datasets are shared out over worker processes, each with the
    file open once."""
    import h5py

    ident = identity(path)
    if ident is None:
        raise FileNotFoundError(path)
    with h5py.File(path, "r") as handle:
        datasets = _datasets(handle)
    arrays = {}
    count = max(1, min(int(workers or BUILD_WORKERS), len(datasets), os.cpu_count() or 1))
    if count == 1:
        with h5py.File(path, "r") as handle:
            for dataset in datasets:
                for name, array in zip(("offsets", "sizes", "masks"), walk(handle, *dataset)):
                    arrays[f"{_key(*dataset)}_{name}"] = array
    else:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor

        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(count, mp_context=context, initializer=_worker_open,
                                 initargs=(ident[0],)) as pool:
            for dataset, grid in pool.map(_walk_in_worker, datasets, chunksize=4):
                for name, array in zip(("offsets", "sizes", "masks"), grid):
                    arrays[f"{_key(*dataset)}_{name}"] = array
    if identity(path) != ident:
        raise RuntimeError("the file changed while its index was built")
    meta = {"version": VERSION, "path": ident[0], "size": ident[1], "mtime_ns": ident[2],
            "datasets": len(datasets)}
    where = index_path(ident)
    where.parent.mkdir(parents=True, exist_ok=True)
    tmp = where.with_name(f"{where.stem}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as out:
            np.savez_compressed(out, meta=np.array(json.dumps(meta)), **arrays)
        os.replace(tmp, where)
    finally:
        tmp.unlink(missing_ok=True)
    return where


def _lock(ident) -> bool:
    """Take the build of `ident`, or False when another builder has it."""
    lock = _lock_path(ident)
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not _stale(lock):
                return False
            lock.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w") as out:
            json.dump({"host": socket.gethostname(), "pid": os.getpid(), "started": time.time()}, out)
        return True
    return False


def _stale(lock) -> bool:
    """A lock whose builder is gone: dead on this host, or simply old."""
    try:
        held = json.loads(lock.read_text())
        age = time.time() - float(held.get("started", 0))
    except (OSError, ValueError):
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return True
        held = {}
    if age > STALE_LOCK_S:
        return True
    if held.get("host") == socket.gethostname() and os.name == "posix":
        try:
            os.kill(int(held["pid"]), 0)
        except (ProcessLookupError, KeyError, ValueError, TypeError):
            return True
        except PermissionError:
            return False
    return False


def main(argv=None) -> int:
    """`python -m plexora.server.volume.sources.ims_index FILE`: build FILE's
    index unless one is built or being built. A build that fails leaves a
    `.failed` note, so the file is not tried again until it changes."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m plexora.server.volume.sources.ims_index FILE.ims", file=sys.stderr)
        return 2
    path = args[0]
    ident = identity(path)
    if ident is None:
        print(f"{path}: no such file", file=sys.stderr)
        return 1
    if index_path(ident).exists() or not _lock(ident):
        return 0
    try:
        if hasattr(os, "nice"):
            try:
                os.nice(10)
            except OSError:
                pass
        started = time.perf_counter()
        where = build(path)
        print(f"{path}: indexed in {time.perf_counter() - started:.1f} s -> {where}")
        return 0
    except Exception as error:  # noqa: BLE001 -- recorded, then reported
        try:
            _failed_path(ident).write_text(f"{type(error).__name__}: {error}\n")
        except OSError:
            pass
        print(f"{path}: not indexed -- {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        _lock_path(ident).unlink(missing_ok=True)


def ensure(path) -> bool:
    """Start building the index of the local file at `path` in the
    background, if it is large enough to want one and has none, none on its
    way and no failed attempt. True when a builder was started. Never waits
    and never raises."""
    try:
        if os.environ.get(ENV_SWITCH, "1").strip().lower() in ("0", "false", "no", "off"):
            return False
        ident = identity(path)
        if ident is None or ident[1] < MIN_BYTES:
            return False
        with _started_lock:
            if ident in _started:
                return False
            _started.add(ident)
        if index_path(ident).exists() or _failed_path(ident).exists():
            return False
        lock = _lock_path(ident)
        if lock.exists() and not _stale(lock):
            return False
        from plexora._subprocess import popen_kwargs

        _root().mkdir(parents=True, exist_ok=True)
        with open(_root() / "build.log", "ab") as log:
            subprocess.Popen(
                [sys.executable, "-m", "plexora.server.volume.sources.ims_index", ident[0]],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                start_new_session=os.name == "posix", **popen_kwargs())
        return True
    except Exception:  # noqa: BLE001 -- an index is an optimisation, never an error
        return False


if __name__ == "__main__":
    sys.exit(main())
