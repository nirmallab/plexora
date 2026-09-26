"""The handles an agent reads projects through.

**An agent's reads must never swap the project on the user's screen.**
`plexora.api.project_data(name)` builds handles whose table reads fall through
to `data_model`, which holds ONE loaded datasource -- the viewer's -- and loads
whichever project it is asked about in its place. That is right for a route
serving the page and exactly wrong for an agent asking about three projects at
once.

So a session builds handles the way a data node does: over a table provider of
its own (`LocalTableProvider`, or `NodeTableProvider` when the table is on a
node), passed into `TableHandle`, so every read goes to the session's copy.
`tests/test_agent_architecture.py` pins it: after an agent has read a table,
`data_model` has still loaded nothing.

Held copies are bounded (`table_limit`) and re-checked on every use against the
project record and the file's fingerprint, so another process re-registering a
project, or the file changing underneath, drops the stale copy rather than
serving it.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from plexora.agent.errors import AgentError


class _Cache:
    """`ProjectData._cache` for one held table: fits, summaries, anything a
    capability asks `ProjectData.cached` to keep. Dropped with the entry."""

    def __init__(self):
        self._entries = {}
        self._lock = threading.Lock()

    def get_or_set(self, key, compute):
        with self._lock:
            if key in self._entries:
                return self._entries[key]
        value = compute()
        with self._lock:
            self._entries.setdefault(key, value)
            return self._entries[key]


class _CachingProvider:
    """A table provider whose `describe()` is computed once.

    `describe` is a pass over every column; a capability asks for it on nearly
    every call, and the node answers it from a cache of its own. Everything
    else passes straight through.
    """

    def __init__(self, provider):
        self._provider = provider
        self._described = None
        self._lock = threading.Lock()

    def describe(self):
        with self._lock:
            if self._described is None:
                self._described = self._provider.describe()
            return self._described

    def __getattr__(self, name):
        return getattr(self._provider, name)


@dataclass
class _Entry:
    identity: tuple
    data: object
    lock: threading.Lock = field(default_factory=threading.Lock)


def _identity(record) -> tuple:
    """What has to be unchanged for a held copy to still be this project's."""
    from plexora.server.providers.base import Fingerprint
    from plexora.server.providers.local import _spec_hash

    spec = record.dataset
    binding = record.resources.get("table")
    if binding is not None:
        return ("node", binding.node, binding.resource_id, _spec_hash(spec))
    if spec is None:
        return ("none", record.image.src)
    fingerprint = Fingerprint.of_path(spec.src)
    return ("local", spec.src, _spec_hash(spec),
            fingerprint.to_dict() if fingerprint is not None else None,
            record.log_transformed)


class _NoTable:
    """The table provider of an image-only handle set: refuses every read.

    `image_data` hands out handles for reading pixels without loading the
    table. Its `TableHandle` must not fall through to `data_model` either, so
    it is given this, and any accidental table read fails loudly here instead
    of quietly loading the viewer's project.
    """

    frame = None

    def __getattr__(self, name):
        raise AgentError("internal_error",
                         "an image-only handle set was asked for its table; use "
                         "session.data() for table reads")


class AgentSession:
    """Provider-backed handle sets, a few at a time."""

    def __init__(self, *, table_limit: int = 2):
        self.table_limit = max(1, int(table_limit))
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._lock = threading.Lock()

    # -- records ---------------------------------------------------------

    def project(self, name: str):
        """The project record, read fresh. `unknown_project` if there is none."""
        from plexora.server.models.project import Project

        try:
            return Project.load(name)
        except KeyError:
            known = sorted(Project.load_all())
            close = [n for n in known if n.casefold() == str(name).casefold()]
            raise AgentError(
                "unknown_project", f"no project named {name!r}",
                detail={"did_you_mean": close[:3], "known_count": len(known),
                        "hint": "call list_projects"}) from None

    def data(self, name: str):
        """Handles for a project whose table reads go to this session's copy."""
        record = self.project(name)
        identity = _identity(record)
        with self._lock:
            entry = self._entries.get(name)
            if entry is not None and entry.identity == identity:
                self._entries.move_to_end(name)
                # The record is re-read every call (it is cheap and it is what
                # another process edits); the table copy is what is kept.
                return self._rebuild(entry, record)
            if entry is not None:
                del self._entries[name]
        data = self._build(record)
        with self._lock:
            self._entries[name] = _Entry(identity, data)
            self._entries.move_to_end(name)
            while len(self._entries) > self.table_limit:
                self._entries.popitem(last=False)
        return data

    def image_data(self, name: str):
        """Handles for reading a project's image and mask, without its table.

        For capabilities that only look at pixels: rendering a region of an
        image-only project, or of one whose table is on a node that is asleep,
        must not wait on (or fail on) a table it never reads.
        """
        from plexora.api.dataset import _project_data_for

        record = self.project(name)
        with self._lock:
            entry = self._entries.get(name)
            if entry is not None and entry.identity == _identity(record):
                return self._rebuild(entry, record)
        return _project_data_for(record, _NoTable(), cache=_Cache())

    def _rebuild(self, entry, record):
        from plexora.api.dataset import _project_data_for

        held = entry.data
        return _project_data_for(record, held.table._provider, cache=held._cache)

    def _build(self, record):
        from plexora.api.dataset import _project_data_for

        provider = None
        if record.has_table:
            binding = record.resources.get("table")
            try:
                if binding is not None:
                    from plexora.server.providers.node import NodeTableProvider

                    provider = NodeTableProvider(binding, record.dataset)
                else:
                    from plexora.server.providers.local import LocalTableProvider

                    provider = LocalTableProvider(record.dataset, record.name)
                provider.load()
            except AgentError:
                raise
            except Exception as exc:
                from plexora.agent.errors import as_agent_error

                error = as_agent_error(exc)
                if error.code == "internal_error":
                    raise AgentError(
                        "resource_unavailable",
                        f"the cell table for {record.name!r} could not be read: {exc}",
                        detail={"node": binding.node if binding else None},
                        retryable=binding is not None) from exc
                raise error from exc
            provider = _CachingProvider(provider)
        else:
            # No table: still never let a read fall through to data_model.
            provider = _NoTable()
        return _project_data_for(record, provider, cache=_Cache())

    # -- lifetime --------------------------------------------------------

    def invalidate(self, name: str | None = None):
        with self._lock:
            if name is None:
                self._entries.clear()
            else:
                self._entries.pop(name, None)

    def held(self) -> list:
        with self._lock:
            return list(self._entries)

    def close(self):
        self.invalidate()
        from plexora.server.utils import source_image

        source_image.close_readers()
