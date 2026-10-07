"""Which Plexora servers are running against this data directory.

An agent's MCP process runs on its own and needs no viewer, but when a viewer
IS open it wants to find the server behind it: to tell the page an agent
changed a gate, and to send it viewer commands. Every serve site announces
itself here (`announce`), and forgets itself on the way out (`forget`); a
record whose process died without forgetting is filtered by `records()`, which
checks the pid (on Windows through the kernel, never `os.kill`) and drops it
from the file.

`<data_root>/servers.json`, owner-readable only because it holds each server's
token -- the same discipline as `nodes.json`. The notebook's sidecars have kept
a registry of their own since before this existed (`sidecars.json`, see
plexora/jupyter.py); `records()` reads both, so a server started from a
notebook is found too.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

SERVERS_FILENAME = "servers.json"
SIDECARS_FILENAME = "sidecars.json"


def _path(root=None) -> Path:
    from plexora import paths

    return Path(root) if root is not None else paths.data_root()


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _alive(pid) -> bool:
    """Whether `pid` is a running process. True when that cannot be
    established: a wrong "yes" costs one wasted probe, a wrong "no" hides a
    healthy server.

    Never `os.kill` on Windows -- there it calls TerminateProcess whatever the
    signal (see jupyter._process_alive) -- so Windows asks the kernel instead
    (`_alive_windows`)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _alive_windows(pid: int) -> bool:
    """OpenProcess + GetExitCodeProcess: a pid with no process behind it
    cannot be opened (ERROR_INVALID_PARAMETER); one that exited and is still
    held open by someone reports an exit code other than STILL_ACTIVE. A
    process of another user that cannot be opened (access denied) exists."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover - no ctypes, an odd Python: assume alive
        return True


def announce(port, token=None, *, mode="terminal", host="127.0.0.1", base_url="",
             pid=None, root=None) -> dict:
    """Record this process's server. Failure is swallowed: a read-only data
    directory must not stop a viewer starting."""
    from plexora.server.models.secret_store import write_private_json

    pid = int(pid or os.getpid())
    record = {
        "pid": pid,
        "port": int(port),
        "host": host if host not in ("0.0.0.0", "", "::") else "127.0.0.1",
        "token": token or None,
        "base_url": base_url or "",
        "mode": mode,
        "started": time.time(),
    }
    try:
        path = _path(root) / SERVERS_FILENAME
        data = {key: value for key, value in _read(path).items()
                if isinstance(value, dict) and _alive(value.get("pid"))}
        data[str(pid)] = record
        write_private_json(path, data)
    except Exception:
        pass
    return record


def forget(pid=None, root=None) -> None:
    from plexora.server.models.secret_store import write_private_json

    pid = str(int(pid or os.getpid()))
    try:
        path = _path(root) / SERVERS_FILENAME
        data = _read(path)
        if data.pop(pid, None) is not None:
            write_private_json(path, data)
    except Exception:
        pass


def _url(record) -> str:
    """Where the server answers on this machine: its own port, at the root.

    `base_url` is how a BROWSER reaches it through a proxy (jupyter-server-
    proxy, Open OnDemand); the proxy strips it on the way in, so a local
    client goes to the port directly.
    """
    host = record.get("host") or "127.0.0.1"
    return f"http://{host}:{int(record['port'])}/"


def _prune(path: Path, dead) -> None:
    """Drop the records of processes found dead from servers.json, so the
    next reader neither checks them again nor probes their ports. Read again
    just before writing, and only those keys removed, so a server that
    announced itself meanwhile is kept. Failure is swallowed."""
    if not dead:
        return
    from plexora.server.models.secret_store import write_private_json

    try:
        data = _read(path)
        gone = [key for key in dead if key in data]
        if not gone:
            return
        for key in gone:
            data.pop(key, None)
        write_private_json(path, data)
    except Exception:
        pass


def records(root=None, *, prune=True) -> list:
    """Every announced server whose process is alive, newest first, as
    `{url, token, pid, mode, source}`. The sidecar registry follows.

    A servers.json record whose process is gone is dropped from the file too
    (`prune`); the notebook's sidecars.json is its own registry
    (plexora/jupyter.py) and is only filtered."""
    root_path = _path(root)
    servers_path = root_path / SERVERS_FILENAME
    found = []
    dead = []
    for key, record in _read(servers_path).items():
        if not isinstance(record, dict) or not record.get("port"):
            continue
        if not _alive(record.get("pid")):
            dead.append(key)
            continue
        found.append({"url": _url(record), "token": record.get("token"),
                      "pid": record.get("pid"), "mode": record.get("mode"),
                      "started": record.get("started") or 0, "source": "servers.json"})
    if prune:
        _prune(servers_path, dead)
    found.sort(key=lambda r: r["started"], reverse=True)
    for record in _read(root_path / SIDECARS_FILENAME).values():
        if not isinstance(record, dict) or not record.get("port"):
            continue
        if not _alive(record.get("pid")):
            continue
        found.append({"url": f"http://127.0.0.1:{int(record['port'])}/",
                      "token": record.get("token"), "pid": record.get("pid"),
                      "mode": "notebook", "started": record.get("started") or 0,
                      "source": "sidecars.json"})
    return found


def stamp(root=None):
    """A cheap fingerprint of the two registries (their mtimes): it changes
    when a server announces or forgets itself, so a process watching for a
    viewer can look again at once instead of polling the network."""
    try:
        root_path = _path(root)
    except Exception:
        return None
    out = []
    for name in (SERVERS_FILENAME, SIDECARS_FILENAME):
        try:
            out.append((root_path / name).stat().st_mtime_ns)
        except OSError:
            out.append(None)
    return tuple(out)
