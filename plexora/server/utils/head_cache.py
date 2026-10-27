"""Where the first megabyte of a remote table is kept on this machine.

Detecting what a file on an SFTP server or a data node IS reads its header
and first rows, and that copy is kept so asking twice reads once. It used to
be kept under a fixed directory in the system temp dir, shared by every
account on the machine and created with the default mode -- so on a login
node another user could read the copies, or place a file there ahead of time
under the name a copy would get and have it read as the remote file.

Now each account has its own directory, created owner-only, and refused if
it is anything else (someone else's, a symlink, open to others): then this
process uses a private directory of its own instead. The temp dir is kept
rather than the data root because these are throwaway copies and the data
root is often on a slow network filesystem.
"""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
import threading
from pathlib import Path

_fallback = None
_lock = threading.Lock()


def _private_dir(path: Path) -> bool:
    """Whether `path` is a real directory only this account can use."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    if os.name == "nt":
        return True
    return info.st_uid == os.geteuid() and not (info.st_mode & 0o077)


def root() -> Path:
    """This account's private head-copy directory."""
    global _fallback
    base = Path(tempfile.gettempdir())
    owner = os.geteuid() if hasattr(os, "geteuid") else os.environ.get("USERNAME", "user")
    mine = base / f"plexora-{owner}"
    try:
        mine.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        pass
    if _private_dir(mine):
        return mine
    with _lock:
        if _fallback is None or not _private_dir(_fallback):
            _fallback = Path(tempfile.mkdtemp(prefix="plexora-"))
        return _fallback


def place(kind: str, key: str, name: str) -> Path:
    """Where the copy of the file called `name`, identified by `key`, goes.

    `name` comes from the far side, so only its last component is used, and a
    name that is no name at all is replaced.
    """
    name = Path(str(name).replace("\\", "/")).name
    if name in ("", ".", ".."):
        name = "file"
    return root() / kind / key / name


def write(local: Path, data: bytes) -> None:
    """Put `data` at `local`, whole or not at all, never through a link."""
    local.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    partial = local.with_name(f"{local.name}.{secrets.token_hex(6)}.partial")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) \
        | getattr(os, "O_BINARY", 0)
    fd = os.open(partial, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(partial, local)
    finally:
        try:
            partial.unlink()
        except OSError:
            pass
