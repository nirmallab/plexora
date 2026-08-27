"""The node API: everything one Plexora process will answer about files it holds.

Version 1. Every response carries `X-Plexora-Node-Api`, and a resource response
also carries the resource's id and its current generation, so a primary can tell
a cached answer from a stale one without asking what changed.

Three properties this surface commits to:

**The wire shapes are the ones the viewer already uses.** `/all_cells` is the
same gzipped buffer `/get_all_cells` serves, a tile is the same WebP or PNG the
tile route serves, `describe` is the same `dd` document. That is what lets the
primary forward a node's answer verbatim instead of decoding and re-encoding
it, and what lets a test assert byte-equality between a local read and a node
read rather than mere equivalence.

**The node owns bytes; the primary owns meaning.** A table is loaded under a
read spec that arrives with the request. The node never decides which column is
the cell id, never remembers a project, and never writes anything outside the
files it was pointed at.

**Nothing is exempt from the token.** Health included -- a same-machine
neighbour is precisely who a node token is keeping out, which is the same
posture `plexora/__init__.py` takes for the Open OnDemand bind.
"""

from __future__ import annotations

import gzip
import hmac
import io
import json

from flask import Blueprint, Response, current_app, jsonify, request, send_file

from plexora.server.node import resources as node_resources
from plexora.server.providers import wire
from plexora.server.providers.base import ResourceError
from plexora.server.providers.http import (
    API_HEADER,
    GENERATION_HEADER,
    RESOURCE_HEADER,
    TOKEN_HEADER,
)
from plexora.server.providers.operations import (
    UnknownOperation,
    run_table_operation,
    run_table_stream,
)

API_VERSION = 1

node_bp = Blueprint("plexora_node", __name__, url_prefix="/node/v1")


# -- the guard ------------------------------------------------------------


@node_bp.before_request
def require_node_token():
    """Constant-time token check on every request, including OPTIONS.

    Two ways in, and both exist for a reason. The primary sends a header, which
    keeps the secret out of access logs. A browser talking to this node
    directly sends `?t=`, because a custom header would make every tile request
    a CORS preflight -- doubling the round trips on the one path where latency
    is the product.
    """
    expected = current_app.config.get("PLEXORA_NODE_TOKEN") or ""
    if not expected:
        # A node with no token is refused at startup, not here -- see
        # `node/app.py`. Reaching this would mean the app was built by hand.
        return _refuse("this node has no token configured")
    if request.method == "OPTIONS":
        # A preflight carries neither header nor query parameter by
        # construction; the browser has not been allowed to send them yet.
        # Answering it reveals nothing but the CORS policy itself.
        return _preflight()
    supplied = request.headers.get(TOKEN_HEADER) or request.args.get("t") or ""
    if hmac.compare_digest(str(supplied), str(expected)):
        return None
    return _refuse("wrong or missing node token")


def _refuse(message):
    return (
        jsonify(success=False, error=message),
        403,
        {"Content-Type": "application/json", API_HEADER: str(API_VERSION)},
    )


@node_bp.after_request
def stamp(response):
    """The version, and the CORS headers a browser needs to read the answer.

    Exact-origin echo from `--allow-origin`, never `*`: this node holds
    somebody's data and a wildcard would let any page on the machine's browser
    read it with the token it found in a URL.
    """
    response.headers[API_HEADER] = str(API_VERSION)
    allowed = current_app.config.get("PLEXORA_NODE_ORIGINS") or []
    origin = request.headers.get("Origin")
    if origin and origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        # Without these the browser can read the body and not the headers, and
        # every one of these carries something a caller acts on -- the value
        # kind a buffer decodes as, the generation a cache is keyed on, the
        # ETag a conditional request needs.
        response.headers["Access-Control-Expose-Headers"] = ", ".join((
            "X-Value-Kind", "X-Cell-Count", "X-Fb-Shape", "X-Fb-Box",
            "X-Centroid-Record-Count", GENERATION_HEADER, RESOURCE_HEADER,
            API_HEADER, "ETag",
        ))
    return response


def _preflight():
    response = current_app.response_class(status=204)
    allowed = current_app.config.get("PLEXORA_NODE_ORIGINS") or []
    origin = request.headers.get("Origin")
    if origin and origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            f"Content-Type, If-None-Match, {TOKEN_HEADER}")
        # Once per URL per session rather than once per request. The GETs are
        # CORS-simple and never preflight at all; this covers the handful of
        # POSTs.
        response.headers["Access-Control-Max-Age"] = "86400"
    return response


