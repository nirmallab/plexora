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

import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from plexora import paths
from plexora.server.models.project import _CONFIG_LOCK, read_config
from plexora.server.models.secret_store import write_private_json

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
    def role(self) -> str | None:
        """What this node is TO the user, when that is known.

        `"client"` means it runs on the machine the browser is on -- the node
        `plexora connect` starts on the user's own laptop. Nothing else can be
        inferred from an address: a node beside the viewer and a node on the
        user's desk are both `http://127.0.0.1:<port>` from where the viewer
        stands, and only the process that built the tunnel knows which is
        which. It is what lets a data form offer "Local" and mean the user's
        own computer by it.

        In `extra` rather than as a stored field, for the same reason
        `managed_by` is: both are notes from whoever registered the node, and
        neither is something the node itself reports about itself.
        """
        return (self.extra or {}).get("role") or None

    @property
    def managed_by(self) -> str | None:
        """What set this node up and will set it up again, e.g. `connect:hpc`.

        The difference between an address somebody typed and one a tunnel
        rewrites every session. An entry with this is not repaired in Settings
        -- it is reconnected, and taking it down when its session ends is
        tidying rather than deletion.
        """
        return (self.extra or {}).get("managed_by") or None

    @property
    def expires_at(self) -> float | None:
        """When the job serving this node runs out, as a Unix time, or None.

        The clock, written where the thing it is about is written. A node
        outlives the process that started it, so after a restart the session
        that knew about the allocation is gone and the tunnel is still up --
        and this entry is the only thing left that knows there is a deadline.

        None for everything that is not in a job at all, which is most nodes.
        In `extra` for the same reason `managed_by` is: it is a note from
        whoever registered the node, not something the node reports about
        itself -- the node has no idea it is in a job.
        """
        value = (self.extra or {}).get("expires_at")
        try:
            return float(value) if value else None
        except (TypeError, ValueError):
            return None

    @property
    def time_left(self) -> int | None:
        """Seconds until this node's job ends, floored at zero, or None.

        Zero is a real answer and is not None -- it means out of time, which is
        a thing to say. None means there is no clock on this node.
        """
        import time

        expires = self.expires_at
        if expires is None:
            return None
        return max(0, int(round(expires - time.time())))

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


# Addresses this process has been told to stop using, as (name, endpoint).
#
# Deleting the entry from nodes.json does not on its own stop this process
# talking to the machine it named. A provider resolves its node once and holds
# it for its life (see providers/node.py), the cache warm-up walks every
# channel of a project on a thread that outlives the load, and the browser can
# have tiles in flight -- so at the moment a connection is torn down there is
# work still carrying the address of a tunnel that has just gone. Left to find
# out for itself, each of those spends two connection attempts and a backoff
# learning what the disconnect already knew, and logs a urllib3 warning about
# a connection the user closed on purpose.
#
# The endpoint is half the key so that reconnecting is not confused with
# resuming. A tunnel comes back on whatever local port was free, and work
# still holding the OLD port has to stay refused: that port is exactly what
# the next session may hand to something else.
_disconnected: set[tuple[str, str]] = set()
_disconnected_lock = threading.Lock()


def is_disconnected(node: Node) -> bool:
    """Whether this exact address was disconnected in this process.

    Asked by the HTTP client before it opens a socket, so that everything
    still holding a stale node fails at once and says why, rather than
    rediscovering it one refused connection at a time.
    """
    with _disconnected_lock:
        return (node.name, node.endpoint) in _disconnected


#: How many times each node's ADDRESS has changed in this process. Providers
#: resolve a node once and hold it for their life (see providers/node.py, which
#: explains why), so without this a reconnect is invisible to work that is
#: already loaded: the tunnel comes back on a new local port, nodes.json is
#: rewritten, and every open project goes on asking for the port that has gone.
#: The counter is what lets a cached node notice, and it is a counter rather
#: than a flag because two reconnects in a row have to be two events.
#:
#: Bumped when the endpoint or the token is not what was stored -- including
#: when NOTHING was stored, because Disconnect removes the entry and the
#: commonest reconnect of all writes over that absence. `record_handshake`
#: writes this file on every successful probe to keep `last_seen` current, and
#: is the reason this is a comparison rather than "bumped on every save":
#: treating bookkeeping as a move would make every provider re-read nodes.json
#: for a change that told it nothing.
_addresses: dict[str, int] = {}
_addresses_lock = threading.Lock()


def address_generation(name: str) -> int:
    """A number that changes when this node's address does.

    Cheap on purpose -- an in-memory read, no file. It is consulted on the way
    to every node call, and the whole point is that the common case (nothing
    has moved) costs nothing.
    """
    with _addresses_lock:
        return _addresses.get(str(name), 0)


def _address_moved(name: str) -> None:
    with _addresses_lock:
        _addresses[str(name)] = _addresses.get(str(name), 0) + 1


def save(node: Node, root=None) -> Node:
    """Write one node's entry, leaving the others untouched."""
    path = nodes_path(root)
    with _CONFIG_LOCK:
        raw = read_config(path)
        before = raw.get(node.name)
        raw[node.name] = node.to_dict()
        _write(path, raw)
    # Compared against what was on disk -- and NOTHING on disk counts as a
    # difference, which is the whole case this has to get right. Disconnect
    # removes the entry, so the commonest reconnect there is (disconnect,
    # connect again) writes over an absence and would otherwise look exactly
    # like a node being registered for the first time. That is precisely when a
    # provider IS holding a retired address, so treating "there was nothing
    # here" as "nothing changed" gets it backwards on the one path that matters.
    #
    # Bumping on a genuine first registration costs nothing: a provider records
    # the generation it saw at its OWN first resolve, not zero, so it does not
    # start a generation behind. What a comparison here does buy is silence for
    # `record_handshake`, which rewrites this file after every successful probe
    # to keep `last_seen` current and moves no address at all.
    was = before if isinstance(before, dict) else {}
    if ((was.get("endpoint") or "").rstrip("/") != node.endpoint
            or (was.get("token") or "") != (node.token or "")):
        _address_moved(node.name)
    # This address is current again, whatever it was before. A reconnect that
    # lands on the port a previous session used is an ordinary thing, and the
    # work still holding that address is now right to use it.
    with _disconnected_lock:
        _disconnected.discard((node.name, node.endpoint))
    return node


def remove(name: str, root=None) -> None:
    path = nodes_path(root)
    with _CONFIG_LOCK:
        raw = read_config(path)
        entry = raw.pop(name, None)
        if entry is None:
            return
        _write(path, raw)
    endpoint = (entry.get("endpoint") or "").rstrip("/") if isinstance(entry, dict) else ""
    if endpoint:
        with _disconnected_lock:
            _disconnected.add((name, endpoint))


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

    The chmod-before-rename discipline this needs is shared with remotes.json,
    so it lives in `secret_store` -- see that module for why the ordering is
    the whole point.
    """
    write_private_json(path, raw)
