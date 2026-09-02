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

from typing import Any, Mapping

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
            body=dict(payload or {}), expected_api=API_VERSION)
        return answer.get("result")

    def stream(self, operation: str, payload: Mapping[str, Any] | None = None):
        return http.stream_request(
            self.node, "POST",
            f"/node/v1/table/{self._binding.resource_id}/stream/{operation}",
            body=dict(payload or {}))


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
            http.json_request(
                self.node, "GET",
                f"/node/v1/resources/{self._binding.resource_id}/status",
                timeout=self.OPEN_TIMEOUT, expected_api=API_VERSION)
        except ResourceUnavailable:
            raise
        except ResourceError:
            pass
        return None

    def tile(self, level, tile):
        width, height = self._tile_size
        data, response = http.bytes_request(
            self.node, "GET",
            f"/node/v1/seg/{self._binding.resource_id}/tile/{level}/{tile}"
            f"?tw={width}&th={height}",
            expected_api=API_VERSION)
        return data, response.headers.get("Content-Type") or "image/png"


class NodeImageProvider(_NodeBacked):
    """A channel image on another machine.

    Translates channel NAMES into pyramid indices before the request goes out.
    The names are the project's -- recorded centrally, renamed centrally -- and
    the node knows only what position a plane sits at, which is exactly the
    split that lets `upload_channels` rename a panel without telling the node
    anything at all.
    """

    _kind_path = "image"

    def __init__(self, binding, channel_names=(), tile_size=(1024, 1024)):
        super().__init__(binding, "image")
        self._channel_names = list(channel_names)
        self._tile_size = tile_size
        self._geometry = None

    def with_channels(self, names, tile_width=None, tile_height=None):
        self._channel_names = list(names or ())
        if tile_width and tile_height:
            self._tile_size = (int(tile_width), int(tile_height))
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
        return None, None, metadata

    def geometry(self, timeout=None) -> dict:
        """The image's shape, including every level's own dimensions.

        Cached for this provider's life: it is a constant for a given file, it
        is asked once per Figure Builder panel, and a round trip per panel of
        an eight-panel figure is eight round trips for one answer.

        `timeout` for the one caller that is drawing decoration rather than
        answering somebody -- see `data_model._node_thumbnail_plane`, which
        would otherwise hold a request thread for the default two minutes per
        card in a grid of them.
        """
        if self._geometry is None:
            self._geometry = http.json_request(
                self.node, "GET",
                f"/node/v1/image/{self._binding.resource_id}/geometry",
                expected_api=API_VERSION, timeout=timeout)
        return self._geometry

    def tile(self, channel, level, tile, quality):
        """One tile, forwarded exactly as the node encoded it.

        `channel` is the viewer's own `<file>_<N>` key, so the index travels in
        the string the client already built and nothing has to be looked up.
        """
        width, height = self._tile_size
        data, response = http.bytes_request(
            self.node, "GET",
            f"/node/v1/image/{self._binding.resource_id}/tile/{channel}/{level}/{tile}"
            f"?q={quality}&tw={width}&th={height}",
            expected_api=API_VERSION)
        return data, response.headers.get("Content-Type") or "image/webp"

    def overview(self, channel_name):
        from plexora.server.models.data_model import UnknownChannelError

        try:
            index = self._index(channel_name)
        except UnknownChannelError:
            # No image rather than an error, matching the local path: the
            # mini-map is decoration and a missing thumbnail beats a failure.
            return None
        data, _ = http.bytes_request(
            self.node, "GET",
            f"/node/v1/image/{self._binding.resource_id}/overview"
            f"?channel_index={index}",
            expected_api=API_VERSION)
        return data

    def channel_stats(self, channel_name) -> dict:
        return http.json_request(
            self.node, "GET",
            f"/node/v1/image/{self._binding.resource_id}/stats"
            f"?channel_index={self._index(channel_name)}",
            expected_api=API_VERSION)

    def gmm(self, channel_name) -> dict:
        return http.json_request(
            self.node, "GET",
            f"/node/v1/image/{self._binding.resource_id}/gmm"
            f"?channel_index={self._index(channel_name)}",
            expected_api=API_VERSION,
            # A cold fit is ~1 s of compute on the node, and it runs behind a
            # single-flight lock there as well as here.
            timeout=180.0)

    def quantization_window(self, channel_name) -> tuple:
        answer = http.json_request(
            self.node, "GET",
            f"/node/v1/image/{self._binding.resource_id}/quantization"
            f"?channel_index={self._index(channel_name)}",
            expected_api=API_VERSION, timeout=180.0)
        return (float(answer.get("qmin") or 0.0), float(answer.get("qmax") or 1.0))

    def ome_metadata(self):
        return self.open()[2]

    def read_region(self, level, box, channel_indices, max_pixels=0,
                    timeout=600.0):
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
        """
        data, _ = http.bytes_request(
            self.node, "POST",
            f"/node/v1/image/{self._binding.resource_id}/region",
            body={"level": int(level), "box": [int(v) for v in box],
                  "channels": [int(i) for i in channel_indices],
                  "max_pixels": int(max_pixels or 0)},
            expected_api=API_VERSION, timeout=timeout)
        array, meta = wire.unpack_array(data)
        return array, tuple(meta.get("box") or box)


# -- the two entry points a handle uses ----------------------------------


def run_node_operation(binding, operation: str, payload=None):
    """Run one table operation on the node that holds the table."""
    node = node_for(binding)
    answer = http.json_request(
        node, "POST", f"/node/v1/table/{binding.resource_id}/op/{operation}",
        body=dict(payload or {}), expected_api=API_VERSION)
    return answer.get("result")


def stream_node_operation(binding, operation: str, payload=None):
    """Run one streaming table operation, forwarding its chunks."""
    node = node_for(binding)
    return http.stream_request(
        node, "POST", f"/node/v1/table/{binding.resource_id}/stream/{operation}",
        body=dict(payload or {}))