# -- errors, in one shape -------------------------------------------------


@node_bp.errorhandler(node_resources.UnknownResource)
def _unknown_resource(exc):
    return jsonify(success=False, error=str(exc)), 404


@node_bp.errorhandler(UnknownOperation)
def _unknown_operation(exc):
    return jsonify(success=False, error=str(exc)), 501


@node_bp.errorhandler(ResourceError)
def _resource_error(exc):
    return jsonify(success=False, error=str(exc)), 409


# -- handshake ------------------------------------------------------------


def _registry():
    return current_app.config["PLEXORA_NODE_RESOURCES"]


@node_bp.route("/hello")
def hello():
    """What this node is and what it serves.

    Also the reachability probe, which is why it lists everything in one
    response: a primary attaching a resource wants the catalogue, and a primary
    checking liveness wants one small authenticated GET, and making those two
    requests would mean two round trips for a question that has one answer.
    """
    from plexora import cli
    from plexora.server.providers.operations import registered_operations

    return jsonify(
        api_version=API_VERSION,
        plexora_version=cli.version_string(),
        node_id=current_app.config.get("PLEXORA_NODE_ID"),
        capabilities=registered_operations(),
        resources=[resource.describe() for resource in _registry().all()],
    )


@node_bp.route("/health")
def health():
    return jsonify(ok=True, node_id=current_app.config.get("PLEXORA_NODE_ID"))


def _stamped(response, resource):
    response.headers[RESOURCE_HEADER] = resource.id
    response.headers[GENERATION_HEADER] = str(resource.generation)
    return response


def _table(resource_id):
    resource = _registry().get(resource_id, kind="table")
    if not resource.loaded:
        raise ResourceError(
            f"table {resource_id!r} has not been loaded on this node yet; the "
            f"primary must POST its read spec first")
    return resource


# -- table ----------------------------------------------------------------


@node_bp.route("/table/<resource_id>/load", methods=["POST"])
def table_load(resource_id):
    """Read (or re-read) this table under the spec the primary just sent."""
    resource = _registry().get(resource_id, kind="table")
    body = request.get_json(silent=True) or {}
    result = node_resources.load_table(
        resource, body.get("spec") or {}, reload=bool(body.get("reload")))
    return _stamped(jsonify(success=True, **result), resource)


@node_bp.route("/table/<resource_id>/inspect")
def table_inspect(resource_id):
    """What this file offers, before anything has been decided about it.

    The same document `adapters/inspection` produces locally, so the primary's
    column and role screens work unchanged against a file they cannot open --
    which is the point: the questions ("which column is the cell id?") are the
    project's and are answered on the primary, while only the looking happens
    here. Needs no prior load, because there is nothing yet to load it under.
    """
    from plexora.server.models.adapters import detect_data_type
    from plexora.server.models.adapters import inspection as data_inspection
    from plexora.server.models.adapters.spatialdata_adapter import (
        list_spatialdata_tables,
    )

    resource = _registry().get(resource_id, kind="table")
    table = request.args.get("table") or None
    data_type = detect_data_type(resource.path)
    document = {"data_type": data_type}
    if data_type == "spatialdata":
        tables = list_spatialdata_tables(resource.path)
        document["tables"] = tables
        names = [entry["name"] for entry in tables]
        if table not in names:
            if len(names) != 1:
                # Nothing else is answerable yet -- which matrix and which
                # subset both depend on which table. The primary asks again
                # with the table once the user has picked one, exactly as the
                # local import screen does.
                return _stamped(_json(document), resource)
            table = names[0]
        document["table"] = table
        inspected = data_inspection.inspect_spatialdata_table(resource.path, table)
    elif data_type == "csv":
        inspected = data_inspection.inspect_csv(resource.path)
    else:
        inspected = data_inspection.inspect_anndata(resource.path)
    document.update(inspected)
    document["proposed"] = data_inspection.propose_read_spec(inspected)
    return _stamped(_json(document), resource)


@node_bp.route("/table/<resource_id>/describe")
def table_describe(resource_id):
    resource = _table(resource_id)
    with resource.lock:
        description = resource.provider.describe()
    return _stamped(_json(description), resource)


