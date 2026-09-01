"""The project record: one typed view of one config.json entry.

A project used to be a bare dict written by four different functions with four
slightly different key sets, and read by everything. The keys drifted: roles
shared a dict with file paths and processing flags (`featureData[0]`), only
element `[0]` of that list was ever used, `has_feature_data` duplicated a fact
already implied by whether there was a table at all, and `image_id` was declared
in the plugin contract but written by nobody.

This module is the one place that knows the on-disk shape. Everything else asks
it questions -- `project.dataset.roles.x`, `project.has_table` -- so adding a
field is a change here rather than a search across the tree.

Two rules make it safe to write through:

**Unknown keys survive.** Anything this module does not model lands in `extra`
and is written back verbatim. A save can therefore never silently drop a key it
did not know about, which is exactly how editing an AnnData project used to
destroy it (the old save rebuilt the entry from `{}` and lost `data_type` and
the read spec).

**Changes go through `patch()`.** It merges. There is deliberately no API for
replacing an entry wholesale.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Iterable, Mapping

from plexora import paths

#: Serializes read-modify-write of config.json within the process. Needed
#: because the segmentation job patches the same file from a background thread
#: while a request may be saving an edit -- without this, whichever wrote last
#: silently discarded the other's change. Re-entrant so `mutate()` can hold it
#: across a load/save pair that each take it again.
_CONFIG_LOCK = threading.RLock()


@contextmanager
def config_transaction():
    """Hold the config write lock across several operations.

    Use it when a decision depends on what is already stored -- `mutate()` is
    this plus a load and a save, and is what most callers want.
    """
    with _CONFIG_LOCK:
        yield


def read_config(path) -> dict:
    """Read the whole of config.json, or {} if there isn't one yet.

    Every reader in the process goes through this, so that no read can land in
    the middle of a write. Reading the file directly is what produced
    "Expecting value: line 1 column 1 (char 0)" during an import: the reader
    caught a truncated file while another thread was rewriting it.
    """
    path = Path(path)
    with _CONFIG_LOCK:
        if not path.exists():
            return {}
        text = _past_transient_locks(lambda: path.read_text(encoding="utf-8"))
    # A zero-byte config is "no projects", not a parse error. It can only come
    # from a crash or from a version of this code that wrote in place, and
    # treating it as {} loses nothing that is still on disk.
    if not text.strip():
        return {}
    return json.loads(text) or {}


def write_config(path, config) -> None:
    """Replace config.json in one step.

    The new copy is written to a sibling temp file and renamed over the old
    one, so a concurrent reader sees either the whole previous file or the
    whole new one -- never the empty window that `open(path, "w")` opens for as
    long as it takes to serialize a few hundred KB. The lock keeps writers off
    each other; the rename is what protects readers in other processes (a
    notebook sidecar and a CLI server can share one data directory).
    """
    path = Path(path)
    with _CONFIG_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(config, handle, indent=4)
                handle.flush()
                os.fsync(handle.fileno())
            _past_transient_locks(lambda: os.replace(tmp, path))
        finally:
            # Nothing to remove on the happy path -- the rename consumed it.
            tmp.unlink(missing_ok=True)


def _past_transient_locks(action, attempts: int = 100, delay: float = 0.02):
    """Run a file operation, retrying past Windows' brief sharing violations.

    Windows refuses to replace a file another process has open, and refuses to
    open one that is being replaced -- and unlike POSIX there is no way to ask
    to be let through. Both windows are one rename long, so a short retry turns
    a hard failure into a wait. This only matters between processes (a notebook
    sidecar and a CLI server sharing a data directory); readers and writers in
    one process are already serialized by the lock. Giving up re-raises rather
    than leaving the caller with a half-truth.

    Two seconds of budget, not the 400 ms this started with: the thing holding
    the handle is often not another Plexora process at all but a scanner --
    Defender, or a sync client walking a data directory that lives in Dropbox --
    and those hold on for longer than one rename. The cost is paid only when a
    write is genuinely blocked, and losing a project record is far worse than
    waiting.
    """
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


#: Column roles the core records centrally. A plugin names these, never a
#: literal column name -- see plexora/api/dataset.py.
ROLE_NAMES = ("cell_id", "x", "y", "image_id", "celltype")

#: Human labels for the roles, used by the requirements modal and the edit page
#: so the wording is identical wherever a role is asked for.
ROLE_LABELS = {
    "cell_id": "Cell ID column",
    "x": "X coordinate column",
    "y": "Y coordinate column",
    "image_id": "Image ID column",
    "celltype": "Cell type column",
}

#: The roles the CSV import screen confirms, in ask order.
#:
#: Every one of these decides how the table is *read*: which column identifies
#: a cell, where the cell sits, and which image it belongs to. The user is
#: already looking at the columns there, so confirming them costs nothing.
#:
#: `celltype` is deliberately absent. Nothing in core reads it, so asking at
#: import puts a question in front of every user for the benefit of whichever
#: plugin might eventually want an annotation column -- and a plugin that does
#: want one declares it (`Requires(roles=("celltype",))`) and gets asked
#: through the requirements modal at the moment it matters. The edit page still
#: offers every role, because that is an editor rather than a checkpoint.
IMPORT_ROLES = ("cell_id", "x", "y", "image_id")

#: Feature-table formats. The key doubles as the adapter registry key
#: (server/models/adapters/__init__.py).
DATA_TYPES = ("csv", "anndata", "spatialdata")

#: How per-cell results are drawn over the image, best first. A segmentation
#: mask shows the real cell shape and is preferred whenever one exists;
#: centroids are the fallback for a project that has coordinates but no mask.
#:
#: The order is the default: `Project.cell_layer` resolves to the first option
#: the project can actually draw, and the stored value only ever records a user
#: overriding that on the edit page. This used to be a question the host asked
#: before a cell-drawing tool could open, on the grounds that both look
#: plausible on screen -- but a user who supplied a mask wants the mask, and
#: making them say so before Thresholding would open was a dialog with a
#: foregone conclusion.
CELL_LAYERS = ("segmentation", "centroids")

#: How an image's pixels are read: as one true-colour picture, or as a stack of
#: independent marker channels. Detected from the file at registration
#: (server/utils/brightfield.py) and overridable per project; absent means Auto.
#: Only the first is ever an `image_kind` -- a fluorescence image is named for
#: the container it came out of ("ome_tiff", "ome_zarr"), because that is what
#: it was before this distinction existed and what every stored config says.
IMAGE_TYPE_BRIGHTFIELD = "brightfield"
IMAGE_TYPE_FLUORESCENCE = "fluorescence"
IMAGE_TYPES = (IMAGE_TYPE_BRIGHTFIELD, IMAGE_TYPE_FLUORESCENCE)


def _clean(mapping):
    """Drop None values so an absent role is absent from the JSON rather than
    stored as an explicit null -- keeps hand-inspected config files readable."""
    return {k: v for k, v in (mapping or {}).items() if v is not None}


@dataclass(frozen=True)
class ColumnRoles:
    """Role -> column name. A None role is not an error; it is information that
    has not been collected yet, and something may ask for it later."""

    cell_id: str | None = None
    x: str | None = None
    y: str | None = None
    image_id: str | None = None
    celltype: str | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ColumnRoles":
        raw = raw or {}
        return cls(**{role: raw.get(role) or None for role in ROLE_NAMES})

    def to_dict(self) -> dict:
        return _clean({role: getattr(self, role) for role in ROLE_NAMES})

    def get(self, role: str) -> str | None:
        if role not in ROLE_NAMES:
            raise KeyError(f"Unknown column role: {role!r}")
        return getattr(self, role)

    def missing(self, roles: Iterable[str]) -> list[str]:
        """Which of `roles` have no column assigned, in the order given."""
        return [role for role in roles if not self.get(role)]

    def with_values(self, values: Mapping[str, Any]) -> "ColumnRoles":
        """Copy with `values` applied. An empty string clears a role, which is
        how the edit form expresses "unset this"; an absent key leaves it be."""
        changes = {}
        for role, value in (values or {}).items():
            if role not in ROLE_NAMES:
                continue
            changes[role] = (value or None) if isinstance(value, str) else None
        return replace(self, **changes)


@dataclass(frozen=True)
class ColumnGroups:
    """The marker/metadata split for this dataset.

    A property of the imported data, not of any one plugin, so it is stored
    centrally and every plugin reads the same answer. Empty means nobody has
    classified the columns yet.
    """

    markers: tuple[str, ...] = ()
    metadata: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ColumnGroups":
        raw = raw or {}
        return cls(
            markers=tuple(raw.get("markers") or ()),
            metadata=tuple(raw.get("metadata") or ()),
        )

    def to_dict(self) -> dict:
        return {"markers": list(self.markers), "metadata": list(self.metadata)}

    @property
    def classified(self) -> bool:
        """Whether the split has been established. Metadata alone does not
        count: a table with no markers has nothing to threshold or plot, and
        treating that as classified would let a marker-hungry plugin open onto
        an empty panel."""
        return bool(self.markers)

    @property
    def all(self) -> tuple[str, ...]:
        return tuple(self.markers) + tuple(self.metadata)


@dataclass(frozen=True)
class DataSpec:
    """The feature table: where it is, how to read it, and what its columns mean.

    `coordinates`/`features`/`subset` are the AnnData/SpatialData read spec and
    stay opaque here -- the adapters own their shape. Everything else is
    format-independent.
    """

    type: str
    src: str
    table: str | None = None
    coordinates: Mapping[str, Any] = field(default_factory=dict)
    features: Mapping[str, Any] = field(default_factory=dict)
    subset: Mapping[str, Any] = field(default_factory=dict)
    #: AnnData/SpatialData only: which obs column supplies the cell identifier,
    #: or None to use the positional row index.
    #:
    #: This looks like a duplicate of `roles.cell_id` and is not. The role
    #: names a column in the table that comes OUT of the adapter; this names an
    #: input to it. They usually hold the same string, but None here means
    #: "number the rows", which is a different instruction from the role naming
    #: a column that happens to be called "id" -- and real exemplar data does
    #: have an obs column literally named "id". Collapsing the two turned the
    #: adapter's "that name is reserved" error into a silent fallback to
    #: positional ids, quietly ignoring the column the user picked.
    obs_id_field: str | None = None
    normalization: str = "none"
    is_transformed: bool = False
    celltype_data: str | None = None
    roles: ColumnRoles = field(default_factory=ColumnRoles)
    columns: ColumnGroups = field(default_factory=ColumnGroups)
    #: AnnData/SpatialData only: the source file's `obs` column names, exactly
    #: as the file carries them and in its own order.
    #:
    #: Recorded because it is the vocabulary the role questions are asked in
    #: for these formats. `columns.metadata` is not: the adapter builds the
    #: table from a read spec and emits four columns (`id`, `X`, `Y`, the id
    #: field) plus the markers, so offering that list asked the user to choose
    #: which of Plexora's own synthesized columns holds the cell id -- a
    #: question with only one possible answer, and never the one they wanted.
    obs_columns: tuple[str, ...] = ()
    #: AnnData/SpatialData only: the extra expression matrices the source file
    #: carries alongside `X`. Recorded for the same reason as `obs_columns` --
    #: it is what the user picks between when saying which matrix holds the
    #: marker intensities, and re-reading the file to find out would mean
    #: opening it every time the edit page loads.
    layers: tuple[str, ...] = ()
    #: AnnData/SpatialData only: the source file's `obsm` arrays, as
    #: {"name": str, "shape": [int, ...]}. Recorded for the same reason as
    #: `layers`, and the shape travels with the name because it is the only
    #: thing that distinguishes one candidate from another: a store commonly
    #: carries `spatial` and `X_umap` with identical (n, 2) float32 shapes, so
    #: the user picking between them needs to see both listed rather than have
    #: one chosen for them by name.
    obsm: tuple[Mapping[str, Any], ...] = ()
    #: The user's explicit answer that this table covers exactly one image, as
    #: opposed to simply having no image-id column recorded.
    #:
    #: A separate field rather than a blank `roles.image_id`, because those are
    #: different states: "I looked and there is one image" is an answer, while
    #: an absent role is a question nobody has put. Whether a table spans
    #: several images cannot be told from the data -- deciding it needs to know
    #: WHICH column is the image id, which is the very thing being asked -- so
    #: it is asked rather than inferred from a column-name heuristic.
    single_image: bool = False
    #: The user's explicit answer that no obs column holds the cell identifier,
    #: so the rows are numbered instead.
    #:
    #: The same distinction `single_image` draws, for the same reason. An unset
    #: `obs_id_field` cannot carry it: that is also the state of a project
    #: nobody has asked yet, and the importer leaves it unset on every AnnData
    #: and SpatialData it registers. Without somewhere to record the answer,
    #: "number the rows" could only be said by leaving the question blank -- and
    #: a blank that satisfies a requirement is indistinguishable from a default
    #: that was never looked at, which is how a project came to keep a
    #: positional cell id while its mask was labelled by an obs column.
    row_number_ids: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "DataSpec | None":
        if not raw or not raw.get("src"):
            return None
        return cls(
            type=raw.get("type") or "csv",
            src=raw["src"],
            table=raw.get("table") or None,
            coordinates=dict(raw.get("coordinates") or {}),
            features=dict(raw.get("features") or {}),
            subset=dict(raw.get("subset") or {}),
            obs_id_field=raw.get("obsIdField") or None,
            normalization=raw.get("normalization") or "none",
            is_transformed=bool(raw.get("isTransformed")),
            celltype_data=raw.get("celltypeData") or None,
            roles=ColumnRoles.from_dict(raw.get("roles")),
            columns=ColumnGroups.from_dict(raw.get("columns")),
            obs_columns=tuple(raw.get("obsColumns") or ()),
            layers=tuple(raw.get("layers") or ()),
            obsm=tuple(dict(entry) for entry in (raw.get("obsm") or ())),
            single_image=bool(raw.get("singleImage")),
            row_number_ids=bool(raw.get("rowNumberIds")),
        )

    def to_dict(self) -> dict:
        out = {
            "type": self.type,
            "src": self.src,
            "normalization": self.normalization,
            "isTransformed": self.is_transformed,
            "roles": self.roles.to_dict(),
            "columns": self.columns.to_dict(),
        }
        if self.table:
            out["table"] = self.table
        if self.coordinates:
            out["coordinates"] = dict(self.coordinates)
        if self.features:
            out["features"] = dict(self.features)
        if self.subset:
            out["subset"] = dict(self.subset)
        if self.obs_id_field:
            out["obsIdField"] = self.obs_id_field
        if self.celltype_data:
            out["celltypeData"] = self.celltype_data
        if self.obs_columns:
            out["obsColumns"] = list(self.obs_columns)
        if self.layers:
            out["layers"] = list(self.layers)
        if self.obsm:
            out["obsm"] = [dict(entry) for entry in self.obsm]
        if self.single_image:
            out["singleImage"] = True
        if self.row_number_ids:
            out["rowNumberIds"] = True
        return out

    @property
    def path(self) -> Path:
        return Path(self.src)


@dataclass(frozen=True)
class ImageSpec:
    """The one input every project has.

    `channels` keeps the stored `imageData` shape ({name, fullname, src}) --
    the viewer's tile path indexes it directly and is deliberately untouched by
    this refactor.

    `pyramid`/`pyramid_key` are the OME-Zarr counterpart of SegmentationSpec's
    `derived`/`source_key`, and only an OME-Zarr image ever has them: a store
    that arrives without enough resolution levels to zoom out of gets the
    missing coarse ones derived once at import (server/utils/ome_zarr.py), and
    the key fingerprints the source so a later load can tell with a stat whether
    they still correspond. Both are dropped from the config when unset, so every
    project that predates them is written back byte for byte.

    `image_type_choice` is the user's Auto/H&E/Fluorescence override and
    `image_type_detected`/`image_type_reason` are what the detector concluded
    at registration (server/utils/brightfield.py). The three are separate on
    purpose: `kind` is the mode actually being served and every gate in the app
    reads it, the choice outlives whatever the detector said, and the reason is
    what the edit page shows so the user can tell whether overriding is
    warranted. None of them is ever written back to the image file.
    """

    src: str | None = None
    kind: str = "ome_tiff"
    channels: tuple[Mapping[str, Any], ...] = ()
    width: int | None = None
    height: int | None = None
    max_level: int | None = None
    tile_width: int | None = None
    tile_height: int | None = None
    num_channels: int | None = None
    pyramid: str | None = None
    pyramid_key: str | None = None
    image_type_choice: str | None = None
    image_type_detected: str | None = None
    image_type_reason: str | None = None

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> "ImageSpec":
        return cls(
            src=entry.get("channelFile"),
            kind=entry.get("image_kind") or "ome_tiff",
            channels=tuple(entry.get("imageData") or ()),
            width=entry.get("width"),
            height=entry.get("height"),
            max_level=entry.get("maxLevel"),
            tile_width=entry.get("tileWidth"),
            tile_height=entry.get("tileHeight"),
            num_channels=entry.get("num_channels"),
            pyramid=entry.get("imagePyramid"),
            pyramid_key=entry.get("imagePyramidKey"),
            image_type_choice=entry.get("imageTypeChoice"),
            image_type_detected=entry.get("imageTypeDetected"),
            image_type_reason=entry.get("imageTypeReason"),
        )

    def to_entry(self) -> dict:
        return _clean({
            "channelFile": self.src,
            "image_kind": self.kind,
            "imageData": list(self.channels),
            "width": self.width,
            "height": self.height,
            "maxLevel": self.max_level,
            "tileWidth": self.tile_width,
            "tileHeight": self.tile_height,
            "num_channels": self.num_channels,
            "imagePyramid": self.pyramid,
            "imagePyramidKey": self.pyramid_key,
            "imageTypeChoice": self.image_type_choice,
            "imageTypeDetected": self.image_type_detected,
            "imageTypeReason": self.image_type_reason,
        })

    @property
    def real_channels(self) -> list[Mapping[str, Any]]:
        """Image channels, excluding the 'Area' placeholder that only exists
        when segmentation was registered."""
        return [c for c in self.channels if c.get("fullname") != "Area"]

    @property
    def channel_names(self) -> list[str]:
        return [c["fullname"] for c in self.real_channels]


@dataclass(frozen=True)
class SegmentationSpec:
    """The mask, when there is one.

    `derived` is the pyramid Plexora serves; `source`/`source_key` fingerprint
    the user's original so a later load can confirm the derived file is still
    current with a stat instead of rebuilding it.
    """

    derived: str | None = None
    source: str | None = None
    source_key: str | None = None
    mode: str | None = None
    status: str = "ready"

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> "SegmentationSpec":
        return cls(
            derived=entry.get("segmentation") or None,
            source=entry.get("segmentationSource") or None,
            source_key=entry.get("segmentationSourceKey") or None,
            mode=entry.get("segmentationMode") or None,
            status=entry.get("segmentation_status") or "ready",
        )

    def to_entry(self) -> dict:
        out = {"segmentation": self.derived, "segmentation_status": self.status}
        out.update(_clean({
            "segmentationSource": self.source,
            "segmentationSourceKey": self.source_key,
            "segmentationMode": self.mode,
        }))
        return out

    @property
    def available(self) -> bool:
        """A mask the viewer can serve right now. A pending job has a source but
        no derived pyramid yet, so this is False until it lands."""
        return bool(self.derived)

    @property
    def pending(self) -> bool:
        return self.status == "pending"

    @property
    def requested(self) -> bool:
        """The user has supplied a mask, whether or not it is servable yet."""
        return bool(self.derived or self.source)


#: The three scientific resources a project can have, in the order the attach
#: screens present them. Deliberately not "everything a project owns": ROIs,
#: figures, gates and settings are application state, they live in this
#: server's own database, and they are never bound to anywhere else.
RESOURCE_KINDS = ("image", "segmentation", "table")


@dataclass(frozen=True)
class ResourceBinding:
    """Where one scientific resource lives, when it is not simply here.

    A project with no bindings is the ordinary single-server project, and that
    is the state of every project registered before this existed: absence means
    local, so there is no migration and nothing to backfill.

    What this holds is deliberately not a path. A path is the answer for a local
    resource and is recorded where it always was (`channelFile`, `segmentation`,
    `dataset.src`); a node-backed resource has no path on this machine, and
    storing the node's own filesystem layout here would bake one machine's
    mount points into a file another machine reads.

    `fingerprint` is what the resource looked like when it was attached -- size,
    mtime, and the per-kind identity facts (`row_count`, the cell-id range, the
    image's dimensions). It is checked before anything is written back through
    the node, because a table whose row count changed cannot receive per-row
    results: every value would land on a different cell than the one it was
    computed for.
    """

    kind: str
    provider: str = "local"
    node: str | None = None
    resource_id: str | None = None
    fingerprint: Mapping[str, Any] | None = None
    capabilities: tuple[str, ...] = ()
    #: Subkeys this class does not model, preserved verbatim -- the same
    #: promise `Project.extra` makes, applied per binding so a newer Plexora's
    #: extra facts survive a save by an older one.
    extra: Mapping[str, Any] = field(default_factory=dict)

    _FIELDS: ClassVar[tuple] = ("provider", "node", "resource_id",
                                "fingerprint", "capabilities")

    @property
    def is_node(self) -> bool:
        return self.provider == "node"

    @classmethod
    def from_dict(cls, kind: str, raw: Mapping[str, Any] | None) -> "ResourceBinding | None":
        if not raw:
            return None
        return cls(
            kind=kind,
            provider=raw.get("provider") or "local",
            node=raw.get("node") or None,
            resource_id=raw.get("resource_id") or None,
            fingerprint=dict(raw["fingerprint"]) if raw.get("fingerprint") else None,
            capabilities=tuple(raw.get("capabilities") or ()),
            extra={k: v for k, v in raw.items() if k not in cls._FIELDS},
        )

    def to_dict(self) -> dict:
        out = dict(self.extra)
        out["provider"] = self.provider
        out.update(_clean({
            "node": self.node,
            "resource_id": self.resource_id,
            "fingerprint": dict(self.fingerprint) if self.fingerprint else None,
        }))
        if self.capabilities:
            out["capabilities"] = list(self.capabilities)
        return out

    def can(self, capability: str) -> bool:
        """Whether the node serving this declared it can do something.

        Asked rather than assumed because a node is a pip install of its own:
        one built without the ROI plugin cannot run `roi.map_cells`, and finding
        that out from a 404 halfway through a user's export is a worse answer
        than not offering the button.
        """
        return capability in self.capabilities


#: Keys ImageSpec, SegmentationSpec and the Project itself own. Anything else
#: in an entry is unmodelled and round-trips through `extra`.
_MODELLED_KEYS = frozenset({
    "channelFile", "image_kind", "imageData", "width", "height", "maxLevel",
    "tileWidth", "tileHeight", "num_channels", "imagePyramid", "imagePyramidKey",
    "imageTypeChoice", "imageTypeDetected", "imageTypeReason",
    "segmentation", "segmentation_status", "segmentationSource",
    "segmentationSourceKey", "segmentationMode",
    "dataset", "createdAt", "lastOpenedAt", "cellLayer", "confirmed",
    "resources",
})

#: Requirement keys that describe the feature table rather than the project.
#: Swapping the data file invalidates the answers to these -- the columns they
#: name may not exist in the new file -- while the mask and the cell-layer
#: choice are unaffected by it.
def _is_table_scoped(key: str) -> bool:
    return (key in ("table", "markers", "features", "coordinates")
            or key.startswith("role:"))


def _with_coordinate_spec(spec, answer: Mapping[str, Any]):
    """Validate one coordinate answer and write it onto a DataSpec.

    Shared by `with_coordinates` and the obs half of `with_role_answers`, so
    the two cannot disagree about what a valid answer is.

    Validation is against what the file was recorded as carrying. An obsm key
    is only checked when that list is known: a project imported before obsm was
    recorded has an empty list, and refusing every answer because we never
    looked would block the one control that can fix it.
    """
    answer = dict(answer or {})
    source = answer.get("source")
    if source == "obsm":
        key = (answer.get("obsm_key") or "").strip()
        if not key:
            raise ValueError("Choose which obsm array holds the coordinates.")
        known = [entry.get("name") for entry in spec.obsm]
        if known and key not in known:
            raise ValueError(f"This file has no obsm array named {key!r}.")
        return replace(spec, coordinates={"source": "obsm", "obsm_key": key})
    if source == "obs":
        x = (answer.get("x_column") or "").strip()
        y = (answer.get("y_column") or "").strip()
        if not x or not y:
            raise ValueError("Pick both an X and a Y coordinate column, or neither.")
        known = list(spec.obs_columns)
        missing = [c for c in (x, y) if known and c not in known]
        if missing:
            raise ValueError(f"This file has no obs column named {missing[0]!r}.")
        return replace(spec, coordinates={
            "source": "obs", "x_column": x, "y_column": y})
    raise ValueError("Say whether the coordinates are in obs or obsm.")


#: Written by a requirements modal that could not name the role it was asking
#: about (Requirement.describe() did not send `role`, so every select posted
#: under this one key). Its presence in a stored `confirmed` list means the
#: role answers recorded alongside it were discarded before they reached the
#: project -- see _repair_confirmed.
_LOST_ROLE_KEY = "role:undefined"


def _repair_confirmed(keys: Iterable[str]) -> tuple[str, ...]:
    """Drop role confirmations that were recorded without the answer.

    A project carrying `role:undefined` was set up through the broken modal:
    the user picked columns, the answers were thrown away, and the questions
    were marked answered anyway -- so the project kept its guessed roles and
    nothing would ever ask again. Un-confirming the roles is what puts them
    back in front of the user once, which is the only way the real answer can
    still be given. Every other confirmation is left alone; those keys were
    posted correctly.

    Done on read so existing configs repair themselves without a migration.
    """
    keys = tuple(keys or ())
    if _LOST_ROLE_KEY not in keys:
        return keys
    return tuple(k for k in keys if not k.startswith("role:"))


def _resources_from_entry(entry: Mapping[str, Any]) -> dict:
    """The `resources` block as typed bindings, keeping only the remote ones.

    A binding that says `provider: "local"` is dropped rather than kept as an
    object meaning "here", so `project.resources` reads as "the resources that
    are somewhere else" and an empty mapping is unambiguous. That matters
    downstream: `ProviderSet.has_remote` is what every dispatch guard tests,
    and a project holding three local bindings must produce the same False as a
    project holding none.
    """
    raw = entry.get("resources")
    if not isinstance(raw, Mapping):
        return {}
    bindings = {}
    for kind in RESOURCE_KINDS:
        binding = ResourceBinding.from_dict(kind, raw.get(kind))
        if binding is not None and binding.is_node:
            bindings[kind] = binding
    return bindings


@dataclass(frozen=True)
class Project:
    """One registered project."""

    name: str
    image: ImageSpec = field(default_factory=ImageSpec)
    segmentation: SegmentationSpec = field(default_factory=SegmentationSpec)
    dataset: DataSpec | None = None
    created_at: str | None = None
    last_opened_at: str | None = None
    #: One of CELL_LAYERS when the user has overridden the default on the edit
    #: page, None otherwise. Read `cell_layer` rather than this: that resolves
    #: the default, and it is what every drawing surface wants.
    cell_layer_choice: str | None = None
    #: Requirement keys the user has explicitly answered, as opposed to values
    #: the column predictor guessed. The two are indistinguishable by looking at
    #: the stored value, and they are not the same thing: a guess should be put
    #: in front of the user once, an answer never again.
    confirmed: tuple[str, ...] = ()
    #: kind -> ResourceBinding, for the resources that are NOT on this machine.
    #:
    #: Empty for every single-server project, which is what makes the whole
    #: multi-source path free when it is not used: `resolve_providers` reads
    #: this, finds nothing, and hands back three local providers.
    resources: Mapping[str, "ResourceBinding"] = field(default_factory=dict)
    #: Keys this module does not model, preserved verbatim across a save.
    extra: Mapping[str, Any] = field(default_factory=dict)
    #: The root this project's registry entry was read from, and therefore
    #: where its image, mask and feature table live.
    #:
    #: None for a project built in memory rather than loaded -- `save()` then
    #: falls back to the user's own root, which is what the programmatic
    #: registration API in plexora/datasource.py wants.
    #:
    #: Never serialized: it is a fact about where the entry was found, not a
    #: field of the entry, and writing it would freeze one machine's layout
    #: into a file that another machine reads.
    home_root: Path | None = None

    # -- construction ----------------------------------------------------

    @classmethod
    def from_entry(cls, name: str, entry: Mapping[str, Any] | None,
                   home_root=None) -> "Project":
        entry = entry or {}
        return cls(
            name=name,
            home_root=Path(home_root) if home_root is not None else None,
            image=ImageSpec.from_entry(entry),
            segmentation=SegmentationSpec.from_entry(entry),
            dataset=DataSpec.from_dict(entry.get("dataset")),
            created_at=entry.get("createdAt"),
            last_opened_at=entry.get("lastOpenedAt"),
            cell_layer_choice=(entry.get("cellLayer")
                               if entry.get("cellLayer") in CELL_LAYERS else None),
            confirmed=_repair_confirmed(entry.get("confirmed")),
            resources=_resources_from_entry(entry),
            extra={k: v for k, v in entry.items() if k not in _MODELLED_KEYS},
        )

    def to_entry(self) -> dict:
        entry = dict(self.extra)
        entry.update(self.image.to_entry())
        entry.update(self.segmentation.to_entry())
        entry.update(_clean({
            "createdAt": self.created_at,
            "lastOpenedAt": self.last_opened_at,
            # The user's override only. Writing the resolved default would
            # freeze today's answer into the file, so a project that later
            # gained a mask would go on drawing centroids.
            "cellLayer": self.cell_layer_choice,
        }))
        if self.confirmed:
            entry["confirmed"] = list(self.confirmed)
        # Omitted entirely when empty, not written as {}. Absence is what says
        # "everything is local", and it is the state of every project that
        # predates this key -- writing an empty object into every config.json
        # on the next save would be a migration nobody asked for.
        if self.resources:
            entry["resources"] = {kind: binding.to_dict()
                                  for kind, binding in self.resources.items()}
        # Written even when None: an explicit null is what says "this project
        # has no feature table", as opposed to an older entry that predates the
        # key. Nothing here has to guess.
        entry["dataset"] = self.dataset.to_dict() if self.dataset else None
        return entry

    def patch(self, **changes) -> "Project":
        """Copy with the named fields replaced. Merge semantics: a field not
        named is carried across untouched, `extra` included."""
        return replace(self, **changes)

    # -- questions everything else asks ----------------------------------

    @property
    def has_table(self) -> bool:
        return self.dataset is not None

    @property
    def source_kind(self) -> str | None:
        return self.dataset.type if self.dataset else None

    @property
    def roles(self) -> ColumnRoles:
        return self.dataset.roles if self.dataset else ColumnRoles()

    @property
    def columns(self) -> ColumnGroups:
        return self.dataset.columns if self.dataset else ColumnGroups()

    @property
    def columns_are_structural(self) -> bool:
        """Whether the marker/metadata split comes from the file's own shape.

        AnnData and SpatialData draw the line themselves -- `var` is markers,
        `obs` is annotations -- so there is nothing for the user to confirm. A
        CSV header does not, which is the entire reason the classification
        screen exists.
        """
        return self.source_kind in ("anndata", "spatialdata")

    # -- where the scientific data lives ---------------------------------

    def resource(self, kind: str) -> "ResourceBinding | None":
        """The binding for one resource, or None when it is on this machine."""
        if kind not in RESOURCE_KINDS:
            raise KeyError(f"Unknown resource kind: {kind!r}")
        return self.resources.get(kind)

    @property
    def is_distributed(self) -> bool:
        return bool(self.resources)

    def with_resource(self, kind: str, binding: "ResourceBinding | None") -> "Project":
        """Bind one resource to a node, or unbind it back to local.

        Merge semantics, like everything else here: binding the table leaves
        the image and the mask exactly as they were. Passing None removes the
        binding, which is how a resource comes home -- the user copies the file
        onto this machine and repoints the project at it.
        """
        if kind not in RESOURCE_KINDS:
            raise KeyError(f"Unknown resource kind: {kind!r}")
        resources = dict(self.resources)
        if binding is None or not binding.is_node:
            resources.pop(kind, None)
        else:
            resources[kind] = binding
        return self.patch(resources=resources)

    def with_roles(self, values: Mapping[str, Any]) -> "Project":
        """Assign column roles centrally. No-op without a table -- a role names
        a column, and there are no columns until there is one."""
        if not self.dataset:
            return self
        return self.patch(dataset=replace(self.dataset, roles=self.roles.with_values(values)))

    # -- which matrix the intensities come from ------------------------------

    @property
    def feature_source(self) -> str:
        """Which matrix is read, as the picker words it: `"X"` for the main
        matrix, `"layer:<name>"` for one of `adata.layers`.

        Prefixed rather than bare so a layer that anndata permits to be called
        "X" cannot be confused with the main matrix.
        """
        layer = (self.dataset.features or {}).get("layer") if self.dataset else None
        return f"layer:{layer}" if layer else "X"

    @property
    def feature_options(self) -> list[dict]:
        """Every matrix this file offers, worded for a picker.

        Empty for a CSV, which has exactly one table of numbers and nothing to
        choose between. For AnnData and SpatialData it is always at least `X`,
        so a file with no layers still gets a one-option list rather than an
        empty one -- the caller decides whether one option is worth a control,
        and the log switch beside it is a real question either way.
        """
        if not self.dataset or not self.columns_are_structural:
            return []
        return [{"value": "X", "label": "X — the main matrix"}] + [
            {"value": f"layer:{name}", "label": f'layers["{name}"]'}
            for name in self.dataset.layers
        ]

    @property
    def log_transformed(self) -> bool:
        """Whether the values are log1p'd as they are read."""
        return bool(self.dataset.is_transformed) if self.dataset else False

    def with_log_transform(self, enabled: bool) -> "Project":
        """Log1p the matrix on the way in, or stop doing so.

        Separate from which matrix is read because it composes with it: a file
        may carry raw counts in `X` and nothing else, and thresholding raw
        counts on a linear axis is unreadable. This is the only way to say "the
        values I have are counts" without going back and editing the file.
        """
        if not self.dataset:
            return self
        return self.patch(dataset=replace(self.dataset, is_transformed=bool(enabled)))

    def with_layers(self, names: Iterable[str]) -> "Project":
        """Record which matrices the source file carries.

        Backfill for a project imported before they were recorded, so the
        choice below can be validated against a real list rather than an empty
        one -- see project_routes._source_layers.
        """
        if not self.dataset:
            return self
        return self.patch(dataset=replace(self.dataset, layers=tuple(names or ())))

    def with_obsm(self, entries: Iterable[Mapping[str, Any]]) -> "Project":
        """Record which obsm arrays the source file carries, and their shapes.

        Backfill for a project imported before they were recorded, exactly like
        `with_layers` -- and needed for the same reason: without it the one
        control that can correct a project reading its coordinates from the
        wrong array is the one control that has nothing to offer.
        """
        if not self.dataset:
            return self
        return self.patch(dataset=replace(
            self.dataset, obsm=tuple(dict(entry) for entry in (entries or ()))))

    def with_feature_source(self, value: str) -> "Project":
        """Point the reader at a different expression matrix.

        Worth being able to change after import, not only during it: raw counts
        and a log-transformed copy live side by side in one file, and which one
        was read decides what every marker histogram in the app is a histogram
        of. Getting it wrong is not a cosmetic mistake -- thresholds set against
        raw counts mean nothing on a log-scaled panel.
        """
        if not self.dataset:
            return self
        value = (value or "").strip()
        if value == "X":
            features = {"source": "X"}
        elif value.startswith("layer:"):
            layer = value.split(":", 1)[1]
            if layer not in self.dataset.layers:
                raise ValueError(f"This file has no matrix named {layer!r}.")
            features = {"source": "layer", "layer": layer}
        else:
            # Neither an answer nor a clearing -- leave the spec alone rather
            # than writing something the adapter cannot read.
            return self
        return self.patch(dataset=replace(self.dataset, features=features))

    # -- the role questions, in the vocabulary they are asked in -------------
    #
    # For a CSV a role simply names a column of the table, and asking, storing
    # and reading it are all the same string. For AnnData and SpatialData the
    # table does not exist until the adapter builds it, and three of the roles
    # are really instructions for building it: which obs column identifies a
    # cell, and which two hold its coordinates. The three properties below are
    # the translation, so every surface asks one question ("which obs column?")
    # and every reader still gets a name that is in the loaded table.

    @property
    def role_columns(self) -> list[str]:
        """The columns to offer when asking which one fills a role.

        Unfiltered on purpose. Which of a file's own annotations holds the cell
        id is not something a name heuristic gets to decide -- guessing is what
        the prefilled answer is for, and narrowing the list just hides the
        column the user was looking for.
        """
        if not self.dataset:
            return []
        if self.dataset.obs_columns:
            return list(self.dataset.obs_columns)
        # A CSV, or an AnnData imported before obs columns were recorded: every
        # column of the table, metadata first because a role names a
        # measurement about a cell far more often than a marker measured on one.
        return list(self.columns.metadata) + list(self.columns.markers)

    @property
    def role_answers(self) -> dict:
        """The current answers, in the same vocabulary. Inverse of
        `with_role_answers`, and what a form prefills its selects from."""
        if not self.dataset:
            return {}
        if not self.columns_are_structural:
            return self.roles.to_dict()
        # x/y are deliberately absent: for these formats the coordinate source
        # is one question with its own answer shape (see `coordinate_options`),
        # not two independent column roles. An obsm array supplies both axes at
        # once, so "which obs column is X" has no answer for it -- which is why
        # this used to report a blank and let `role_defaults` label it.
        return _clean({
            # None means "number the rows", which is a real answer and not a
            # gap -- see DataSpec.obs_id_field.
            "cell_id": self.dataset.obs_id_field,
            # Read straight off the source file by whatever needs it, so this
            # one is an obs name in both vocabularies.
            "image_id": self.roles.image_id,
            "celltype": self.roles.celltype,
        })

    @property
    def role_defaults(self) -> dict:
        """The answer a role has that names no column, worded for an option.

        Only the cell id has one, and only for the formats where the adapter
        can number the rows itself. It is offered as a real choice rather than
        as the wording on a blank: a blank that satisfies the question looks
        exactly like a default nobody read, which is the whole failure this
        replaces. See DataSpec.row_number_ids.
        """
        if not self.dataset or not self.columns_are_structural:
            return {}
        return {"cell_id": "Row number (0, 1, 2 … in file order)"}

    @property
    def coordinate_options(self) -> dict:
        """What the coordinate question is made of: the candidate sources, and
        which one is currently recorded.

        Both vocabularies are offered because a file can answer in either -- an
        `obsm` array holding both axes, or a pair of `obs` columns -- and which
        one a given file uses is not something a name heuristic gets to settle.
        Every obsm array is listed with its shape rather than filtered down to
        the ones that "look spatial": a store routinely carries `spatial` and
        `X_umap` at identical (n, 2) float32, so filtering cannot separate them
        and choosing by name is the mistake this replaces.

        `current` is the recorded read spec, which for a fresh import is the
        importer's proposal -- a prefill to accept or correct, never a settled
        answer. It is empty when nothing was detected.
        """
        if not self.dataset or not self.columns_are_structural:
            return {}
        return {
            "obsm": [dict(entry) for entry in self.dataset.obsm],
            "obs": list(self.dataset.obs_columns),
            "current": dict(self.dataset.coordinates or {}),
        }

    def with_role_answers(self, values: Mapping[str, Any]) -> "Project":
        """Record answers to the role questions.

        For AnnData and SpatialData this writes the read spec as well as the
        roles: naming an obs column in `roles.cell_id` alone would point the
        role at a column the loaded table does not have. Going through the read
        spec is what makes the answer take effect -- and it matters, because a
        segmentation overlay only lines up when the cell ids are the mask's own
        label values, which live in obs and never in a positional row index.
        """
        if not self.dataset:
            return self
        values = {role: value for role, value in (values or {}).items()
                  if role in ROLE_NAMES}
        # Naming an image-id column is the other answer to the question
        # `single_image` answers, so it retracts it. Applied before the format
        # split because it holds for every format -- a CSV spans several images
        # exactly as readily as an AnnData does.
        project = self
        if values.get("image_id"):
            project = project.patch(dataset=replace(project.dataset, single_image=False))
        if not project.columns_are_structural:
            return project.with_roles(values)

        spec = project.dataset
        roles = {}
        if "cell_id" in values:
            column = values["cell_id"] or None
            # Naming a column is the other answer to the question
            # `row_number_ids` answers, so it retracts it -- the same way
            # naming an image-id column retracts `single_image` above.
            spec = replace(spec, obs_id_field=column,
                           row_number_ids=spec.row_number_ids and not column)
            # The adapter names the emitted column after the obs field it was
            # given, and falls back to its positional "id" when given none.
            roles["cell_id"] = column or "id"
        if "x" in values or "y" in values:
            # The obs half of the coordinate question, still reachable from the
            # programmatic API and from a CSV-shaped payload. The surfaces ask
            # it through `with_coordinates` now, which can also express the
            # obsm answer these two selects have no way to name.
            coordinates = dict(spec.coordinates or {})
            x = (values["x"] if "x" in values else coordinates.get("x_column")) or None
            y = (values["y"] if "y" in values else coordinates.get("y_column")) or None
            if x and y:
                spec = _with_coordinate_spec(
                    spec, {"source": "obs", "x_column": x, "y_column": y})
            elif not x and not y:
                spec = replace(spec, coordinates={})
            else:
                # Half a position is not a position, and applying it would read
                # one axis from obs and the other from obsm.
                raise ValueError(
                    "Pick both an X and a Y coordinate column, or neither.")
            # The adapter always emits its coordinates under these two names,
            # whichever obs columns they were read from.
            roles["x"], roles["y"] = "X", "Y"
        for role in ("image_id", "celltype"):
            if role in values:
                roles[role] = values[role]
        return self.patch(dataset=replace(spec, roles=spec.roles.with_values(roles)))

    def with_coordinates(self, answer: Mapping[str, Any]) -> "Project":
        """Record where each cell's position is read from.

        One question with two answer shapes, because the file has two places to
        put the same thing:

            {"source": "obsm", "obsm_key": "spatial"}
            {"source": "obs", "x_column": "X_centroid", "y_column": "Y_centroid"}

        Validated against what the file actually carries, so a stale form or a
        renamed key is refused rather than stored and discovered later by the
        adapter -- at which point the project is already unreadable.
        """
        if not self.dataset:
            return self
        spec = _with_coordinate_spec(self.dataset, answer)
        # The adapter always emits its coordinates under these two names,
        # whichever source they were read from.
        return self.patch(dataset=replace(
            spec, roles=spec.roles.with_values({"x": "X", "y": "Y"})))

    def with_single_image(self, flag: bool = True) -> "Project":
        """Record the user's answer that this table covers exactly one image.

        Kept apart from `roles.image_id` on purpose -- see DataSpec.single_image.
        Setting it clears any image-id column, since the two are alternative
        answers to one question rather than facts that can both hold.
        """
        if not self.dataset:
            return self
        spec = self.dataset
        if flag:
            spec = replace(spec, roles=spec.roles.with_values({"image_id": None}))
        return self.patch(dataset=replace(spec, single_image=bool(flag)))

    def with_row_number_ids(self, flag: bool = True) -> "Project":
        """Record the user's answer that no obs column holds the cell id.

        Kept apart from an unset `obs_id_field` on purpose -- see
        DataSpec.row_number_ids. Setting it clears any named column, since the
        two are alternative answers to one question.

        AnnData and SpatialData only: a CSV's cell id is a column of the file
        the user classified, and there is no row-numbering fallback to choose.
        """
        if not self.dataset or not self.columns_are_structural:
            return self
        spec = self.dataset
        if flag:
            spec = replace(spec, obs_id_field=None,
                           roles=spec.roles.with_values({"cell_id": "id"}))
        return self.patch(dataset=replace(spec, row_number_ids=bool(flag)))

    def with_columns(self, markers: Iterable[str], metadata: Iterable[str]) -> "Project":
        if not self.dataset:
            return self
        groups = ColumnGroups(markers=tuple(markers or ()), metadata=tuple(metadata or ()))
        return self.patch(dataset=replace(self.dataset, columns=groups))

    # -- the cell layer --------------------------------------------------

    @property
    def cell_layer_options(self) -> list[str]:
        """How this project could draw per-cell results, best first.

        Segmentation leads because it shows the real cell shape; centroids are
        the fallback and need only coordinates. An empty list means the project
        can draw neither, and whatever wants to is missing something more basic
        -- a mask, or the coordinate roles.
        """
        options = []
        if self.segmentation.requested:
            options.append("segmentation")
        if self.roles.x and self.roles.y:
            options.append("centroids")
        return options

    @property
    def cell_layer(self) -> str | None:
        """How this project draws per-cell results.

        The user's override if they made one on the edit page, otherwise the
        best this project can do: the mask when there is one, centroids
        otherwise. None only when it can draw neither, in which case whatever
        wanted to draw is missing something more basic.

        A stale override -- centroids chosen back when there was no mask, say --
        is honoured, because it is still a choice somebody made.
        """
        options = self.cell_layer_options
        if self.cell_layer_choice in options:
            return self.cell_layer_choice
        return options[0] if options else None

    def with_cell_layer(self, value: str | None) -> "Project":
        """Record an override, or clear one.

        None and "" both mean "no override" -- go back to whatever this project
        resolves to. Being able to say that matters as much as being able to
        choose: an override outlives the state it was made in, so a project
        whose mask was still converting when somebody saved the edit page would
        otherwise be pinned to centroids for good, with no way back.

        An unknown value is ignored rather than stored -- the client sends this
        and the viewer would have nothing to draw.
        """
        if value and value not in CELL_LAYERS:
            return self
        return self.patch(cell_layer_choice=value or None)

    @property
    def image_type(self) -> str:
        """The mode this project is being served in, override or not.

        Read off `image.kind` rather than off the choice, because the choice
        can be "Auto" and because a re-registration is what turns a choice into
        a served mode -- so this is always what the viewer is actually doing,
        which is what a caller asking the question wants to know.
        """
        return IMAGE_TYPE_BRIGHTFIELD if self.image.kind == IMAGE_TYPE_BRIGHTFIELD \
            else IMAGE_TYPE_FLUORESCENCE

    def with_image_type(self, value: str | None) -> "Project":
        """Record an H&E/fluorescence override, or clear one.

        None and "" both mean "no override" -- go back to what the detector
        makes of the file. Being able to say that matters as much as being able
        to choose, for the same reason it does for the cell layer: an override
        outlives the state it was made in.

        Storing the choice does not change what is served; the caller
        re-registers the image, because the two modes disagree about the
        project's channel count and layer list. An unknown value is ignored
        rather than stored.
        """
        if value and value not in IMAGE_TYPES:
            return self
        return self.patch(image=replace(self.image, image_type_choice=value or None))

    # -- what the user has actually answered -----------------------------

    def with_confirmed(self, keys: Iterable[str]) -> "Project":
        """Mark requirement keys as explicitly answered.

        Additive: confirming one thing never un-confirms another, so the modal
        can post only the keys it showed.
        """
        seen = set(self.confirmed)
        added = []
        for key in keys or ():
            # Deduped against the batch as well as against what is stored: the
            # modal's `confirm` list and the answers it carries name the same
            # keys, and both reach here.
            if key and key not in seen:
                seen.add(key)
                added.append(key)
        if not added:
            return self
        return self.patch(confirmed=self.confirmed + tuple(added))

    def unconfirmed(self, keys: Iterable[str]) -> list[str]:
        """Which of `keys` the user has never answered, in the order given."""
        return [key for key in keys if key not in self.confirmed]

    def forget_table_answers(self) -> "Project":
        """Drop the answers that describe the feature table.

        Called when the data file is replaced: the roles and the marker split
        were confirmed against columns that may not exist in the new file, so
        the fresh predictions have to be put in front of the user again.

        The two answers that name no column go with them. "One image" and
        "number the rows" were said about the old table and say nothing about
        the new one -- and left standing they would keep their questions
        answered, so neither would ever be asked about the file that replaced
        it.
        """
        project = self.patch(
            confirmed=tuple(k for k in self.confirmed if not _is_table_scoped(k)))
        if not project.dataset:
            return project
        return project.patch(dataset=replace(
            project.dataset, single_image=False, row_number_ids=False))

    # -- persistence -----------------------------------------------------

    @staticmethod
    def _config_path(data_root=None) -> Path:
        return paths.config_path(data_root)

    @property
    def is_shared(self) -> bool:
        """Whether this project belongs to a root the user cannot write to.

        A shared project is readable by everyone on the machine and editable by
        nobody through Plexora: the site administrator who provisioned the root
        owns it. What a user produces while exploring one -- gates, ROIs,
        figures -- is theirs and goes to their own root instead, which is what
        `paths.project_state_dir` is for.
        """
        return self.home_root is not None and self.home_root != paths.data_root()

    @property
    def read_dir(self) -> Path:
        """Where this project's image, mask and feature table are read from."""
        return paths.project_dir(self.name, self.home_root)

    @property
    def state_dir(self) -> Path:
        """Where this user's own state for this project is written."""
        return paths.project_state_dir(self.name)

    @classmethod
    def load(cls, name: str, data_root=None) -> "Project":
        """Read one project. Raises KeyError if it is not registered.

        Searches every root when `data_root` is not given, the user's own
        first, so a project of the user's own shadows a shared one of the same
        name -- somebody who has made their own copy means to open it.
        """
        for root in ([Path(data_root)] if data_root is not None else paths.roots()):
            config = read_config(paths.config_path(root))
            if name in config:
                return cls.from_entry(name, config[name], home_root=root)
        raise KeyError(f"Unknown datasource: {name!r}")

    @classmethod
    def find(cls, name: str, data_root=None) -> "Project | None":
        """Read one project, or None. For the many callers that already have a
        fallback for an unknown/stale datasource name."""
        try:
            return cls.load(name, data_root)
        except KeyError:
            return None

    @staticmethod
    def load_all(data_root=None) -> dict:
        """Every registered project, as raw entries keyed by name.

        Merged across the roots when `data_root` is not given. Earlier roots
        win, and `paths.roots()` puts the user's own first -- the same
        precedence `load()` applies, kept in step because the two disagreeing
        would mean a name listed from one root opened the other.
        """
        if data_root is not None:
            return read_config(paths.config_path(data_root))
        merged: dict = {}
        for root in paths.roots():
            for name, entry in read_config(paths.config_path(root)).items():
                merged.setdefault(name, entry)
        return merged

    @staticmethod
    def load_roots() -> dict:
        """Which root each visible project came from, keyed by name.

        Same precedence as `load_all`. Lets a caller that already has the
        merged entries tell the user's projects from the shared ones without
        re-reading every config.
        """
        found: dict = {}
        for root in paths.roots():
            for name in read_config(paths.config_path(root)):
                found.setdefault(name, root)
        return found

    @staticmethod
    def root_for(name: str) -> Path | None:
        """The root whose config.json holds `name`, or None if none does."""
        for root in paths.roots():
            if name in read_config(paths.config_path(root)):
                return root
        return None

    @staticmethod
    def config_path_for(name: str) -> Path:
        """The config.json holding `name`'s entry.

        Falls back to the user's own root for a name nobody has registered,
        which is what a caller writing a brand-new entry wants. Callers that
        intend to WRITE must also check `paths.is_writable` on the parent: this
        happily returns a path inside a read-only shared root, because that is
        genuinely where the entry lives.
        """
        root = Project.root_for(name)
        return paths.config_path(root if root is not None else paths.data_root())

    def _write_root(self, data_root=None) -> Path:
        """The root this project's entry is written to.

        An explicit argument wins, then the root it was loaded from, then the
        user's own. A shared project resolves to the shared root and the write
        will fail there -- deliberately, since silently redirecting it into the
        user's root would fork the registry entry and leave two projects with
        one name. Callers that can do something better refuse earlier, on
        `is_shared`.
        """
        if data_root is not None:
            return Path(data_root)
        return self.home_root if self.home_root is not None else paths.data_root()

    def save(self, data_root=None) -> dict:
        """Write this project back, leaving every other project untouched.

        Only the target root's own config.json is read and rewritten -- never
        the merged view, which would copy every shared project into the user's
        registry the first time they saved anything.
        """
        root = self._write_root(data_root)
        path = paths.config_path(root)
        with _CONFIG_LOCK:
            config = read_config(path)
            config[self.name] = self.to_entry()
            write_config(path, config)
            return config[self.name]

    @classmethod
    def mutate(cls, name: str, change, data_root=None) -> "Project | None":
        """Read-modify-write one project atomically with respect to other
        writers in this process.

        `change` takes a Project and returns one. Returns None if the project
        vanished before the lock was taken -- which really happens: the
        segmentation job outlives a delete.
        """
        with _CONFIG_LOCK:
            project = cls.find(name, data_root)
            if project is None:
                return None
            updated = change(project)
            updated.save(data_root)
            return updated

    def delete(self, data_root=None) -> None:
        """Remove this project from the registry. Does not touch its data
        directory -- the caller owns that, since it may want the files kept."""
        root = self._write_root(data_root)
        path = paths.config_path(root)
        with _CONFIG_LOCK:
            config = read_config(path)
            config.pop(self.name, None)
            write_config(path, config)


def all_projects(data_root=None) -> list[Project]:
    if data_root is not None:
        return [Project.from_entry(name, entry, home_root=data_root)
                for name, entry in Project.load_all(data_root).items()]
    homes = Project.load_roots()
    return [Project.from_entry(name, entry, home_root=homes.get(name))
            for name, entry in Project.load_all().items()]
