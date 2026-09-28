"""The project data a plugin is handed.

Every plugin receives image data. It may additionally receive segmentation and
a feature table (CSV, Parquet, AnnData or SpatialData), plus metadata naming which
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
    """Which column holds each cell-level role, as the project recorded it.

    A plugin should never hardcode a column name such as `"X_centroid"` --
    that column may not exist, or may be named differently, in the next
    dataset. Read the role off the schema instead (`schema.x`, `schema.y`,
    `schema.cell_id`, `schema.celltype`, `schema.image_id`) and it resolves to
    whatever column the project actually recorded for that role, or `None`
    when the project has not recorded one.

    Building one is cheap: it reads the project record only, never the
    feature table. Which columns are markers is a different question, answered
    from the data itself by `TableHandle.markers`, not by this schema.
    """

    #: Column holding each cell's unique id, or None if not recorded.
    cell_id: str | None = None
    #: Column holding each cell's x coordinate, or None if not recorded.
    x: str | None = None
    #: Column holding each cell's y coordinate, or None if not recorded.
    y: str | None = None
    #: Column holding each cell's type/phenotype label, or None if not recorded.
    celltype: str | None = None
    #: Column holding the source image id, for a table pooled across several
    #: images, or None if not recorded.
    image_id: str | None = None
    # Roles the project schema gains after this dataclass was written land in
    # `extra` rather than forcing a constructor change that would break every
    # plugin's `DatasetSchema(...)` call already in the wild.
    #: Roles beyond the five above, keyed by role name. Look one up with
    #: `get()` rather than indexing this mapping directly.
    extra: Mapping[str, str] = field(default_factory=dict)

    #: The roles that are proper fields above. Anything in ROLE_NAMES but not
    #: here goes to `extra`.
    _FIELDS: ClassVar[tuple] = ("cell_id", "x", "y", "celltype", "image_id")

    @classmethod
    def from_project(cls, project: Project) -> "DatasetSchema | None":
        """Build the schema recorded on a project.

        Args:
            project (Project): The project record to read roles from.

        Returns:
            DatasetSchema | None: None when the project has no feature table,
            since there are then no columns for a role to name.
        """
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
        """Look up a role by name, including one that only exists in `extra`.

        Args:
            role (str): The role name, e.g. `"x"`, or a project-specific role
                recorded after this dataclass was written.

        Returns:
            str | None: The column name for that role, or None if the
            project never recorded one.
        """
        if role in self._FIELDS:
            return getattr(self, role)
        return self.extra.get(role)


@dataclass(frozen=True)
class ImageSource:
    """Where the image file physically lives, for a plugin that opens it directly.

    Most plugins never need this: the viewer already serves tiles, and
    `ImageHandle.read_region` reads a rectangle of pixels without the caller
    having to know where the file is. `ImageSource` is for the rare plugin
    that has to open the file itself -- Figure Builder re-renders a captured
    panel at publication resolution, which means reading a rectangle of
    source pixels at a chosen pyramid level, something no tile API expresses
    since tile routes answer in the viewer's own quantised, screen-sized
    terms.

    **`path` is empty when the image is on a data node**, since there is then
    no file at that path on this machine. Check `locator.is_local` before
    opening `path`, or call `ImageHandle.read_region` instead -- it reads the
    pixels correctly wherever the file physically is.
    """

    # Exposed as a typed view rather than by handing over the raw config
    # entry, so the on-disk shape stays core's business. Opening the file is
    # the caller's job: doing it here would pull tifffile and zarr into every
    # plugin that merely asks how big the image is. Counterpart of
    # `TableSource`, for the same reason.
    #: Path to the image file, or `""` when the image is on a data node.
    path: str
    #: The image format, e.g. `"ome_tiff"`, `"svs"`.
    kind: str
    #: Pyramid levels the file holds, level 0 being full resolution.
    levels: int | None = None
    #: `(width, height)` in full-resolution pixels.
    size: tuple[int | None, int | None] = (None, None)
    #: Where this image actually is. Local for every ordinary project.
    locator: ResourceLocator = field(
        default_factory=lambda: ResourceLocator(kind="image"))

    @property
    def is_local(self) -> bool:
        """Whether the image file is on this machine.

        Returns:
            bool: True when `path` names a real, readable file; False when
            the image is on a data node and `path` is empty.
        """
        return self.locator.is_local


class ImageHandle:
    """The project's image: channels, size, pyramid depth, and pixel access.

    Every project has one -- `ProjectData.image` -- the one input every
    plugin is guaranteed, even a "blank" project with no image file, where
    the size and channel list are simply empty. Built over the project
    record already in hand; nothing is read from disk until a method below
    is called.

    Args:
        project (Project): The project record this handle wraps. A plugin
            gets one from `ProjectData.image`, e.g.
            `api.project_data(name).image`, rather than constructing this
            directly.
    """

    def __init__(self, project: Project):
        self._project = project

    @property
    def locator(self) -> ResourceLocator:
        """Where this project's image is served from.

        Returns:
            ResourceLocator: The image's location -- local, or on a data node.
        """
        return _locator(self._project, "image", self._project.image.src)

    @property
    def source(self) -> ImageSource | None:
        """The image file's own path, format and size.

        Returns:
            ImageSource | None: None for a project with no image at all. A
            node-backed image still returns a source -- the channels, the
            size and the pyramid depth are recorded centrally and are true
            wherever the file sits -- but its `path` is empty; see
            `ImageSource`.
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
        """Real image channels, excluding the synthetic 'Area' channel a
        project only gets once it has a registered segmentation mask.

        Returns:
            list[dict]: One dict per channel, each with `name`, `fullname`
            and `src` keys.
        """
        return list(self._project.image.real_channels)

    @property
    def channel_names(self) -> list[str]:
        """Channel names in display order.

        Returns:
            list[str]: One name per channel.
        """
        return self._project.image.channel_names

    @property
    def kind(self) -> str | None:
        """The image format, e.g. `"ome_tiff"`, `"svs"`.

        Returns:
            str | None: The format name, or None for a project with no
            image.
        """
        return self._project.image.kind

    @property
    def size(self) -> tuple[int | None, int | None]:
        """Full-resolution image size.

        Returns:
            tuple[int | None, int | None]: `(width, height)` in pixels.
        """
        return self._project.image.width, self._project.image.height

    @property
    def max_level(self) -> int | None:
        """The deepest pyramid level, 0 being full resolution.

        Returns:
            int | None: The highest valid `level` for `read_region`.
        """
        return self._project.image.max_level

    @property
    def tile_size(self) -> tuple[int | None, int | None]:
        """The pyramid's tile size.

        Returns:
            tuple[int | None, int | None]: `(tile_width, tile_height)` in
            pixels.
        """
        return self._project.image.tile_width, self._project.image.tile_height

    def stats(self, channel: str) -> dict:
        """Per-channel intensity statistics.

        Args:
            channel (str): A channel name from `channel_names`.

        Returns:
            dict: Includes the `vmin`/`vmax` display hints the viewer shows
            immediately, before its full mixture-model fit lands.
        """
        return data_model.get_image_channel_stats(channel, self._project.name)

    def quantization_window(self, channel: str) -> tuple:
        """The byte-domain intensity window for one channel.

        Args:
            channel (str): A channel name from `channel_names`.

        Returns:
            tuple: `(qmin, qmax)` computed from full-resolution data.
        """
        # Deliberately split from the full Gaussian-mixture fit (see `stats`)
        # so a caller that only needs the byte-domain window does not pay
        # that fit's cost, around one second.
        return data_model.get_channel_quantization_window(channel, self._project.name)

    @property
    def is_local(self) -> bool:
        """Whether the image file is on this machine.

        Worth asking before opening `source.path`: for a node-backed image
        there is no file at any path here, and `read_region` is how the
        pixels are reached instead.

        Returns:
            bool: True when the image is a local file.
        """
        return self.locator.is_local

    def geometry(self) -> dict:
        """The pyramid's shape: each level's own width and height.

        Only useful for a node-backed image, where a caller cannot open the
        file to find out directly. For a local image, open the file instead --
        the answer is the same and does not cost a request.

        Returns:
            dict: The pyramid geometry.

        Raises:
            ResourceNotLocal: If the image is a local file; open it directly
                instead of asking for its geometry.
        """
        provider = self._provider()
        if provider is None:
            raise ResourceNotLocal(
                "this image is on this machine; open it rather than asking for "
                "its geometry over an API")
        return provider.geometry()

    def read_region(self, level, box, channel_indices, max_pixels=0):
        """Read a rectangle of pixels from one or more channels.

        Works whether the image is local or on a data node, which is the
        point of it: a publication-resolution panel and Quick Edit's preview
        both read through here rather than through any tile API.

        Args:
            level (int): The pyramid level to read at, 0 being full resolution.
            box (tuple): `(x0, y0, x1, y1)` in that level's own pixel
                coordinates.
            channel_indices (list[int]): Positions in `channels`, not channel
                names -- the same indexing a pyramid tile is addressed by.
            max_pixels (int, optional): Downsample so the returned array has
                at most this many pixels. 0 (the default) means no limit.

        Returns:
            tuple: `(pixels, clipped_box)` -- the array read, and the box
            that actually existed. A capture running a few pixels past the
            edge of a slide is ordinary, so the returned box may be smaller
            than `box`.

        Raises:
            ResourceNotLocal: If the image is a local file; open it directly
                instead of reading it through this API.
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
    """The project's segmentation mask, when it has one.

    Args:
        project (Project): The project record this handle wraps. A plugin
            gets one from `ProjectData.segmentation`, not by constructing
            this directly.
    """

    def __init__(self, project: Project):
        self._project = project

    @property
    def available(self) -> bool:
        """Whether this project has a segmentation mask at all.

        Returns:
            bool: True once a mask has been registered.
        """
        return self._project.segmentation.available

    @property
    def pending(self) -> bool:
        """Whether the background mask-conversion job is still running.

        Returns:
            bool: True while the mask is being converted into tiles.
        """
        return self._project.segmentation.pending

    @property
    def locator(self) -> ResourceLocator:
        """Where this project's segmentation mask is served from.

        Returns:
            ResourceLocator: The mask's location -- local, or on a data node.
        """
        return _locator(self._project, "segmentation", self._project.segmentation.derived)

    def provider(self):
        """The object that reads this mask's labels.

        Constructs the provider without opening anything: a node's provider
        when the mask is on one, otherwise the local file's.

        Returns:
            An object with a `read_region(level, box, max_pixels=0)` method
            -- the same call `read_region` below already makes for you.
        """
        binding = self._project.resources.get("segmentation")
        if binding is not None:
            from plexora.server.providers.node import NodeSegmentationProvider

            return NodeSegmentationProvider(binding).with_tile_size(
                self._project.image.tile_width or 1024,
                self._project.image.tile_height or 1024)
        from plexora.server.providers.local import LocalSegmentationProvider

        return LocalSegmentationProvider(self._project.segmentation.derived)

    def read_region(self, level, box, max_pixels=0):
        """Read cell-label pixels for a rectangle of the mask.

        Reads wherever the mask physically is -- local file or data node --
        without the caller having to choose.

        Args:
            level (int): The pyramid level to read at, 0 being full resolution.
            box (tuple): `(x0, y0, x1, y1)` in that level's own pixel
                coordinates.
            max_pixels (int, optional): Downsample so the returned array has
                at most this many pixels. 0 (the default) means no limit.

        Returns:
            An array of uint32 cell-label ids, one per pixel of the box that
            actually existed; 0 where the box runs off the mask.
        """
        return self.provider().read_region(level, box, max_pixels=max_pixels)

    def centroid_manifest(self) -> dict:
        """The cell-centroid tile cache's manifest, building it if needed.

        Returns:
            dict: `{"status": "missing"}` when this project has no feature
            table or no resolved x/y role, since there are then no positions
            to place. Otherwise the manifest, with `"status": "ready"` and
            the tile grid it describes.
        """
        return data_model.get_centroid_manifest(self._project.name)

    def centroid_tiles(self, level, tiles, gates=None, max_points=None):
        """Cell centroids for a set of tiles, optionally gated.

        Args:
            level (int): The centroid cache's pyramid level.
            tiles (list[dict]): Which tiles to read, each `{"x": ..., "y": ...}`
                in that level's tile grid, as named by `centroid_manifest()`.
            gates (dict, optional): `{column: (low, high)}` ranges; only cells
                whose rows satisfy every range are returned. None (the
                default) returns every cell in the requested tiles.
            max_points (int, optional): Thin each tile down to at most this
                many points. None (the default) returns every matching point.

        Returns:
            A numpy structured array with `id`, `x` and `y` fields, one row
            per centroid.
        """
        return data_model.get_centroid_tiles(self._project.name, level, tiles, gates, max_points)


@dataclass(frozen=True)
class TableSource:
    """Where the feature table file physically lives, for a plugin that opens
    it directly rather than reading rows through `TableHandle.frame()`.

    Gating needs this: it writes gate thresholds back into the source
    AnnData's `uns`, which no table-reading API expresses. Exposed as a typed
    view rather than by handing over the raw config entry, so the on-disk
    shape stays core's business.

    **`path` is empty when the table is on a data node**, exactly as for
    `ImageSource`. Use `TableHandle.run()` for work that has to open the
    file: it runs a registered operation wherever the file physically is,
    which matters because such an operation typically checks the file's row
    count against the loaded frame before writing to it, and that check means
    nothing if the two are on different machines.
    """

    #: The table's format: `"csv"`, `"parquet"`, `"anndata"` or `"spatialdata"`.
    kind: str
    #: Path to the table file, or `""` when the table is on a data node.
    path: str
    #: Which table inside the store, for a SpatialData `.zarr` that holds
    #: more than one, or None otherwise.
    table: str | None = None
    #: The AnnData/SpatialData read spec's subset selection, opaque here
    #: since only the format adapters interpret its shape. Empty for a CSV
    #: or Parquet table, which is read whole.
    subset: Mapping[str, Any] = field(default_factory=dict)
    #: Where this table actually is. Local for every ordinary project.
    locator: ResourceLocator = field(
        default_factory=lambda: ResourceLocator(kind="table"))

    @property
    def is_local(self) -> bool:
        """Whether the table's file is on this machine.

        Returns:
            bool: True when `path` names a real, readable file; False when
            the table is on a data node and `path` is empty.
        """
        return self.locator.is_local


class TableHandle:
    """The feature table, whatever format it was imported from.

    Every method warms the table first, so a caller never has to reason
    about load order or touch `data_model`'s globals directly.

    Args:
        project (Project): The project record this handle wraps. A plugin
            gets one from `ProjectData.table`, e.g.
            `api.project_data(name).table`, rather than constructing this
            directly.
        provider (optional): A data node's own loaded table. Set only when
            this handle is built on a node serving several tables at once; a
            plugin building its own handle never passes this.

    Example:
        ```python
        from plexora import api

        data = api.project_data("my_project")
        if data.table.available:
            markers = data.table.markers
            geometry = data.table.geometry()
        ```
    """

    def __init__(self, project: Project, provider=None):
        self._project = project
        self._provider = provider

    @property
    def available(self) -> bool:
        """Whether this project has a feature table at all.

        Returns:
            bool: True once a table has been registered.
        """
        return self._project.has_table

    @property
    def source_kind(self) -> str:
        """The feature table's format.

        Returns:
            str: One of `"csv"`, `"parquet"`, `"anndata"` or `"spatialdata"`.
        """
        return self._project.source_kind or "csv"

    @property
    def locator(self) -> ResourceLocator:
        """Where this project's feature table is served from.

        Returns:
            ResourceLocator: The table's location -- local, or on a data node.
        """
        spec = self._project.dataset
        return _locator(self._project, "table", spec.src if spec else None)

    @property
    def is_local(self) -> bool:
        """Whether the table's file is on this machine.

        Worth asking before offering a control rather than after pressing it:
        a node that is asleep cannot run an export, and a button that fails
        is a worse answer than one that explains itself.

        Returns:
            bool: True when the table is a local file.
        """
        return self.locator.is_local

    @property
    def source(self) -> TableSource | None:
        """The feature table file's own path, format and read spec.

        Returns:
            TableSource | None: None for a project with no feature table.
        """
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
        """Whether `frame()` hands back log1p'd marker values.

        Marker intensities are log-normal, so anything that fits a
        distribution to them -- gating's auto-threshold, for instance -- has
        to know which side of the transform it is standing on. This is the
        project's recorded answer (the log1p switch beside the matrix
        picker); nothing about the numbers themselves says whether they have
        already been transformed.

        Returns:
            bool: True when the values `frame()` returns are already log1p'd.
        """
        # Fit a mixture to raw counts as if they were symmetric and the
        # components land in the wrong places; take the log of values that
        # are already logged and they land in different wrong places. This
        # flag is how a caller avoids both.
        return self._project.log_transformed

    @property
    def expression_fingerprint(self) -> str:
        """A short string identifying which values `frame()` currently returns.

        Returns:
            str: Which matrix is read and whether it is log1p'd, combined
            into one string. Useful as part of a cache key for anything
            derived from the table's values, so a cached result is
            invalidated when the matrix or the transform changes.
        """
        return f"{self._project.feature_source}|{int(bool(self._project.log_transformed))}"

    def frame(self):
        """The whole feature table as a polars DataFrame.

        Returns:
            polars.DataFrame | None: None if this project has no feature
            table.

        Raises:
            ResourceNotLocal: If the table is on a data node. Only a reduced
                copy is kept on this server (see `geometry()`), and returning
                that here as if it were the whole frame would be missing
                every marker column while still answering `frame["id"]`
                correctly -- a shape of bug that passes every test written
                against a local table. Use `geometry()` for ids and
                coordinates, `columns()` for named columns, or `run()` for
                work that must read the file.
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
        """The cell id, the coordinates, and whatever other columns fill a
        role -- the part of the table this server always has.

        Identical to `frame()` for an ordinary project, since it is the same
        object. For a table on a data node it is the compact copy the
        primary server keeps, so the spatial index, the centroid layers and
        the hover lookup never wait on a network round trip. Prefer this
        over `frame()` whenever only ids and positions are needed: it works
        the same way regardless of where the table physically is.

        Returns:
            polars.DataFrame | None: None if this project has no feature
            table.
        """
        if self._provider is not None:
            return self._provider.frame
        data_model._ensure_loaded(self._project.name)
        return data_model.get_datasource_df()

    def run(self, operation: str, payload: Mapping[str, Any] | None = None) -> Any:
        """Run a registered table operation wherever this table's file is.

        The way to do anything that cannot be expressed as "send me some
        values": a spatial join with an ROI, writing an annotation column
        onto the cells, writing gate thresholds into an AnnData's `uns`,
        exporting the whole table as CSV. Locally this calls the operation
        directly; for a table on a data node the name and payload are sent to
        the node, which runs the same registered function against its own
        loaded copy and sends the result back.

        Args:
            operation (str): The registered operation's name. See
                `plexora.api.table_operation`.
            payload (dict, optional): Arguments for the operation.

        Returns:
            Whatever the operation returns.
        """
        # Payload and result must both survive `json.dumps` -- see
        # `plexora/server/providers/operations.py` for why that constraint is
        # the point rather than a limitation.
        binding = self._project.resources.get("table")
        if binding is not None:
            from plexora.server.providers.node import run_node_operation

            return run_node_operation(binding, operation, payload)
        from plexora.server.providers.operations import run_table_operation

        return run_table_operation(
            operation, _project_data_for(self._project, self._provider), payload)

    def stream(self, operation: str, payload: Mapping[str, Any] | None = None):
        """Run a registered streaming table operation, without materializing
        its whole result at once.

        The same mechanism as `run`, for a result too large to build in
        memory -- exporting the whole table as CSV is the current use.
        Locally the generator is consumed directly; for a table on a data
        node the chunks arrive off the wire and are forwarded straight
        through, so the export is never held whole in either process.

        Args:
            operation (str): The registered streaming operation's name. See
                `plexora.api.table_stream`.
            payload (dict, optional): Arguments for the operation.

        Returns:
            An iterator over the operation's result, in chunks.
        """
        binding = self._project.resources.get("table")
        if binding is not None:
            from plexora.server.providers.node import stream_node_operation

            return stream_node_operation(binding, operation, payload)
        from plexora.server.providers.operations import run_table_stream

        return run_table_stream(
            operation, _project_data_for(self._project, self._provider), payload)

    def describe(self) -> dict:
        """Per-column summary statistics and a histogram.

        Returns:
            dict: One entry per column, each with summary statistics and a
            50-bin histogram. Cached per datasource.
        """
        if self._provider is not None:
            return self._provider.describe()
        return data_model.get_datasource_description(self._project.name)

    @property
    def markers(self) -> list[str]:
        """Columns a plugin can meaningfully threshold or plot.

        This is the classification the project recorded at import -- one
        answer, shared by every plugin, so two tools never disagree about
        whether a column is a marker. NOT the same list as
        `ImageHandle.channel_names`: a structural channel like DNA is
        commonly a real image channel with no matching feature column.

        Returns:
            list[str]: Column names. Falls back to a histogram-based guess
            for a project whose columns were never classified -- costs a
            `describe()` call, which is why it is not the primary path, but
            gives a usable answer instead of an empty panel.
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

        For a CSV or Parquet table this is the recorded half of the
        marker/metadata split: the file's columns, minus the ones classified
        as markers. For AnnData and SpatialData it is the file's own `.obs`
        column names.

        Returns:
            list[str]: Column names.
        """
        # For AnnData/SpatialData this is NOT the same list as
        # `Project.dataset.columns.metadata`: that field holds whatever the
        # loaded table ended up with, and the two registration paths disagree
        # about it -- the import route stores the obs names there, while
        # `register_anndata_datasource` stores the adapter's synthesized
        # `id`/`X`/`Y`/`obs_id`. Neither is wrong for its own purpose, and
        # neither is what a plugin is asking for: "which annotations does
        # this project have" has one answer, and for these formats it is obs.
        # Reported through the same preference `Project.role_columns` already
        # uses, so the list an annotation is chosen from here and the list a
        # role is chosen from cannot drift apart.
        spec = self._project.dataset
        if spec is not None and spec.obs_columns:
            return [str(column) for column in spec.obs_columns]
        return list(self._project.columns.metadata)

    def metadata_values(self, column: str) -> MetadataColumn:
        """One metadata column's values, aligned row-for-row with `frame()`.

        The format-agnostic way to read an annotation: `metadata_columns`
        names what a project has, this is how a plugin gets the values,
        without needing to know that a CSV keeps them in the loaded frame
        while AnnData and SpatialData keep them in an `.obs` the frame never
        materializes. The same row subset that built the table is applied
        here, so alignment is never the caller's problem.

        Args:
            column (str): A column name from `metadata_columns`.

        Returns:
            MetadataColumn: The column's values.

        Raises:
            KeyError: If this project has no such column.
        """
        # Deliberately not `frame()[column]`: that works for a CSV and
        # returns nothing at all for the two structural formats, which is
        # the shape of bug that passes every test written against sample
        # CSVs.
        if self._provider is not None:
            return self._provider.metadata_column(column)
        return data_model.get_metadata_column(self._project.name, column)

    def columns(self, names) -> dict:
        """Numeric numpy views of the named columns.

        Args:
            names (Iterable[str]): Column names to read.

        Returns:
            dict: `{name: numpy array}`. Cached one set of names at a time,
            so repeated range queries over the same columns reuse the same
            arrays.
        """
        if self._provider is not None:
            return self._provider.filter_columns(list(names))
        data_model._ensure_loaded(self._project.name)
        return data_model.get_filter_columns(self._project.name, list(names))

    def range_mask(self, gates: Mapping[str, tuple], mode: str = "and"):
        """A boolean mask over table rows for a set of column ranges.

        Args:
            gates (dict): `{column: (low, high)}` ranges.
            mode (str, optional): `"and"` (every gate must match, the
                default) or `"or"` (matching any gate is enough).

        Returns:
            A boolean numpy array, one entry per row of `geometry()`.
        """
        return data_model.apply_range_mask(self.columns(gates.keys()), gates, mode)

    def ids_matching(self, gates: Mapping[str, tuple], mode: str = "and") -> list:
        """Cell ids whose rows satisfy a set of column ranges.

        Args:
            gates (dict): `{column: (low, high)}` ranges.
            mode (str, optional): `"and"` (every gate must match, the
                default) or `"or"` (matching any gate is enough).

        Returns:
            list: Matching cell ids, in table order. Empty if this project
            has no feature table or `gates` is empty.
        """
        frame = self.geometry()
        if frame is None or not gates:
            return []
        return frame["id"].to_numpy()[self.range_mask(gates, mode)].tolist()