@node_bp.route("/table/<resource_id>/all_cells")
def table_all_cells(resource_id):
    """Whole columns as one gzipped buffer -- the /get_all_cells wire shape.

    Byte-identical to what the primary serves for a local table, deliberately:
    it is what lets the primary hand a browser the node's bytes unchanged, and
    what lets a test compare the two with `==` rather than with a tolerance.
    """
    resource = _table(resource_id)
    columns = [c for c in (request.args.get("columns") or "").split(",") if c]
    data_type = int if request.args.get("dtype") == "integer" else float
    with resource.lock:
        array = resource.provider.all_cells(columns, data_type)
    content = gzip.compress(array.tobytes("C"))
    response = current_app.response_class(content)
    response.headers.set("Content-Type", "application/octet-stream")
    response.headers["Content-Length"] = len(content)
    response.headers["Content-Encoding"] = "gzip"
    return _stamped(response, resource)


@node_bp.route("/table/<resource_id>/geometry")
def table_geometry(resource_id):
    """The columns the primary keeps its own copy of: ids, coordinates, roles.

    Arrow IPC, so a text column arrives as text. This is the one payload that
    is proportional to the table's row count and is fetched once per load --
    everything else is either bounded or asked for a column at a time.
    """
    resource = _table(resource_id)
    columns = [c for c in (request.args.get("columns") or "").split(",") if c]
    with resource.lock:
        payload = resource.provider.geometry(columns)
    response = current_app.response_class(
        payload, mimetype="application/vnd.apache.arrow.file")
    return _stamped(response, resource)


@node_bp.route("/table/<resource_id>/columns")
def table_columns(resource_id):
    """Named columns as float32, for range queries. One frame, not one each."""
    resource = _table(resource_id)
    names = [c for c in (request.args.get("names") or "").split(",") if c]
    with resource.lock:
        columns = resource.provider.filter_columns(names)
    return _stamped(_frame(wire.pack_columns(columns)), resource)


@node_bp.route("/table/<resource_id>/metadata_column")
def table_metadata_column(resource_id):
    """One annotation column, aligned with the loaded table.

    Numbers come back as raw bytes and text as JSON -- see providers/wire.py
    for why that split is the honest one rather than a shortcut.
    """
    resource = _table(resource_id)
    column = request.args.get("column") or ""
    with resource.lock:
        try:
            values = resource.provider.metadata_column(column)
        except KeyError:
            return jsonify(success=False, error=f"no column named {column!r}"), 404
    return _stamped(_frame(wire.pack_array(
        values.values,
        name=values.name,
        categories=list(values.categories) if values.categories else None,
    )), resource)


@node_bp.route("/table/<resource_id>/rows")
def table_rows(resource_id):
    """Whole rows by cell id. One row, for a hover."""
    resource = _table(resource_id)
    raw = [value for value in (request.args.get("ids") or "").split(",") if value]
    with resource.lock:
        rows = resource.provider.rows(raw)
    return _stamped(_json({"rows": rows}), resource)


@node_bp.route("/table/<resource_id>/op/<path:operation>", methods=["POST"])
def table_operation(resource_id, operation):
    """Run a registered table operation here, where the file is.

    The result is returned as data, refusals included: `ColumnExists` carries a
    list of taken names and a suggestion, and that is something the user acts
    on rather than an error to translate twice.
    """
    resource = _table(resource_id)
    payload = request.get_json(silent=True) or {}
    with resource.lock:
        dataset = node_resources.dataset_for(resource)
        result = run_table_operation(operation, dataset, payload)
    return _stamped(_json({"result": result}), resource)


@node_bp.route("/table/<resource_id>/stream/<path:operation>", methods=["POST"])
def table_stream_operation(resource_id, operation):
    """Run a streaming table operation, forwarding its chunks as they come.

    Not wrapped in `stream_with_context`: the generator holds the resource lock
    for its whole life, which is what stops a reload from swapping the frame
    halfway through an export.
    """
    resource = _table(resource_id)
    payload = request.get_json(silent=True) or {}

    def chunks():
        with resource.lock:
            dataset = node_resources.dataset_for(resource)
            for piece in run_table_stream(operation, dataset, payload):
                yield piece if isinstance(piece, bytes) else str(piece).encode("utf-8")

    response = current_app.response_class(chunks(), mimetype="text/csv")
    return _stamped(response, resource)


# -- segmentation ---------------------------------------------------------


@node_bp.route("/seg/<resource_id>/tile/<level>/<tile>")
def seg_tile(resource_id, level, tile):
    """One label tile, as the PNG the viewer's shader reads label ids out of."""
    resource = _registry().get(resource_id, kind="segmentation")
    encoded, mimetype = _seg_tile_bytes(resource, level, tile)
    return _stamped(_image(encoded, mimetype, _etag(resource, "seg", level, tile)),
                    resource)


