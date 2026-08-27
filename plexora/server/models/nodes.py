"""The registry of data nodes this server knows how to reach.

One file, `<data_root>/nodes.json`, holding an endpoint and a token per node.
Kept apart from config.json for two reasons that are really the same reason:

- **A node is not a property of a project.** Several projects on one machine
  routinely read from the same HPC scratch node, and a token that is rotated is
  rotated once, not once per project. config.json says which *resource* a
  project reads and which node serves it; this says how to get to that node.
- **It holds a secret.** config.json is a record a user is expected to open,
  copy between machines, and paste into a bug report. This one is written 0600
  and is not.

Same write discipline as `models/project.py` -- one lock, temp file, rename --
because a background probe updating `last_seen` can land on top of a request
adding a node, and the failure mode of losing that write is a project that no
longer knows where its data is.

**Two addresses, on purpose.** `endpoint` is how THIS server reaches the node.
`browser_endpoint` is how the user's browser reaches it, and it is genuinely
different in the deployments that matter: under Open OnDemand the primary talks
to `http://compute-3:8642` while the browser must go through the portal at
`/rnode/compute-3/8642/`, and under an SSH tunnel the browser reaches
`http://127.0.0.1:8643` while the primary -- sitting on the compute node --
cannot reach the user's laptop at all. Recording only one of them is what makes
a routing decision unrepresentable rather than merely wrong.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from plexora import paths
from plexora.server.models.project import (
    _CONFIG_LOCK,
    _past_transient_locks,
    read_config,
)

#: What the file is called, inside the user's own data root. Never a shared
#: root: a shared root is provisioned by an administrator and read-only, and a
#: user's node tokens are the user's.
FILENAME = "nodes.json"

#: The node API version this build speaks. Bumped when the wire shapes change
#: incompatibly; a node offering a different major is refused with both numbers
#: rather than allowed to fail one endpoint at a time.
API_VERSION = 1


def nodes_path(root=None) -> Path:
    return (Path(root) if root is not None else paths.data_root()) / FILENAME


@dataclass(frozen=True)
class Node:
    """One data node, as this server reaches it."""

    name: str
    #: How the primary reaches it: an absolute http(s) URL, no trailing slash.
    endpoint: str
    #: The per-node bearer token. Never logged, never put in an ETag.
    token: str = ""
    #: How the BROWSER reaches it, when that is not the same thing. Either an
    #: absolute origin or a path that resolves against the page's own origin
    #: (`/rnode/compute-3/8642/`) -- the second form is what makes an Open
    #: OnDemand deployment expressible at all.
    browser_endpoint: str | None = None
    #: What the node reported at the last successful handshake. Advisory: the
    #: version check happens per response, not against this.
    api_version: int | None = None
    node_id: str | None = None
    plexora_version: str | None = None
    last_seen: str | None = None
    #: Unmodelled keys, round-tripped verbatim -- same promise as Project.extra.
    extra: Mapping[str, Any] = field(default_factory=dict)

    _FIELDS = ("endpoint", "token", "browser_endpoint", "api_version",
               "node_id", "plexora_version", "last_seen")

    @classmethod
    def from_dict(cls, name: str, raw: Mapping[str, Any] | None) -> "Node | None":
        raw = raw or {}
        endpoint = (raw.get("endpoint") or "").rstrip("/")
        if not endpoint:
            return None
        return cls(
            name=name,
            endpoint=endpoint,
            token=raw.get("token") or "",
            browser_endpoint=(raw.get("browser_endpoint") or "").rstrip("/") or None,
            api_version=raw.get("api_version"),
            node_id=raw.get("node_id") or None,
            plexora_version=raw.get("plexora_version") or None,
            last_seen=raw.get("last_seen") or None,
            extra={k: v for k, v in raw.items() if k not in cls._FIELDS},
        )

    def to_dict(self) -> dict:
        out = dict(self.extra)
        out["endpoint"] = self.endpoint
        for key, value in (
            ("token", self.token),
            ("browser_endpoint", self.browser_endpoint),
            ("api_version", self.api_version),
            ("node_id", self.node_id),
            ("plexora_version", self.plexora_version),
            ("last_seen", self.last_seen),
        ):
            if value:
                out[key] = value
        return out

    @property
    def browser_url(self) -> str:
        """Where the browser should send its own requests.

        Falls back to `endpoint` when nothing else was recorded, which is right
        for the ordinary case -- a desktop or a tunnel, where the two addresses
        are the same loopback port.
        """
        return self.browser_endpoint or self.endpoint

    def url(self, path: str) -> str:
        return f"{self.endpoint}/{str(path).lstrip('/')}"


def load_all(root=None) -> dict:
    """Every registered node, keyed by name."""
    raw = read_config(nodes_path(root))
    nodes = {}
    for name, entry in (raw or {}).items():
        node = Node.from_dict(name, entry)
        if node is not None:
            nodes[name] = node
    return nodes


def find(name: str, root=None) -> "Node | None":
    return load_all(root).get(name)


def get(name: str, root=None) -> Node:
    """One node, or a KeyError naming what is registered.

    The message lists the known names because the usual cause is a project
    copied from another machine: its config names a node this install has never
    been told about, and "which nodes DO I know" is the next question.
    """
    nodes = load_all(root)
    if name in nodes:
        return nodes[name]
    known = ", ".join(sorted(nodes)) or "none"
    raise KeyError(
        f"no data node named {name!r} is registered on this machine "
        f"(known nodes: {known})"
    )


def save(node: Node, root=None) -> Node:
    """Write one node's entry, leaving the others untouched."""
    path = nodes_path(root)
    with _CONFIG_LOCK:
        raw = read_config(path)
        raw[node.name] = node.to_dict()
        _write(path, raw)
    return node


def remove(name: str, root=None) -> None:
    path = nodes_path(root)
    with _CONFIG_LOCK:
        raw = read_config(path)
        if raw.pop(name, None) is None:
            return
        _write(path, raw)


def record_handshake(name: str, hello: Mapping[str, Any], when: str, root=None):
    """Fold a `/hello` response into the stored entry.

    Best-effort and never fatal: this is bookkeeping for the settings page, and
    a node that answered is a node that works whether or not the note about it
    could be written. A read-only data root is an ordinary state on a cluster.
    """
    try:
        node = find(name, root)
        if node is None:
            return None
        return save(replace(
            node,
            api_version=hello.get("api_version") or node.api_version,
            node_id=hello.get("node_id") or node.node_id,
            plexora_version=hello.get("plexora_version") or node.plexora_version,
            last_seen=when,
        ), root)
    except OSError:
        return None


def _write(path: Path, raw: dict) -> None:
    """Replace nodes.json in one step, owner-readable only.

    The temp file is chmod'd BEFORE the rename rather than the destination
    after it: between a rename and a chmod there is a window in which the
    tokens are world-readable, and on a shared cluster filesystem that window
    is the whole threat.
    """
    import json

    path = Path(path)
    with _CONFIG_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(raw, handle, indent=4)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                # Windows has no meaningful equivalent and POSIX may refuse on
                # some filesystems. Not fatal -- losing the registry is worse
                # than a permissive mode on a file inside the user's own root.
                pass
            _past_transient_locks(lambda: os.replace(tmp, path))
        finally:
            tmp.unlink(missing_ok=True)
