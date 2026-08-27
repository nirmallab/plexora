"""The dataset a plugin is handed.

Every plugin receives image data. It may additionally receive segmentation and
a feature table (CSV, AnnData or SpatialData), plus metadata naming which
columns hold the cell id, image id, coordinates and cell type, and which
columns are markers rather than measurements.

Three rules make this contract durable:

**Plugins read roles, never column names.** `schema.x` resolves to whatever the
project recorded for the `x` role. A plugin that hardcodes `"X_centroid"`
breaks on the next dataset; one that reads `schema.x` does not.

**Plugins never touch data_model directly.** This module does, and it is core
code, so it is free to. That inversion is the whole point: `data_model` holds
mutable module-level globals mutated under a load lock, with two confusingly
adjacent loaders -- `_ensure_loaded()` warms the feature table/BallTree while
`ensure_loaded()` warms the image pyramid and returns load_generation. Handing
that surface to third parties would freeze it forever and invite the exact race
its own comments warn about. Handles below call the right one and expose
neither.

**Plugins never read the raw config entry.** They get `Project`
(server/models/project.py), which is typed and has one definition of every
field. The handles here are the read-only slice of it a plugin needs; anything
missing from them is a gap to fill here rather than to route around, since a
plugin that learns the on-disk shape freezes that shape forever.

A role a project has not collected yet resolves to None. That is not an error
state -- it is what a plugin declares in `Requires` so the host can ask for it
(see plexora/api/plugin.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping

from plexora.server.models import data_model
from plexora.server.models.adapters import MetadataColumn
from plexora.server.models.project import ROLE_NAMES, Project
from plexora.server.providers.base import LOCAL, NODE, ResourceLocator, ResourceNotLocal


def _locator(project: Project, kind: str, path: str | None) -> ResourceLocator:
    """Where one of a project's resources is, as a plugin sees it.

    Reads the project record only -- `resources` is empty for every ordinary
    project, so this is a dict lookup that misses and a two-field object. It is
    deliberately not `providers.resolve_providers`: that opens nothing either,
    but it constructs live providers, and a plugin asking "where is this" must
    not be the thing that decides a node is unreachable.
    """
    binding = project.resources.get(kind)
    if binding is None:
        return ResourceLocator(kind=kind, provider=LOCAL, path=path or None)
    return ResourceLocator(kind=kind, provider=NODE, node=binding.node,
                           resource_id=binding.resource_id)


@dataclass(frozen=True)
class DatasetSchema:
    """Role -> column name, as the project recorded it.

    Cheap and load-free: this reads the project record only, never the table.
    Marker discovery is a property of the data and lives on TableHandle.markers.
    """

    cell_id: str | None = None
    x: str | None = None
    y: str | None = None
    celltype: str | None = None
    image_id: str | None = None
    #: Roles added to the record later land here rather than forcing a
    #: dataclass change that would break every plugin's constructor call.
    extra: Mapping[str, str] = field(default_factory=dict)

    #: The roles that are proper fields above. Anything in ROLE_NAMES but not
    #: here goes to `extra`.
    _FIELDS: ClassVar[tuple] = ("cell_id", "x", "y", "celltype", "image_id")

    @classmethod
    def from_project(cls, project: Project) -> "DatasetSchema | None":
        """None when the project has no feature table -- there are no columns
        for a role to name."""
        if not project.has_table:
            return None
        roles = project.roles
        extra = {
            role: roles.get(role)
            for role in ROLE_NAMES
            if role not in cls._FIELDS and roles.get(role)
        }
        return cls(**{role: roles.get(role) for role in cls._FIELDS}, extra=extra)

    def get(self, role: str) -> str | None:
        """A role by name, including ones that only exist in `extra`."""
        if role in self._FIELDS:
            return getattr(self, role)
        return self.extra.get(role)


@dataclass(frozen=True)
class ImageSource:
    """Where the image physically is, for the rare plugin that has to open the
    file itself rather than ask the viewer for tiles.

    The counterpart of `TableSource`, and it exists for the same kind of reason.
    Figure Builder re-renders a captured panel at publication resolution, which
    means reading a rectangle of source pixels at a chosen pyramid level --
    something no amount of tile-serving API can express, since the tile routes
    answer in the viewer's own quantised, screen-sized terms.

    Exposed as a typed view rather than by handing over the config entry, so the
    on-disk shape stays core's business. Opening it is the caller's job: doing
    that here would drag tifffile and zarr into every plugin that merely asks
    how big the image is.

    **`path` is empty when the image is on a data node.** That is the contract,
    not an oversight: there is no file at that path on this machine, and a
    plugin that opened it would find nothing and report a broken project. Check
    `locator.is_local` before reaching for it, or ask `ImageHandle` to do the
    read -- `read_region` and `render_panel` exist precisely so a plugin never
    has to care which machine the pixels are on.
    """

    path: str
    kind: str
    #: Pyramid levels the file holds, level 0 being full resolution.
    levels: int | None = None
    size: tuple[int | None, int | None] = (None, None)
    #: Where this image actually is. Local for every ordinary project.
    locator: ResourceLocator = field(
        default_factory=lambda: ResourceLocator(kind="image"))

    @property
    def is_local(self) -> bool:
        return self.locator.is_local


class ImageHandle:
    """The one input every plugin is guaranteed."""

    def __init__(self, project: Project):
        self._project = project

    @property
    def locator(self) -> ResourceLocator:
        """Where this project's image is served from."""
        return _locator(self._project, "image", self._project.image.src)

    @property
    def source(self) -> ImageSource | None:
        """The image file itself. None for a project with no image at all.

        A node-backed image still returns a source -- the channels, the size
        and the pyramid depth are recorded centrally and are true wherever the
        file sits -- but its `path` is empty. See `ImageSource`.
        """
        locator = self.locator
        spec = self._project.image
        if not spec.src and locator.is_local:
            return None
        return ImageSource(
            path=spec.src if locator.is_local else "",
            kind=spec.kind,
            levels=spec.max_level,
            size=(spec.width, spec.height),
            locator=locator,
        )

    @property
    def channels(self) -> list[dict]:
        """Real image channels, excluding the 'Area' placeholder that only
        exists when segmentation was registered."""
        return list(self._project.image.real_channels)

    @property
    def channel_names(self) -> list[str]:
        return self._project.image.channel_names

    @property
    def kind(self) -> str | None:
        return self._project.image.kind

    @property
    def size(self) -> tuple[int | None, int | None]:
        return self._project.image.width, self._project.image.height

    @property
    def max_level(self) -> int | None:
        return self._project.image.max_level

    @property
    def tile_size(self) -> tuple[int | None, int | None]:
        return self._project.image.tile_width, self._project.image.tile_height

    def stats(self, channel: str) -> dict:
        """Per-channel intensity statistics, including the vmin/vmax hints the
        viewer uses for immediate display before the full GMM fit lands."""
        return data_model.get_image_channel_stats(channel, self._project.name)

    def quantization_window(self, channel: str) -> tuple:
        """(qmin, qmax) from FULL-RESOLUTION data. Deliberately split from the
        GMM fit so callers that only need the byte-domain window do not pay the
        ~1 s GaussianMixture cost."""
        return data_model.get_channel_quantization_window(channel, self._project.name)

    @property
    def is_local(self) -> bool:
        """Whether the image file is on this machine.

        Worth asking before opening `source.path`: for a node-backed image
        there is no file at any path here, and `read_region` is how the pixels
        are reached instead.
        """
        return self.locator.is_local

    def geometry(self) -> dict:
        """The pyramid's shape, including each level's own dimensions.

        Only meaningful for a node-backed image, where a caller cannot open the
        file to find out. A local caller has the file and should read it -- the
        answer is the same and does not cost a request.
        """
        provider = self._provider()
        if provider is None:
            raise ResourceNotLocal(
                "this image is on this machine; open it rather than asking for "
                "its geometry over an API")
        return provider.geometry()

    def read_region(self, level, box, channel_indices, max_pixels=0):
        """(pixels, clipped_box) for a rectangle of one or more channels.

        The read no tile API can express: a publication-resolution panel, and
        Quick Edit's preview. `box` is in the chosen LEVEL's pixels, and what
        comes back is the box that actually existed -- a capture running a few
        pixels off the edge of a slide is an ordinary thing to have drawn.

        `channel_indices` are positions in `channels`, not names, because that
        is what a pyramid is addressed by and what the caller resolving a URL
        key already has.
        """
        provider = self._provider()
        if provider is None:
            raise ResourceNotLocal(
                f"the image for {self._project.name!r} is on this machine; open "
                "it directly rather than reading it through this API")
        return provider.read_region(level, box, channel_indices,
                                    max_pixels=max_pixels)

    def _provider(self):
        """The node provider for this image, or None when it is local.

        Built here rather than taken from data_model's `_providers`, because a
        plugin may ask about a project that is not the loaded one -- Figure
        Builder's library spans projects, and its export runs against whichever
        one a panel came from.
        """
        binding = self._project.resources.get("image")
        if binding is None:
            return None
        from plexora.server.providers.node import NodeImageProvider

        return NodeImageProvider(binding).with_channels(
            self._project.image.channel_names,
            self._project.image.tile_width, self._project.image.tile_height)


