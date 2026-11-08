"""Remote servers on the website: `remote_sessions`, answered by the page.

The website cannot run `ssh`, so a saved server is connected by the page
itself (services/webRemote.js): SSH in the page through the relay, for the
seconds it takes to start a data node detached on the server, and from then
on an end-to-end encrypted room the node dials out to. The design and its
reasons are in docs/internal/WEB_REMOTE_RELAY.md.

This module is what `remote_sessions` hands every call to when Plexora runs
in the browser (`active()`), so the routes that read connections -- Settings'
cards, `/data_places`, the connection dialog's poll, `/remote_health` -- read
the page's connections through the same session shape the desktop's threads
have, and nothing above this layer knows the difference. Two halves:

- **What to run.** `start` builds the start script from the saved server with
  the desktop's own command builders (`connect.node_command_line`,
  `srun_command_line`, `install_command_line`), so a profile means the same
  thing in both: the same srun line, the same environment, the same install.
  The room's id, password and key are made by the page and never pass through
  here; the script names the room by a placeholder the page fills in.
- **What is happening.** The page holds each connection (`/__plexora/remote/
  v1/sessions`); a `WebSession` here is a view of one, read when a route asks.
  The node it started is put on the map (`plexora.nodes.register_node`) at
  the page's address for its room, `<site>/__plexora/relay/<room>`, which the
  page answers through the room -- so a project's tiles, tables and file
  picker reach the node the way they reach any other, and keep reaching it
  across a tab reload: the room's credentials are kept by the page, and only
  a connection that ended is taken off the map.

A files-only server (`Remote.files`: an SFTP server where nothing of ours runs,
such as HMS O2's transfer node) has no node: the page holds the SSH
connection itself and opens its SFTP side (`files_plan`), and `PageFiles` is
`server/utils/sftp.py`'s client for it, registered under the server's ssh
target, so the chunk cache, browsing and `/fetch_file` read an `sftp://`
address on the website exactly as they do on the desktop.

Nothing here holds a secret: passwords and Duo answers go from the dialog to
the page (`answer`), never into a file or a log.
"""

from __future__ import annotations

import json
import os
import random
import shlex
import time
import urllib.error
import urllib.request
from concurrent.futures import Future
from urllib.parse import quote

#: The page's API for its connections, and the prefix it answers a room's
#: node traffic under (services/webRemote.js).
API_PREFIX = "/__plexora/remote/v1"
RELAY_PREFIX = "/__plexora/relay"
#: Where the page writes its room id into the start script. The room id is
#: base64url, so it needs no quoting where it lands.
ROOM_PLACEHOLDER = "__PLEXORA_ROOM__"
#: What the node prints each time it joins its room (server/node/relay_link.py).
JOINED_MARK = "[plexora-relay] joined"
#: How long a read of the page's connections is reused: one poll of the
#: dialog asks several routes, which should not each ask the page.
CACHE_SECONDS = 0.4
#: Where the node's port is drawn from. The node binds it on the server's
#: loopback, where only its own relay link talks to it.
PORTS = (20000, 60000)

#: The page's states are `remote_sessions`' own (STATE_*), so the words a
#: surface draws for a desktop connection are the words it draws here.
ENDED = ("failed", "exited")
#: How long one read of a files-only server may take, the page's wait for a
#: dropped connection to sign in again included (services/webRemote.js
#: FILES_WAIT_MS is 300 s).
FILES_CALL_TIMEOUT = 330.0


class PageError(RuntimeError):
    """The page did not answer as it should."""


def active() -> bool:
    """Whether this Plexora is the website's, whose connections the page holds."""
    return os.environ.get("PLEXORA_BACKEND") == "browser"


def _site() -> str:
    origin = (os.environ.get("PLEXORA_SITE_ORIGIN")
              or (os.environ.get("PLEXORA_ALLOWED_ORIGINS") or "").split(",")[0]).strip()
    base = (os.environ.get("PLEXORA_BASE_URL") or "").rstrip("/")
    return origin.rstrip("/") + base


