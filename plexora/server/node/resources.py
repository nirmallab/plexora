"""What a node is serving, and the state each of those things is in.

A node is a Plexora process with the viewer, the project registry and the
plugin store all switched off. It has no config.json, no SQLite, no figures and
no ROIs -- it holds files and answers questions about them. That is the whole
of the "singular Plexora database" rule: application truth lives on the
primary, and a node never becomes a second place where a project is described.

So this module is deliberately not `data_model`. Two differences follow from
one fact -- a node serves SEVERAL resources at once:

- **No module globals for "the loaded one".** There is a registry keyed by
  resource id, and every read names which resource it is about. data_model's
  single-loaded-datasource globals are the right design for a viewer showing
  one project and the wrong one here.
- **The read spec arrives from the primary.** A node is told *how* to read a
  table (which matrix, which subset, which obs column is the cell id) on every
  load, and it stores that spec only for as long as the process runs. The
  project that owns those answers is on the primary, and it stays there --
  which is also why restarting a node loses nothing.

What a node DOES hold is derived: the loaded frame, the open pyramid, a tile
cache. All of it is reconstructible from the files it was pointed at, and none
of it is anybody's only copy.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from plexora.server.providers.base import RESOURCE_KINDS, ResourceError

#: How `--serve` names one resource: `kind:id=path`.
#:
#: The id is the node's own name for it and is what the primary records in the
#: project's resource binding. It is chosen by whoever starts the node rather
#: than generated, because it has to survive a restart: a node that renumbered
#: its resources on every launch would orphan every project pointing at it.
SERVE_SYNTAX = "kind:id=path"


class UnknownResource(KeyError):
    """This node does not serve anything by that name."""


@dataclass
class Resource:
    """One thing a node serves, and whatever it has loaded of it."""

    id: str
    kind: str
    path: str
    #: Bumped every time the underlying data is (re)read, so the primary can
    #: tell a cached answer from a stale one without asking what changed. The
    #: counterpart of data_model's `load_generation`, per resource rather than
    #: per process -- which is the whole reason it is per resource: a table
    #: reloading must not invalidate an image node's tile ETags.
    generation: int = 0
    provider: Any = None
    #: Tables only: the read spec the primary last pushed, and the synthesized
    #: project record built from it so plugin code sees the handles it expects.
    spec: Any = None
    project: Any = None
    #: Serializes load against read. A reload swaps the frame under whatever is
    #: reading it, and a describe() halfway through that would be describing
    #: two different tables.
    lock: threading.RLock = field(default_factory=threading.RLock)
    #: Images and masks only: the open pyramid, kept across requests because a
    #: pan is a burst of tile reads against one file and reopening a pyramidal
    #: TIFF is a directory walk each time.
    opened: Any = None
    #: Images only: the mean-pooled overview and the OME header that came out
    #: of the same open. Held together because one file read produced all three.
    opened_overview: Any = None
    opened_metadata: Any = None
    #: Per-resource derived results -- quantization windows, stats packets, GMM
    #: fits. Dropped whenever the pyramid is reopened, which is the only event
    #: that can invalidate them.
    derived: dict = field(default_factory=dict)

    @property
    def loaded(self) -> bool:
        return self.generation > 0

    def describe(self) -> dict:
        """What `/hello` says about this resource."""
        fingerprint = None
        try:
            fingerprint = self.provider.fingerprint()
        except Exception:  # pragma: no cover - an unreadable file at handshake
            fingerprint = None
        return {
            "id": self.id,
            "kind": self.kind,
            "generation": self.generation,
            "loaded": self.loaded,
            "fingerprint": fingerprint.to_dict() if fingerprint is not None else None,
        }


class Registry:
    """Every resource one node process serves."""

    def __init__(self):
        self._resources: dict[str, Resource] = {}
        self._lock = threading.RLock()

    def add(self, kind: str, resource_id: str, path: str) -> Resource:
        kind = (kind or "").strip().lower()
        if kind not in RESOURCE_KINDS:
            raise ResourceError(
                f"{kind!r} is not a resource kind. Use one of: "
                f"{', '.join(RESOURCE_KINDS)}.")
        resource_id = (resource_id or "").strip()
        if not resource_id:
            raise ResourceError(f"a resource needs an id -- write it as {SERVE_SYNTAX}")
        resolved = Path(path).expanduser()
        if not resolved.exists():
            raise ResourceError(f"there is nothing at {resolved}")

        with self._lock:
            if resource_id in self._resources:
                raise ResourceError(
                    f"this node already serves a resource called {resource_id!r}")
            resource = Resource(id=resource_id, kind=kind, path=str(resolved))
            resource.provider = _provider_for(kind, str(resolved))
            self._resources[resource_id] = resource
            return resource

    def get(self, resource_id: str, kind: str | None = None) -> Resource:
        resource = self._resources.get(resource_id)
        if resource is None:
            known = ", ".join(sorted(self._resources)) or "none"
            raise UnknownResource(
                f"this node does not serve {resource_id!r} (it serves: {known})")
        if kind is not None and resource.kind != kind:
            raise UnknownResource(
                f"{resource_id!r} is a {resource.kind} on this node, not a {kind}")
        return resource

    def all(self) -> list[Resource]:
        return [self._resources[key] for key in sorted(self._resources)]

    def __len__(self) -> int:
        return len(self._resources)


def _provider_for(kind: str, path: str):
    """The local provider that reads this kind of resource.

    The same classes the primary uses for a resource on its own disk -- one
    implementation, two transports. A table's provider is built without a spec
    and gets one on its first `load`, because the spec belongs to the project
    and the project is on the primary.
    """
    from plexora.server.providers.local import (
        LocalImageProvider,
        LocalSegmentationProvider,
        LocalTableProvider,
    )

    if kind == "image":
        return LocalImageProvider(path)
    if kind == "segmentation":
        return LocalSegmentationProvider(path)
    return LocalTableProvider(None)


def parse_serve(argument: str) -> tuple[str, str, str]:
    """`kind:id=path` as its three parts.

    Written this way rather than as three flags because a node commonly serves
    several resources and the three parts of one belong together on the command
    line: `--serve table:cells=/data/cells.h5ad --serve image:slide=/data/s.tif`
    reads as two things, where three parallel repeated flags would have to be
    matched up by position.
    """
    kind, separator, rest = str(argument).partition(":")
    if not separator:
        raise ResourceError(f"--serve {argument!r} is not {SERVE_SYNTAX}")
    resource_id, separator, path = rest.partition("=")
    if not separator or not path:
        raise ResourceError(f"--serve {argument!r} is not {SERVE_SYNTAX}")
    return kind.strip(), resource_id.strip(), path.strip()


def load_table(resource: Resource, spec_dict: Mapping[str, Any], reload: bool = False) -> dict:
    """Read this node's table under the read spec the primary just sent.

    The spec is applied every time rather than cached-and-compared, because the
    primary is the only thing that knows when it changed -- a user re-answering
    "which column is the cell id" happens over there, and the answer arriving
    here IS the notification.

    Returns what the primary needs to record: the new generation, the
    fingerprint the write paths will check against, and enough of the loaded
    table's shape to validate it against the image and the mask.
    """
    from plexora.server.models.project import DataSpec, Project

    spec = DataSpec.from_dict(dict(spec_dict or {}))
    if spec is None:
        raise ResourceError("the table read spec has no source file")
    # The path is this NODE's, always: the primary's copy of the spec names a
    # file on the primary's filesystem (or nothing at all), and honouring it
    # here is how a node would be talked into reading somebody else's file.
    spec = _with_src(spec, resource.path)

    from plexora.server.providers.local import LocalTableProvider

    with resource.lock:
        # A fresh provider rather than a mutated one: the provider holds the
        # frame it loaded, and swapping the spec underneath a live object is
        # how a reader ends up with one table's rows under another's schema.
        provider = LocalTableProvider(spec, resource.id)
        loaded = provider.load(reload=reload)
        resource.provider = provider
        resource.spec = spec
        resource.project = _project_for(resource, spec)
        resource.generation += 1
        fingerprint = resource.provider.fingerprint()
        return {
            "generation": resource.generation,
            "fingerprint": fingerprint.to_dict() if fingerprint is not None else None,
            "columns": list(loaded.table.columns),
            "row_count": int(loaded.table.height),
            "id_column": loaded.id_column,
            "x_column": loaded.x_column,
            "y_column": loaded.y_column,
            "feature_columns": list(loaded.feature_columns),
            "celltype_column": loaded.celltype_column,
            "obs_columns": list(loaded.obs_columns),
            "layers": list(loaded.layers),
            "obsm": [dict(entry) for entry in loaded.obsm],
        }


def _with_src(spec, path):
    from dataclasses import replace

    return replace(spec, src=str(path))


def _project_for(resource: Resource, spec):
    """A project record for the node's own use.

    Plugin code -- the ROI writer, gating's `uns` writer -- takes a `Dataset`,
    and a `Dataset` is built from a `Project`. On the primary that comes from
    config.json; here it is synthesized from the spec the primary pushed, so
    the same functions run against the same shapes without knowing which
    machine they are on.

    It is NOT persisted anywhere, and that is the point: it is a view of what
    the primary said, for the duration of one load. The moment this process
    exits, the only record of the project is back where it belongs.
    """
    from plexora.server.models.project import Project

    return Project.from_entry(resource.id, {"dataset": spec.to_dict()})


def dataset_for(resource: Resource):
    """The plugin-API handle set for a node-side table.

    Built with the resource's own provider bound to the table handle, so
    `frame()` reads what this node loaded rather than reaching for data_model's
    globals -- which on a node describe nothing at all.
    """
    from plexora.api.dataset import _dataset_for

    if resource.project is None:
        raise ResourceError(
            f"resource {resource.id!r} has not been loaded yet; the primary "
            f"must POST its read spec first")
    return _dataset_for(resource.project, table_provider=resource.provider)
