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

import contextlib
import gzip
import hmac
import io
import json
import threading
from collections import OrderedDict

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


def _ready(resource):
    """The resource, if it is in a state to answer questions about bytes.

    A mask added while the node runs may still be converting -- see
    `node_resources.PREPARING`. Serving a tile of a half-written pyramid would
    produce a picture rather than an error, which is the failure mode this
    whole state exists to make impossible.
    """
    if resource.state == node_resources.PREPARING:
        raise ResourceError(
            f"{resource.id!r} is still being prepared on this node -- poll "
            f"/node/v1/resources/{resource.id}/status")
    if resource.state == node_resources.ERROR:
        raise ResourceError(
            f"{resource.id!r} could not be prepared on this node: "
            f"{resource.error}")
    return resource


def _table(resource_id):
    resource = _ready(_registry().get(resource_id, kind="table"))
    if not resource.loaded:
        raise ResourceError(
            f"table {resource_id!r} has not been loaded on this node yet; the "
            f"primary must POST its read spec first")
    return resource


# -- what this node serves, while it is running ---------------------------


def _dynamic_or_403():
    """None when this node accepts runtime changes, a 403 response when not.

    Named in the refusal, because "403" on its own sends somebody looking for a
    wrong token when the answer is a flag they did not pass.
    """
    if current_app.config.get("PLEXORA_NODE_DYNAMIC"):
        return None
    return jsonify(
        success=False,
        error="this node was started without --dynamic, so it serves only the "
              "resources named on its command line",
    ), 403


def _persist():
    """Write the manifest, if this node keeps one. Never fatal."""
    path = current_app.config.get("PLEXORA_NODE_MANIFEST")
    if path:
        node_resources.save_manifest(path, _registry())


@node_bp.route("/resources", methods=["POST"])
def add_resource():
    """Serve a file this node was not started with.

    The case this exists for: the user is on their laptop looking at a viewer
    running on a cluster, and picks a file that is HERE. Nothing could have
    named it at startup -- the node started when the session did, and the file
    was chosen minutes later.

    A segmentation mask comes back as `preparing`: it may need converting into
    a tiled label pyramid before a single tile can be served, and on a
    whole-slide mask that is minutes. The conversion runs on a thread and the
    caller polls `status`, rather than this request hanging until it is done.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal

    body = request.get_json(silent=True) or {}
    kind = str(body.get("kind") or "").strip().lower()
    resource_id = str(body.get("id") or "").strip()
    path = node_resources.unquote_path(body.get("path") or "")
    if not path:
        raise ResourceError("a resource needs a path on this machine")

    resource = _registry().add(kind, resource_id, path)
    if kind == "segmentation" and not resource.prepared:
        _prepare_in_background(resource)
    elif kind == "image":
        # Same reasoning as the warm-up at start-up (see app.warm_resources):
        # the user who just shared this image is about to look at it, and the
        # pyramid open and per-channel quantization reads should not happen
        # inside their first zoom. Backgrounded, so this request still returns
        # in the next millisecond.
        _warm_in_background(resource)
    _persist()
    return _stamped(jsonify(success=True, resource=resource.describe()), resource)


def _warm_in_background(resource):
    """Open a freshly shared image and read its quantization windows."""
    from plexora.server.node import app as node_app

    registry = _OneResource(resource)
    node_app.warm_resources(registry, log=lambda *_a, **_k: None)


class _OneResource:
    """The slice of `Registry` that `warm_resources` actually uses.

    Warming one resource and warming a node's whole catalogue are the same
    walk over the same list, so they share an implementation rather than
    growing a second one that could drift from it.
    """

    __slots__ = ("_resource",)

    def __init__(self, resource):
        self._resource = resource

    def all(self):
        return [self._resource]


@node_bp.route("/resources/<resource_id>/status")
def resource_status(resource_id):
    """Whether this resource can be read yet, and why not if it cannot.

    Not behind `--dynamic`: describing what this node already serves is what
    `/hello` does for every resource at once, and a caller that has just been
    handed a `preparing` needs somewhere to ask again.
    """
    resource = _registry().get(resource_id)
    return _stamped(jsonify(success=True, resource=resource.describe()), resource)


@node_bp.route("/resources/<resource_id>", methods=["DELETE"])
def remove_resource(resource_id):
    """Stop serving one resource. Nothing on disk is touched.

    The node was pointed at somebody's file and never given permission to
    delete it -- and the pyramid a mask conversion left beside it may be what
    another project is reading.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal
    removed = _registry().remove(resource_id)
    _persist()
    return jsonify(success=True, removed=removed)