class SegHandle:
    """Segmentation mask, when the project has one."""

    def __init__(self, project: Project):
        self._project = project

    @property
    def available(self) -> bool:
        return self._project.segmentation.available

    @property
    def pending(self) -> bool:
        """True while the background mask-conversion job is still running."""
        return self._project.segmentation.pending

    def centroid_manifest(self) -> dict:
        return data_model.get_centroid_manifest(self._project.name)

    def centroid_tiles(self, level, tiles, gates=None, max_points=None):
        return data_model.get_centroid_tiles(self._project.name, level, tiles, gates, max_points)


@dataclass(frozen=True)
class TableSource:
    """Where the feature table physically lives, for the rare plugin that has
    to open the file itself rather than read rows through `frame()`.

    Gating needs this: it writes gate thresholds back into the source AnnData's
    `uns`, which no amount of table-reading API can express. Exposed as a typed
    view rather than by handing over the config entry, so the on-disk shape
    stays core's business.

    **`path` is empty when the table is on a data node**, exactly as for
    `ImageSource`. Everything that used to open it is a *table operation* now
    (`TableHandle.run`), which runs where the file is -- and has to, because
    every one of those writes checks the file's row count against the loaded
    frame before touching anything, and that check means nothing if the two are
    on different machines.
    """

    kind: str
    path: str
    table: str | None = None
    subset: Mapping[str, Any] = field(default_factory=dict)
    #: Where this table actually is. Local for every ordinary project.
    locator: ResourceLocator = field(
        default_factory=lambda: ResourceLocator(kind="table"))

    @property
    def is_local(self) -> bool:
        return self.locator.is_local


