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
)

API_VERSION = node_registry.API_VERSION


def node_for(binding):
    """The node entry a resource binding names, or a KeyError worth reading."""
    return node_registry.get(binding.node)


class _NodeBacked:
    """What every node provider shares: an address and a resource id."""

    is_local = False

    def __init__(self, binding, kind):
        self._binding = binding
        self._kind = kind
        self._node = None

    @property
    def node(self):
        # Resolved lazily and cached for this provider's life. `resolve_providers`
        # runs inside data_model's load lock and must not read nodes.json there
        # -- a lock held across a file read on a network filesystem is a lock
        # held across a network filesystem.
        if self._node is None:
            self._node = node_for(self._binding)
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

    def load(self, reload: bool = False):
        """Have the node read the file, then pull back the compact copy.

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
            source_obs_ids=[],
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

    def open(self):
        """Nothing to open here, and that is the answer.

        `load_datasource` puts whatever this returns in the `seg` global, and
        for a node-backed mask there is no array on this machine to put there.
        None is what every consumer of that global already checks for.
        """
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
        except ResourceError:
            metadata = {}
        return None, None, metadata

    def geometry(self) -> dict:
        """The image's shape, including every level's own dimensions.

        Cached for this provider's life: it is a constant for a given file, it
        is asked once per Figure Builder panel, and a round trip per panel of
        an eight-panel figure is eight round trips for one answer.
        """
        if self._geometry is None:
            self._geometry = http.json_request(
                self.node, "GET",
                f"/node/v1/image/{self._binding.resource_id}/geometry",
                expected_api=API_VERSION)
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

    def read_region(self, level, box, channel_indices, max_pixels=0):
        """(pixels, clipped_box) for a rectangle of one or more channels.

        The box goes out unclipped and comes back clipped: only the node knows
        each level's real dimensions, and a caller that guessed them from a
        halving rule would clip against a rectangle the file does not have.

        This is what Figure Builder's export and Quick Edit's preview read
        through. It is deliberately raw pixels rather than a rendered panel:
        the render is a few milliseconds of numpy over an already screen-sized
        array, and doing it here keeps one implementation of the colour
        blending rather than two that can disagree about a figure.
        """
        data, _ = http.bytes_request(
            self.node, "POST",
            f"/node/v1/image/{self._binding.resource_id}/region",
            body={"level": int(level), "box": [int(v) for v in box],
                  "channels": [int(i) for i in channel_indices],
                  "max_pixels": int(max_pixels or 0)},
            expected_api=API_VERSION, timeout=600.0)
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
