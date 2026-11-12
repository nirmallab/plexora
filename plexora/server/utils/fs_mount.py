"""A remote file given to a C library as a path, where a file object will not do.

h5py reads a Python file object through its `fileobj` driver, whose largest
address is `SIZE_MAX - 1`: 4 GiB in the website's 32-bit WebAssembly. A
remote .ims beyond that -- the 349 GB Imaris file over SFTP, 2026-11-12 --
fails with "Unable to get link info (attempting I/O in temporary file space)"
at its first group past 4 GiB. Opened by path, HDF5's own driver addresses
64 bits, as it already does for a picked file.

On the website the boot sets a mounter (plexora/browser/remote_mount.py):
the file appears in Pyodide's filesystem and each read the library makes
there is a read of the `RemoteFile`, through its cache, made without
suspending (server/utils/no_suspend.py). Elsewhere there is none, and the
caller keeps the file object (a 64-bit desktop has no such limit).
"""

from __future__ import annotations

from typing import Callable, Optional

_mount: Optional[Callable[[object, str], str]] = None
_release: Optional[Callable[[str], None]] = None


def set_mounter(mount: Optional[Callable[[object, str], str]],
                release: Optional[Callable[[str], None]] = None) -> None:
    """`mount(stream, name) -> path` and `release(path)`; None: no mounter."""
    global _mount, _release
    _mount, _release = mount, release


def path_for(stream, name: str) -> Optional[str]:
    """A path in this process's filesystem that reads `stream`, or None where
    there is no mounter. Release it with `release(stream)`."""
    if _mount is None:
        return None
    path = _mount(stream, name)
    try:
        stream._plexora_mounted = path
    except AttributeError:
        pass
    return path


def release(stream) -> None:
    """The path `path_for` made for `stream`, gone."""
    path = getattr(stream, "_plexora_mounted", None)
    if path is None or _release is None:
        return
    try:
        _release(path)
    finally:
        stream._plexora_mounted = None