class TableHandle:
    """The feature table, whatever it was imported from.

    Every method warms the table first, so callers never reason about load
    order or touch data_model's globals.

    `provider` is set only on a data node, where this handle is built over one
    of the several tables that process is serving. data_model's globals name a
    single loaded datasource, which is exactly right for a viewer and means
    nothing on a node -- so a node passes its own loaded provider and every
    read below goes there instead. Plugin code sees one handle either way.
    """

    def __init__(self, project: Project, provider=None):
        self._project = project
        self._provider = provider

    @property
    def available(self) -> bool:
        return self._project.has_table

    @property
    def source_kind(self) -> str:
        """'csv', 'anndata' or 'spatialdata'."""
        return self._project.source_kind or "csv"

    @property
    def locator(self) -> ResourceLocator:
        """Where this project's cell table is served from."""
        spec = self._project.dataset
        return _locator(self._project, "table", spec.src if spec else None)

    @property
    def is_local(self) -> bool:
        """Whether the table's file is on this machine.

        Worth asking before offering a control rather than after pressing it: a
        node that is asleep cannot run an export, and a button that fails is a
        worse answer than one that explains itself.
        """
        return self.locator.is_local

    @property
    def source(self) -> TableSource | None:
        spec = self._project.dataset
        if spec is None:
            return None
        locator = self.locator
        return TableSource(
            kind=spec.type,
            path=spec.src if locator.is_local else "",
            table=spec.table,
            subset=dict(spec.subset),
            locator=locator,
        )

    @property
    def log_transformed(self) -> bool:
        """Whether `frame()` hands back log1p'd values.

        The scale, not a formatting detail. Marker intensities are log-normal,
        so anything that fits a distribution to them has to know which side of
        the transform it is standing on -- fit a mixture to raw counts as if
        they were symmetric and the components land in the wrong places, and
        take the log of values that are already logged and they land in
        different wrong places. Gating's auto-threshold reads this to decide
        which, and gets the same answer out of the same data either way.

        This is the project's recorded answer (the log1p switch beside the
        matrix picker), which is the only thing that knows: nothing about the
        numbers themselves says whether they have been transformed.
        """
        return self._project.log_transformed

    def frame(self):
        """The whole table as a polars DataFrame (None if this project has no
        feature data).

        Only available when the table is on this machine. A node-backed table
        raises `ResourceNotLocal` rather than handing back the reduced copy
        this server keeps -- a frame that is missing every marker column but
        answers `frame["id"]` perfectly well is the shape of bug that passes
        every test and produces a plausible, wrong picture. Ask for what you
        actually need instead: `geometry()` for ids and coordinates,
        `columns()` for named columns, `run()` for anything that has to read
        the file.
        """
        if self._provider is not None:
            return self._provider.frame
        locator = self.locator
        if not locator.is_local:
            raise ResourceNotLocal(
                f"the cell table for {self._project.name!r} lives on node "
                f"{locator.node!r}, so the whole frame is not on this server. "
                "Use geometry(), columns(), metadata_values(), or run() for "
                "work that has to happen where the file is."
            )
        data_model._ensure_loaded(self._project.name)
        return data_model.get_datasource_df()

    def geometry(self):
        """The part of the table this server always has: the cell id, the
        coordinates, and whatever other columns fill a role.

        Identical to `frame()` for an ordinary project -- it is the same object
        -- and for a node-backed table it is the compact copy the primary pulls
        once so the spatial index, the centroid layers and the hover lookup
        never wait on a network round trip. Anything that needs only ids and
        positions should ask for this rather than `frame()`, because that is
        what it means and it works in both topologies.
        """
        if self._provider is not None:
            return self._provider.frame
        data_model._ensure_loaded(self._project.name)
        return data_model.get_datasource_df()

    def run(self, operation: str, payload: Mapping[str, Any] | None = None) -> Any:
        """Run a registered table operation where this table's file is.

        The seam for everything that cannot be expressed as "send me some
        values": the ROI spatial join, writing annotation columns onto the
        cells, writing gate thresholds into `uns`, exporting a CSV of the whole
        table. Locally this is a direct call; for a node-backed table the name
        and the payload go to the node, which runs the identical registered
        function against its own loaded copy and sends the result back.

        Payload and result must both survive `json.dumps` -- see
        `plexora/server/providers/operations.py` for why that constraint is the
        point rather than a limitation.
        """
        binding = self._project.resources.get("table")
        if binding is not None:
            from plexora.server.providers.node import run_node_operation

            return run_node_operation(binding, operation, payload)
        from plexora.server.providers.operations import run_table_operation

        return run_table_operation(
            operation, _dataset_for(self._project, self._provider), payload)

    def stream(self, operation: str, payload: Mapping[str, Any] | None = None):
        """Run a registered streaming table operation, yielding its chunks.

        The same seam as `run`, for the one result that must not be
        materialized: a CSV of the whole table. Locally the generator is
        consumed directly; for a node-backed table the chunks arrive off the
        wire and are forwarded straight through, so the export is never held
        whole in either process.
        """
        binding = self._project.resources.get("table")
        if binding is not None:
            from plexora.server.providers.node import stream_node_operation

            return stream_node_operation(binding, operation, payload)
        from plexora.server.providers.operations import run_table_stream

        return run_table_stream(
            operation, _dataset_for(self._project, self._provider), payload)

    def describe(self) -> dict:
        """Per-column summary stats plus a 50-bin histogram. Cached per
        datasource by data_model."""
        if self._provider is not None:
            return self._provider.describe()
        return data_model.get_datasource_description(self._project.name)

    @property
    def markers(self) -> list[str]:
        """Columns a plugin can meaningfully threshold or plot.

        This is the classification the project recorded at import -- one
        answer, shared by every plugin, so two tools never disagree about
        whether a column is a marker.

        A structural channel like DNA is commonly a real image channel with no
        feature column, so this is NOT the same list as image.channel_names --
        conflating the two is a long-standing source of bugs here.

        The histogram fallback covers a project whose columns were never
        classified: better a usable guess than an empty panel. It costs a
        describe(), which is why it is not the primary path.
        """
        recorded = self._project.columns
        if recorded.classified:
            return list(recorded.markers)
        reserved = {"id"} | {c for c in self._project.roles.to_dict().values() if c}
        description = self.describe()
        return [
            name for name, info in description.items()
            if name not in reserved and info.get("histogram")
        ]

    @property
    def metadata_columns(self) -> list[str]:
        """The non-marker columns: identifiers, coordinates, morphology,
        annotations.

        For a CSV that is the recorded half of the marker/metadata split -- the
        file's columns, minus the ones the user called markers.

        For AnnData and SpatialData it is the file's own `.obs` columns, which
        is NOT the same list as `columns.metadata` for those formats. That field
        holds whatever the loaded table ended up with, and the two registration
        paths disagree about it: the import route stores the obs names there,
        while `register_anndata_datasource` stores the adapter's synthesized
        `id`/`X`/`Y`/`obs_id`. Neither is wrong for its own purpose, and neither
        is what a plugin is asking for -- "which annotations does this project
        have" has one answer, and for these formats it is obs. Reported through
        the same preference `Project.role_columns` already uses, so the list a
        role is chosen from and the list an annotation is chosen from cannot
        drift apart.
        """
        spec = self._project.dataset
        if spec is not None and spec.obs_columns:
            return [str(column) for column in spec.obs_columns]
        return list(self._project.columns.metadata)

    def metadata_values(self, column: str) -> MetadataColumn:
        """One metadata column's values, aligned row-for-row with `frame()`.

        The format-agnostic way to read an annotation. `metadata_columns` names
        what a project has; this is how a plugin gets the values, without
        needing to know that a CSV keeps them in the loaded frame while AnnData
        and SpatialData keep them in an `.obs` the frame never materialized.
        Alignment is core's problem, not the caller's: the same subset that
        built the table is applied here.

        Deliberately not `frame()[column]`. That works for a CSV and returns
        nothing at all for the two structural formats, which is the shape of bug
        that passes every test written against sample CSVs.

        Raises KeyError if this project has no such column.
        """
        if self._provider is not None:
            return self._provider.metadata_column(column)
        return data_model.get_metadata_column(self._project.name, column)

    def columns(self, names) -> dict:
        """Numeric numpy views of the named columns, cached one set at a time
        so repeated range queries reuse the same arrays."""
        if self._provider is not None:
            return self._provider.filter_columns(list(names))
        data_model._ensure_loaded(self._project.name)
        return data_model.get_filter_columns(self._project.name, list(names))

    def range_mask(self, gates: Mapping[str, tuple], mode: str = "and"):
        """Boolean mask over rows for {column: (low, high)} ranges. `mode` is
        'and' (every gate must match) or 'or' (any)."""
        return data_model.apply_range_mask(self.columns(gates.keys()), gates, mode)

    def ids_matching(self, gates: Mapping[str, tuple], mode: str = "and") -> list:
        """Cell ids whose rows satisfy the gates, in table order."""
        frame = self.geometry()
        if frame is None or not gates:
            return []
        return frame["id"].to_numpy()[self.range_mask(gates, mode)].tolist()


