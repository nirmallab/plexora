"""Finding, and talking to, a Plexora server that is already running.

The MCP process does all its headless work itself. A running Plexora server is
only needed for two things: telling open viewers that an agent changed
something (so the gating overlay redraws without a reload), and sending viewer
commands (plexora/agent/viewer.py). Both are optional -- nothing here is on the
path of a read -- and a server that is not there is an ordinary answer.

Found, in order: `--server`/`--token`, then `PLEXORA_SERVER_URL` /
`PLEXORA_AUTH_TOKEN`, then the servers this data directory's running
instances announced (`<data_root>/servers.json`, and the notebook's
`sidecars.json`), taking the first that answers `/health` -- and, among
those, one whose viewer control plane (`/agent/v1`) answers, so a viewer
started before agents could drive it is not picked over one that can.

Loopback HTTP with `?token=` -- the same token every other client of that
server uses. Nothing is sent anywhere else.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_S = 3.0

#: How long a candidate on this machine gets to accept a connection. A
#: listening port accepts at once even when the server is busy (the kernel
#: queues it); a dead one on Windows takes ~2 s to refuse (SYN retries), which
#: is what made every search cost 2 s per dead record.
LOCAL_CONNECT_TIMEOUT_S = 0.3
#: The same, for a server on another host (`PLEXORA_SERVER_URL`, `--server`).
REMOTE_CONNECT_TIMEOUT_S = TIMEOUT_S

#: The route whose answer says a server has the agent viewer control plane.
CONTROL_PLANE_PROBE = "/agent/v1/viewer/sessions"

_UNPROBED = object()


class ServerLink:
    """One running Plexora server, as a base URL and its token."""

    def __init__(self, base_url: str, token: str | None = None, *, source: str = "flag"):
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token or None
        self.source = source
        self._control_plane = _UNPROBED
        #: When a request last found nobody answering (time.monotonic()), or
        #: None once one is answered. A holder of a cached link stops sending
        #: events to it until it is found again (plexora/mcp/server.py),
        #: rather than paying a refused connect per event.
        self.unreachable_at = None

    def describe(self) -> dict:
        return {"url": self.base_url, "authenticated": bool(self.token),
                "found_via": self.source, "control_plane": self.control_plane}

    def probe_control_plane(self):
        """True when the server answers the agent viewer routes, False when it
        predates them (404), None when nobody answered."""
        try:
            status, _ = self.request("GET", CONTROL_PLANE_PROBE, timeout=TIMEOUT_S)
        except OSError:
            return None
        if status == 404:
            return False
        return 200 <= status < 300 or None

    @property
    def control_plane(self):
        """`probe_control_plane`, asked once per link."""
        if self._control_plane is _UNPROBED:
            self._control_plane = self.probe_control_plane()
        return self._control_plane

    def url(self, path: str, **query) -> str:
        params = {k: v for k, v in query.items() if v is not None}
        if self.token:
            params["token"] = self.token
        full = urllib.parse.urljoin(self.base_url, path.lstrip("/"))
        return full + ("?" + urllib.parse.urlencode(params) if params else "")

    def request(self, method: str, path: str, body=None, *, timeout=TIMEOUT_S, **query):
        """(status, parsed JSON or bytes). Raises OSError when nobody answers."""
        data = None
        headers = {}
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
                headers["Content-Type"] = "application/octet-stream"
            else:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url(path, **query), data=data,
                                         method=method, headers=headers)
        import time

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
                status = response.status
                kind = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
            kind = exc.headers.get("Content-Type", "") if exc.headers else ""
        except OSError:
            self.unreachable_at = time.monotonic()
            raise
        self.unreachable_at = None
        if "json" in kind:
            try:
                return status, json.loads(payload or b"null")
            except ValueError:
                return status, payload
        return status, payload

    @property
    def is_local(self) -> bool:
        host = (urllib.parse.urlsplit(self.base_url).hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")

    def accepts(self, timeout=None) -> bool:
        """Whether anything accepts a connection on the server's port, within
        `timeout` (short on this machine: `LOCAL_CONNECT_TIMEOUT_S`)."""
        import socket

        parts = urllib.parse.urlsplit(self.base_url)
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            return False
        if timeout is None:
            timeout = LOCAL_CONNECT_TIMEOUT_S if self.is_local else REMOTE_CONNECT_TIMEOUT_S
        try:
            with socket.create_connection((parts.hostname or "127.0.0.1", port),
                                          timeout=timeout):
                return True
        except OSError:
            return False

    def health(self, timeout=TIMEOUT_S) -> bool:
        try:
            status, _ = self.request("GET", "/health", timeout=timeout)
        except OSError:
            return False
        return 200 <= status < 300

    def notify(self, project, plugin, kind, payload=None) -> bool:
        """Tell this server's open viewers that `plugin`'s state for `project`
        changed. False when the server has no viewer channel or nobody answered
        (then `request` set `unreachable_at`)."""
        try:
            status, answer = self.request("POST", "/agent/v1/events", {
                "project": project, "plugin": plugin, "kind": kind,
                "payload": payload or {}, "origin": "agent"})
        except OSError:
            return False
        return status == 200 and isinstance(answer, dict) and bool(answer.get("delivered"))


def _candidates(server=None, token=None):
    """The servers to try, in order of preference, each URL once: the flag;
    else the environment, then the announced servers newest first (a URL
    announced by several records -- a port reused by successive servers --
    is the newest one's)."""
    if server:
        yield ServerLink(server, token, source="flag")
        return
    seen = set()

    def first(link):
        if link.base_url in seen:
            return None
        seen.add(link.base_url)
        return link

    env_url = os.environ.get("PLEXORA_SERVER_URL")
    if env_url:
        link = first(ServerLink(env_url, token or os.environ.get("PLEXORA_AUTH_TOKEN"),
                                source="environment"))
        if link is not None:
            yield link
    try:
        from plexora.server.models import server_records
    except ImportError:  # pragma: no cover
        return
    for record in server_records.records():
        link = first(ServerLink(record["url"], record.get("token"),
                                source=record.get("source", "servers.json")))
        if link is not None:
            yield link


def _answering(links):
    """The links that answer `/health`, in their order -- probed in
    parallel, and only after their port accepted a connection within a short
    timeout, so a search over dead records costs one short timeout, not two
    seconds per record."""
    links = list(links)
    if not links:
        return []
    if len(links) == 1:
        link = links[0]
        return [link] if link.accepts() and link.health() else []
    from concurrent.futures import ThreadPoolExecutor

    def probe(link):
        return link.accepts() and link.health()

    with ThreadPoolExecutor(max_workers=min(8, len(links)),
                            thread_name_prefix="plexora-find-server") as pool:
        answers = list(pool.map(probe, links))
    return [link for link, ok in zip(links, answers) if ok]


def find_server(server=None, token=None, *, require=False):
    """The first running server that answers, or None -- preferring one with
    the viewer control plane; one without it is still attached (it can take
    events), and `control_plane` on the link says what it lacks.

    With an explicit `server`, a server that does not answer is an error worth
    reporting (the user asked for it); found automatically, it is just absent.
    """
    if server:
        link = ServerLink(server, token, source="flag")
        if not link.health():
            raise OSError(f"no Plexora server answered at {server}")
        return link
    fallback = None
    for link in _answering(_candidates(None, token)):
        if link.control_plane is not False:
            return link
        fallback = fallback or link
    if fallback is not None:
        return fallback
    if require:
        raise OSError("no running Plexora server was found")
    return None
