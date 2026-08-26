"""What a scientific resource is, separated from where it lives.

A project has always had three scientific resources -- the image, the
segmentation mask and the cell table -- and until now all three were assumed
readable from the one process that also owns the application database. This
package is that assumption made explicit and then made optional: a resource is
reached through a *provider*, and a provider is either local (the file is on
this machine, and the code below is exactly what data_model.py already did) or
node-backed (the file is on another Plexora process, reached over the node API).

Three rules hold the design together, and every one of them is load-bearing:

**The database does not move.** config.json, the per-datasource SQLite, the
plugin store, figures and ROIs stay on the primary. A node is a data service
with no project state of its own -- it is handed a read spec and answers
questions about bytes. Nothing here ever gives a node something to own.

**Local is the default and pays nothing.** Every dispatch guard in
data_model.py tests one module-global boolean first, so a single-server project
-- which is every existing project -- costs one global read and one branch on a
path that has always existed. Local providers return the very objects the
module globals held before this package existed; they are delegation, not a
rewrite.

**Computation goes to the data.** A provider method is not "fetch the bytes and
compute here". `map_rois` runs the spatial join where the cells are, and
`write_roi_columns` runs the write where the file is, because both need the
loaded frame and the file on disk to agree about which row is which cell --
a check (`adapters._write_obs_columns`) that is meaningless across a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

#: A resource read from this process's own filesystem.
LOCAL = "local"
#: A resource read over the node API from another Plexora process.
NODE = "node"

#: The three scientific resources a project can have. Deliberately not a list
#: of everything a project owns: ROIs, figures, gates and settings are
#: application state and are never distributed -- see the package docstring.
RESOURCE_KINDS = ("image", "segmentation", "table")


@dataclass(frozen=True)
class ResourceLocator:
    """Where one resource is, in the one vocabulary every caller shares.

    `path` is the honest answer only for a local resource. For a node-backed
    one it is None, and that is a contract rather than a gap: a plugin holding
    a path to a file on another machine would open it, find nothing, and report
    a missing project. Anything that used to reach for `.path` asks the handle
    to do the work instead (see `plexora/api/dataset.py`).
    """

    kind: str
    provider: str = LOCAL
    path: str | None = None
    #: Which entry of nodes.json serves this, for a node-backed resource.
    node: str | None = None
    #: What that node calls it. Node-scoped, never globally unique -- two nodes
    #: may both serve a resource called "cells".
    resource_id: str | None = None

    @property
    def is_local(self) -> bool:
        return self.provider == LOCAL

    def to_dict(self) -> dict:
        out = {"kind": self.kind, "provider": self.provider}
        if self.path:
            out["path"] = self.path
        if self.node:
            out["node"] = self.node
        if self.resource_id:
            out["resource_id"] = self.resource_id
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None, kind: str) -> "ResourceLocator":
        raw = raw or {}
        return cls(
            kind=kind,
            provider=raw.get("provider") or LOCAL,
            path=raw.get("path") or None,
            node=raw.get("node") or None,
            resource_id=raw.get("resource_id") or None,
        )

    def __str__(self) -> str:
        if self.is_local:
            return f"{self.kind} at {self.path}"
        return f"{self.kind} {self.resource_id!r} on node {self.node!r}"


@dataclass(frozen=True)
class Fingerprint:
    """Identity of the bytes a resource was last read from.

    Size and mtime rather than a content hash, for the reason
    `data_model._image_fingerprint` already gives: these files are routinely
    gigabytes on a network filesystem, and hashing one costs more than every
    read this exists to avoid.

    `identity` carries the per-kind facts that make a cross-resource mistake
    visible at attach time rather than as a silently wrong picture: a table's
    row count and cell-id range, an image's dimensions and channel count, a
    mask's label ceiling. Two resources that disagree about how many cells
    there are, or where they sit, render perfectly and mean nothing.
    """

    size: int | None = None
    mtime_ns: int | None = None
    identity: Mapping[str, Any] = field(default_factory=dict)

    def matches(self, other: "Fingerprint | None") -> bool:
        """Whether `other` describes the same bytes.

        None on either side is "cannot be established", and is never a match --
        the same posture `_image_fingerprint` takes, because the cost of
        rereading is always smaller than the cost of serving something stale.
        """
        if other is None or self.size is None or other.size is None:
            return False
        return self.size == other.size and self.mtime_ns == other.mtime_ns

    def to_dict(self) -> dict:
        return {
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "identity": dict(self.identity),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "Fingerprint | None":
        if not raw:
            return None
        return cls(
            size=raw.get("size"),
            mtime_ns=raw.get("mtime_ns"),
            identity=dict(raw.get("identity") or {}),
        )

    @classmethod
    def of_path(cls, path, identity: Mapping[str, Any] | None = None) -> "Fingerprint | None":
        """Fingerprint a file, or None if it cannot be stat'd.

        A directory (a .zarr store) is fingerprinted by the directory entry
        itself, which changes when a child is added or removed but not when one
        is rewritten in place. That is weaker than for a file and is the best
        a stat can do; anything stronger would mean walking a store with
        thousands of chunks on every load.
        """
        import os

        try:
            stat = os.stat(path)
        except OSError:
            return None
        return cls(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                   identity=dict(identity or {}))


# -- failures a caller has to be able to tell apart ----------------------


class ResourceError(RuntimeError):
    """Something went wrong reaching a resource."""


class ResourceUnavailable(ResourceError):
    """The node serving this resource cannot be reached right now.

    Distinct from every other failure because it is the recoverable one: a
    laptop that went to sleep, a tunnel that dropped, a compute job that ended.
    Callers degrade rather than fail -- the Cells control reports the layer
    unusable and names the node, the project still opens, and the next attempt
    re-validates.
    """

    def __init__(self, message, node=None, resource=None):
        super().__init__(message)
        self.node = node
        self.resource = resource


class ResourceNotLocal(ResourceError):
    """Something asked for a filesystem path to a resource that is not here.

    Raised rather than returning None so the caller cannot pass the gap along
    and open it later: `Path(None)` and `open("")` both fail somewhere far from
    the mistake. Every raise names the operation that should have been used
    instead, because there always is one -- that is the point of the provider
    methods.
    """


class ResourceMoved(ResourceError):
    """The bytes changed under a resource whose identity was recorded.

    The one failure that must never be papered over: a table whose row count
    changed cannot have per-row results written back to it, because every value
    would land on a different cell than the one it was computed for.
    """


class NodeVersionMismatch(ResourceError):
    """A node speaks a node-API version this primary does not.

    Carries both sides so the message can say which end to upgrade rather than
    only that they disagree.
    """

    def __init__(self, required, offered, node=None):
        super().__init__(
            f"node {node!r} speaks node API {offered}, this server needs {required}"
            if node else f"node API {offered}, expected {required}")
        self.required = required
        self.offered = offered
        self.node = node


# -- what each kind of provider answers ----------------------------------
#
# Protocols rather than base classes: the local implementations are thin
# delegations to code that already exists and predates this package, and making
# them inherit would invite moving that code in here, which is exactly what the
# no-op refactor must not do.


class TableProvider(Protocol):
    """The cell table: reading it, querying it, and writing back to it.

    Every method here is something that must happen where the table's file is.
    The split is not arbitrary -- what is missing is as deliberate as what is
    present. There is no `path` and no `frame()` on the node side, because a
    multi-million-row frame is the thing that must not cross a network, and
    there is no ball-tree or centroid query, because the primary keeps a
    compact (id, x, y) copy and answers those itself without a round trip.
    """

    is_local: bool

    @property
    def locator(self) -> ResourceLocator: ...

    def load(self, reload: bool = False): ...
    """The normalized table. Local: a NormalizedDatasource. Node: the same,
    materialized from the buffers the node sends."""

    def describe(self) -> dict: ...
    """Per-column stats plus 50-bin histograms -- the `dd` payload."""

    def all_cells(self, columns, data_type): ...
    """Whole columns as one flat numpy array, the wire shape /get_all_cells
    already serves."""

    def metadata_column(self, column): ...
    """One annotation column as a MetadataColumn, aligned with the table."""

    def filter_columns(self, columns) -> dict: ...
    """Numeric numpy views of named columns, for range queries."""

    def rows(self, ids) -> list: ...
    """Whole rows by cell id. The nearest-cell tooltip's payload -- the one
    place a full row is genuinely wanted, and it is one row."""

    def fingerprint(self) -> "Fingerprint | None": ...

    def run(self, operation: str, payload: Mapping[str, Any]) -> dict: ...
    """A registered table operation -- see `operations.py`. This is the seam
    every scientific write-back and the ROI spatial join go through, because
    all of them need the file and the loaded frame to be the same machine's."""