@node_bp.route("/browse", methods=["POST"])
def browse():
    """Open a file dialog on THIS machine, and answer with what was chosen.

    The dialog belongs here because this is where the desktop is. On the layout
    that matters the viewer runs on a compute node with no display at all,
    while this process runs on the laptop the user is sitting in front of --
    so "Browse..." can only mean anything if the dialog opens over here.

    Behind `--dynamic` for the same reason adding a resource is: it lets the
    token holder walk this account's filesystem. What it does NOT do is read
    anything -- the only path that leaves is one a person picked in a dialog
    they were looking at.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal

    from plexora.server.utils import native_dialog

    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode") or "file")
    if mode not in ("file", "directory"):
        raise ResourceError("mode must be 'file' or 'directory'")
    file_filter = str(body.get("filter") or "any")
    if file_filter not in native_dialog.FILTER_NAMES:
        raise ResourceError(f"unknown file filter: {file_filter}")
    if not native_dialog.available():
        raise ResourceError(
            "this machine has no desktop to open a file dialog on")

    try:
        # None is the honest answer to "the user pressed Cancel", and travels
        # as one: the caller leaves the field alone rather than clearing it.
        return jsonify(success=True, path=native_dialog.browse_for_path(
            mode=mode, file_filter=file_filter))
    except RuntimeError as exc:
        raise ResourceError(str(exc)) from exc


@node_bp.route("/list_dir", methods=["POST"])
def list_dir():
    """One directory on THIS machine, listed.

    The counterpart of `/browse` for a machine with no desktop, which is the
    ordinary state of a compute node and of every host somebody keeps their
    images on. Without it, "Remote" in a data form could only ever mean "type
    the full path from memory" -- and the paths on a cluster are exactly the
    ones nobody remembers.

    Behind `--dynamic` for the same reason `/browse` is: it lets the token
    holder walk this account's filesystem. What it does not do is read
    anything -- names, sizes, and which entries are directories.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal

    from plexora.server.utils import dir_listing

    body = request.get_json(silent=True) or {}
    try:
        found = dir_listing.listing(
            body.get("path") or "", show_hidden=bool(body.get("show_hidden")))
    except dir_listing.ListingError as exc:
        raise ResourceError(str(exc)) from exc
    return jsonify(success=True, **found)


def _prepare_in_background(resource):
    """Convert a freshly shared mask, off the request thread.

    The manifest is rewritten when it lands, because the conversion is what
    makes the resource cheap to restore: a node coming back finds the derived
    pyramid beside the mask and adopts it instead of doing this again.
    """
    import threading

    with resource.lock:
        if resource.prepared or resource.state == node_resources.PREPARING:
            # Two shares of the same mask racing. One conversion is enough, and
            # two writing the same pyramid would be worse than wasteful.
            return
        resource.state = node_resources.PREPARING
        resource.error = None
    app = current_app._get_current_object()

    def run():
        from plexora.server.node import app as node_app

        try:
            node_app._make_mask_servable(resource, log=lambda *_a, **_k: None)
        except Exception as exc:
            resource.state = node_resources.ERROR
            resource.error = str(exc)
            return
        resource.state = node_resources.READY
        resource.error = None
        path = app.config.get("PLEXORA_NODE_MANIFEST")
        if path:
            node_resources.save_manifest(
                path, app.config["PLEXORA_NODE_RESOURCES"])

    threading.Thread(target=run, name=f"prepare-{resource.id}",
                     daemon=True).start()


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
    resource = _ready(_registry().get(resource_id, kind="segmentation"))
    encoded, mimetype = _seg_tile_bytes(resource, level, tile)
    return _stamped(_image(encoded, mimetype, _etag(resource, "seg", level, tile)),
                    resource)


