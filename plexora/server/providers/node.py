"""Providers for resources on another Plexora process.

Each one is the local provider's opposite number: same method names, same
return shapes, a network in between. Everything that decides what a resource
*means* -- which column is the cell id, which matrix holds the intensities,
what the project is called -- stays here on the primary and is sent with the
request. The node contributes bytes.

**What crosses, and what deliberately does not.** A table node sends: the
compact (id, coordinates, roles) copy, once per load; whole columns, on demand
and already the shape the browser gets today; a dd document; one row for a
hover; and the results of operations that ran over there. It never sends the
table. An image node sends encoded tiles, already in the viewer's own format,
and a few hundred floats of statistics per channel. It never sends a plane.

**Reads retry, writes do not.** See `http.py`. A `POST /op/roi.map_to_cells`
that times out may already have written two columns into somebody's `.h5ad`,
and repeating it is how a refusal turns into a duplicate.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Mapping
from urllib.parse import quote

import numpy as np

from plexora.server.models import nodes as node_registry
from plexora.server.providers import http, wire
from plexora.server.providers.base import (
    NODE,
    Fingerprint,
    ResourceError,
    ResourceLocator,
    ResourceNotLocal,
    ResourceUnavailable,
)

API_VERSION = node_registry.API_VERSION


def node_for(binding):
    """The node entry a resource binding names, or a sentence saying it is gone.

    `ResourceUnavailable`, not the registry's own KeyError, and the difference
    is the whole behaviour of a project whose node has been disconnected. That
    is the ORDINARY end state -- Disconnect forgets the entry on purpose, so
    the map no longer has it while the project still points at it -- and a
    KeyError travelled all the way out of `load_datasource` as a 500 on
    `/init_database`. The viewer's page had already rendered, so what the user
    saw was a project that opened onto nothing and said nothing.

    `load_datasource` catches this kind and records it per resource, which is
    what `/resource_status` reads and what puts the node's name, and a way to
    bring it back, in front of somebody.
    """
    try:
        return node_registry.get(binding.node)
    except KeyError as exc:
        raise ResourceUnavailable(
            f"data node {binding.node!r} is not connected to this Plexora. "
            f"Connect it and reopen this project.",
            node=binding.node) from exc


class _NodeBacked:
    """What every node provider shares: an address and a resource id."""

    is_local = False

    def __init__(self, binding, kind):
        self._binding = binding
        self._kind = kind
        self._node = None
        self._generation = None

    @property
    def node(self):
        # Resolved lazily and cached, but not for this provider's whole life.
        # `resolve_providers` runs inside data_model's load lock and must not
        # read nodes.json there -- a lock held across a file read on a network
        # filesystem is a lock held across a network filesystem -- so the first
        # read happens here, on the first call that needs it.
        #
        # And it happens AGAIN whenever the registry says this node's address
        # moved. A tunnel comes back on whatever local port was free, so a
        # reconnect rewrites nodes.json with a new endpoint and a new token
        # while the loaded project holds the old ones; reopening the project
        # does not help, because `load_datasource` returns early for a name it
        # has already loaded. What that produced was the worst shape a failure
        # can have: the node genuinely up, the connections panel probing the
        # registry and calling it healthy, and every tile, stat and GMM refused
        # against a port that had gone -- with no resource error recorded,
        # because the load that cached the old address had succeeded.
        generation = node_registry.address_generation(self._binding.node)
        if self._node is not None and generation != self._generation:
            # Only on a re-resolve, and only if it works. A node that has been
            # removed from the map is a DISCONNECT, which the cached entry
            # already reports correctly and in its own words (see
            # `http.request`'s is_disconnected check); replacing that with
            # "not connected to this Plexora" would swap a sentence about a
            # tunnel the user closed for one about a project they should
            # reopen. Only a node that is still on the map, at a new address,
            # is picked up here.
            try:
                self._node = node_for(self._binding)
            except ResourceUnavailable:
                pass
            self._generation = generation
        if self._node is None:
            self._node = node_for(self._binding)
            self._generation = generation
        return self._node

    @property
    def locator(self) -> ResourceLocator:
        return ResourceLocator(kind=self._kind, provider=NODE,
                               node=self._binding.node,
                               resource_id=self._binding.resource_id)

    @property
    def binding(self):
        return self._binding

    def _path(self, suffix: str) -> str:
        return f"/node/v1/{self._kind_path}/{self._binding.resource_id}/{suffix.lstrip('/')}"

    def fingerprint(self) -> Fingerprint | None:
        """What the node said this resource was when it was attached.

        The recorded copy, not a fresh stat: the file is on the other machine,
        so the only way to re-derive this is a round trip, and every caller
        that needs it fresh (the write paths) gets it back from the operation
        that checked it.
        """
        return Fingerprint.from_dict(self._binding.fingerprint)


class NodeTableProvider(_NodeBacked):
    """A cell table on another machine.

    Holds the read spec because the node does not: the spec is the project's,
    and it is pushed on every load rather than remembered over there. That is
    what makes a node restart free -- it comes back knowing nothing, and the
    next load tells it everything it needs.
    """

    _kind_path = "table"

    def __init__(self, binding, spec):
        super().__init__(binding, "table")
        self._spec = spec
        self._generation = None
        self._loaded = None

    @property
    def spec(self):
        return self._spec

    @property
    def generation(self):
        return self._generation

    @property
    def frame(self):
        """The compact copy this server holds. Never the whole table."""
        return self._loaded.table if self._loaded is not None else None

    def load(self, reload: bool = False, stage=None, report=None):
        """Have the node read the file, then pull back the compact copy.

        `stage`/`report` are accepted for signature parity with the local
        provider and not used: the read happens on the node, and what crosses
        the wire is one metadata document and one compact frame. Reporting
        stages of somebody else's read would be reporting a guess.

        Two calls, and the split is the design. The first sends the read spec
        and gets back the table's shape -- row count, column names, the
        fingerprint the write paths will check against. The second pulls the
        cell ids, the coordinates and whatever other columns fill a role: about
        twenty bytes a cell, which is the same order as what the browser
        already downloads, and it buys the spatial index, the centroid layers
        and the hover lookup with no round trip under any of them.
        """
        from plexora.server.models.adapters import NormalizedDatasource

        described = http.json_request(
            self.node, "POST", f"/node/v1/table/{self._binding.resource_id}/load",
            body={"spec": self._spec.to_dict(), "reload": bool(reload)},
            expected_api=API_VERSION,
        )
        self._generation = described.get("generation")

        columns = self._compact_columns(described)
        frame = self._fetch_geometry(columns, described)
        self._loaded = NormalizedDatasource(
            table=frame,
            id_column=described.get("id_column") or "id",
            x_column=described.get("x_column") or "",
            y_column=described.get("y_column") or "",
            feature_columns=list(described.get("feature_columns") or []),
            celltype_column=described.get("celltype_column"),
            obs_columns=list(described.get("obs_columns") or []),
            layers=list(described.get("layers") or []),
            obsm=[dict(entry) for entry in (described.get("obsm") or [])],
        )
        return self._loaded

    def _compact_columns(self, described) -> list[str]:
        """Which columns the primary keeps a copy of.

        The id, the coordinates, and every other column that fills a role --
        the image id above all, because "which image is this project" is asked
        on the primary, by both the ROI and the gating plugins, before anything
        is written. Nothing else: a marker column here would be the start of
        keeping the table, one column at a time.
        """
        available = set(described.get("columns") or [])
        wanted = ["id", described.get("id_column"),
                  described.get("x_column"), described.get("y_column"),
                  described.get("celltype_column")]
        roles = self._spec.roles.to_dict() if self._spec is not None else {}
        wanted.extend(roles.values())
        # A measured 3-D object table's plane extent, so a pick on a z-stack
        # keeps to the cells on screen (server/utils/spatial_index.py).
        from plexora.server.utils.spatial_index import DEPTH_COLUMN, EXTENT_COLUMNS

        if roles.get("z"):
            wanted.extend((*EXTENT_COLUMNS, DEPTH_COLUMN))
        seen, columns = set(), []
        for name in wanted:
            if name and name in available and name not in seen:
                seen.add(name)
                columns.append(name)
        return columns

    def _fetch_geometry(self, columns, described):
        """The compact copy, with every column's own dtype intact.

        Arrow IPC rather than the packed float32 frames the range queries use,
        and the difference matters: two of these columns are routinely text.
        The image id is what `mapping.current_image_id` compares to decide
        whose cells these are before an ROI export writes onto them, and a cell
        type is a category label -- casting either to float32 would turn it
        into NaN and the write would go to the wrong image, silently.
        """
        import io

        import polars as pl

        row_count = int(described.get("row_count") or 0)
        if not columns or not row_count:
            return pl.DataFrame({name: [] for name in columns or ["id"]})

        data, _ = http.bytes_request(
            self.node, "GET",
            f"/node/v1/table/{self._binding.resource_id}/geometry"
            f"?columns={','.join(columns)}",
            expected_api=API_VERSION,
        )
        frame = pl.read_ipc(io.BytesIO(data))
        if frame.height != row_count:
            raise ResourceError(
                f"node {self._binding.node!r} sent {frame.height} rows of "
                f"geometry for a table it says has {row_count}")
        return frame

    # -- reads -----------------------------------------------------------

    def describe(self) -> dict:
        return http.json_request(
            self.node, "GET",
            f"/node/v1/table/{self._binding.resource_id}/describe",
            expected_api=API_VERSION)

    def all_cells(self, columns, data_type):
        dtype = "integer" if np.issubdtype(data_type, int) else "float"
        data, _ = http.bytes_request(
            self.node, "GET",
            f"/node/v1/table/{self._binding.resource_id}/all_cells"
            f"?columns={','.join(columns)}&dtype={dtype}",
            expected_api=API_VERSION)
        # urllib3 has already undone the body's Content-Encoding, so this is
        # the same buffer a local read produces -- which is the point of the
        # node speaking the viewer's own wire shape.
        return np.frombuffer(data, dtype=np.uint32 if dtype == "integer" else np.float32)

    def filter_columns(self, columns) -> dict:
        data, _ = http.bytes_request(
            self.node, "GET",
            f"/node/v1/table/{self._binding.resource_id}/columns"
            f"?names={','.join(columns)}",
            expected_api=API_VERSION)
        return wire.unpack_columns(data)

    def metadata_column(self, column: str):
        from plexora.server.models.adapters import MetadataColumn

        data, _ = http.bytes_request(
            self.node, "GET",
            f"/node/v1/table/{self._binding.resource_id}/metadata_column"
            f"?column={column}",
            expected_api=API_VERSION)
        values, meta = wire.unpack_array(data)
        categories = meta.get("categories")
        return MetadataColumn(
            name=meta.get("name") or column,
            values=values,
            categories=tuple(categories) if categories else None,
        )

    def rows(self, ids) -> list:
        answer = http.json_request(
            self.node, "GET",
            f"/node/v1/table/{self._binding.resource_id}/rows"
            f"?ids={http.encode_ids(ids)}",
            expected_api=API_VERSION)
        return answer.get("rows") or []

    # -- work that has to happen there ------------------------------------

    def run(self, operation: str, payload: Mapping[str, Any], dataset=None) -> Any:
        answer = http.json_request(
            self.node, "POST",
            f"/node/v1/table/{self._binding.resource_id}/op/{operation}",
            body=dict(payload or {}), expected_api=API_VERSION,
            headers=_licence_headers(self.node, operation))
        return answer.get("result")

    def stream(self, operation: str, payload: Mapping[str, Any] | None = None):
        return http.stream_request(
            self.node, "POST",
            f"/node/v1/table/{self._binding.resource_id}/stream/{operation}",
            body=dict(payload or {}), headers=_licence_headers(self.node, operation))


class NodeSegmentationProvider(_NodeBacked):
    """A label mask on another machine."""

    _kind_path = "seg"

    def __init__(self, binding, tile_size=(1024, 1024)):
        super().__init__(binding, "segmentation")
        self._tile_size = tile_size

    def with_tile_size(self, width, height):
        self._tile_size = (int(width or 1024), int(height or 1024))
        return self

    #: How long the availability check below may take. Short, because it runs
    #: inside `load_datasource`'s lock: a dead address is settled by the
    #: connect timeout long before this matters, and a node that accepts a
    #: connection and then says nothing must not hold the load open.
    OPEN_TIMEOUT = 10.0

    def open(self):
        """Nothing to LOAD here -- but the node still has to be asked.

        `load_datasource` puts whatever this returns in the `seg` global, and
        for a node-backed mask there is no array on this machine to put there.
        None is what every consumer of that global already checks for.

        The round trip is not for the value. It is for the question
        `load_datasource` is actually asking each provider -- can this resource
        be read? -- and returning None without asking made a mask on a machine
        that had gone indistinguishable from a mask that was fine. The project
        opened reporting nothing wrong and every label tile 404'd.

        Only unreachability is raised. A node that answers something else --
        it does not know this resource, or it is still converting one -- is a
        condition the tile path already reports in its own terms, and not this
        function's to decide.
        """
        try:
            self._remember(http.json_request(
                self.node, "GET",
                f"/node/v1/resources/{self._binding.resource_id}/status",
                timeout=self.OPEN_TIMEOUT, expected_api=API_VERSION))
        except ResourceUnavailable:
            raise
        except ResourceError:
            pass
        return None

    #: How long a node's account of a mask is reused before it is asked again:
    #: long enough that `/volume/info`, the objects card and a section render
    #: in one page load ask once, short enough that a mask the node has just
    #: finished converting is seen as the volume it now is.
    STATUS_TTL = 5.0
    #: (node, resource id) -> (when, the node's resource status).
    _statuses: dict = {}

    def _remember(self, answer):
        """Keep the node's `resource` description out of a status answer."""
        import time

        status = answer.get("resource") if isinstance(answer, Mapping) else None
        if isinstance(status, Mapping):
            self._statuses[self._status_key()] = (time.monotonic(), dict(status))
            return dict(status)
        return {}

    def _status_key(self):
        return (getattr(self.node, "name", str(self.node)), self._binding.resource_id)

    def status(self) -> dict:
        """The node's own account of this mask (`resources.describe`)."""
        import time

        held = self._statuses.get(self._status_key())
        if held is not None and time.monotonic() - held[0] < self.STATUS_TTL:
            return held[1]
        return self._remember(http.json_request(
            self.node, "GET", f"/node/v1/resources/{self._binding.resource_id}/status",
            timeout=self.OPEN_TIMEOUT, expected_api=API_VERSION)) or {}

    def label_volume(self, t: int = 0):
        """This mask as a `NodeLabelVolume` at frame `t`, or None when the node
        holds it as a 2-D mask (or is too old to say)."""
        facts = (self.status() or {}).get("label_volume")
        if not facts:
            return None
        return NodeLabelVolume(self, facts).frame(t)

    def tile(self, level, tile, view=""):
        """One label tile, as the node encodes it. `view` names the plane of a
        3-D mask (`LabelVolume.at_view` on the node); a 2-D mask ignores it."""
        width, height = self._tile_size
        query = f"?tw={width}&th={height}"
        if view:
            query += f"&view={quote(str(view), safe='')}"
        data, response = http.bytes_request(
            self.node, "GET",
            f"/node/v1/seg/{self._binding.resource_id}/tile/{level}/{tile}{query}",
            expected_api=API_VERSION)
        return data, response.headers.get("Content-Type") or "image/png"

    def read_region(self, level, box, max_pixels=0, timeout=600.0, z=None, t=None):
        """Labels (uint32) for `box` = (x0, y0, x1, y1) at `level`, padded
        with 0 where it runs off the mask -- what `padded_label_region` gives
        for a local one, computed on the node that has the file. `z`/`t` pick
        the plane of a 3-D mask, as `LocalSegmentationProvider.read_region`'s
        `z` does; None is the node's default plane."""
        body = {"level": int(level), "box": [int(v) for v in box],
                "max_pixels": int(max_pixels or 0)}
        if z is not None:
            body["z"] = int(z)
        if t is not None:
            body["t"] = int(t)
        data, _ = http.bytes_request(
            self.node, "POST",
            f"/node/v1/seg/{self._binding.resource_id}/region",
            body=body, expected_api=API_VERSION, timeout=timeout)
        array, _meta = wire.unpack_array(data)
        return array

    def read_block(self, level, z0, z1, rows, cols, t=0, timeout=600.0):
        """`(Z, H, W)` label ids for planes z0..z1 inclusive of frame `t` at
        `level` -- `LabelVolume.read`, on the node that holds the volume."""
        rows = rows if isinstance(rows, slice) else slice(*rows)
        cols = cols if isinstance(cols, slice) else slice(*cols)
        data, _ = http.bytes_request(
            self.node, "POST",
            f"/node/v1/seg/{self._binding.resource_id}/block",
            body={"level": int(level), "t": int(t), "z0": int(z0), "z1": int(z1),
                  "rows": [rows.start, rows.stop], "cols": [cols.start, cols.stop]},
            expected_api=API_VERSION, timeout=timeout)
        array, _meta = wire.unpack_array(data)
        return array


