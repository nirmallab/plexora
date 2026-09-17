"""Providers for resources this process is holding in memory.

The third transport, beside `local` (a file on this machine) and `node` (a file
on another one). It exists for exactly one deployment: the notebook kernel that
serves its own objects to the viewer sidecar over the node API -- see
`plexora/memory.py`, which is what starts that node and fills its registry.

**Nothing here is reachable from an ordinary project.** A project record can say
`local` or `node://...`, and there is no third spelling; a memory resource
exists only inside a node's `Registry`, for the life of the process holding it,
and the project that reads it does so through the perfectly ordinary
`node://kernel/<id>` binding. That is what keeps the promise the provider
package opens with: application truth stays in one place, and the kernel is a
data service like any other.

A *snapshot* is what the kernel hands over: the state of a live object at the
moment a viewer was opened. Snapshots rather than references, because the
notebook goes on editing between cells, and a table whose obs came from before
an edit and whose X came from after is not a table anybody asked for. Replacing
one wholesale (`viewer.refresh()`) is the only way state changes here.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from plexora.server.providers.base import LOCAL, Fingerprint, ResourceLocator
from plexora.server.providers.local import (
    LocalSegmentationProvider,
    LocalTableProvider,
)

#: What a memory resource writes where a path would go. Never stored in a
#: project -- see the module docstring -- but a node `Resource` has a `path`
#: field and several diagnostics print it, so it says what it is rather than
#: being blank. It is also load-bearing in one place: `api._file_fingerprint`
#: stats this and gets nothing, which is what keeps the on-disk quantization
#: window store out of the way of an array that can change between cells.
MEMORY_SCHEME = "memory://"

#: Distinct per snapshot within this process, and monotonic. Stands in for an
#: mtime in the fingerprint: the objects here have no file to stat, and what a
#: fingerprint has to answer is "are these the same bytes as last time", which
#: for a wholesale replacement is exactly "is this the same snapshot".
_stamps = itertools.count(1)


def locator_for(resource_id: str) -> str:
    return f"{MEMORY_SCHEME}{resource_id}"


@dataclass
class Snapshot:
    """One in-memory resource, frozen at the moment a viewer asked for it.

    Subclasses supply `provider()`, which is what the node registry calls to
    build the reader for this kind -- the memory counterpart of
    `node_resources._provider_for`, moved onto the snapshot because unlike a
    path a snapshot already knows what it is.
    """

    #: Roughly how much memory this holds. Reported, never enforced: it goes
    #: into the fingerprint and into the size warning the kernel prints.
    nbytes: int = 0
    #: This snapshot's identity within the process. See `_stamps`.
    stamp: int = field(default_factory=lambda: next(_stamps))

    def fingerprint(self, identity=None) -> Fingerprint:
        """Identity of what this snapshot holds.

        Constructed rather than stat'd, and the fields keep their meanings: the
        size is the memory the object occupies, and the stamp stands in for a
        modification time. Two snapshots of the same object taken either side of
        an edit differ in the stamp whether or not the edit changed the size,
        which is the property `Fingerprint.matches` actually needs.
        """
        return Fingerprint(size=int(self.nbytes), mtime_ns=int(self.stamp),
                           identity=dict(identity or {}))

    def provider(self):  # pragma: no cover - abstract
        raise NotImplementedError


# -- tables ---------------------------------------------------------------


@dataclass
class TableSnapshot(Snapshot):
    """An AnnData or a flat table, ready for the adapters to read.

    `group` is a zarr group over an in-memory store (the AnnData case) and
    `frame` a polars DataFrame (the flat case); exactly one is set. Which one
    decides only which adapter reads it -- everything after that is the
    ordinary read-spec machinery, because the snapshot was written in the
    format the adapter already knows.
    """

    group: Any = None
    frame: Any = None

    def adapter(self, spec):
        """The adapter that reads this snapshot under `spec`.

        Constructed here rather than looked up by `spec.type`, because
        `spec.type` says what the table IS (`anndata`, `csv`) and must go on
        saying so -- it is an answer the user gave, and where the bytes live
        this session is not a reason to change it.
        """
        from plexora.server.models.adapters.memory_adapter import (
            MemoryAnnDataAdapter,
            MemoryFrameAdapter,
        )

        if self.group is not None:
            return MemoryAnnDataAdapter(spec, self.group)
        return MemoryFrameAdapter(spec, self.frame)

    def provider(self):
        return MemoryTableProvider(None, self)


class MemoryTableProvider(LocalTableProvider):
    """The cell table, read from a snapshot instead of from a file.

    Two overrides and no third. Everything the node's read routes call --
    `describe`, `all_cells`, `filter_columns`, `metadata_column`, `rows`,
    `geometry`, `run` -- is `LocalTableProvider`'s, computing over the frame
    this loaded; those methods were already pure over a frame (see
    providers/local.py's docstring), so there is nothing about them that a
    file makes true.
    """

    def __init__(self, spec, snapshot, name: str | None = None):
        super().__init__(spec, name)
        self._snapshot = snapshot

    @property
    def locator(self) -> ResourceLocator:
        return ResourceLocator(kind="table", provider=LOCAL,
                               path=locator_for(self._name or "table"))

    def load(self, reload: bool = False, stage=None, report=None):
        """Read the snapshot into a NormalizedDatasource.

        `reload` is accepted and ignored for the same reason the local provider
        ignores it: this always reads the snapshot it holds, and a snapshot is
        replaced wholesale rather than re-read.
        """
        self._loaded = self._snapshot.adapter(self._spec).load_table(
            stage=stage, report=report)
        return self._loaded

    def read_obs_column(self, column: str):
        adapter = self._snapshot.adapter(self._spec)
        read = getattr(adapter, "read_obs_column", None)
        return read(column) if read is not None else None

    def fingerprint(self) -> Fingerprint:
        """The snapshot's identity, plus what the loaded table claims.

        `LocalTableProvider.fingerprint` stats `spec.src`, which for a memory
        table is `memory://...` and stats to nothing -- so without this the
        fingerprint would be None, which never matches anything and would have
        every write-back path report the table as moved.
        """
        from plexora.server.providers.local import _frame_identity, _spec_hash

        identity: dict[str, Any] = {"spec": _spec_hash(self._spec)}
        frame = self.frame
        if frame is not None:
            identity.update(_frame_identity(frame))
        return self._snapshot.fingerprint(identity)


# -- images and masks -----------------------------------------------------


@dataclass
class ImageSnapshot(Snapshot):
    """A channel image as a pyramid, plus what one open produced beside it.

    The three fields are exactly what `LocalImageProvider.open()` returns, held
    rather than recomputed: the overview is a bounded materialization and the
    metadata is synthesized, so neither is worth deriving twice.
    """

    pyramid: Any = None
    overview: Any = None
    metadata: Any = field(default_factory=dict)

    def provider(self):
        return MemoryImageProvider(self)


class MemoryImageProvider:
    """The channel image, read from a pyramid this process is holding.

    `open()` returns the same `(channels, overview, metadata)` triple every
    other image provider returns, which is the whole interface the node's tile,
    stats, GMM and region routes use -- so none of them can tell where the
    pixels came from.

    `geometry()` is the one addition. The node's geometry route otherwise reads
    the resource's PATH off disk, and a memory resource has no path; see
    `node/api.image_geometry`, which prefers this when a provider defines it.
    """

    is_local = True

    def __init__(self, snapshot: ImageSnapshot):
        self._snapshot = snapshot

    @property
    def locator(self) -> ResourceLocator:
        return ResourceLocator(kind="image", provider=LOCAL, path=None)

    @property
    def path(self):
        return None

    def open(self):
        snapshot = self._snapshot
        return snapshot.pyramid, snapshot.overview, snapshot.metadata

    def geometry(self) -> dict:
        from plexora.server.utils import ome_zarr

        return ome_zarr.geometry(self._snapshot.pyramid)

    def fingerprint(self) -> Fingerprint:
        return self._snapshot.fingerprint()


@dataclass
class SegmentationSnapshot(Snapshot):
    """A label mask as a pyramid.

    `mode` is "filled" always, and not by assumption: these levels were derived
    here from the caller's own label array by subsampling, so the interiors are
    intact and the viewer derives boundaries itself. The file path has to READ a
    mask to find out which kind it is (see `app._convert_mask_if_needed`);
    nothing here has to, because nothing here converted anything.
    """

    pyramid: Any = None
    mode: str = "filled"

    def provider(self):
        return MemorySegmentationProvider(self)


class MemorySegmentationProvider(LocalSegmentationProvider):
    """The label mask, read from a pyramid this process is holding.

    Subclasses the local provider for its interface and replaces the two halves
    that name a file. There is no conversion step and no derived file: the
    pyramid was built in memory when the snapshot was taken, which is what a
    mask on disk needs `pyramidize_segmentation_mask` for.
    """

    def __init__(self, snapshot: SegmentationSnapshot):
        super().__init__(None)
        self._snapshot = snapshot

    @property
    def locator(self) -> ResourceLocator:
        return ResourceLocator(kind="segmentation", provider=LOCAL, path=None)

    def open(self):
        return self._snapshot.pyramid

    def fingerprint(self) -> Fingerprint:
        return self._snapshot.fingerprint()
