"""Which Plexora servers are running against this data directory.

An agent's MCP process runs on its own and needs no viewer, but when a viewer
IS open it wants to find the server behind it: to tell the page an agent
changed a gate, and to send it viewer commands. Every serve site announces
itself here (`announce`), and forgets itself on the way out (`forget`); a
record whose process died without forgetting is filtered by `records()`, which
checks the pid.

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
    """Whether `pid` exists; True when that cannot be established (see
    jupyter._process_alive -- never `os.kill` on Windows)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
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


def records(root=None) -> list:
    """Every announced server whose process is alive, newest first, as
    `{url, token, pid, mode, source}`. The sidecar registry follows."""
    root_path = _path(root)
    found = []
    for record in _read(root_path / SERVERS_FILENAME).values():
        if not isinstance(record, dict) or not record.get("port"):
            continue
        if not _alive(record.get("pid")):
            continue
        found.append({"url": _url(record), "token": record.get("token"),
                      "pid": record.get("pid"), "mode": record.get("mode"),
                      "started": record.get("started") or 0, "source": "servers.json"})
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
