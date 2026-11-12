"""A remote file as a path in the website's filesystem (server/utils/fs_mount.py).

The worker makes the file (workers/pythonWorker.js `plexoraMountReader`): its
size is the remote file's, and each read HDF5 (or any C library) makes of it
calls `read` below, from inside the read syscall. That is a call back into
Python with JavaScript frames under it, so the read is made inside
`no_suspend.held()`: synchronously, through `RemoteFile` and its cache.
"""

from __future__ import annotations

import itertools
import re

ROOT = "/remote"
_names = itertools.count(1)
#: path -> (the read callback's proxy, the stream)
_mounted: dict = {}


def mount(stream, name: str) -> str:
    import js
    from pyodide.ffi import create_proxy

    from plexora.server.utils import no_suspend

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "file")[-80:]
    path = f"{ROOT}/{next(_names)}-{safe}"

    def read(position, length):
        with no_suspend.held():
            return stream.pread(int(position), int(length))

    proxy = create_proxy(read)
    js.self.plexoraMountReader(path, int(stream.size), proxy)
    _mounted[path] = (proxy, stream)
    return path


def release(path: str) -> None:
    import js

    held = _mounted.pop(path, None)
    try:
        js.self.plexoraUnmountReader(path)
    finally:
        if held is not None:
            held[0].destroy()


def available() -> bool:
    """Whether this worker can make such a file."""
    try:
        import js
    except ImportError:
        return False
    return bool(getattr(js.self, "plexoraMountReader", None))