@dataclass(frozen=True)
class Dataset:
    """Everything the host offers a plugin about one project."""

    name: str
    image: ImageHandle
    segmentation: SegHandle
    table: TableHandle
    schema: DatasetSchema | None
    project: Project

    @property
    def source_kind(self) -> str | None:
        return self.table.source_kind if self.table.available else None

    def cached(self, key, compute):
        """Memoize an expensive derived value against this datasource.

        Entries are dropped when the datasource reloads, so a plugin cannot
        serve a result derived from data that has since changed underneath it.
        Intended for genuinely costly work -- a mixture-model fit, a spatial
        index -- not for ordinary lookups.

        `key` is namespaced per datasource here, so plugins do not have to
        remember to include the project name and cannot collide across
        projects.
        """
        return data_model.gmm_cache_get_or_set((self.name, key), compute)


def _dataset_for(project: Project, table_provider=None) -> Dataset:
    """The handle set for a project record already in hand.

    Split out of `dataset()` so a caller holding a `Project` -- a handle
    dispatching a table operation, a node answering one -- does not re-read
    config.json to get back something it already has.

    `table_provider` is a node's own loaded table. See `TableHandle`: it is the
    one thing a node cannot get from data_model, because data_model describes a
    single loaded datasource and a node serves several.
    """
    return Dataset(
        name=project.name,
        image=ImageHandle(project),
        segmentation=SegHandle(project),
        table=TableHandle(project, provider=table_provider),
        schema=DatasetSchema.from_project(project),
        project=project,
    )


def dataset(name: str) -> Dataset:
    """Build the handle set for a datasource. Raises KeyError if unknown.

    Construction is cheap -- it reads the project record only. Nothing is
    loaded from disk until a handle method is actually called.
    """
    return _dataset_for(Project.load(name))
