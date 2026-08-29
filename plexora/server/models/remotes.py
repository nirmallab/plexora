"""Saved remote servers -- the things a user should only have to type once.

`plexora connect user@host --srun "-p interactive -t 4:00:00"
--remote-command "conda run -n imaging plexora"` is a correct command and an
unreasonable thing to ask somebody to remember. This is the file that
remembers it: `<data_root>/remotes.json`, one entry per server, keyed by a
short name the user chooses. After that, connecting is the name and nothing
else -- from the terminal (`plexora connect hpc`) or from Settings, which is
the same store read by a different front end.

**No password is ever stored here, and there is no field for one.** Secrets
reach ssh through the askpass relay (`plexora/askpass.py`) and live in memory
for the seconds between the user typing one and ssh consuming it. What is
recorded is the shape of the connection -- which host, which queue, which
command -- all of which the user would otherwise be retyping, and none of
which is a credential. That is why this file can be copied between machines
and pasted into a bug report; `nodes.json`, next door, cannot.

It is still written 0600 (see `secret_store`): a hostname and an account name
are not secrets, but they are nobody else's business on a shared filesystem,
and the two files are written by the same code so the careful one cannot drift
away from the careless one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from plexora import paths
from plexora.server.models.project import _CONFIG_LOCK, read_config
from plexora.server.models.secret_store import write_private_json

FILENAME = "remotes.json"


def remotes_path(root=None) -> Path:
    return (Path(root) if root is not None else paths.data_root()) / FILENAME


@dataclass(frozen=True)
class Remote:
    """One saved server, and everything `connect` needs to reach it.

    The field names are `connect.Session`'s parameter names on purpose --
    `as_session_kwargs` is a rename-free hand-off, so a flag added to one is
    not silently dropped by the other.
    """

    name: str
    #: `[user@]host`, exactly as it would be typed after `ssh`.
    target: str
    #: How to invoke Plexora over there. The escape hatch for a login shell
    #: whose non-interactive PATH does not have it -- by a wide margin the most
    #: common reason a connection fails.
    remote_command: str = "plexora"
    #: A project to open on arrival. Optional; without one the picker opens.
    datasource: str | None = None
    data_dir: str | None = None
    plugins: str | None = None
    #: srun arguments, or None for a host that runs Plexora directly. The empty
    #: string is meaningful and distinct from None: it means "use srun with no
    #: arguments", i.e. this is a login node and the site's defaults will do.
    srun: str | None = None
    bind_node: bool = False
    jump: str | None = None
    ssh_opts: tuple = ()
    forwards: tuple = ()
    #: Stage-C data nodes: resources to serve from the remote host alongside
    #: the viewer (`kind:id=path`), and from THIS machine back to it.
    serve: tuple = ()
    local_serve: tuple = ()
    node_name: str | None = None
    #: Whether to run a data node on the machine the BROWSER is on. On by
    #: default and stored as an opt-out, because the option it enables --
    #: picking a file from your own computer in the viewer's data forms --
    #: cannot be offered at all without it, and nothing on this record could
    #: name that file in advance.
    local_node: bool = True
    extra: Mapping[str, Any] = field(default_factory=dict)

    _FIELDS = ("target", "remote_command", "datasource", "data_dir", "plugins",
               "srun", "bind_node", "jump", "ssh_opts", "forwards", "serve",
               "local_serve", "node_name", "local_node")

    @classmethod
    def from_dict(cls, name: str, raw: Mapping[str, Any] | None) -> "Remote | None":
        raw = raw or {}
        target = (raw.get("target") or "").strip()
        if not target:
            return None

        def listed(key):
            value = raw.get(key) or ()
            if isinstance(value, str):
                value = [value]
            return tuple(str(item) for item in value if str(item).strip())

        def optional(key):
            value = raw.get(key)
            return str(value) if value is not None else None

        return cls(
            name=name,
            target=target,
            remote_command=(raw.get("remote_command") or "plexora"),
            datasource=optional("datasource") or None,
            data_dir=optional("data_dir") or None,
            plugins=optional("plugins"),
            # Not `or None`: "" is a real value here -- see the field comment.
            srun=optional("srun"),
            bind_node=bool(raw.get("bind_node")),
            jump=optional("jump") or None,
            ssh_opts=listed("ssh_opts"),
            forwards=listed("forwards"),
            serve=listed("serve"),
            local_serve=listed("local_serve"),
            node_name=optional("node_name") or None,
            # Absent means yes: every record written before this existed
            # describes a connection that should now start one.
            local_node=bool(raw.get("local_node", True)),
            extra={k: v for k, v in raw.items() if k not in cls._FIELDS},
        )

    def to_dict(self) -> dict:
        out = dict(self.extra)
        out["target"] = self.target
        out["remote_command"] = self.remote_command
        for key, value in (
            ("datasource", self.datasource),
            ("data_dir", self.data_dir),
            ("plugins", self.plugins),
            ("bind_node", self.bind_node),
            ("jump", self.jump),
            ("ssh_opts", list(self.ssh_opts)),
            ("forwards", list(self.forwards)),
            ("serve", list(self.serve)),
            ("local_serve", list(self.local_serve)),
            ("node_name", self.node_name),
        ):
            if value:
                out[key] = value
        if self.srun is not None:
            out["srun"] = self.srun
        # Written only when switched off, so the file stays a record of what
        # somebody chose rather than of every default.
        if not self.local_node:
            out["local_node"] = False
        return out

    def as_session_kwargs(self) -> dict:
        """What `connect.Session(**...)` wants, minus the target."""
        return {
            "datasource": self.datasource,
            "remote_command": self.remote_command,
            "srun": self.srun,
            "bind_node": self.bind_node,
            "jump": self.jump,
            "ssh_opts": tuple(self.ssh_opts),
            "data_dir": self.data_dir,
            "plugins": self.plugins,
            "forwards": tuple(self.forwards),
            "also_serve": tuple(self.serve),
            "local_serve": tuple(self.local_serve),
            # The saved connection's own name, when nothing more specific was
            # given. It is what the nodes this session registers are called and
            # what `managed_by` records, so falling back to the host would give
            # two saved connections to the same cluster one identity between
            # them -- and would rename every node the day somebody edits the
            # target. The node on the user's own machine needs this most: its
            # manifest is keyed on it, and that manifest is how a project
            # reopened next week finds its local files again.
            "node_name": self.node_name or self.name,
            "local_node": self.local_node,
        }

    def as_node_kwargs(self) -> dict:
        """What `connect.NodeSession(**...)` wants, minus the target.

        The other thing a saved connection is good for: not "run Plexora over
        there and show me it", but "let the Plexora I am already running read
        files over there".

        **This record is the source of truth for how that host is reached, and
        a data node inherits all of it.** `srun` and `bind_node` most of all.
        Somebody who wrote "this is a cluster login node -- run Plexora inside
        a job" has said something about the machine, not about one feature of
        it; a data node that quietly ignored it would put sustained tile I/O on
        a login node against that instruction. Deciding that serving bytes is
        exempt from a site's rules is not this layer's call to make, and making
        it here would mean the same profile meant two different things
        depending on which part of the UI opened it.

        `plugins` crosses over because a node runs plugin *server* code -- the
        same table operations the primary would have run.

        What stays behind is only what describes a viewer that is not being
        started: `datasource`, `data_dir`, and the `forwards` that exist so a
        browser can reach a second port beside that viewer. `serve` stays
        behind too, because it is the question the Local/Remote switch exists
        to stop asking in advance: the paths are chosen in the form, minutes
        after the connection opens.
        """
        return {
            "remote_command": self.remote_command,
            "srun": self.srun,
            "bind_node": self.bind_node,
            "jump": self.jump,
            "ssh_opts": tuple(self.ssh_opts),
            "plugins": self.plugins,
            "node_name": self.node_name or self.name,
        }


def load_all(root=None) -> dict:
    """Every saved server, keyed by name."""
    raw = read_config(remotes_path(root))
    out = {}
    for name, entry in (raw or {}).items():
        remote = Remote.from_dict(name, entry)
        if remote is not None:
            out[name] = remote
    return out


def find(name: str, root=None) -> "Remote | None":
    return load_all(root).get(name)


def get(name: str, root=None) -> Remote:
    remotes = load_all(root)
    if name in remotes:
        return remotes[name]
    known = ", ".join(sorted(remotes)) or "none"
    raise KeyError(
        f"no saved remote server named {name!r} on this machine "
        f"(saved servers: {known})"
    )


def save(remote: Remote, root=None) -> Remote:
    """Write one entry, leaving the others untouched.

    Saving over an existing name UPDATES it. That is what makes re-saving a
    profile after editing it in Settings the same operation as creating one,
    and it is what `plexora connect --save` relies on.
    """
    path = remotes_path(root)
    with _CONFIG_LOCK:
        raw = read_config(path)
        raw[remote.name] = remote.to_dict()
        write_private_json(path, raw)
    return remote


def remove(name: str, root=None) -> None:
    path = remotes_path(root)
    with _CONFIG_LOCK:
        raw = read_config(path)
        if raw.pop(name, None) is None:
            return
        write_private_json(path, raw)


def rename(remote: Remote, name: str) -> Remote:
    return replace(remote, name=name)