def relay_endpoint(room: str) -> str:
    """The address a room's node is registered at: the page answers it."""
    return f"{_site()}{RELAY_PREFIX}/{room}"


def _call(method, path, body=None, timeout=15.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"{_site()}{API_PREFIX}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        raise PageError(payload.get("error") or f"The page answered {exc.code}.") from None
    except (urllib.error.URLError, OSError) as exc:
        raise PageError(f"The page did not answer: {exc}") from None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise PageError("The page's answer was not JSON.") from None


# -- what to run --------------------------------------------------------------


def refusal(remote) -> str | None:
    """Why the website cannot connect to `remote`, or None when it can."""
    if remote.gcloud:
        return None  # answered by the Google Cloud path (gcloud_vm_node), not here
    if remote.workstation and str((remote.workstation or {}).get("os", "")).lower() == "windows":
        return ("The website cannot start Plexora on a Windows workstation yet. "
                "Use the desktop app for this one.")
    if remote.jump:
        return ("This server is reached through a jump host, which the website cannot do yet. "
                "Use the desktop app for this one, or save the server without the jump host if "
                "it is reachable directly.")
    user, host = _user_host(remote.target)
    if not user:
        return (f"Add your user name to the address, as you@{host}: the website does not know "
                f"the account name on that machine.")
    if not host:
        return "This server has no address."
    return None


def _user_host(target):
    from plexora import connect

    user, host = connect.split_target(str(target or "").strip())
    return user, (host or "").strip()


def _host_port(host, ssh_opts=()):
    """`host` and its SSH port: `host:2222` names one, then a `Port=` among the
    profile's ssh options (what the desktop's ssh is given), then 22."""
    if host.count(":") == 1 and host.rsplit(":", 1)[1].isdigit():
        name, port = host.rsplit(":", 1)
        return name, int(port)
    for option in ssh_opts or ():
        key, _, value = str(option).partition("=")
        if key.strip().lower() == "port" and value.strip().isdigit():
            return host.strip("[]"), int(value.strip())
    return host.strip("[]"), 22


def files_plan(remote) -> dict:
    """What the page needs to connect a files-only server: where to sign in.

    Nothing runs over there, so there is no command: the page signs in and
    opens the server's SFTP side (services/webRemote.js `runFiles`).
    """
    from plexora import connect
    from plexora.server.utils import sftp

    user, host = _user_host(remote.target)
    host, ssh_port = _host_port(host, remote.ssh_opts)
    return {
        "kind": "files",
        "user": user,
        "host": host,
        "port": ssh_port,
        "target": sftp.canonical_target(remote.target),
        "timeout": int(connect.FILES_TIMEOUT),
    }


def start_script(remote, *, port=None, rng=random) -> dict:
    """The start command and what the page needs to run it.

    `{command, user, host, port, timeout, install, queued}`: `command` is one
    `sh -c '…'` with ROOM_PLACEHOLDER where the room id goes. The relay file
    arrives on its stdin as one line (the page writes it), so the room's key
    is never on a command line `ps` or `squeue` would show.
    """
    from plexora import connect

    user, host = _user_host(remote.target)
    host, ssh_port = _host_port(host, remote.ssh_opts)
    node_name = remote.node_name or remote.name
    node_port = int(port) if port is not None else rng.randint(*PORTS)
    launch = connect.node_command_line(
        remote.remote_command, node_port, (), plugins=remote.plugins, dynamic=True,
        node_id=f"connect-{connect._slug(node_name)}-data", host="127.0.0.1")
    launch += ' --relay-file "$d/$r.json"'
    if remote.srun is not None:
        launch = connect.srun_command_line(remote.srun, launch)
    lines = [
        "umask 077",
        'd="$HOME/.plexora/relay"',
        f"r={ROOM_PLACEHOLDER}",
        'mkdir -p "$d"',
        # The relay file: one line on stdin, never on a command line.
        'IFS= read -r line',
        'printf \'%s\\n\' "$line" > "$d/$r.json"',
    ]
    if remote.install:
        lines.append(f"{connect.install_command_line(remote.remote_command)} "
                     f"|| {{ echo 'Installing Plexora failed.'; exit 1; }}")
        lines.append(f"echo {connect.INSTALL_DONE_MARK}")
    # Detached, so the node outlives this SSH connection: the page closes it
    # once the node has joined its room. setsid where there is one (Linux);
    # elsewhere (a Mac workstation has no setsid) job control, which starts the
    # background job in a process group of its own, so neither a hangup nor a
    # signal to the script's group reaches it.
    lines.append("if command -v setsid >/dev/null 2>&1; then s=setsid; else s=; set -m 2>/dev/null; fi")
    lines.append(f'nohup $s {launch} > "$d/$r.log" 2>&1 < /dev/null &')
    lines.append('echo "[plexora-start] pid=$!"')
    lines.append('exec tail -n +1 -F "$d/$r.log" 2>/dev/null')
    script = "\n".join(lines)
    timeout = connect.default_timeout(None, remote.srun) if remote.srun is not None \
        else connect.NODE_START_TIMEOUT
    if remote.install:
        timeout += connect.INSTALL_TIMEOUT
    return {
        "command": "sh -c " + shlex.quote(script),
        "user": user,
        "host": host,
        "port": ssh_port,
        "timeout": int(timeout),
        "install": bool(remote.install),
        "queued": remote.srun is not None,
        "node_name": node_name,
        "marks": {"installed": connect.INSTALL_DONE_MARK, "joined": JOINED_MARK},
    }


# -- what is happening -------------------------------------------------------


class _Tail:
    """What connect's failure hints read off a watched process: its lines."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.alive = False

    def tail(self, count=12):
        return self.lines[-count:]


def _diagnosis(remote, view) -> str | None:
    """The desktop's account of a start that failed, from the lines it printed."""
    from plexora import connect

    error = view.get("error")
    if view.get("state") != "failed" or not view.get("ended_by_script"):
        return error
    lines = view.get("lines") or []
    stale = connect.unsupported_remote_flag(lines)
    if stale:
        return str(connect._old_remote_hint(remote.target, stale, remote.remote_command, _Tail(lines)))
    refused = connect.scheduler_refusal(lines)
    if refused:
        return refused
    if connect.looks_like_missing_command(lines):
        return str(connect._missing_command_hint(remote.remote_command, _Tail(lines)))
    return error


class WebSession:
    """One of the page's connections, in the shape `RemoteSession` has.

    Built from the page's record each time a route asks (`get`), so it is
    never stale and never the thing that keeps a connection alive.
    """

    def __init__(self, remote, view):
        from plexora.server.models import remote_sessions

        self.remote = remote
        self.view = view
        self.name = remote.name if remote is not None else view.get("name")
        self.kind = remote_sessions.KIND_NODE
        self.id = view.get("id")
        self.state = view.get("state") or "connecting"
        self.room = view.get("room")

    @property
    def node_name(self):
        return (getattr(self.remote, "node_name", None) or self.name) if self.remote else self.name

    @property
    def files_only(self):
        return bool(getattr(self.remote, "files", None))

    def _files_place(self):
        """`{base, home}` once a files-only connection is up, as the desktop's."""
        home = (self.view.get("files") or {}).get("home")
        if not self.files_only or self.state != "connected" or not home:
            return None
        from plexora.server.utils import sftp

        return {"base": f"{sftp.SCHEME}://{sftp.canonical_target(self.remote.target)}",
                "home": home}

    @property
    def time_limit(self):
        from plexora.server.models import recipes

        return recipes.srun_seconds(self.remote.srun) if self.remote is not None else None

    @property
    def expires_at(self):
        started = self.view.get("job_started_at")
        if not self.time_limit or not started:
            return None
        if self.state in ENDED:
            return None
        return float(started) / 1000.0 + self.time_limit

    @property
    def time_left(self):
        expires = self.expires_at
        if expires is None:
            return None
        return max(0, int(round(expires - time.time())))

    def _phrase(self):
        from plexora.server.models import remote_sessions

        if self.state == "waiting_for_app":
            return ("Starting the data node over there… a first start can take a few "
                    "minutes while it loads.")
        if self.state == "connecting" and self.view.get("detail"):
            return self.view["detail"]
        if self.files_only and self.state == "connecting":
            return "Opening an SFTP connection… nothing is started over there."
        return remote_sessions.PHRASES.get(self.state, "")

    def status(self, log_lines=25):
        from plexora.server.models import remote_sessions

        view = self.view
        node = view.get("node") or {}
        lines = view.get("lines") or []
        error = _diagnosis(self.remote, view) if self.remote is not None else view.get("error")
        found = (node.get("platform") or None)
        return {
            "name": self.name,
            "kind": self.kind,
            "phase": self._phrase(),
            "node": (self.node_name if self.state == "connected" and node
                     and not self.files_only else None),
            "files": self._files_place(),
            "state": self.state,
            "time_limit": self.time_limit,
            "time_left": self.time_left,
            "error": error,
            "recovery": "",
            "url": None,
            "prompt": view.get("prompt"),
            "data_nodes": [],
            "node_errors": [],
            "platform": found,
            "os_mismatch": None,
            "log": [remote_sessions.redact(line) for line in lines[-int(log_lines):]],
        }

    def answer(self, text, prompt_id=None):
        try:
            _call("POST", f"/sessions/{self.id}/answer", {"id": prompt_id, "answer": text})
        except PageError:
            return False
        _forget_cache()
        return True

    def stop(self):
        try:
            _call("DELETE", f"/sessions/{self.id}")
        except PageError:
            pass
        _forget_cache()
        if self.files_only:
            _files_unregister(self.remote)
        else:
            _unregister(self.node_name)

    def drop_own_node(self):
        _unregister(self.node_name)

    def _connection_alive(self):
        return self.state not in ENDED


def _files_call(name, body, timeout):
    """`(status, body bytes)` for one read of a files-only server, asked of
    the page; URLError/OSError when the page could not be asked at all."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"{_site()}{API_PREFIX}/files/{quote(name, safe='')}",
                                     data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


class PageFiles:
    """`sftp.SFTPClient`'s reading half, answered by the page.

    The website has no `ssh` to spawn: the page signs in through the relay and
    holds the SFTP conversation (services/webRemote.js), and this asks it one
    call at a time at `/__plexora/remote/v1/files/<profile name>`. By name, so
    a connection the page signed in to again (after a relay restart, or a
    reload of the tab) answers without anything here being made again.

    The page waits for a connection that is signing in again before it
    answers, so a read made meanwhile is slow rather than failed; one the page
    cannot answer (Disconnect, a sign-in nobody completed) is
    `sftp.SFTPNotConnected`, the desktop's sentence and its handling.
    """

    #: `sftp.stats()` reads the process id of the desktop's ssh; there is none.
    process = None

    def __init__(self, name, target, home=None):
        self.name = name
        self.target = target
        self.label = target
        self.home = home
        self.call_timeout = FILES_CALL_TIMEOUT
        self.counters = {"requests": 0, "replies": 0, "bytes_out": 0, "bytes_in": 0,
                         "reads": 0, "read_bytes": 0, "wait_s": 0.0}
        self._closed = False

    @property
    def alive(self) -> bool:
        return not self._closed

    def close(self) -> None:
        self._closed = True

    def run(self, func, *args):
        """`func(*args)` now, as a finished Future: the website's Python has no
        threads to hand it to, and the call waits on the page either way."""
        future = Future()
        try:
            future.set_result(func(*args))
        except BaseException as exc:  # noqa: BLE001 -- handed to the caller
            future.set_exception(exc)
        return future

    def _ask(self, body, *, raw=False):
        from plexora.server.utils import sftp

        started = time.monotonic()
        self.counters["requests"] += 1
        try:
            code, payload = _files_call(self.name, body, self.call_timeout)
        except TimeoutError:
            raise
        except (urllib.error.URLError, OSError) as exc:
            raise sftp.SFTPError(f"The page did not answer: {exc}") from None
        finally:
            self.counters["wait_s"] += time.monotonic() - started
        if code >= 400:
            try:
                said = json.loads(payload or b"{}")
            except ValueError:
                said = {}
            if said.get("not_connected"):
                raise sftp.SFTPNotConnected(self.target)
            raise sftp._status_error(int(said.get("fx", sftp.FX_FAILURE)), said.get("error") or "",
                                     body.get("path", ""))
        self.counters["replies"] += 1
        self.counters["bytes_in"] += len(payload)
        if raw:
            return payload
        try:
            return json.loads(payload or b"{}")
        except ValueError:
            raise sftp.SFTPError("The page's answer was not JSON.") from None

    @staticmethod
    def _attrs(said):
        from plexora.server.utils import sftp

        said = said or {}
        return sftp.Attrs(size=said.get("size"), mode=said.get("mode"), mtime=said.get("mtime"))

    def stat(self, path):
        return self._attrs(self._ask({"op": "stat", "path": path}).get("attrs"))

    def lstat(self, path):
        return self._attrs(self._ask({"op": "lstat", "path": path}).get("attrs"))

    def realpath(self, path):
        return str(self._ask({"op": "realpath", "path": path}).get("path") or "")

    def listdir(self, path):
        return [(str(name), self._attrs(attrs))
                for name, attrs in self._ask({"op": "listdir", "path": path}).get("entries") or []]

    def read_range(self, path, start, end):
        start, end = max(0, int(start)), int(end)
        if end <= start:
            return b""
        data = self._ask({"op": "read", "path": path, "start": start, "end": end}, raw=True)
        self.counters["reads"] += 1
        self.counters["read_bytes"] += len(data)
        return data

    def read_all(self, path):
        data = self._ask({"op": "read", "path": path, "start": 0}, raw=True)
        self.counters["reads"] += 1
        self.counters["read_bytes"] += len(data)
        return data


def _files_target(remote):
    from plexora.server.utils import sftp

    return sftp.canonical_target(remote.target)


def _files_register(session):
    """A files-only connection the page holds (or is signing in to again),
    as `sftp`'s client for its target. Once per connection."""
    from plexora.server.utils import sftp

    remote = session.remote
    home = (session.view.get("files") or {}).get("home")
    if remote is None or not home:
        return
    target = _files_target(remote)
    current = sftp._clients.get(target)
    if isinstance(current, PageFiles) and current.alive and current.name == remote.name:
        current.home = home
        return
    sftp.register(target, PageFiles(remote.name, target, home))


def _files_unregister(remote):
    from plexora.server.utils import sftp

    if remote is None:
        return
    target = _files_target(remote)
    current = sftp._clients.get(target)
    if isinstance(current, PageFiles) and current.name == remote.name:
        current.close()
        sftp.unregister(target, current)


def files_client(target):
    """`sftp`'s client for `target` on the website, from the page's connections:
    what `sftp.client_for` asks when nothing is registered (browser/boot.py)."""
    from plexora.server.utils import sftp

    _views()
    return sftp._clients.get(sftp.canonical_target(target))


_cache = {"at": 0.0, "log": 0, "views": None}


def _forget_cache():
    _cache["views"] = None


def _views(log_lines=25):
    """The page's connections, read at most once per CACHE_SECONDS."""
    now = time.monotonic()
    if (_cache["views"] is not None and now - _cache["at"] < CACHE_SECONDS
            and _cache["log"] >= log_lines):
        return _cache["views"]
    try:
        payload = _call("GET", f"/sessions?log={int(log_lines)}")
    except PageError:
        payload = {"sessions": [], "rooms": None}
    views = payload.get("sessions") or []
    _cache.update(at=now, log=log_lines, views=views)
    _reconcile(views, payload.get("rooms"))
    return views


def _remote(name):
    from plexora.server.models import remotes as remote_store

    return remote_store.find(name)


def _register(session):
    """Put a connected session's node on the map, once, at its room's address."""
    from plexora import nodes as node_api
    from plexora.server.models import nodes as node_registry

    node = session.view.get("node") or {}
    room = session.room
    if session.state != "connected" or not node or not room:
        return
    name = session.node_name
    endpoint = relay_endpoint(room)
    entry = node_registry.load_all().get(name)
    if entry is not None and entry.endpoint == endpoint and entry.token == node.get("token"):
        return
    try:
        node_api.register_node(name, endpoint, token=node.get("token"), browser_endpoint=endpoint,
                               managed_by=f"connect:{name}", expires_at=session.expires_at)
    except Exception:  # noqa: BLE001 - asked again on the next poll
        pass


def _unregister(name):
    """Take a node a website connection put on the map off it. Never fatal."""
    try:
        from plexora import nodes as node_api
        from plexora.server.models import nodes as node_registry

        entry = node_registry.load_all().get(name)
        if (entry is not None and entry.managed_by == f"connect:{name}"
                and RELAY_PREFIX in str(entry.endpoint or "")):
            node_api.forget_node(name)
    except Exception:  # noqa: BLE001
        pass


def _reconcile(views, rooms):
    """Keep the map in step with the page: connected nodes on, ended ones off.

    `rooms` is every room the page still holds the key to; a node on the map
    at a room the page no longer knows (its storage was cleared) cannot be
    reached again and comes off too.
    """
    for view in views:
        session = WebSession(_remote(view.get("name")), view)
        if session.files_only:
            # Kept while the page signs in again (it holds the reads), gone
            # once the connection has ended.
            if session.state in ENDED:
                _files_unregister(session.remote)
            else:
                _files_register(session)
            continue
        if session.state == "connected":
            _register(session)
        elif session.state in ENDED:
            _unregister(session.node_name)
    if rooms is None:
        return
    try:
        from plexora.server.models import nodes as node_registry

        known = set(rooms)
        for name, entry in node_registry.load_all().items():
            endpoint = str(entry.endpoint or "")
            if RELAY_PREFIX in endpoint and endpoint.rsplit("/", 1)[-1] not in known:
                _unregister(name)
    except Exception:  # noqa: BLE001
        pass


# -- remote_sessions' API ---------------------------------------------------


def get(name, kind=None):
    from plexora.server.models import remote_sessions

    if kind not in (None, remote_sessions.KIND_NODE):
        return None
    for view in _views():
        if view.get("name") == name:
            return WebSession(_remote(name), view)
    return None


def all_sessions():
    from plexora.server.models import remote_sessions

    return {remote_sessions._key(remote_sessions.KIND_NODE, view.get("name")):
            WebSession(_remote(view.get("name")), view) for view in _views()}


def start(remote, *, kind=None, **_ignored):
    """Ask the page to connect `remote`; the session as it then stands."""
    from plexora.server.models import remote_sessions

    problem = refusal(remote)
    if problem:
        raise remote_sessions.ConnectionRefused(problem)
    plan = files_plan(remote) if remote.files else start_script(remote)
    try:
        view = _call("POST", "/sessions", {"name": remote.name, "plan": plan})
    except PageError as exc:
        raise remote_sessions.ConnectionRefused(str(exc)) from None
    _forget_cache()
    return WebSession(remote, view)


def stop(name, kind=None):
    session = get(name, kind)
    if session is None:
        return False
    session.stop()
    return True


def forget(name, kind=None):
    stop(name, kind)


def find_by_nonce(nonce):
    return None
