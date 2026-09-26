"""Finding, and talking to, a Plexora server that is already running.

The MCP process does all its headless work itself. A running Plexora server is
only needed for two things: telling open viewers that an agent changed
something (so the gating overlay redraws without a reload), and sending viewer
commands (plexora/agent/viewer.py). Both are optional -- nothing here is on the
path of a read -- and a server that is not there is an ordinary answer.

Found, in order: `--server`/`--token`, then `PLEXORA_SERVER_URL` /
`PLEXORA_AUTH_TOKEN`, then the servers this data directory's running
instances announced (`<data_root>/servers.json`, and the notebook's
`sidecars.json`), taking the first that answers `/health`.

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


class ServerLink:
    """One running Plexora server, as a base URL and its token."""

    def __init__(self, base_url: str, token: str | None = None, *, source: str = "flag"):
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token or None
        self.source = source

    def describe(self) -> dict:
        return {"url": self.base_url, "authenticated": bool(self.token),
                "found_via": self.source}

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
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
                status = response.status
                kind = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
            kind = exc.headers.get("Content-Type", "") if exc.headers else ""
        if "json" in kind:
            try:
                return status, json.loads(payload or b"null")
            except ValueError:
                return status, payload
        return status, payload

    def health(self) -> bool:
        try:
            status, _ = self.request("GET", "/health", timeout=TIMEOUT_S)
        except OSError:
            return False
        return 200 <= status < 300

    def notify(self, project, plugin, kind, payload=None) -> bool:
        """Tell this server's open viewers that `plugin`'s state for `project`
        changed. False when the server has no viewer channel or nobody answered."""
        try:
            status, answer = self.request("POST", "/agent/v1/events", {
                "project": project, "plugin": plugin, "kind": kind,
                "payload": payload or {}, "origin": "agent"})
        except OSError:
            return False
        return status == 200 and isinstance(answer, dict) and bool(answer.get("delivered"))


def _candidates(server=None, token=None):
    if server:
        yield ServerLink(server, token, source="flag")
        return
    env_url = os.environ.get("PLEXORA_SERVER_URL")
    if env_url:
        yield ServerLink(env_url, token or os.environ.get("PLEXORA_AUTH_TOKEN"),
                         source="environment")
    try:
        from plexora.server.models import server_records
    except ImportError:  # pragma: no cover
        return
    for record in server_records.records():
        yield ServerLink(record["url"], record.get("token"), source=record.get("source",
                                                                              "servers.json"))


def find_server(server=None, token=None, *, require=False):
    """The first running server that answers, or None.

    With an explicit `server`, a server that does not answer is an error worth
    reporting (the user asked for it); found automatically, it is just absent.
    """
    for link in _candidates(server, token):
        if link.health():
            return link
        if server:
            raise OSError(f"no Plexora server answered at {server}")
    if require:
        raise OSError("no running Plexora server was found")
    return None