class ImageProvider(Protocol):
    """The image pyramid: tiles, per-channel statistics, and full-resolution
    reads that no tile API can express (Figure Builder's panel render and Quick
    Edit's region read)."""

    is_local: bool

    @property
    def locator(self) -> ResourceLocator: ...

    def open(self): ...
    """(channels, zarray, metadata) -- exactly the three objects
    load_datasource() has always put in its globals."""

    def tile(self, channel: str, level, tile, quality): ...
    def overview(self, channel: str): ...
    def channel_stats(self, channel: str) -> dict: ...
    def gmm(self, channel: str) -> dict: ...
    def quantization_window(self, channel: str) -> tuple: ...
    def ome_metadata(self): ...
    def geometry(self) -> dict: ...
    def read_region(self, level, box, channels): ...
    def render_panel(self, spec: Mapping[str, Any]): ...
    def fingerprint(self) -> "Fingerprint | None": ...


class SegmentationProvider(Protocol):
    """The label mask: label tiles, and the pyramid conversion that produces
    them. Conversion runs where the mask is -- it reads every pixel of a file
    that is often larger than the image."""

    is_local: bool

    @property
    def locator(self) -> ResourceLocator: ...

    def open(self): ...
    def tile(self, level, tile): ...
    def fingerprint(self) -> "Fingerprint | None": ...