@dataclass(frozen=True)
class ProjectData:
    """Everything a plugin can read about one project: its image, mask,
    feature table and column schema.

    Called `Dataset` until a Dataset became the folder a cohort of projects
    lives in (`plexora.create_dataset`) -- two things one word could not be,
    and this was the one that was always misnamed: it is not a dataset, it is
    the data of one project. `Dataset` stays as an alias for a plugin already
    using the old name.

    Attributes:
        name (str): The project's name.
        image (ImageHandle): The project's image.
        segmentation (SegHandle): The project's segmentation mask, if it has
            one.
        table (TableHandle): The project's feature table, if it has one.
        schema (DatasetSchema | None): Which column holds each role, or None
            if this project has no feature table.
        project (Project): The underlying project record. Most plugins do
            not need it directly; it is here for a call, such as
            `plexora.api.layers`, that takes one.

    Example:
        ```python
        from plexora import api

        data = api.project_data("my_project")
        data.image.channel_names
        data.table.markers
        data.schema.x
        ```
    """

    name: str
    image: ImageHandle
    segmentation: SegHandle
    table: TableHandle
    schema: DatasetSchema | None
    project: Project
    #: Where `cached` keeps its results when this handle set was built for a
    #: caller other than the viewer -- an agent session, which must not share
    #: the loaded datasource's cache because it is not reading that datasource.
    #: None for every handle `project_data` builds, which keeps the viewer's
    #: behaviour exactly what it was.
    _cache: Any = field(default=None, repr=False, compare=False)

    @property
    def source_kind(self) -> str | None:
        """The feature table's format.

        Returns:
            str | None: One of `"csv"`, `"parquet"`, `"anndata"` or
            `"spatialdata"`, or None if this project has no feature table.
        """
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
        if self._cache is not None:
            return self._cache.get_or_set((self.name, key), compute)
        return data_model.gmm_cache_get_or_set((self.name, key), compute)