def _seg_tile_bytes(resource, level, tile):
    from plexora.server.models import data_model

    with resource.lock:
        pyramid = _opened(resource)
        array = data_model.read_tile(pyramid, None, level, tile, *_tile_size())
    return data_model.encode_tile_array(array, True, "png")


# -- image ----------------------------------------------------------------


@node_bp.route("/image/<resource_id>/geometry")
def image_geometry(resource_id):
    from plexora.server.providers import local as local_providers

    resource = _registry().get(resource_id, kind="image")
    return _stamped(_json(local_providers.image_geometry(resource.path)), resource)


@node_bp.route("/image/<resource_id>/ome_metadata")
def image_ome_metadata(resource_id):
    resource = _registry().get(resource_id, kind="image")
    with resource.lock:
        _opened(resource)
        metadata = resource.opened_metadata
    if hasattr(metadata, "model_dump"):
        metadata = metadata.model_dump(mode="json")
    elif hasattr(metadata, "dict"):
        metadata = metadata.dict()
    elif not metadata:
        metadata = {}
    return _stamped(
        current_app.response_class(json.dumps(metadata), mimetype="application/json"),
        resource)


@node_bp.route("/image/<resource_id>/tile/<channel>/<level>/<tile>")
def image_tile(resource_id, channel, level, tile):
    """One channel tile, encoded exactly as the primary would encode it.

    The quantization window comes from this node's own full-resolution read and
    is cached here, so the bytes are the same bytes a local read would produce
    -- which is what makes forwarding them verbatim correct rather than merely
    convenient.
    """
    from plexora.server.models import data_model

    resource = _registry().get(resource_id, kind="image")
    quality = request.args.get("q", "webp")
    index = _channel_index(channel)
    with resource.lock:
        pyramid = _opened(resource)
        array = data_model.read_tile(pyramid, index, level, tile, *_tile_size())
        qmin, qmax = _quantization(resource, index)
    encoded, mimetype = data_model.encode_tile_array(array, False, quality, qmin, qmax)
    return _stamped(
        _image(encoded, mimetype, _etag(resource, "tile", channel, level, tile, quality)),
        resource)


@node_bp.route("/image/<resource_id>/overview")
def image_overview(resource_id):
    from plexora.server.models import data_model

    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    with resource.lock:
        _opened(resource)
        qmin, qmax = _quantization(resource, index)
        encoded = data_model.encode_overview(resource.opened_overview[index], qmin, qmax)
    return _stamped(
        _image(encoded, "image/webp", _etag(resource, "overview", index)), resource)


@node_bp.route("/image/<resource_id>/stats")
def image_stats(resource_id):
    from plexora.server.models import data_model

    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    with resource.lock:
        _opened(resource)
        qmin, qmax = _quantization(resource, index)
        stats = _cached(resource, ("stats", index), lambda: data_model.channel_stats_of(
            resource.opened_overview[index], qmin, qmax))
    return _stamped(_json(stats), resource)


@node_bp.route("/image/<resource_id>/gmm")
def image_gmm(resource_id):
    from plexora.server.models import data_model

    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    with resource.lock:
        _opened(resource)
        qmin, qmax = _quantization(resource, index)
        packet = _cached(resource, ("gmm", index), lambda: data_model.channel_gmm_of(
            resource.opened_overview[index], qmin, qmax))
    return _stamped(_json(packet), resource)


@node_bp.route("/image/<resource_id>/quantization")
def image_quantization(resource_id):
    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    with resource.lock:
        _opened(resource)
        qmin, qmax = _quantization(resource, index)
    return _stamped(_json({"qmin": qmin, "qmax": qmax}), resource)