def _seg_tile_bytes(resource, level, tile):
    from plexora.server.models import data_model

    width, height = _tile_size()

    def encode():
        with _reading(resource) as pyramid:
            array = data_model.read_tile(pyramid, None, level, tile, width, height)
        return data_model.encode_tile_array(array, True, "png")

    return _cached_tile(
        (resource.id, _open_generation(resource), "seg",
         str(level), str(tile), width, height),
        encode)


# -- image ----------------------------------------------------------------


@node_bp.route("/image/<resource_id>/geometry")
def image_geometry(resource_id):
    from plexora.server.providers import local as local_providers

    resource = _registry().get(resource_id, kind="image")
    return _stamped(_json(local_providers.image_geometry(resource.path)), resource)


@node_bp.route("/image/<resource_id>/ome_metadata")
def image_ome_metadata(resource_id):
    resource = _registry().get(resource_id, kind="image")
    with _reading(resource):
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
    width, height = _tile_size()

    def encode():
        with _reading(resource) as pyramid:
            array = data_model.read_tile(pyramid, index, level, tile, width, height)
            qmin, qmax = _quantization(resource, index)
        # Encoded outside the lock. It is pure over the array and the window,
        # which is the same property that lets the primary forward these bytes
        # without decoding them.
        return data_model.encode_tile_array(array, False, quality, qmin, qmax)

    encoded, mimetype = _cached_tile(
        (resource.id, _open_generation(resource), "tile", index,
         str(level), str(tile), quality, width, height),
        encode)
    return _stamped(
        _image(encoded, mimetype, _etag(resource, "tile", channel, level, tile, quality)),
        resource)


@node_bp.route("/image/<resource_id>/overview")
def image_overview(resource_id):
    from plexora.server.models import data_model

    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    with _reading(resource):
        qmin, qmax = _quantization(resource, index)
        encoded = data_model.encode_overview(resource.opened_overview[index], qmin, qmax)
    return _stamped(
        _image(encoded, "image/webp", _etag(resource, "overview", index)), resource)


@node_bp.route("/image/<resource_id>/stats")
def image_stats(resource_id):
    from plexora.server.models import data_model

    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    with _reading(resource):
        qmin, qmax = _quantization(resource, index)
        stats = _cached(resource, ("stats", index), lambda: data_model.channel_stats_of(
            resource.opened_overview[index], qmin, qmax))
    return _stamped(_json(stats), resource)


@node_bp.route("/image/<resource_id>/gmm")
def image_gmm(resource_id):
    from plexora.server.models import data_model

    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    # A mixture fit is a second of CPU over an already-materialized overview --
    # it touches no file. Under the old exclusive lock that second was one in
    # which no tile of any channel could be served.
    with _reading(resource):
        qmin, qmax = _quantization(resource, index)
        packet = _cached(resource, ("gmm", index), lambda: data_model.channel_gmm_of(
            resource.opened_overview[index], qmin, qmax))
    return _stamped(_json(packet), resource)


@node_bp.route("/image/<resource_id>/quantization")
def image_quantization(resource_id):
    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    with _reading(resource):
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

    with _reading(resource) as pyramid:
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


#: Guards the per-key single-flight dictionaries. Module-level and plain, for
#: the reason spelled out in `_cached`: it must have no ordering relationship
#: with the resource locks.
_COMPUTE_GUARD = threading.Lock()

#: Encoded tiles this node has already produced.
#:
#: The primary keeps one of these (`_tile_png_cache` in routes/data_routes.py)
#: and a node had none, which made the two topologies differ in a way nothing
#: intended: a project served from a node re-read and re-encoded every tile on
#: every request, while the same project on the primary answered a repeat from
#: memory. Direct routing made it worse rather than better -- the browser goes
#: straight to the node, so the primary's cache is not in the path at all, and
#: there was nothing on this side to take its place.
#:
#: Keyed by resource AND generation, so a reload invalidates by making the old
#: entries unreachable rather than by hunting them down; they then age out.
#: Sized for one viewport across a handful of channels rather than for a whole
#: session -- each entry pins its encoded bytes, so at ~50-300 KB a tile this
#: is a budget in the low hundreds of MB, and a node is frequently a laptop.
_TILE_CACHE_MAX = 600
_tile_cache = OrderedDict()
_tile_cache_lock = threading.Lock()