class _NodePlaneLevel:
    """One level of one plane of a node's label volume, `[rows, cols]`."""

    ndim = 2

    def __init__(self, labels, level: int, z: int, shape):
        self._labels = labels
        self._level = int(level)
        self._z = int(z)
        self.shape = (int(shape[0]), int(shape[1]))
        self.dtype = np.dtype(np.uint32)
        self.chunks = self.shape

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)
        rows = index[0] if index else slice(None)
        cols = index[1] if len(index) > 1 else slice(None)
        rows = slice(*rows.indices(self.shape[0])[:2])
        cols = slice(*cols.indices(self.shape[1])[:2])
        return self._labels.read(self._level, self._z, self._z, rows, cols)[0]

    def __array__(self, dtype=None, copy=None):
        plane = self[:, :]
        return plane.astype(dtype) if dtype is not None else plane


class _NodePlanePyramid:
    """A plane of a node's label volume, shaped like a 2-D mask's zarr group."""

    def __init__(self, labels, z: int):
        self._labels = labels
        self.z = int(z)

    def __len__(self):
        return self._labels.levels

    def __iter__(self):
        return iter(str(index) for index in range(self._labels.levels))

    def __contains__(self, key):
        try:
            return 0 <= int(key) < self._labels.levels
        except (TypeError, ValueError):
            return False

    def __getitem__(self, key):
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise KeyError(key) from None
        if not 0 <= index < self._labels.levels:
            raise KeyError(key)
        return _NodePlaneLevel(self._labels, index, self.z,
                               self._labels.level_shapes()[index])

    def array_keys(self):
        return list(iter(self))

    def keys(self):
        return list(iter(self))