@node_bp.route("/image/<resource_id>/region", methods=["POST"])
def image_region(resource_id):
    """A rectangle of source pixels at a chosen level.

    Quick Edit's read, and the one thing no tile API can express -- the tile
    routes answer in the viewer's quantised, screen-sized terms and this needs
    the numbers.
    """
    import numpy as np

    resource = _registry().get(resource_id, kind="image")
    body = request.get_json(silent=True) or {}
    level = int(body.get("level") or 0)
    box = [int(value) for value in body.get("box") or (0, 0, 0, 0)]
    indices = [int(value) for value in body.get("channels") or []]
    limit = int(body.get("max_pixels") or 0)

    with resource.lock:
        pyramid = _opened(resource)
        plane = (pyramid if hasattr(pyramid, "shape") else pyramid[str(level)])
        height, width = plane.shape[-2], plane.shape[-1]
        # Clipped HERE, against the level's real dimensions, and the clipped
        # box travels back with the pixels. The caller cannot do this: it would
        # have to guess each level's size from a halving rule that real
        # pyramids do not always follow.
        x0 = max(0, min(box[0], width))
        y0 = max(0, min(box[1], height))
        x1 = max(x0, min(box[2], width))
        y1 = max(y0, min(box[3], height))
        if limit and (x1 - x0) * (y1 - y0) * max(1, len(indices)) > limit:
            raise ResourceError(
                "this region covers more of the image than one read can carry; "
                "ask for a lower resolution")
        if x1 <= x0 or y1 <= y0:
            stack = np.zeros((max(1, len(indices)), 1, 1), dtype=np.float32)
        else:
            stack = np.stack([np.asarray(plane[index, y0:y1, x0:x1])
                              for index in indices])
    return _stamped(_frame(wire.pack_array(stack, box=[x0, y0, x1, y1],
                                           channels=indices, level=level)),
                    resource)


# -- shared plumbing ------------------------------------------------------


def _tile_size():
    """The tile grid this request is addressed in.

    Sent by the caller rather than decided here, because the grid is a property
    of the PROJECT -- `tileWidth`/`tileHeight` were recorded when the image was
    registered -- and a node that picked its own would be answering a different
    question than the one the viewer asked.
    """
    try:
        return (int(request.args.get("tw") or 1024),
                int(request.args.get("th") or 1024))
    except ValueError:
        raise ResourceError("tw and th must be whole numbers of pixels")


def _opened(resource):
    """This resource's open pyramid, opened once and kept.

    A node holds files open across requests for the same reason the primary
    does: reopening a pyramidal TIFF is a directory walk, and a pan is a burst
    of tile requests against the same file.
    """
    if getattr(resource, "opened", None) is None:
        if resource.kind == "image":
            channels, overview, metadata = resource.provider.open()
            resource.opened = channels
            resource.opened_overview = overview
            resource.opened_metadata = metadata
        else:
            resource.opened = resource.provider.open()
        resource.derived = {}
        if resource.generation == 0:
            resource.generation = 1
    return resource.opened


def _cached(resource, key, compute):
    derived = getattr(resource, "derived", None)
    if derived is None:
        derived = resource.derived = {}
    if key not in derived:
        derived[key] = compute()
    return derived[key]


def _quantization(resource, index):
    """(qmin, qmax) for one channel, from full-resolution data, cached.

    Cached on the node rather than requested from the primary, because the
    read it stands for is the expensive one -- every pixel of the plane -- and
    it is the node that has the pixels.
    """
    from plexora.server.models import data_model

    return _cached(resource, ("qwindow", index),
                   lambda: data_model.quantization_window_of(resource.opened, index))


def _channel_index(channel):
    """The pyramid index a tile URL names.

    Tile URLs carry `<file>_<N>`, which is what the viewer already builds, so a
    node parses the same string the primary's tile route does rather than
    inventing a second spelling.
    """
    from plexora.server.models.data_model import _parse_channel

    index, is_segmentation = _parse_channel(channel)
    if is_segmentation:
        raise ResourceError(f"{channel!r} does not name a channel index")
    return index


def _channel_index_arg():
    raw = request.args.get("channel_index")
    if raw is None or raw == "":
        raise ResourceError("channel_index is required")
    return int(raw)


def _etag(resource, *parts):
    joined = "-".join(str(part) for part in parts)
    node_id = current_app.config.get("PLEXORA_NODE_ID")
    return f'"{node_id}:{resource.id}:{resource.generation}-{joined}"'


def _image(encoded, mimetype, etag):
    """A binary answer with the same caching contract the primary's routes use.

    The ETag embeds the resource's generation rather than the process's, which
    is the whole reason generations are per resource: a table reloading must
    not make an image node's tiles look stale.
    """
    if etag and request.headers.get("If-None-Match") == etag:
        response = current_app.response_class(status=304)
    else:
        response = send_file(io.BytesIO(encoded), mimetype=mimetype)
    if etag:
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, max-age=31536000"
    return response


def _json(data):
    import orjson

    return current_app.response_class(
        response=orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY),
        mimetype="application/json",
    )


def _frame(payload):
    return current_app.response_class(payload, mimetype=wire.CONTENT_TYPE)