def _open_generation(resource):
    """This resource's generation, with the pyramid guaranteed open.

    Read BEFORE a tile cache key is built, because opening is what sets the
    first generation: a key built beforehand files the tile under generation 0
    and the next request, arriving after the open, never finds it. That is a
    cache that stores everything and returns nothing, which looks exactly like
    no cache at all -- so it is worth the extra lock acquisition to make the
    ordering explicit rather than incidental.
    """
    with _reading(resource):
        return resource.generation


def _cached_tile(key, encode):
    """(bytes, mimetype) for one tile, from memory when it is there.

    Two requests missing on the same tile both encode it, exactly as they do on
    the primary. Holding a lock across the read-and-encode instead would make
    every OTHER tile wait on this one, which is the trade the whole readers-
    writer change was made to avoid.
    """
    with _tile_cache_lock:
        hit = _tile_cache.get(key)
        if hit is not None:
            _tile_cache.move_to_end(key)
            return hit
    value = encode()
    with _tile_cache_lock:
        _tile_cache[key] = value
        _tile_cache.move_to_end(key)
        while len(_tile_cache) > _TILE_CACHE_MAX:
            _tile_cache.popitem(last=False)
    return value


def _opened(resource):
    """This resource's open pyramid, opened once and kept.

    A node holds files open across requests for the same reason the primary
    does: reopening a pyramidal TIFF is a directory walk, and a pan is a burst
    of tile requests against the same file.

    Callers must already hold the resource's WRITE lock -- this mutates. Read
    paths go through `_reading`, which takes the write lock only on the one
    request that finds the file shut.
    """
    if getattr(resource, "opened", None) is None:
        if resource.kind == "image":
            channels, overview, metadata = resource.provider.open()
            # `opened` last, and deliberately: `_reading` tests it without a
            # lock, so it must never be visible while its two companions are
            # still from the previous open.
            resource.opened_overview = overview
            resource.opened_metadata = metadata
            resource.opened = channels
        else:
            resource.opened = resource.provider.open()
        resource.derived = {}
        resource.compute_locks = {}
        if resource.generation == 0:
            resource.generation = 1
    return resource.opened


@contextlib.contextmanager
def _reading(resource):
    """Shared access to this resource's open pyramid.

    Yields the pyramid with the read lock held, having opened it first if it
    was shut. Opening is the one step that needs exclusivity, so it is done in
    its own short write section rather than by holding the write lock for the
    whole read -- which is what made every tile of every channel queue behind
    one channel's mixture fit.

    The loop is not paranoia. Between releasing the write lock and taking the
    read lock, a reload can run and shut the pyramid again; the second pass
    then reopens it. In the ordinary case -- an open pyramid -- this costs one
    attribute read and one lock acquisition.
    """
    while True:
        if resource.opened is None:
            with resource.lock:
                _opened(resource)
        resource.lock.read.acquire()
        if resource.opened is not None:
            break
        resource.lock.read.release()
    try:
        yield resource.opened
    finally:
        resource.lock.read.release()


def _cached(resource, key, compute):
    """One derived value per key, computed once even under concurrent misses.

    Single-flight matters here in a way it does not for an ordinary memo: the
    entries are quantization windows, and each one is a full-resolution read of
    an entire channel plane. Two readers racing used to mean two of those.
    """
    derived = getattr(resource, "derived", None)
    if derived is None:
        derived = resource.derived = {}
    if key in derived:
        return derived[key]

    locks = getattr(resource, "compute_locks", None)
    if locks is None:
        locks = resource.compute_locks = {}
    # Handing out the per-key lock is itself a race, so that step is guarded --
    # but NOT by `resource.lock`. This runs inside `_reading`, which holds the
    # read lock, and asking a readers-writer lock to upgrade is a deadlock
    # against itself. A plain independent lock has no such relationship, and it
    # is only ever held for a dict lookup, never around `compute()`.
    with _COMPUTE_GUARD:
        gate = locks.get(key)
        if gate is None:
            gate = locks[key] = threading.Lock()

    with gate:
        # Re-read rather than trusting the earlier miss: a reopen between the
        # two swaps `derived` for a fresh dict.
        derived = resource.derived
        if key in derived:
            return derived[key]
        value = compute()
        derived[key] = value
        return value


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