class NodeLabelVolume:
    """A data node's 3-D label volume, read over the wire.

    `volume/labels.LabelVolume`'s face -- `depth`, `height`, `width`,
    `frames`, `levels`, `level_shapes()`, `plane(z)`, `frame(t)`,
    `read(level, z0, z1, rows, cols)` -- so measurement, sections and QC in
    depth take a node's mask exactly as they take a local store, while every
    id is read on the machine that has the file
    (`NodeSegmentationProvider.read_block`). `path` is the mask's `node://`
    locator, for the records that name where labels came from.
    """

    def __init__(self, provider, facts, t: int = 0):
        self._provider = provider
        self._facts = dict(facts)
        self.path = (f"node://{getattr(provider.node, 'name', provider.node)}/"
                     f"{provider._binding.resource_id}")
        self.depth = int(self._facts.get("depth") or 1)
        self.height = int(self._facts.get("height") or 0)
        self.width = int(self._facts.get("width") or 0)
        self.frames = max(1, int(self._facts.get("frames") or 1))
        self.t = max(0, min(self.frames - 1, int(t))) if self.frames > 1 else 0
        shapes = self._facts.get("level_shapes") or [[self.height, self.width]]
        self._shapes = [[int(h), int(w)] for h, w in shapes]

    @property
    def levels(self) -> int:
        return len(self._shapes)

    def level_shapes(self):
        return [list(shape) for shape in self._shapes]

    def plane(self, z: int) -> _NodePlanePyramid:
        return _NodePlanePyramid(self, max(0, min(self.depth - 1, int(z))))

    def default_plane(self) -> _NodePlanePyramid:
        return self.plane(self.depth // 2)

    def frame(self, t: int) -> "NodeLabelVolume":
        if self.frames == 1 or int(t) == self.t:
            return self
        return NodeLabelVolume(self._provider, self._facts, t)

    def read(self, level: int, z0: int, z1: int, rows: slice, cols: slice) -> np.ndarray:
        """`(Z, H, W)` for planes z0..z1 inclusive at `level`."""
        return self._provider.read_block(level, z0, z1, rows, cols, t=self.t)

    def __getitem__(self, key):
        return self.default_plane()[key]

    def __len__(self):
        return self.levels

    def __iter__(self):
        return iter(self.default_plane())

    def __contains__(self, key):
        return key in self.default_plane()


class NodeVolume:
    """A volume whose planes are on a data node: what it is, never its pixels.

    What `data_model.volume_source` holds for a node-backed volume. Everything
    the primary does with a volume it does with `.volume` -- parse a view token
    against its extent, pick a window class, size a section's tile source,
    answer `/volume/<ds>/info` -- while every pixel is read over there. So the
    read methods refuse in a sentence rather than pretend: reaching one means
    the local read path was taken for an image that is not local.
    """

    is_local = False

    def __init__(self, volume, node_name=""):
        self.volume = volume
        self._node_name = node_name

    def _not_here(self, *_args, **_kwargs):
        raise ResourceNotLocal(
            f"this volume's planes are on data node {self._node_name!r}; they are "
            f"read through the node, never from a pyramid on this machine")

    plane_pyramid = level_array = read_plane = read_block = _not_here

    def level_shape(self, level):
        info = self.volume.levels[min(level, len(self.volume.levels) - 1)]
        return info.height, info.width

    def close(self) -> None:
        pass


def _query(params, lead="&") -> str:
    from urllib.parse import urlencode

    return f"{lead}{urlencode(params)}" if params else ""


class NodeImageProvider(_NodeBacked):
    """A channel image on another machine.

    Translates channel NAMES into pyramid indices before the request goes out.
    The names are the project's -- recorded centrally, renamed centrally -- and
    the node knows only what position a plane sits at, which is exactly the
    split that lets `upload_channels` rename a panel without telling the node
    anything at all.

    **Volumes.** A node has no project, so it cannot know whether a stack's z
    axis is depth or imaging cycles (volume/sources/tiff.py says why that is a
    decision). `with_volume` hands it the project's answer, and every request
    about a volumetric project then carries `view` -- the default plane spelled
    as its own token -- plus `zs`/`ts`, which is what makes the node read the
    same planes, with the same per-volume windows, as a local read. A planar
    project carries none of it: its URLs are the ones it has always sent.
    """

    _kind_path = "image"

    def __init__(self, binding, channel_names=(), tile_size=(1024, 1024)):
        super().__init__(binding, "image")
        self._channel_names = list(channel_names)
        self._tile_size = tile_size
        self._geometry = None
        self._z_semantics = None
        self._t_semantics = None
        self._volumetric = False
        self._spacing = {}
        self._volume_asked = False
        #: A `NodeVolume` once the node has described this image as a volume.
        #: Read by data_model right after `open()`, like the local provider's.
        self.volume_source = None
        #: For a volumetric project whose node could not describe the image as
        #: a volume: the sentence a view is refused with (`views.resolve`).
        self.views_refused = None

    def with_channels(self, names, tile_width=None, tile_height=None):
        self._channel_names = list(names or ())
        if tile_width and tile_height:
            self._tile_size = (int(tile_width), int(tile_height))
        return self

    def with_volume(self, image):
        """Read the node's file the way the project reads it.

        `image` is the project's `ImageSpec`: its z/t reading, whether it is a
        volume at all, and any spacing somebody typed (a section's row height
        follows the z step, so the node has to draw it with the same one).
        """
        self._z_semantics = getattr(image, "z_semantics", None) or None
        self._t_semantics = getattr(image, "t_semantics", None) or None
        self._volumetric = bool(getattr(image, "is_volumetric", False))
        self._spacing = {"pixel_size": getattr(image, "pixel_size", None),
                         "pixel_size_z": getattr(image, "pixel_size_z", None),
                         "frame_interval": getattr(image, "frame_interval", None)}
        return self

    def _index(self, channel_name):
        from plexora.server.models.data_model import UnknownChannelError

        try:
            return self._channel_names.index(channel_name)
        except ValueError:
            raise UnknownChannelError(
                f"{channel_name!r} is not a channel of the image on node "
                f"{self._binding.node!r}. Its channels may have been renamed "
                f"since this page was opened.") from None

    def open(self):
        """(channels, overview, metadata) with the two arrays absent.

        The pyramid and the mean-pooled overview stay on the node -- they are
        the things that must not cross -- so the globals they would have filled
        are None here, and every function that reads them is behind a dispatch
        guard that routes to this provider instead. The OME header does come
        back: it is kilobytes, the viewer shows it, and a request per lookup
        would be a round trip for a constant.

        A volumetric project also asks the node what the volume is (one
        geometry round trip, cached), so `volume_source` is set by the time
        data_model reads it.
        """
        try:
            metadata = http.json_request(
                self.node, "GET",
                f"/node/v1/image/{self._binding.resource_id}/ome_metadata",
                expected_api=API_VERSION)
        except ResourceUnavailable:
            # The MACHINE did not answer, which is a different thing from this
            # image having no OME header -- and it is the one thing
            # `load_datasource` has to hear about. Swallowed with the rest, it
            # recorded no failure at all: `/resource_status` reported the
            # project perfectly healthy while the viewer pointed every tile
            # request at a port nothing was listening on. That is what "the
            # project opens and nothing happens" was.
            raise
        except ResourceError:
            # Anything else is the node answering something unhelpful about
            # this one file. The header is optional; the project is not.
            metadata = {}
        if self._volumetric:
            self._attach_volume()
        return None, None, metadata

    def _attach_volume(self):
        """Ask the node what this image is as a volume.

        No volume in the answer leaves the project on its default plane, the
        way it was served before volumes, with `views_refused` saying why --
        the commonest cause being a node whose Plexora predates them, which
        answers geometry without the keys rather than with an error.
        """
        from dataclasses import replace

        from plexora.server.volume.model import Volume

        self._volume_asked = True
        self.volume_source = None
        self.views_refused = None
        node = self._binding.node
        try:
            geometry = self.geometry()
        except ResourceUnavailable:
            raise
        except ResourceError as exc:
            self.views_refused = (f"data node {node!r} could not describe this image "
                                  f"as a volume: {exc}")
            return
        described = geometry.get("volume")
        volume = None
        if isinstance(described, Mapping):
            try:
                volume = Volume.from_dict(described)
            except (KeyError, TypeError, ValueError):
                volume = None
        if volume is None or volume.is_planar:
            self.views_refused = (
                f"data node {node!r} does not serve this image as a volume, so only "
                f"its default plane can be shown. The Plexora installed there may "
                f"predate volumes: upgrade it there (pip install --upgrade plexora) "
                f"and reopen this project.")
            return
        spacing = volume.spacing.overlaid(**self._spacing, frames=volume.frames)
        if spacing is not volume.spacing:
            volume = replace(volume, spacing=spacing)
        self.volume_source = NodeVolume(volume, node)

    def volume(self):
        """The `NodeVolume` this image is, asked of the node once; None for a
        planar project, or one whose node could not describe it as a volume
        (`views_refused` says why). The local provider's `volume()`, over a
        network."""
        if self._volumetric and self.volume_source is None and not self._volume_asked:
            self._attach_volume()
        return self.volume_source

    def read_volume_block(self, level, t, channel, z0, z1, rows, cols, timeout=600.0):
        """`(Z, H, W)` raw values of channel index `channel`: planes z0..z1
        inclusive of frame `t` at `level`, under the project's z/t reading --
        a local volume source's `read_block`, on the node that has the file.
        For the few million voxels a surface preview reads; a whole build runs
        over there instead (`build_surface_on_node`)."""
        rows = rows if isinstance(rows, slice) else slice(*rows)
        cols = cols if isinstance(cols, slice) else slice(*cols)
        data, _ = http.bytes_request(
            self.node, "POST", f"/node/v1/image/{self._binding.resource_id}/block",
            body={"level": int(level), "t": int(t), "c": int(channel), "z0": int(z0),
                  "z1": int(z1), "rows": [rows.start, rows.stop],
                  "cols": [cols.start, cols.stop], "zs": self._z_semantics,
                  "ts": self._t_semantics},
            expected_api=API_VERSION, timeout=timeout)
        array, _meta = wire.unpack_array(data)
        return array

    def _view_params(self, view=None) -> dict:
        """The parameters a request about this image carries.

        `{}` for a planar image -- its requests are byte for byte what they
        always were. For a volume: the view's canonical token (the default
        plane spelled as itself, so the node never has to guess what "default"
        means), the project's z/t reading, and for a section the anisotropy
        its rows are drawn at.
        """
        from plexora.server.volume.viewspec import BadView, ViewSpec

        text = "" if view is None else str(view).strip()
        source = self.volume()
        if source is None:
            if text:
                raise BadView(self.views_refused
                              or "this image has a single plane, so it has no views "
                                 "to choose from")
            return {}
        spec = ViewSpec.parse(text, source.volume)
        params = {"view": spec.token()}
        if self._z_semantics:
            params["zs"] = self._z_semantics
        if self._t_semantics:
            params["ts"] = self._t_semantics
        if spec.kind == "section":
            params["az"] = repr(float(source.volume.spacing.anisotropy()))
        return params

    def _served(self, response, params):
        """Refuse an answer that is not of the view that was asked for.

        A node that predates volumes ignores `view=` and answers with its
        default plane -- a perfectly good picture of the wrong planes, which is
        exactly what must never be captioned as the right ones.
        """
        from plexora.server.volume.viewspec import BadView

        if not params:
            return
        served = response.headers.get(http.VIEW_HEADER)
        if served != params["view"]:
            raise BadView(
                f"data node {self._binding.node!r} did not serve view "
                f"{params['view']!r} of this image"
                + (f" (it served {served!r})" if served else
                   ": the Plexora installed there predates volumes. Upgrade it "
                   "there (pip install --upgrade plexora) to see this image's "
                   "planes, projections and sections"))

    def _image_json(self, suffix, params, timeout=None):
        """One JSON answer about this image, checked against the view asked for."""
        path = (f"/node/v1/image/{self._binding.resource_id}/{suffix}"
                f"{_query(params, '&' if '?' in suffix else '?')}")
        if not params:
            return http.json_request(self.node, "GET", path, expected_api=API_VERSION,
                                     timeout=timeout)
        response = http.request(self.node, "GET", path, expected_api=API_VERSION,
                                timeout=timeout)
        self._served(response, params)
        return http.json_of(self.node, path, response)

    def geometry(self, timeout=None) -> dict:
        """The image's shape, including every level's own dimensions.

        Cached for this provider's life: it is a constant for a given file, it
        is asked once per Figure Builder panel, and a round trip per panel of
        an eight-panel figure is eight round trips for one answer.

        `timeout` for the one caller that is drawing decoration rather than
        answering somebody -- see `data_model._node_thumbnail_plane`, which
        would otherwise hold a request thread for the default two minutes per
        card in a grid of them.

        A volumetric project asks with its z/t reading, so the shape is the
        one the node will serve its planes in (a stack read as depth has C
        channels; read as cycles, C x Z).
        """
        if self._geometry is None:
            reading = {}
            if self._volumetric:
                if self._z_semantics:
                    reading["zs"] = self._z_semantics
                if self._t_semantics:
                    reading["ts"] = self._t_semantics
            self._geometry = http.json_request(
                self.node, "GET",
                f"/node/v1/image/{self._binding.resource_id}/geometry"
                f"{_query(reading, '?')}",
                expected_api=API_VERSION, timeout=timeout)
        return self._geometry

    def tile(self, channel, level, tile, quality, view="", window=None):
        """One tile, forwarded exactly as the node encoded it.

        `channel` is the viewer's own `<file>_<N>` key, so the index travels in
        the string the client already built and nothing has to be looked up.
        `view` names a plane, projection or section of a volume ("" = the
        default plane); see the class docstring. `window` is the (qmin, qmax)
        the viewer named for a volume's 8-bit tile (`w=`), which the node
        quantizes with, so the bytes are the ones the address promises.
        """
        width, height = self._tile_size
        params = self._view_params(view)
        if params and window is not None:
            params["w"] = f"{float(window[0]):g},{float(window[1]):g}"
        data, response = http.bytes_request(
            self.node, "GET",
            f"/node/v1/image/{self._binding.resource_id}/tile/{channel}/{level}/{tile}"
            f"?q={quality}&tw={width}&th={height}{_query(params)}",
            expected_api=API_VERSION)
        self._served(response, params)
        return data, response.headers.get("Content-Type") or "image/webp"

    def overview(self, channel_name):
        from plexora.server.models.data_model import UnknownChannelError

        try:
            index = self._index(channel_name)
        except UnknownChannelError:
            # No image rather than an error, matching the local path: the
            # mini-map is decoration and a missing thumbnail beats a failure.
            return None
        params = self._view_params()
        data, response = http.bytes_request(
            self.node, "GET",
            f"/node/v1/image/{self._binding.resource_id}/overview"
            f"?channel_index={index}{_query(params)}",
            expected_api=API_VERSION)
        self._served(response, params)
        return data

    def channel_stats(self, channel_name, view="", provisional=False) -> dict:
        """The stats packet; `view` matters only for a slab projection, whose
        distribution is its own (data_model._slab_view). `provisional`: a
        volume's packet answered now, made with the provisional window while
        the node scans for the exact one (`window_exact` says which; a node
        too old to know answers exactly, and says nothing)."""
        params = self._view_params(view)
        if provisional and params:
            params["provisional"] = "1"
        return self._image_json(f"stats?channel_index={self._index(channel_name)}", params)

    def gmm(self, channel_name, view="") -> dict:
        return self._image_json(
            f"gmm?channel_index={self._index(channel_name)}", self._view_params(view),
            # A cold fit is ~1 s of compute on the node, and it runs behind a
            # single-flight lock there as well as here.
            timeout=180.0)

    def quantization_window(self, channel_name, window_class=None) -> tuple:
        """(qmin, qmax) for one channel; `window_class` ("volume"/"std") for a
        volume, whose one window spans every plane (volume/windows.py)."""
        params = self._view_params() if window_class else {}
        if params:
            params["wc"] = window_class
        answer = self._image_json(
            f"quantization?channel_index={self._index(channel_name)}", params,
            timeout=180.0)
        return (float(answer.get("qmin") or 0.0), float(answer.get("qmax") or 1.0))

    def level(self, channel_name, ask, covered, pool, window):
        """`(bytes, shape, hint)`: one channel's 3-D level, built on the node.

        Every choice is the primary's and goes out explicitly -- the level,
        the area it `covered`, the planes, `pool` (from the project's z step,
        which the node may not hold) and the window -- so the node reads,
        pools and quantizes exactly what a local read would
        (server/volume/serve.py builds both)."""
        params = self._view_params()
        if not params:
            from plexora.server.volume.viewspec import BadView

            raise BadView(self.views_refused or "this image has a single plane")
        x0, y0, x1, y1 = (int(v) for v in covered)
        params.update({"t": ask.t, "level": ask.level, "format": ask.voxel_format,
                       "box": f"{x0},{y0},{x1 - x0},{y1 - y0}", "z": f"{ask.z0},{ask.z1}",
                       "pool": int(pool),
                       "w": f"{float(window[0]):g},{float(window[1]):g}"})
        path = (f"/node/v1/image/{self._binding.resource_id}/level"
                f"?channel_index={self._index(channel_name)}{_query(params)}")
        data, response = http.bytes_request(self.node, "GET", path,
                                            expected_api=API_VERSION, timeout=600.0)
        self._served(response, params)
        shape = tuple(int(v) for v in (response.headers.get("X-Plexora-Shape") or "").split(",")
                      if v)
        if len(shape) != 3:
            raise ResourceError(f"data node {self._binding.node!r} sent a level without "
                                f"its shape")
        raw_hint = response.headers.get("X-Plexora-Hint")
        hint = tuple(float(v) for v in raw_hint.split(",")) if raw_hint else None
        return data, shape, hint

    def volume_window_state(self, channel_name) -> tuple:
        """`((qmin, qmax), exact)`: a volume channel's window, answered now --
        the provisional one while the node scans for the exact one
        (data_model.volume_window_state, over a network)."""
        params = self._view_params()
        params.update({"wc": "volume", "provisional": "1"})
        answer = self._image_json(
            f"quantization?channel_index={self._index(channel_name)}", params, timeout=180.0)
        window = (float(answer.get("qmin") or 0.0), float(answer.get("qmax") or 1.0))
        return window, bool(answer.get("exact", True))

    def ome_metadata(self):
        return self.open()[2]

    def read_region(self, level, box, channel_indices, max_pixels=0,
                    timeout=600.0, view=None):
        """(pixels, clipped_box) for a rectangle of one or more channels.

        The box goes out unclipped and comes back clipped: only the node knows
        each level's real dimensions, and a caller that guessed them from a
        halving rule would clip against a rectangle the file does not have.

        This is what Figure Builder's export and Quick Edit's preview read
        through. It is deliberately raw pixels rather than a rendered panel:
        the render is a few milliseconds of numpy over an already screen-sized
        array, and doing it here keeps one implementation of the colour
        blending rather than two that can disagree about a figure.

        The default `timeout` is ten minutes because an export reads real
        rectangles of a real image and somebody is waiting for the figure.
        A thumbnail is not that, and passes a short one.

        `view` reads a plane, projection or section of a volume, in that
        view's own pixels (a section's rows are z); None is the default plane.
        """
        params = self._view_params(view)
        data, response = http.bytes_request(
            self.node, "POST",
            f"/node/v1/image/{self._binding.resource_id}/region",
            body={"level": int(level), "box": [int(v) for v in box],
                  "channels": [int(i) for i in channel_indices],
                  "max_pixels": int(max_pixels or 0), **params},
            expected_api=API_VERSION, timeout=timeout)
        self._served(response, params)
        array, meta = wire.unpack_array(data)
        return array, tuple(meta.get("box") or box)


# -- the two entry points a handle uses ----------------------------------


def _licence_headers(node, operation: str) -> dict:
    """The entitlement proof for an operation that serves a Paid capability
    (plexora/licensing/tokens.py). Empty -- and nothing looked at -- for the
    Free plumbing that is nearly every operation."""
    from plexora.licensing import tokens

    if tokens.required_for(operation) is None:
        return {}
    return tokens.headers_for(getattr(node, "token", "") or "", operation)


def run_node_operation(binding, operation: str, payload=None):
    """Run one table operation on the node that holds the table."""
    node = node_for(binding)
    answer = http.json_request(
        node, "POST", f"/node/v1/table/{binding.resource_id}/op/{operation}",
        body=dict(payload or {}), expected_api=API_VERSION,
        headers=_licence_headers(node, operation))
    return answer.get("result")


def stream_node_operation(binding, operation: str, payload=None):
    """Run one streaming table operation, forwarding its chunks."""
    node = node_for(binding)
    return http.stream_request(
        node, "POST", f"/node/v1/table/{binding.resource_id}/stream/{operation}",
        body=dict(payload or {}), headers=_licence_headers(node, operation))


#: How often a measurement running on a node is asked how far it has got.
MEASURE_POLL_SECONDS = 1.0


def measure_objects_on_node(image_binding, seg_binding, *, channel_names, indices,
                            units="physical", z_semantics=None, t_semantics=None,
                            pixel_size=None, pixel_size_z=None, frame_interval=None,
                            progress=None, cancelled=None, poll=MEASURE_POLL_SECONDS):
    """`volume.objects.measure_columns`, run on the node that holds the image
    and the mask: `(frame, manifest)`, the table exactly as a local
    measurement makes it (parquet on the wire, so every column keeps its
    dtype).

    A job over there, not one long request: it is minutes to hours on a whole
    z-stack, `progress(done, total)` is relayed from it, and `cancelled()`
    stops it. The project's z/t reading and any pixel size somebody typed are
    sent, so the node reads the stack the way this machine's viewer does.
    """
    import io
    import time

    import polars as pl

    node = node_for(image_binding)
    started = http.json_request(
        node, "POST", "/node/v1/volume/measure",
        body={"image": image_binding.resource_id, "segmentation": seg_binding.resource_id,
              "channel_names": list(channel_names), "indices": [int(i) for i in indices],
              "units": units, "zs": z_semantics, "ts": t_semantics,
              "pixel_size": pixel_size, "pixel_size_z": pixel_size_z,
              "frame_interval": frame_interval},
        expected_api=API_VERSION)
    job = str(started.get("job") or "")
    if not job:
        raise ResourceError("the data node did not start the measurement")
    try:
        while True:
            if cancelled is not None and cancelled():
                http.json_request(node, "POST", f"/node/v1/volume/measure/{job}/cancel",
                                  expected_api=API_VERSION)
                raise InterruptedError("cancelled")
            status = http.json_request(node, "GET", f"/node/v1/volume/measure/{job}",
                                       expected_api=API_VERSION)
            if progress is not None and status.get("total"):
                progress(int(status.get("done") or 0), int(status["total"]))
            state = status.get("state")
            if state == "done":
                break
            if state == "error":
                raise ResourceError(status.get("error") or "the measurement failed on the node")
            if state == "cancelled":
                raise InterruptedError("cancelled")
            time.sleep(poll)
        data, _ = http.bytes_request(node, "GET", f"/node/v1/volume/measure/{job}/result",
                                     expected_api=API_VERSION)
    except ResourceUnavailable:
        raise
    return pl.read_parquet(io.BytesIO(data)), dict(status.get("manifest") or {})


# -- surfaces of a node-held z-stack ----------------------------------------------


class NodeVolumeReader:
    """A node-held z-stack as a surface reads it: the volume's description
    (spacing included, as the project states it) and `read_block` over the
    wire. Enough to plan a surface, preview a threshold and measure a mesh;
    a build reads the whole stack, so it runs on the node instead."""

    is_local = False

    def __init__(self, provider, volume):
        self._provider = provider
        self.volume = volume

    def read_block(self, level, t, c, z0, z1, rows, cols):
        return self._provider.read_volume_block(level, t, c, z0, z1, rows, cols)

    def close(self) -> None:
        pass


#: (project, node, resource, reading) -> (made at, reader): a preview asks per
#: slider move, and the volume's description is a round trip.
_READERS: "OrderedDict[tuple, tuple]" = OrderedDict()
_READERS_LOCK = threading.Lock()
READER_TTL = 60.0


def project_volume_reader(project):
    """A `NodeVolumeReader` for a project whose z-stack a data node holds;
    None when it does not, or when the node cannot describe it as a volume."""
    import time

    binding = (getattr(project, "resources", None) or {}).get("image")
    if binding is None or not getattr(binding, "node", None):
        return None
    image = project.image
    # By address too: a node that reconnects comes back on another port.
    entry = node_for(binding)
    key = (project.name, binding.node, getattr(entry, "endpoint", None),
           getattr(entry, "token", None), binding.resource_id, image.z_semantics,
           image.t_semantics, image.pixel_size, image.pixel_size_z, image.frame_interval,
           tuple(image.channel_names))
    now = time.monotonic()
    with _READERS_LOCK:
        held = _READERS.get(key)
        if held is not None and now - held[0] < READER_TTL:
            return held[1]
    provider = NodeImageProvider(binding).with_channels(
        image.channel_names, image.tile_width, image.tile_height).with_volume(image)
    described = provider.volume()
    if described is None:
        return None
    reader = NodeVolumeReader(provider, described.volume)
    with _READERS_LOCK:
        _READERS[key] = (now, reader)
        while len(_READERS) > 16:
            _READERS.popitem(last=False)
    return reader


SURFACE_POLL_SECONDS = 1.0


def build_surface_on_node(binding, body, *, stage=None, progress=None, cancelled=None,
                          poll=SURFACE_POLL_SECONDS):
    """`surface.store.extract_on_node`, run on the node `binding` names:
    `(mesh, level, info)`, the mesh exactly as a local extraction makes it
    (every array float-exact on the wire).

    A job over there, like a measurement (`measure_objects_on_node`): the
    node's stage and progress are relayed to `stage(name)` and
    `progress(done, total)`, and `cancelled()` stops it."""
    import io
    import time

    from plexora.server.surface import mesh as mesh_mod

    node = node_for(binding)
    started = http.json_request(node, "POST", "/node/v1/volume/surface", body=body,
                                expected_api=API_VERSION)
    job = str(started.get("job") or "")
    if not job:
        raise ResourceError("the data node did not start the surface build")
    seen_stage = None
    while True:
        if cancelled is not None and cancelled():
            http.json_request(node, "POST", f"/node/v1/volume/jobs/{job}/cancel",
                              expected_api=API_VERSION)
            raise InterruptedError("cancelled")
        status = http.json_request(node, "GET", f"/node/v1/volume/jobs/{job}",
                                   expected_api=API_VERSION)
        if stage is not None and status.get("stage") and status["stage"] != seen_stage:
            seen_stage = status["stage"]
            stage(seen_stage)
        if progress is not None and status.get("total"):
            progress(int(status.get("done") or 0), int(status["total"]))
        state = status.get("state")
        if state == "done":
            break
        if state == "error":
            raise ResourceError(status.get("error") or "the surface build failed on the node")
        if state == "cancelled":
            raise InterruptedError("cancelled")
        time.sleep(poll)
    data, _ = http.bytes_request(node, "GET", f"/node/v1/volume/jobs/{job}/result",
                                 expected_api=API_VERSION)
    arrays = np.load(io.BytesIO(data), allow_pickle=False)
    surface = mesh_mod.Mesh(
        positions=arrays["positions"], triangles=arrays["triangles"],
        object_id=arrays["object_id"],
        normals=arrays["normals"] if "normals" in arrays.files else None,
        positions_raw=arrays["positions_raw"] if "positions_raw" in arrays.files else None,
        objects=arrays["objects"] if "objects" in arrays.files else None)
    result = status.get("result") or {}
    return surface, int(result.get("level") or 0), dict(result.get("info") or {})
