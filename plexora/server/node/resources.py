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

#: What a resource is doing right now.
#:
#: Everything named on a command line is `ready` by the time the node answers
#: at all -- startup does the waiting. A resource added while the node is
#: RUNNING cannot borrow that guarantee: a raw segmentation mask has to be
#: converted before a single tile of it can be served, and on a whole-slide
#: mask that is minutes. So the state is something the node reports and a
#: caller polls, rather than something inferred from a request that hangs.
READY = "ready"
PREPARING = "preparing"
ERROR = "error"


class UnknownResource(KeyError):
    """This node does not serve anything by that name."""


@dataclass
class Resource:
    """One thing a node serves, and whatever it has loaded of it."""

    id: str
    kind: str
    #: What is actually served. For a mask this is the derived pyramid once
    #: `_make_mask_servable` has repointed it, which is not what anybody asked
    #: for -- see `source_path`.
    path: str
    #: The path this resource was ASKED for, before any conversion repointed
    #: it. Two things need it and neither can use `path`: the manifest, which
    #: has to record what to re-serve rather than a derived file that may have
    #: been cleaned up, and the idempotence check in `Registry.add`, which is
    #: asked about the original path every time.
    source_path: str = ""
    state: str = READY
    #: Why `state` is `error`, in a sentence worth putting in front of a user.
    error: str | None = None
    #: Whether this has been through `app._make_mask_servable`. Masks only, and
    #: the reason it is recorded rather than inferred from `path != source_path`
    #: is that the commonest good outcome of that check is no change at all --
    #: a mask that was already a servable pyramid. Without the flag, sharing
    #: such a mask twice would put it back into `preparing` and make the caller
    #: poll for a conversion that is not going to happen.
    prepared: bool = False
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

    def repoint(self, path) -> None:
        """Serve a different file for this resource.

        Startup only, and in practice only for a mask: the file named on the
        command line is the operator's own, and what the tile route hands out
        is the pyramid derived from it. The provider is rebuilt rather than
        told -- it closed over the path it was constructed with.
        """
        self.path = str(path)
        self.provider = _provider_for(self.kind, self.path)
        self.opened = None

    def describe(self) -> dict:
        """What `/hello` says about this resource."""
        fingerprint = None
        try:
            fingerprint = self.provider.fingerprint()
        except Exception:  # pragma: no cover - an unreadable file at handshake
            fingerprint = None
        described = {
            "id": self.id,
            "kind": self.kind,
            "generation": self.generation,
            "loaded": self.loaded,
            # What a caller polls between sharing a mask and attaching it. Sent
            # for every resource, not only the ones that can be anything but
            # ready, so a primary reading `/hello` never has to know which
            # kinds convert.
            "state": self.state,
            "error": self.error,
            "fingerprint": fingerprint.to_dict() if fingerprint is not None else None,
        }
        if self.kind == "segmentation":
            # Which kind of mask this is, so the primary records the matching
            # `segmentationMode` rather than assuming the default. The two
            # render differently and neither fails: a filled pyramid drawn as
            # outlines has the shader trace the boundary of each cell's
            # interior, and an outline mask drawn as filled has it trace the
            # boundary of each stroke and hollow it out. Wrong pictures, no
            # errors -- so the node, which is the only process that can read
            # the file, says which it is.
            from plexora.server.utils import segmentation_pyramid

            try:
                described["mask_mode"] = segmentation_pyramid.generated_mask_kind(
                    self.path)
            except Exception:
                # A mask still converting, or one whose conversion failed, has
                # no mode to report yet. `state` is what says so; a handshake
                # that raised here would take the node's whole catalogue down
                # over one resource.
                described["mask_mode"] = None
        return described