def _project_data_for(project: Project, table_provider=None, cache=None) -> ProjectData:
    """The handle set for a project record already in hand.

    Split out of `project_data()` so a caller holding a `Project` -- a handle
    dispatching a table operation, a node answering one -- does not re-read
    config.json to get back something it already has.

    `table_provider` is a node's own loaded table. See `TableHandle`: it is the
    one thing a node cannot get from data_model, because data_model describes a
    single loaded datasource and a node serves several.

    `cache` is an object with `get_or_set(key, compute)`, for a caller that
    keeps its own derived results (see `ProjectData._cache`).
    """
    return ProjectData(
        name=project.name,
        image=ImageHandle(project),
        segmentation=SegHandle(project),
        table=TableHandle(project, provider=table_provider),
        schema=DatasetSchema.from_project(project),
        project=project,
        _cache=cache,
    )


def project_data(name: str) -> ProjectData:
    """Build the handle set for a project.

    Construction is cheap: it reads the project record only. Nothing is
    loaded from disk until a handle method is actually called.

    Args:
        name (str): The project's name.

    Returns:
        ProjectData: The handle set.

    Raises:
        KeyError: If no project has this name.

    Example:
        ```python
        from plexora import api

        data = api.project_data("my_project")
        data.image.channel_names
        data.table.markers
        data.schema.x
        ```
    """
    return _project_data_for(Project.load(name))


#: The names this had before a Dataset became the folder above a project.
#: Kept because `api.dataset(name)` is in every bundled plugin and in whatever
#: anybody has written outside this repo, and because the rename buys clarity
#: rather than capability -- there is nothing to be gained by breaking it.
Dataset = ProjectData
dataset = project_data
_dataset_for = _project_data_for