class Registry:
    """Every resource one node process serves."""

    def __init__(self):
        self._resources: dict[str, Resource] = {}
        self._lock = threading.RLock()

    def add(self, kind: str, resource_id: str, path: str,
            state: str = READY) -> Resource:
        """Serve one more file, or return the one already serving it.

        Adding the same `(kind, id, path)` twice is a no-op rather than a
        refusal. A resource id is derived from the file's own path (see
        `nodes.share_path`), so a user reopening a project, retrying after a
        dropped tunnel, or opening a second tab all ask for exactly the
        resource this node is already serving -- and a refusal there is one the
        caller can neither act on nor distinguish from a genuine clash.
        """
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
            existing = self._resources.get(resource_id)
            if existing is not None:
                if existing.kind == kind and existing.source_path == str(resolved):
                    return existing
                raise ResourceError(
                    f"this node already serves a different resource called "
                    f"{resource_id!r}")
            resource = Resource(id=resource_id, kind=kind, path=str(resolved),
                                source_path=str(resolved), state=state)
            resource.provider = _provider_for(kind, str(resolved))
            self._resources[resource_id] = resource
            return resource

    def remove(self, resource_id: str) -> bool:
        """Stop serving one resource. False if it was not being served.

        Nothing on disk is touched: the node was pointed at somebody's file and
        was never given permission to delete it -- and a derived mask pyramid
        beside it may well be what another project is reading.
        """
        with self._lock:
            return self._resources.pop(str(resource_id).strip(), None) is not None

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


# -- what a node was serving last time ------------------------------------
#
# A manifest is `--serve` flags written down, and deliberately nothing more:
# kinds, ids and paths on THIS machine. It never holds a project name, a role,
# a read spec or a token, because those are the primary's and a node that
# started keeping them would be a second place a project is described -- the
# one thing this whole design refuses (see the module docstring).
#
# It exists so that a laptop node started fresh by `plexora connect` serves the
# files last session shared, under the same ids, without the user pointing at
# anything again. That is the entire "reopen a project and it just works"
# promise on the local side.


def load_manifest(path) -> list[tuple[str, str, str]]:
    """`(kind, id, path)` for everything a manifest names, or nothing.

    A manifest that cannot be read is not an error worth refusing to start
    over: the worst case is a node that serves only what its command line
    named, which is exactly the behaviour of every node before manifests
    existed.
    """
    import json

    try:
        raw = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    entries = []
    for entry in (raw or {}).get("resources") or ():
        kind = str(entry.get("kind") or "").strip()
        resource_id = str(entry.get("id") or "").strip()
        file_path = str(entry.get("path") or "").strip()
        if kind and resource_id and file_path:
            entries.append((kind, resource_id, file_path))
    return entries


def save_manifest(path, registry) -> bool:
    """Record what this node serves. Best effort, and never fatal.

    Written 0600 through the same helper the node and remote registries use:
    the paths in here describe somebody's filesystem in detail, which is not a
    thing to leave world-readable on a shared machine even though it is not a
    secret in the token sense.
    """
    from plexora.server.models.secret_store import write_private_json

    payload = {"resources": [
        # `source_path`, not `path`: what to ask for again, rather than the
        # derived pyramid a mask conversion left behind. Re-asking is cheap --
        # the conversion is adopted rather than repeated -- and it keeps the
        # manifest true after a derived file is cleaned up.
        {"kind": resource.kind, "id": resource.id,
         "path": resource.source_path or resource.path}
        for resource in registry.all()
    ]}
    try:
        write_private_json(Path(path), payload)
    except OSError:
        return False
    return True


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
    return kind.strip(), resource_id.strip(), unquote_path(path)


def unquote_path(path: str) -> str:
    """A path with the quotes a person put round it taken back off.

    Anyone whose path has a space in it quotes it, because everywhere else that
    a path is typed -- a shell, mostly -- it has to be. Here it must not be:
    these entries reach the node as one argv element already, and in the
    Settings textarea the line ending is the separator, so a quote is never
    punctuation and always a character in a filename that does not exist. The
    failure it caused was a hard one to read, too: the path came back in the
    error message looking correct, because the offending quote sat flush
    against the message's own.
    """
    path = str(path).strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
        path = path[1:-1].strip()
    return path


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
