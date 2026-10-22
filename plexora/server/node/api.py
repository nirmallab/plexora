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
import logging
import os
import threading
from collections import OrderedDict, deque
from time import perf_counter

from flask import Blueprint, Response, current_app, jsonify, request, send_file

from plexora.server.node import resources as node_resources
from plexora.server.providers import wire
from plexora.server.providers.base import ResourceError
from plexora.server.providers.http import (
    API_HEADER,
    FILE_NAME_HEADER,
    GENERATION_HEADER,
    RESOURCE_HEADER,
    TOKEN_HEADER,
    VIEW_HEADER,
)
from plexora.server.providers.operations import (
    UnknownOperation,
    run_table_operation,
    run_table_stream,
)
from plexora.server.volume.viewspec import BadView
from plexora.telemetry import node_hooks as _node_telemetry
from plexora.telemetry import performance as _perf

_log = logging.getLogger(__name__)

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
            API_HEADER, FILE_NAME_HEADER, "ETag", "Server-Timing",
        ))
        # The timing of a direct tile is the browser's measure of the link
        # (views/tileFetch.js), which it may read only when allowed to.
        response.headers["Timing-Allow-Origin"] = origin
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


@node_bp.errorhandler(BadView)
def _bad_view(exc):
    """A view this image does not have, in the tile route's own terms."""
    return jsonify(success=False, error=str(exc)), 400


@node_bp.errorhandler(Exception)
def _unexpected_error(exc):
    """Anything else, as JSON with its own sentence, never a bare 500.

    The primary reports whatever `error` says. Flask's own 500 page has none,
    so a node that could not open a DICOM slide for want of pydicom -- whose
    exception spells out the exact `pip install` that fixes it -- reached the
    user as "data node 'gcloud' failed: 500". HTTP errors keep their own
    status; they are Flask's answers, not failures.
    """
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        return exc
    current_app.logger.exception("node request failed")
    return jsonify(success=False,
                   error=str(exc) or type(exc).__name__), 500


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
    from plexora import _resources, cli, paths
    from plexora.server.providers.operations import registered_operations
    from plexora.server.utils import native_dialog

    return jsonify(
        api_version=API_VERSION,
        plexora_version=cli.version_string(),
        node_id=current_app.config.get("PLEXORA_NODE_ID"),
        capabilities=registered_operations(),
        # What kind of machine this is -- cores, memory, a GPU if there is one,
        # and how much room is left where the data would go. Descriptive only:
        # nothing routes, sizes or schedules from it, which is why every field
        # is optional and a machine that will not say is still a machine that
        # serves tiles. Additive with no API_VERSION bump, exactly like
        # `dialogs` below. Everything but the disk is worked out once per
        # process -- this route is on the health-probe path, and re-reading a
        # graphics card every few seconds is a cost nobody chose.
        machine=_resources.machine_facts(disk_path=str(paths.data_root())),
        # What kind of file dialog this machine can put on a screen -- see
        # native_dialog.dialog_kind. Stated up here rather than discovered from
        # a refusal because a refusal crossing the network arrives as prose:
        # "I have no desktop" and "my dialogs only do one kind at a time" are
        # the same "no" by the time the primary reads them, and guessing the
        # first left the listing picker standing in for the file dialog of a
        # laptop that had one.
        #
        # Additive on purpose, with no API_VERSION bump: a node too old to say
        # this omits the key, and a missing answer means the primary keeps
        # doing what it did before. Absence is the compatibility story.
        dialogs=native_dialog.dialog_kind(),
        resources=[resource.describe() for resource in _registry().all()],
        # Cumulative tile and request counts for the primary's optional
        # telemetry (plexora/telemetry/node_hooks.py); a node never uploads.
        # Absent when telemetry is off on this machine, and additive like
        # `machine` and `dialogs`.
        **_node_telemetry_block(),
    )


def _node_telemetry_block():
    block = _node_telemetry.snapshot()
    return {"telemetry": block} if block is not None else {}


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


@node_bp.route("/detect", methods=["POST"])
def detect_path():
    """What one file on THIS machine is, without serving it.

    The step before `/resources` for the one caller that cannot name a kind:
    Import Sample, where the user browsed this node's filesystem and picking
    the file IS the question. A mask and an image are the same suffix and the
    same size, and the pixels that tell them apart are on this side of the
    tunnel.

    Behind `--dynamic` for the reason `/read_file` is, and the same strength
    of reason: it opens a window of this account's files. What it hands back
    is a verdict and not bytes.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal

    body = request.get_json(silent=True) or {}
    path = node_resources.unquote_path(body.get("path") or "")
    if not path:
        raise ResourceError("a path on this machine is needed")
    return jsonify(success=True, detected=node_resources.detect_kind(path))


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

    Also what opening a project asks about its node-served mask, which makes it
    the moment a mask restored at startup starts being prepared.
    """
    resource = _registry().get(resource_id)
    _prepare_on_first_use(resource)
    return _stamped(jsonify(success=True, resource=resource.describe()), resource)


@node_bp.route("/resources/<resource_id>/prepare", methods=["POST"])
def prepare_again(resource_id):
    """Try converting a mask again -- what opening the project asks for when
    this node reports the last attempt failed.

    Behind `--dynamic` for the reason `/resources` is: it writes a pyramid.
    A mask that is ready or already converting is left alone and described,
    so a second viewer tab asking at the same moment costs nothing.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal
    resource = _registry().get(resource_id, kind="segmentation")
    if not resource.prepared:
        _prepare_in_background(resource)
    return _stamped(jsonify(success=True, resource=resource.describe()), resource)


@node_bp.route("/resources/<resource_id>/pyramidize", methods=["POST"])
def pyramidize_image(resource_id):
    """Build the pyramidized copy of a flat image this node serves.

    The import dialog's "Pyramidize and import", for an image on this
    machine: the copy is written HERE, beside the original (or under this
    node's data root when that folder is read-only), because the original is
    here and copying a stack across the network to pyramidize it would be the
    slow way round. Runs on a thread; the caller polls `status`, which says
    `preparing` with `progress = {stage, percent, label}` until the resource
    is `ready` -- served from the copy -- or `error` with a sentence.

    Behind `--dynamic`: it writes a file. An image that needs nothing, or one
    already building, is described and left alone.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal
    resource = _registry().get(resource_id, kind="image")
    if resource.pyramid and resource.state != node_resources.PREPARING:
        _pyramidize_in_background(resource)
    return _stamped(jsonify(success=True, resource=resource.describe()), resource)


def _pyramidize_in_background(resource):
    """`data_model._pyramidized_copy` for a node image, off the request thread."""
    import threading

    from plexora.server.models import data_model

    with resource.lock:
        if resource.state == node_resources.PREPARING:
            return
        resource.state = node_resources.PREPARING
        resource.error = None
        resource.progress = {"stage": "opening", "percent": 0,
                             "label": data_model.IMAGE_PYRAMID_STAGES["opening"][2]}
    app = current_app._get_current_object()

    def on_change(percent, key, message):
        resource.progress = {"stage": key, "percent": int(percent), "label": message}

    stage_, report = data_model._staged_reporter(data_model.IMAGE_PYRAMID_STAGES, on_change)

    def stage(key, detail=None):
        stage_(key, detail, sticky=detail is not None and key == "building")

    def run():
        try:
            stage("opening")
            copy = data_model._pyramidized_copy(
                resource.source_path or resource.path,
                node_resources.node_image_dir(resource.id), stage, report)
            with resource.lock:
                node_resources.serve_image_copy(resource, copy)
        except Exception as exc:  # noqa: BLE001 -- said to the user, verbatim
            resource.state = node_resources.ERROR
            resource.error = str(exc)
            resource.progress = None
            return
        resource.state = node_resources.READY
        resource.error = None
        resource.progress = None
        _warm_in_background(resource)
        path = app.config.get("PLEXORA_NODE_MANIFEST")
        if path:
            node_resources.save_manifest(path, app.config["PLEXORA_NODE_RESOURCES"])

    threading.Thread(target=run, name=f"pyramidize-{resource.id}",
                     daemon=True).start()


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
    if mode not in ("file", "directory", "any"):
        raise ResourceError("mode must be 'file', 'directory' or 'any'")
    file_filter = str(body.get("filter") or "any")
    if file_filter not in native_dialog.FILTER_NAMES:
        raise ResourceError(f"unknown file filter: {file_filter}")
    if not native_dialog.available():
        raise ResourceError(
            "this machine has no desktop to open a file dialog on")
    # "any" is one dialog taking a file OR a folder, which only macOS has. A
    # refusal here is not the end of Browse: the caller turns it into the
    # relayed listing picker, which lists THIS machine and can select either.
    if mode == "any" and not native_dialog.hybrid_available():
        raise ResourceError(
            "this machine's file dialog cannot take a folder")

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
    anything -- names, sizes, dates, and which entries are directories.
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
    return _gzipped_when_large(jsonify(success=True, **found))


#: Bytes of JSON past which an answer is gzipped for a caller that takes it --
#: the same line the viewer's own `serialize_and_submit_json` draws.
_GZIP_JSON_BYTES = 256 * 1024


def _gzipped_when_large(response):
    """`response`, gzipped when it is big and the caller accepts gzip.

    A 50,000-entry listing is ~8 MB of JSON and ~1 MB compressed, and the link
    it crosses is very often a tunnel to a laptop. The primary's pool asks for
    gzip and urllib3 undoes it on arrival, so nothing downstream sees this.
    """
    if (response.content_length or 0) <= _GZIP_JSON_BYTES or \
            "gzip" not in (request.headers.get("Accept-Encoding") or ""):
        return response
    response.set_data(gzip.compress(response.get_data(), compresslevel=3))
    response.headers["Content-Encoding"] = "gzip"
    response.vary.add("Accept-Encoding")
    return response


@node_bp.route("/stat_entries", methods=["POST"])
def stat_entries():
    """Sizes, dates and kinds for up to 500 names in one folder on THIS machine.

    What a `partial` listing left out, asked for a screenful at a time. Bare
    names only, so this can stat nothing the listing did not already name.
    Behind `--dynamic` like `/list_dir`, for the same reason.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal

    from plexora.server.utils import dir_listing

    body = request.get_json(silent=True) or {}
    try:
        found = dir_listing.stat_entries(body.get("path") or "", body.get("names"))
    except (dir_listing.ListingError, ValueError) as exc:
        raise ResourceError(str(exc)) from exc
    return jsonify(success=True, **found)


@node_bp.route("/fs_places")
def fs_places():
    """THIS machine's places for the explorer's sidebar.

    Home, Desktop/Documents/Downloads, the volumes with their labels and free
    space, the cloud folders, the data folder and a cluster's `$SCRATCH`-style
    roots -- see `fs_places.describe`, which never waits long on any mount and
    never lists a cloud folder. Behind `--dynamic`: it names this account's
    filesystem.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal

    from plexora.server.utils import fs_places as places

    return jsonify(success=True, **places.describe())


@node_bp.route("/search_dir", methods=["POST"])
def search_dir():
    """Names matching a query under one folder on THIS machine, budgeted.

    The bounds are clamped to `dir_search`'s caps whatever is asked. A third
    search while two run is a 429 rather than the usual 409, so the primary
    can tell "busy, try again" from "that folder is wrong" and say so.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal

    from plexora.server.utils import dir_listing, dir_search

    body = request.get_json(silent=True) or {}
    try:
        found = dir_search.search(
            body.get("path") or "", body.get("query"),
            show_hidden=bool(body.get("show_hidden")),
            limit=_whole(body, "limit", dir_search.SEARCH_LIMIT),
            budget_ms=_whole(body, "budget_ms", dir_search.SEARCH_BUDGET_MS),
            max_depth=_whole(body, "max_depth", dir_search.SEARCH_MAX_DEPTH))
    except dir_search.SearchBusy as exc:
        return jsonify(success=False, error=str(exc)), 429
    except (dir_listing.ListingError, ValueError, TypeError) as exc:
        raise ResourceError(str(exc)) from exc
    return jsonify(success=True, **found)


def _whole(body, field, default):
    """A whole-number field of a request body, or `default` when absent."""
    value = body.get(field)
    if value is None:
        return default
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError
        return int(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{field} must be a number") from None


# -- one file's bytes, in either direction --------------------------------
#
# What `/list_dir` stops short of. A plugin's Upload button on a machine with
# no desktop needs the file it named, and its Download button needs somewhere
# on this machine to put the result -- and the browser asking for either is on
# a third machine with no route here.
#
# Both behind `--dynamic`, for a stronger version of the reason `/browse` and
# `/list_dir` are: those let the token holder see this account's filesystem,
# and these let them read and write it. A node started without the flag serves
# exactly the resources on its command line and nothing else.


@node_bp.route("/read_file", methods=["POST"])
def read_file():
    """Send one file from THIS machine, streamed.

    The body is the file and nothing else: the primary forwards these bytes to
    the browser untouched, so wrapping them in an envelope would mean decoding
    a gigabyte on the way past. What the caller also needs -- the name to build
    a File out of -- rides in a header.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal

    from plexora.server.utils import file_transfer

    body = request.get_json(silent=True) or {}
    try:
        path, size, mimetype, name = file_transfer.open_read(body.get("path"))
    except file_transfer.TransferError as exc:
        raise ResourceError(str(exc)) from exc

    handle = open(path, "rb")

    def chunks():
        # Closed by the generator rather than by a `with` around the open,
        # which would have shut the file before Flask read a byte of it.
        with handle:
            while True:
                piece = handle.read(file_transfer.CHUNK)
                if not piece:
                    return
                yield piece

    return Response(chunks(), mimetype=mimetype, headers={
        "Content-Length": str(size),
        FILE_NAME_HEADER: name,
    })


@node_bp.route("/write_file", methods=["POST"])
def write_file():
    """Put one file onto THIS machine's filesystem.

    Directory and name arrive as query parameters because the body is the
    payload -- there is nowhere else to put them without buffering the file to
    parse a multipart envelope, which for an export of a whole cell table is
    the difference between streaming and holding it.

    An existing file is refused rather than replaced, and the refusal is a 409
    carrying `exists` so the caller can offer "Replace?" rather than making the
    user guess which of two sentences means what.
    """
    refusal = _dynamic_or_403()
    if refusal is not None:
        return refusal

    from plexora.server.utils import file_transfer

    overwrite = str(request.args.get("overwrite") or "").strip().lower() in (
        "1", "true", "yes")
    try:
        path, written = file_transfer.write_file(
            request.args.get("dir") or "", request.args.get("name") or "",
            request.stream, overwrite=overwrite)
    except file_transfer.TransferError as exc:
        # Answered here rather than through the ResourceError handler because
        # only this route knows about `exists`, and that flag is the whole
        # difference between a dead end and a question.
        return jsonify(success=False, error=str(exc), exists=exc.exists), 409

    return jsonify(success=True, path=str(path), bytes=written)


def _prepare_in_background(resource):
    """Convert a freshly shared mask, off the request thread.

    The manifest is rewritten when it lands, because the conversion is what
    makes the resource cheap to restore: a node coming back finds the derived
    pyramid beside the mask and adopts it instead of doing this again.
    """
    import threading

    with resource.lock:
        resource.prepare_on_use = False
        if resource.prepared or resource.state == node_resources.PREPARING:
            # Two shares of the same mask racing. One conversion is enough, and
            # two writing the same pyramid would be worse than wasteful.
            return
        resource.state = node_resources.PREPARING
        resource.error = None
        resource.progress = {"stage": "checking", "done": 0, "total": 0}
    app = current_app._get_current_object()
    registry = app.config.get("PLEXORA_NODE_RESOURCES")

    def report(stage, done, total):
        resource.progress = {"stage": stage, "done": int(done), "total": int(total)}

    def run():
        from plexora.server.node import app as node_app

        try:
            node_app._make_mask_servable(resource, log=lambda *_a, **_k: None,
                                         progress=report, registry=registry)
        except Exception as exc:
            resource.state = node_resources.ERROR
            resource.error = str(exc)
            resource.progress = None
            return
        resource.state = node_resources.READY
        resource.error = None
        # The user is looking at this mask right now; open the pyramid before
        # their next tile asks for it.
        _warm_in_background(resource)
        path = app.config.get("PLEXORA_NODE_MANIFEST")
        if path:
            node_resources.save_manifest(
                path, app.config["PLEXORA_NODE_RESOURCES"])

    threading.Thread(target=run, name=f"prepare-{resource.id}",
                     daemon=True).start()


def _prepare_on_first_use(resource):
    """Start preparing a mask restored from the manifest, now that something
    has asked about it (see `app._restore_manifest`).

    A flag check on every other call, which is what lets the tile routes call
    it: tiles are the first use when the primary is too old to probe status on
    open. Two requests racing here are settled by `_prepare_in_background`'s
    own guard.
    """
    if resource.prepare_on_use:
        _prepare_in_background(resource)


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
    from plexora.server.models.adapters import detect_data_type, is_flat_table
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
    elif is_flat_table(data_type):
        inspected = data_inspection.inspect_flat_table(resource.path, data_type)
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


def _licence_refusal(operation):
    """403 for an operation that serves a Pro capability, unless the primary
    sent a valid entitlement proof (plexora/licensing/tokens.py). None -- and
    nothing looked at -- for every Free operation. A node has no licence of
    its own and never contacts the BioCognia platform."""
    from plexora.licensing import manifest, tokens

    required = tokens.required_for(operation)
    if required is None:
        return None
    token = current_app.config.get("PLEXORA_NODE_TOKEN") or ""
    if tokens.verify_proof(token, request.headers.get(tokens.PROOF_HEADER), required):
        return None
    return jsonify(success=False,
                   error=f"{operation} serves a Plexora Pro feature and the primary sent no "
                         f"valid licence proof for it.",
                   license={"code": "license_required", "entitlement": required,
                            "plan_required": manifest.plan_for(required)}), 403


@node_bp.route("/table/<resource_id>/op/<path:operation>", methods=["POST"])
def table_operation(resource_id, operation):
    """Run a registered table operation here, where the file is.

    The result is returned as data, refusals included: `ColumnExists` carries a
    list of taken names and a suggestion, and that is something the user acts
    on rather than an error to translate twice.
    """
    refused = _licence_refusal(operation)
    if refused is not None:
        return refused
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
    refused = _licence_refusal(operation)
    if refused is not None:
        return refused
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
    """One label tile, as the PNG the viewer's shader reads label ids out of.

    Not behind `_ready`, unlike every other read. A mask still converting, or
    one whose conversion failed, is served from the file it was shared as:
    `data_model.read_tile` draws a missing level from the finest one below it,
    so the picture is right, only slower. The conversion never touches that
    file -- it writes a `.tmp` and renames it into place -- so reading it
    meanwhile is safe. Refusing instead left the cell layer blank for as long
    as the conversion took, and forever when it failed.
    """
    resource = _registry().get(resource_id, kind="segmentation")
    _prepare_on_first_use(resource)
    # The plane of a 3-D mask (a label volume): the same `view` token the
    # viewer puts on every label tile, whether it asks the primary (which
    # forwards it) or this node directly. A 2-D mask has one plane for every
    # view, so the token is dropped from its cache key and ETag -- otherwise
    # each plane of the image would re-fetch the identical tile.
    view = (request.args.get("view") or "").strip()
    encoded, mimetype = _seg_tile_bytes(resource, level, tile, view)
    return _stamped(_image(encoded, mimetype,
                           _etag(resource, "seg", level, tile, _seg_view_key(resource, view))),
                    resource)


def _label_volume_of(pyramid):
    """`pyramid` when it is a label volume (anything with `at_view`), else None."""
    return pyramid if hasattr(pyramid, "at_view") else None


def _seg_view_key(resource, view):
    """`view` for a mask served as a label volume, "" for a 2-D one."""
    if not view or not getattr(resource, "path", None):
        return ""
    # Held per path on the resource (`_label_volume_facts`): asked per tile.
    return view if resource._label_volume_facts() is not None else ""


def _seg_tile_bytes(resource, level, tile, view=""):
    from plexora.server.models import data_model

    width, height = _tile_size()
    view = _seg_view_key(resource, view)

    def encode():
        import numpy as np

        with _reading(resource) as pyramid:
            volume = _label_volume_of(pyramid)
            if volume is not None and view:
                try:
                    pyramid = volume.at_view(view)
                except BadView as exc:
                    raise ResourceError(str(exc)) from exc
            try:
                array = data_model.read_tile(pyramid, None, level, tile, width, height)
            except Exception:
                if resource.state != node_resources.PREPARING:
                    raise
                # The file it was shared as, while it converts, is not always
                # readable as a 2-D mask -- a 3-D TIFF or an Imaris file is
                # becoming a label volume. No cells until it lands (the
                # repoint bumps the generation, so nothing blank is kept).
                array = np.zeros((height, width, 4), dtype=np.uint8)
        _perf.mark("read")
        encoded = data_model.encode_tile_array(array, True, "png")
        _perf.mark("enc")
        return encoded

    return _cached_tile(
        (resource.id, _open_generation(resource), "seg",
         str(level), str(tile), width, height, view),
        encode)


@node_bp.route("/seg/<resource_id>/region", methods=["POST"])
def seg_region(resource_id):
    """Labels for a rectangle at a chosen level, as uint32.

    The label twin of `image_region`, with one difference on purpose: the
    answer is PADDED to the box asked for (zeros where it runs off the mask),
    not clipped. That is what `label_overlay.padded_label_region` does on the
    primary for a local mask, and doing the same here is what makes a render
    against a node-hosted mask byte for byte the render against the local
    file. Not behind `_ready`, for the reason `seg_tile` gives.
    """
    import numpy as np

    from plexora.server.utils.label_overlay import padded_label_region

    resource = _registry().get(resource_id, kind="segmentation")
    _prepare_on_first_use(resource)
    body = request.get_json(silent=True) or {}
    level = int(body.get("level") or 0)
    box = [int(value) for value in body.get("box") or (0, 0, 0, 0)]
    limit = int(body.get("max_pixels") or 0)
    area = max(0, box[2] - box[0]) * max(0, box[3] - box[1])
    if limit and area > limit:
        raise ResourceError(
            "this region covers more of the mask than one read can carry; "
            "ask for a lower resolution")
    with _reading(resource) as pyramid:
        volume = _label_volume_of(pyramid)
        if volume is not None and (body.get("z") is not None or body.get("t") is not None):
            # A plane of a 3-D mask, as `LocalSegmentationProvider.read_region`
            # reads one; no z is the frame's default plane.
            frame = volume.frame(int(body.get("t") or 0))
            z = body.get("z")
            pyramid = frame.plane(int(z)) if z is not None else frame.default_plane()
        labels = padded_label_region(pyramid, level, box)
    return _stamped(_frame(wire.pack_array(np.ascontiguousarray(labels, dtype=np.uint32),
                                           box=box, level=level)),
                    resource)


#: The most label ids one block read carries: (Z, H, W) uint32 at 4 bytes each,
#: so 512 MB -- an XZ/YZ section through a whole stack, or a measurement's
#: chunk of planes, and not an accidental whole-volume download.
BLOCK_MAX_VOXELS = 128 * 1024 * 1024


@node_bp.route("/seg/<resource_id>/block", methods=["POST"])
def seg_block(resource_id):
    """`(Z, H, W)` label ids of a 3-D mask: planes z0..z1 inclusive of frame
    `t` at `level`, over `rows`/`cols` (half-open [start, stop], clipped to
    the level) -- `LabelVolume.read`, for the primary's `NodeLabelVolume`.
    Measurement, sections and QC in depth read a node's mask through this.
    """
    import numpy as np

    resource = _registry().get(resource_id, kind="segmentation")
    _prepare_on_first_use(resource)
    body = request.get_json(silent=True) or {}
    level = int(body.get("level") or 0)
    with _reading(resource) as pyramid:
        volume = _label_volume_of(pyramid)
        if volume is None:
            raise ResourceError("this mask is one 2-D segmentation, not a label volume; "
                                "it has no planes to read a block of")
        volume = volume.frame(int(body.get("t") or 0))
        if not 0 <= level < volume.levels:
            raise ResourceError(f"level {level} is outside this mask's {volume.levels} levels")
        height, width = volume.level_shapes()[level]
        z0 = max(0, min(volume.depth - 1, int(body.get("z0") or 0)))
        z1 = int(body["z1"]) if body.get("z1") is not None else z0
        z1 = max(z0, min(volume.depth - 1, z1))
        rows = slice(*slice(*(body.get("rows") or [None, None])).indices(height)[:2])
        cols = slice(*slice(*(body.get("cols") or [None, None])).indices(width)[:2])
        voxels = (z1 - z0 + 1) * max(0, rows.stop - rows.start) * max(0, cols.stop - cols.start)
        if voxels > BLOCK_MAX_VOXELS:
            raise ResourceError("this block covers more of the mask than one read can "
                                "carry; ask for fewer planes or a lower resolution")
        block = volume.read(level, z0, z1, rows, cols)
    return _stamped(_frame(wire.pack_array(np.ascontiguousarray(block, dtype=np.uint32),
                                           level=level, z0=z0, z1=z1)),
                    resource)


# -- 3-D objects, measured where the pixels are ---------------------------
#
# A z-stack's objects are measured against the whole stack, plane by plane, so
# the measurement runs on the node that holds the image and its label volume
# (`volume.objects.measure_columns`, the same code the primary runs for a local
# image) and only the table travels. A job, polled, because it is minutes to
# hours: progress goes back to the primary's task and a cancel stops it.

#: job id -> {"state", "stage", "done", "total", "error", "manifest", "meta",
#: "result", "planes", "cancel"}: a measurement's or a surface build's
#: (`planes` is a measurement's per-plane cell index, server/volume/planes.py).
_JOBS: "OrderedDict[str, dict]" = OrderedDict()
_JOBS_LOCK = threading.Lock()
#: Finished jobs kept for their result; the oldest go first.
JOBS_KEPT = 8


def _job(job_id) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(str(job_id))
    if job is None:
        raise ResourceError(f"no job {job_id!r} on this node")
    return job


def _semantics(body):
    """The primary's z/t reading of the stack, checked."""
    from plexora.server.volume.model import T_SEMANTICS, Z_SEMANTICS

    zs = body.get("zs") or None
    ts = body.get("ts") or None
    if zs is not None and zs not in Z_SEMANTICS:
        raise ResourceError(f"zs is one of {', '.join(Z_SEMANTICS)}, not {zs!r}")
    if ts is not None and ts not in T_SEMANTICS:
        raise ResourceError(f"ts is one of {', '.join(T_SEMANTICS)}, not {ts!r}")
    return zs, ts


def _start_job(name, work):
    """Run `work(job)` on its own thread as a polled job; the job's id.
    `work` sets `result` (bytes) and whatever `meta`/`manifest` it reports,
    and may raise InterruptedError for a cancel."""
    import secrets

    job_id = secrets.token_hex(8)
    job = {"state": "running", "stage": None, "done": 0, "total": 0, "error": None,
           "manifest": None, "meta": None, "result": None, "planes": None,
           "cancel": threading.Event()}

    def run():
        try:
            work(job)
            job["state"] = "done"
        except InterruptedError:
            job["state"] = "cancelled"
        except Exception as exc:  # noqa: BLE001 -- said to the primary, not raised
            job.update(state="error", error=str(exc) or type(exc).__name__)

    with _JOBS_LOCK:
        _JOBS[job_id] = job
        while len(_JOBS) > JOBS_KEPT:
            oldest = next(iter(_JOBS))
            if _JOBS[oldest]["state"] == "running":
                break
            _JOBS.pop(oldest)
    threading.Thread(target=run, name=f"plexora-{name}-{job_id}", daemon=True).start()
    return job_id


def _progress_of(job):
    def progress(done, total):
        job["done"], job["total"] = int(done), int(total)
    return progress


@node_bp.route("/volume/measure", methods=["POST"])
def volume_measure():
    """Start measuring a label volume's objects against an image's planes."""
    body = request.get_json(silent=True) or {}
    image = _registry().get(str(body.get("image") or ""), kind="image")
    seg = _registry().get(str(body.get("segmentation") or ""), kind="segmentation")
    zs, ts = _semantics(body)
    names = [str(name) for name in body.get("channel_names") or []]
    indices = [int(index) for index in body.get("indices") or []]
    units = str(body.get("units") or "physical")

    def work(job):
        import shutil
        import tempfile
        from dataclasses import replace
        from pathlib import Path

        from plexora.server.volume import labels as volume_labels
        from plexora.server.volume import objects
        from plexora.server.volume import planes as volume_planes
        from plexora.server.volume.sources import open_volume

        source = None
        scratch = Path(tempfile.mkdtemp(prefix="plexora-planes-"))
        try:
            # Opened here, not borrowed from the tile path's handles: an hour
            # of reading must not hold those resources' locks.
            if not volume_labels.is_label_volume(seg.path):
                raise ResourceError("this node holds the mask as one 2-D segmentation, "
                                    "not a label volume")
            labels = volume_labels.open_label_volume(seg.path)
            source = open_volume(image.path, rgb=image.reads_colour,
                                 z_semantics=zs, t_semantics=ts)
            if source is None or source.volume.is_planar:
                raise ResourceError("this node reads the image as a single plane, so it "
                                    "has no planes to measure objects in")
            spacing = source.volume.spacing.overlaid(
                pixel_size=body.get("pixel_size"), pixel_size_z=body.get("pixel_size_z"),
                frame_interval=body.get("frame_interval"), frames=source.volume.frames)
            if spacing is not source.volume.spacing:
                source.volume = replace(source.volume, spacing=spacing)
            folder = scratch / volume_planes.FOLDER
            frame, manifest = objects.measure_columns(
                labels, source, channel_names=names, indices=indices, units=units,
                progress=_progress_of(job), cancelled=job["cancel"].is_set,
                planes_folder=folder)
            buffer = io.BytesIO()
            frame.write_parquet(buffer)
            # The plane index travels as one tar beside the table (the primary
            # keeps both); the voxels never leave this machine.
            job.update(manifest=manifest, result=buffer.getvalue(),
                       planes=volume_planes.pack(folder))
        finally:
            if source is not None:
                source.close()
            shutil.rmtree(scratch, ignore_errors=True)

    return jsonify(success=True, job=_start_job("measure", work)), 202


@node_bp.route("/volume/measure/<job_id>")
def volume_measure_status(job_id):
    job = _job(job_id)
    return jsonify(success=True, state=job["state"], done=job["done"], total=job["total"],
                   error=job["error"], manifest=job["manifest"])


@node_bp.route("/volume/measure/<job_id>/result")
@node_bp.route("/volume/jobs/<job_id>/result")
def volume_job_result(job_id):
    """A finished job's bytes: a measurement's table as parquet (every column
    keeps its dtype), a surface's mesh arrays as an .npz."""
    job = _job(job_id)
    if job["state"] != "done" or job["result"] is None:
        raise ResourceError(f"job {job_id!r} is {job['state']}, not done")
    return current_app.response_class(job["result"], mimetype="application/octet-stream")


@node_bp.route("/volume/jobs/<job_id>/planes")
def volume_job_planes(job_id):
    """A finished measurement's per-plane cell index (planes.pack): which
    cells have voxels on each plane."""
    job = _job(job_id)
    if job["state"] != "done" or job.get("planes") is None:
        raise ResourceError(f"job {job_id!r} has no per-plane cell index")
    return current_app.response_class(job["planes"], mimetype="application/x-tar")


@node_bp.route("/volume/measure/<job_id>/cancel", methods=["POST"])
@node_bp.route("/volume/jobs/<job_id>/cancel", methods=["POST"])
def volume_job_cancel(job_id):
    _job(job_id)["cancel"].set()
    return jsonify(success=True)


@node_bp.route("/volume/jobs/<job_id>")
def volume_job_status(job_id):
    job = _job(job_id)
    return jsonify(success=True, state=job["state"], stage=job["stage"], done=job["done"],
                   total=job["total"], error=job["error"], manifest=job["manifest"],
                   result=job["meta"])


# -- per-cell pixel features, read where the pixels are --------------------
#
# The Thresholding panel's Single Cell Classifier learns from each cell's
# pixels as well as its table row. For an image this node holds the pass runs
# here (`utils.cell_features.accumulate`, the code the primary runs for a
# local image) and one row of numbers per cell travels back -- never a plane.
# The job is polled, read and cancelled through the generic `/volume/jobs/`
# routes above.


@node_bp.route("/cells/features", methods=["POST"])
def cells_features():
    """Start building one marker's per-cell pixel features: `image` and its
    channel `marker_index` (with an optional nuclear `dna_index`) at `level`,
    against the 2-D mask `segmentation` at `mask_level`."""
    body = request.get_json(silent=True) or {}
    image = _registry().get(str(body.get("image") or ""), kind="image")
    seg = _registry().get(str(body.get("segmentation") or ""), kind="segmentation")
    marker_index = int(body.get("marker_index"))
    dna_index = body.get("dna_index")
    dna_index = int(dna_index) if dna_index is not None else None
    level = int(body.get("level") or 0)
    mask_level = int(body.get("mask_level") if body.get("mask_level") is not None else level)
    _prepare_on_first_use(seg)

    def work(job):
        import numpy as np

        from plexora.server.utils import cell_features
        from plexora.server.utils.label_overlay import padded_label_region

        # One short read section per tile: a pass over a whole slide must not
        # hold the resource's lock against a reload for its whole length.
        with _reading(image) as pyramid:
            plane = pyramid if hasattr(pyramid, "shape") else pyramid[str(level)]
            height, width = int(plane.shape[-2]), int(plane.shape[-1])

        def reader(index):
            def read(box):
                clipped = cell_features.clipped_box(box, width, height)
                x0, y0, x1, y1 = clipped
                with _reading(image) as pyramid:
                    plane = pyramid if hasattr(pyramid, "shape") else pyramid[str(level)]
                    block = (np.asarray(plane[index, y0:y1, x0:x1]) if plane.ndim == 3
                             else np.asarray(plane[y0:y1, x0:x1]))
                return cell_features.pad_to_box(block, clipped, box)
            return read

        def read_labels(box):
            with _reading(seg) as pyramid:
                volume = _label_volume_of(pyramid)
                if volume is not None:
                    pyramid = volume.frame(0).default_plane()
                return padded_label_region(pyramid, mask_level, box)

        job["stage"] = "pixels"
        labels, features = cell_features.accumulate(
            reader(marker_index), read_labels, width=width, height=height,
            read_dna=reader(dna_index) if dna_index is not None else None,
            progress=_progress_of(job), cancelled=job["cancel"].is_set)
        job.update(meta={"cells": int(labels.size), "level": level,
                         "feature_version": cell_features.FEATURE_VERSION},
                   result=cell_features.pack(labels, features))

    return jsonify(success=True, job=_start_job("cell-features", work)), 202


# -- surfaces, extracted where the voxels are -----------------------------
#
# Surface Studio's extraction reads a whole channel or a whole label volume,
# so for a z-stack this node holds it runs here (`surface.store.extract_on_node`,
# the code the primary runs for a local stack) and only the mesh travels; the
# primary measures it, joins its object table and writes the surface.


@node_bp.route("/volume/surface", methods=["POST"])
def volume_surface():
    """Start extracting a surface: `spec` (Surface Studio's request) and
    `image` (an isosurface's channel `channel`) or `segmentation` (cell
    surfaces, with the image's `scale`/`image_width` and the primary's object
    `table` when it has one)."""
    body = request.get_json(silent=True) or {}
    spec = body.get("spec") or {}
    zs, ts = _semantics(body)
    image = seg = None
    if (spec.get("type") or "isosurface") == "isosurface":
        image = _registry().get(str(body.get("image") or ""), kind="image")
    else:
        seg = _registry().get(str(body.get("segmentation") or ""), kind="segmentation")
    body = dict(body, zs=zs, ts=ts)

    def work(job):
        import numpy as np

        from plexora.server.surface import store as surface_store

        def open_image(zs_, ts_):
            from plexora.server.volume.sources import open_volume

            # Its own handle, as a measurement opens one: a long read must not
            # hold the tile path's resource lock.
            return open_volume(image.path, rgb=image.reads_colour,
                               z_semantics=zs_, t_semantics=ts_)

        def stage(name):
            job["stage"], job["done"], job["total"] = str(name), 0, 0

        surface, level, info = surface_store.extract_on_node(
            body, image=open_image if image is not None else None,
            labels=seg.path if seg is not None else None, stage=stage,
            report=_progress_of(job), cancelled=job["cancel"].is_set)
        if job["cancel"].is_set():
            raise InterruptedError("cancelled")
        arrays = {"positions": surface.positions, "triangles": surface.triangles,
                  "object_id": surface.object_id}
        for name in ("normals", "positions_raw", "objects"):
            value = getattr(surface, name)
            if value is not None:
                arrays[name] = value
        buffer = io.BytesIO()
        np.savez(buffer, **arrays)
        job.update(meta={"level": int(level), "info": _json_safe(info)},
                   result=buffer.getvalue())

    return jsonify(success=True, job=_start_job("surface", work)), 202


def _json_safe(value):
    """`value` with numpy scalars and arrays as plain JSON."""
    return json.loads(json.dumps(value, default=lambda v: v.tolist() if hasattr(v, "tolist")
                                 else str(v)))


# -- image ----------------------------------------------------------------


@node_bp.route("/batch", methods=["POST"])
def tile_batch_route():
    """Several of this node's tiles in one request, for a browser reaching it
    over a slow link (server/tile_batch.py). Each tile is dispatched as its
    own GET -- the token in its query is checked like any tile's -- and the
    token this request came with is passed on to each."""
    from plexora.server import tile_batch

    token = request.headers.get(TOKEN_HEADER)
    return tile_batch.respond(current_app._get_current_object(), request, tile_batch.NODE,
                              {TOKEN_HEADER: token} if token else None)


@node_bp.route("/image/<resource_id>/geometry")
def image_geometry(resource_id):
    """How big this node's image is, in `local.image_geometry`'s vocabulary.

    The provider answers when it can. This is the only place on the image read
    path that still reaches for a PATH -- tiles, the overview, stats, the GMM
    and region reads all go through `resource.provider.open()` -- and a resource
    served out of this process's memory has no path to read (see
    `providers/memory.py`). Every file-backed provider defines no `geometry`,
    so the fallback below is what every ordinary node still runs.
    """
    from plexora.server.providers import local as local_providers

    resource = _registry().get(resource_id, kind="image")
    geometry = getattr(resource.provider, "geometry", None)
    # A file this node reads as a z-stack or time series is described by the
    # planes it serves -- `data_model._describe_volume_plane`'s rule, so the
    # channel count is the volume's under the reading asked for (`zs`/`ts`,
    # the project's) -- with the volume keys beside it. Asked first because
    # some volumes (Imaris) have no 2-D reader to describe them at all; a
    # planar file comes back None and gets the answer it always got.
    memory_volume = getattr(resource.memory, "volume", None)
    volume = (_volume_geometry(resource) if geometry is None
              else _describe_volume(memory_volume) if memory_volume is not None else None)
    described = (volume if volume is not None
                 else geometry() if geometry is not None
                 # `rgb` for the same reason the provider gets it: a brightfield
                 # slide's virtual halving chain is a different level count and
                 # a different tile size from the planes the channel-stack
                 # branch would report, and geometry that disagreed with the
                 # tiles would have the viewer ask for levels the node cannot
                 # serve.
                 else local_providers.image_geometry(resource.path,
                                                     rgb=resource.reads_colour))
    # What this node made of the file, carried with the shape so the primary
    # learns both in the one round trip `attach_image` already makes. Additive:
    # a primary reading a node too old to say it sees no keys and records an
    # ordinary channel stack, which is what it did before.
    described = dict(described)
    described["image_type"] = resource.image_type
    described["image_type_reason"] = resource.image_type_reason
    # And the names the file gives its own channels -- the OME-XML, a DICOM
    # slide's optical paths, a zarr's omero block. Only the machine holding
    # the file can read them, and without them every image attached through
    # a node was named `<resource>_0`, `<resource>_1`, ... however well it
    # described itself. Additive and best effort, like the keys above.
    described.setdefault("channel_names", _own_channel_names(
        resource, described.get("num_channels")))
    return _stamped(_json(described), resource)


def _own_channel_names(resource, count):
    """The channel names the resource's file carries, or None."""
    path = getattr(resource, "path", None)
    if not path or not count or str(path).startswith("memory://"):
        return None
    try:
        from plexora.datasource import _channel_names_from_image_metadata

        names = _channel_names_from_image_metadata(path, int(count))
    except Exception:  # noqa: BLE001 -- names are a courtesy, not the image
        return None
    return list(names) if names and len(names) == int(count) else None


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

    `view`/`zs`/`ts` (see `_volume_view`) serve a plane, projection or section
    of a volume, with the volume's one window per channel -- the window class
    a local read of the same view uses, so those bytes are identical too.
    """
    from plexora.server.models import data_model

    resource = _registry().get(resource_id, kind="image")
    quality = request.args.get("q", "webp")
    index = _channel_index(channel)
    width, height = _tile_size()
    with _reading(resource):
        view = _volume_view(resource, request.args, served=True)
        if view is None:
            (qmin, qmax), exact = _windowed(resource, index)
        elif quality in ("hd", "legacy"):
            # A 16-bit tile is the voxels themselves (encode_tile_array never
            # reads the window for it), so a view's tile waits on no window:
            # asking `_volume_window` here made a channel's first HD tile wait
            # for a scan of its whole stack -- the primary's own HD path
            # (data_model.encode_tile) never asked.
            (qmin, qmax), exact = (None, None), True
        elif request.args.get("w"):
            # The window the viewer named (`w=`, services/volumeWindows.js):
            # the bytes are fixed by the address, as on the primary.
            from plexora.server.volume.windows import parse_stamp

            (qmin, qmax), exact = parse_stamp(request.args.get("w")), True
        else:
            (qmin, qmax), exact = _volume_window(
                resource, view, index, view.spec.window_class()), True

    def encode():
        with _reading(resource) as pyramid:
            if view is not None:
                # Resolved again under this read lock: a reopen between the
                # two sections closes the sources the first one found.
                opened = perf_counter()
                again = _volume_view(resource, request.args, served=True)
                pyramid = _view_pyramid(resource, again)
                # The read's `open` part (Server-Timing), as on the primary.
                _perf.part("open", (perf_counter() - opened) * 1000.0)
            array = data_model.read_tile(pyramid, index, level, tile, width, height)
        _perf.mark("read")
        # Encoded outside the lock. It is pure over the array and the window,
        # which is the same property that lets the primary forward these bytes
        # without decoding them.
        encoded = data_model.encode_tile_array(array, False, quality, qmin, qmax)
        _perf.mark("enc")
        return encoded

    # The window is part of the key and the ETag: a tile rendered under the
    # provisional window and one rendered under the exact window are different
    # pictures, and a key that conflated them would keep serving the guess.
    # A view adds itself to both, and only a view: a plain tile's key and tag
    # are the ones they have always been.
    generation = _open_generation(resource)
    base = (resource.id, generation, "tile", index,
            str(level), str(tile), quality, width, height, qmin, qmax)
    key = base
    parts = ("tile", channel, level, tile, quality, qmin, qmax)
    if view is not None:
        key += view.key
        parts += view.key
    with _tile_cache_lock:
        hit = key in _tile_cache
    encoded, mimetype = _cached_tile(key, encode)
    if view is not None:
        _read_ahead(resource, view, index, level, tile, width, height)
        _tiles_ahead(resource, generation, view, base, index, level, tile, width, height,
                     quality, qmin, qmax, hit)
    return _viewed(_stamped(
        _image(encoded, mimetype, _etag(resource, *parts), durable=exact),
        resource), view)


def _read_ahead(resource, view, index, level, tile, width, height):
    """Queue the block of planes ahead of a plane tile just served
    (server/volume/prefetch.py): read the same tile there, in the background,
    so stepping across the block's edge finds its chunks decoded."""
    from plexora.server.models import data_model
    from plexora.server.volume.bricks import z_decode
    from plexora.server.volume.prefetch import PREFETCHER

    spec = view.spec
    if spec.kind != "plane" or spec.default:
        return
    base = view.base
    try:
        # What one read decodes, as the primary strides (data_model._read_ahead).
        block = z_decode(base)
        depth = int(base.volume.depth)
    except Exception:  # noqa: BLE001 -- a source that cannot say reads nothing ahead
        return
    zs, ts, t = view.zs, view.ts, int(spec.t)

    def read(z):
        with _reading(resource):
            source = _volume_source(resource, zs, ts)
            if source is None:
                return
            data_model.read_tile(source.plane_pyramid(t, z), index, level, tile, width, height)

    region = (resource.id, getattr(resource, "generation", 0), view.reading, t, index,
              str(level), str(tile))
    PREFETCHER.after_tile(region, int(spec.z), depth, block, read)


def _tiles_ahead(resource, generation, view, base, index, level, tile, width, height,
                 quality, qmin, qmax, hit):
    """Make this tile at the planes ahead of the hand (prefetch.py
    `TILE_PREFETCHER`), into this node's tile cache under the key `image_tile`
    would file it under, when it is a plane of a stack decoded a plane at a
    time, or a coarse level's tile of any stack (`prefetch.planes_ahead`) --
    the primary's `data_routes._tiles_ahead`, for a node: the browser's
    standby request for the next plane, direct or forwarded, is then answered
    from memory. `base` is the key without its view."""
    from dataclasses import replace

    from plexora.server.models import data_model
    from plexora.server.volume.bricks import z_decode
    from plexora.server.volume.plane_view import plane_pyramid
    from plexora.server.volume.prefetch import TILE_PREFETCHER, levels_pending, planes_ahead

    spec = view.spec
    if spec.kind != "plane" or spec.default:
        return
    try:
        ahead = planes_ahead(z_decode(view.base), len(view.base.volume.levels), level,
                             levels_pending(view.base))
        if ahead is None:
            return
        depth = int(view.base.volume.depth)
    except Exception:  # noqa: BLE001 -- a source that cannot say makes nothing ahead
        return
    zs, ts = view.zs, view.ts

    def key_at(z):
        return base + (replace(spec, z=int(z)).token(), zs or "", ts or "", "")

    def held(z):
        with _tile_cache_lock:
            return key_at(z) in _tile_cache

    def encode(z):
        if held(z):
            return False
        with _reading(resource):
            source = _volume_source(resource, zs, ts)
            if source is None or getattr(resource, "generation", 0) != generation:
                return False
            pyramid = plane_pyramid(source, replace(spec, z=int(z)), served=True)
            array = data_model.read_tile(pyramid, index, level, tile, width, height)
        _store_tile(key_at(z), data_model.encode_tile_array(array, False, quality, qmin, qmax))
        return True

    region = (resource.id, generation, view.reading, spec.t, index, str(level), str(tile),
              quality, width, height, qmin, qmax)
    try:
        TILE_PREFETCHER.after_plane_tile(region, spec.z, depth, encode, held, hit=hit,
                                         ahead=ahead)
    except Exception:  # noqa: BLE001 -- a read ahead is never the tile's error
        pass


@node_bp.route("/image/<resource_id>/level")
def image_level(resource_id):
    """One channel's 3-D level, as the primary's `/volume/<ds>/level` builds it.

    By the same functions (server/volume/serve.py), with the choices the
    primary made sent explicitly: `level`, `box`, `z`, `pool` and the window
    `w=` -- so the bytes are the ones a local read of the same stack makes,
    and the primary forwards them untouched. `view`/`zs`/`ts` give the
    reading (`_volume_view`); the level is of the volume itself.
    """
    from plexora.server.models import data_model
    from plexora.server.volume import serve

    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    with _reading(resource):
        view = _volume_view(resource, request.args)
        if view is None:
            raise BadView("a level is of a volume: name its view (view=, zs=, ts=)")
        volume = view.base.volume
        try:
            ask = serve.parse_level(volume, request.args)
        except serve.BadLevel as error:
            raise BadView(str(error)) from None
        if ask.named is not None:
            window = ask.named
        else:
            window = _volume_window(resource, view, index, "volume")
        rows, cols, covered = serve.area_at(volume, ask.level, ask.box)
        pool = ask.pool or serve.z_pool(volume, ask.level)
    parts = ("level", serve.PAYLOAD_VERSION, index, ask.t, ask.level,
             f"{window[0]:g},{window[1]:g}",
             ask.voxel_format, ",".join(str(int(v)) for v in covered), ask.z0, ask.z1,
             pool) + view.key
    etag = _etag(resource, *parts)
    headers = serve.level_headers(ask, covered, pool, window)
    if request.headers.get("If-None-Match") == etag:
        response = current_app.response_class(status=304)
        response.headers["ETag"] = etag
        response.headers.update(headers)
        return _viewed(_stamped(response, resource), view)

    def read():
        with _reading(resource):
            again = _volume_view(resource, request.args)
            return serve.read_level(again.base, ask, index, rows, cols, pool)

    key = (resource.id, _open_generation(resource)) + parts
    data, shape = serve.level_payload(key, read, window, ask.voxel_format, _perf.mark)
    response = _image(data, "application/octet-stream", etag)
    response.headers.update(headers)
    response.headers.update(serve.payload_headers(shape))
    return _viewed(_stamped(response, resource), view)


@node_bp.route("/image/<resource_id>/block", methods=["POST"])
def image_block(resource_id):
    """`(Z, H, W)` raw values of one channel of a z-stack: planes z0..z1
    inclusive of frame `t` at `level`, over `rows`/`cols` (half-open, clipped
    to the level), under the primary's z/t reading -- a local volume
    source's `read_block`, for `providers.node.NodeVolumeReader`. What a
    surface preview reads; capped like a mask block."""
    import numpy as np

    resource = _registry().get(resource_id, kind="image")
    body = request.get_json(silent=True) or {}
    zs, ts = _semantics(body)
    level = int(body.get("level") or 0)
    channel = int(body.get("c") or 0)
    t = int(body.get("t") or 0)
    with _reading(resource):
        source = _volume_source(resource, zs, ts)
        if source is None or source.volume.is_planar:
            raise ResourceError("this node reads the image as a single plane, so it has no "
                                "planes to read a block of")
        volume = source.volume
        if not 0 <= level < len(volume.levels):
            raise ResourceError(f"level {level} is outside this image's "
                                f"{len(volume.levels)} levels")
        if not 0 <= channel < volume.channels:
            raise ResourceError(f"channel {channel} is outside this image's "
                                f"{volume.channels} channels")
        if not 0 <= t < volume.frames:
            raise ResourceError(f"t {t} is outside this image's {volume.frames} frames")
        info = volume.levels[level]
        z0 = max(0, min(volume.depth - 1, int(body.get("z0") or 0)))
        z1 = int(body["z1"]) if body.get("z1") is not None else z0
        z1 = max(z0, min(volume.depth - 1, z1))
        rows = slice(*slice(*(body.get("rows") or [None, None])).indices(info.height)[:2])
        cols = slice(*slice(*(body.get("cols") or [None, None])).indices(info.width)[:2])
        voxels = (z1 - z0 + 1) * max(0, rows.stop - rows.start) * max(0, cols.stop - cols.start)
        if voxels > BLOCK_MAX_VOXELS:
            raise ResourceError("this block covers more of the image than one read can "
                                "carry; ask for fewer planes or a lower resolution")
        block = np.asarray(source.read_block(level, t, channel, z0, z1, rows, cols))
    return _stamped(_frame(wire.pack_array(np.ascontiguousarray(block), level=level,
                                           z0=z0, z1=z1)),
                    resource)


@node_bp.route("/image/<resource_id>/overview")
def image_overview(resource_id):
    from plexora.server.models import data_model

    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    with _reading(resource):
        view = _volume_view(resource, request.args)
        if view is None:
            (qmin, qmax), exact = _windowed(resource, index)
            encoded = data_model.encode_overview(resource.opened_overview[index], qmin, qmax)
        else:
            (qmin, qmax), exact = _volume_window(
                resource, view, index, view.spec.window_class()), True
            encoded = data_model.encode_overview(
                _view_overview(resource, view)[index], qmin, qmax)
    parts = ("overview", index, qmin, qmax) + (view.key if view is not None else ())
    return _viewed(_stamped(
        _image(encoded, "image/webp", _etag(resource, *parts), durable=exact),
        resource), view)


@node_bp.route("/image/<resource_id>/stats")
def image_stats(resource_id):
    from plexora.server.models import data_model

    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    with _reading(resource):
        view = _volume_view(resource, request.args)
        if view is not None:
            # A slab's own distribution, or the default plane's for a plane or
            # a section -- the split data_model._slab_view makes locally.
            if request.args.get("provisional") == "1":
                # Answered now, saying which window it was made with: the
                # primary levels with it and asks again once it is exact.
                (qmin, qmax), exact = _volume_window_now(resource, view, index,
                                                         view.spec.window_class())
            else:
                (qmin, qmax), exact = _volume_window(
                    resource, view, index, view.spec.window_class()), None
            stats = _cached(resource, ("stats", index, qmin, qmax) + view.stats_key,
                            lambda: _persisted_packet(
                                resource, f"stats|{index}|{qmin!r}|{qmax!r}|{view.stats_key!r}",
                                lambda: data_model.channel_stats_of(
                                    _view_overview(resource, view)[index], qmin, qmax)))
            if exact is not None:
                stats = {**stats, "window_exact": bool(exact)}
            return _viewed(_stamped(_json(stats), resource), view)
        qmin, qmax = _quantization(resource, index)
        # Keyed by the window as well as the channel, so numbers computed
        # against a provisional window are recomputed once the exact one
        # lands, instead of memoized past it.
        stats = _cached(resource, ("stats", index, qmin, qmax),
                        lambda: data_model.channel_stats_of(
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
        view = _volume_view(resource, request.args)
        if view is not None:
            qmin, qmax = _volume_window(resource, view, index, view.spec.window_class())
            packet = _cached(resource, ("gmm", index, qmin, qmax) + view.stats_key,
                             lambda: _persisted_packet(
                                 resource, f"gmm|{index}|{qmin!r}|{qmax!r}|{view.stats_key!r}",
                                 lambda: data_model.channel_gmm_of(
                                     _view_overview(resource, view)[index], qmin, qmax)))
            return _viewed(_stamped(_json(packet), resource), view)
        qmin, qmax = _quantization(resource, index)
        packet = _cached(resource, ("gmm", index, qmin, qmax),
                         lambda: data_model.channel_gmm_of(
                             resource.opened_overview[index], qmin, qmax))
    return _stamped(_json(packet), resource)


@node_bp.route("/image/<resource_id>/quantization")
def image_quantization(resource_id):
    resource = _registry().get(resource_id, kind="image")
    index = _channel_index_arg()
    with _reading(resource):
        view = _volume_view(resource, request.args)
        if view is None:
            (qmin, qmax), exact = _windowed(resource, index)
        else:
            # `wc` names the class outright (the primary asks for a channel's
            # "volume" window without a view in mind); else the view's own.
            window_class = request.args.get("wc") or view.spec.window_class()
            if request.args.get("provisional") == "1":
                (qmin, qmax), exact = _volume_window_now(resource, view, index, window_class)
            else:
                (qmin, qmax), exact = _volume_window(resource, view, index, window_class), True
    return _viewed(_stamped(_json({"qmin": qmin, "qmax": qmax, "exact": exact}), resource),
                   view)


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

    view = None
    with _reading(resource) as pyramid:
        view = _volume_view(resource, body)
        if view is not None:
            # A view's own pixels: a projection's values, a section's rows of z.
            from plexora.server.volume.plane_view import plane_pyramid

            pyramid = plane_pyramid(view.source, view.spec)
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
    meta = {"view": view.spec.token()} if view is not None else {}
    return _viewed(_stamped(_frame(wire.pack_array(stack, box=[x0, y0, x1, y1],
                                                   channels=indices, level=level,
                                                   **meta)),
                            resource), view)


# -- volumes --------------------------------------------------------------
#
# A node has no project, so how a stack's z and t axes are read arrives with
# the request -- `zs`/`ts`, the project's recorded choice (volume/sources/
# tiff.py says why that is a decision and not a fact) -- and so does the view:
# `view` is always a concrete token, because the primary spells the default
# plane as itself. A request carrying none of these is the image exactly as it
# was read before volumes, through exactly the code it always went through.

#: The parameters that put a request on the volume path.
_VOLUME_ARGS = ("view", "zs", "ts", "az", "wc")


class _VolumeView:
    """One request's volume and view.

    `base` is the file opened under the request's z/t reading; `source` is the
    same, respaced when a section is drawn at the primary's anisotropy (see
    `_Respaced`). Windows and overviews always come from `base`: spacing moves
    no pixel value, only a section's row count.
    """

    __slots__ = ("base", "source", "spec", "zs", "ts", "az")

    def __init__(self, base, source, spec, zs, ts, az):
        self.base, self.source, self.spec = base, source, spec
        self.zs, self.ts, self.az = zs, ts, az

    @property
    def reading(self) -> str:
        return f"{self.zs or ''}|{self.ts or ''}"

    @property
    def key(self) -> tuple:
        """What tells this view's tiles apart, for a cache key or an ETag."""
        return (self.spec.token(), self.zs or "", self.ts or "", self.az or "")

    @property
    def stats_key(self) -> tuple:
        """What tells this view's statistics apart: one packet for every plane
        and section of a reading, one per slab (`ViewSpec.stats_key`)."""
        return (self.reading, self.spec.stats_key())


def _volume_view(resource, args, served=False):
    """The volume and view `args` name, or None for the image as it always was.

    Call with the resource's read lock held (`_reading`): the source comes out
    of `derived`, which a reopen replaces. BadView -- a 400 -- for a view this
    image does not have under that reading, rather than some other picture
    under the requested name.
    """
    from plexora.server.volume.model import T_SEMANTICS, Z_SEMANTICS
    from plexora.server.volume.viewspec import ViewSpec

    if not any(args.get(name) for name in _VOLUME_ARGS):
        return None
    zs = str(args.get("zs") or "") or None
    ts = str(args.get("ts") or "") or None
    if zs is not None and zs not in Z_SEMANTICS:
        raise BadView(f"zs is one of {', '.join(Z_SEMANTICS)}, not {zs!r}")
    if ts is not None and ts not in T_SEMANTICS:
        raise BadView(f"ts is one of {', '.join(T_SEMANTICS)}, not {ts!r}")
    base = _volume_source(resource, zs, ts)
    if base is None or base.volume.is_planar:
        raise BadView("this node reads this image as a single plane, so it has no "
                      "views to choose from")
    spec = ViewSpec.parse(str(args.get("view") or ""), base.volume)
    if served and not spec.served:
        raise BadView("a sum projection is exported, not served as tiles: "
                      "summed planes overflow a 16-bit tile")
    source, az = base, None
    if spec.kind == "section" and args.get("az"):
        az = str(args.get("az"))
        try:
            anisotropy = float(az)
        except ValueError:
            raise BadView("az is a section's z step over its x step: a number") from None
        if not anisotropy > 0:
            raise BadView("az is a section's z step over its x step: above zero")
        if anisotropy != base.volume.spacing.anisotropy():
            source = _Respaced(base, anisotropy)
    return _VolumeView(base, source, spec, zs, ts, az)


#: Derived (slab and section) pyramids kept per resource: a section's tiles
#: share one, and its read caches, instead of building one per tile.
_VIEW_PYRAMIDS = 16
_view_pyramids_lock = threading.Lock()


def _view_pyramid(resource, view):
    """The pyramid serving `view`, built once per view while it is in use.

    A plane is the reader's own (cached by the volume model); a slab or a
    section is a `DerivedPyramid` whose levels hold what they have read, so
    rebuilding one per tile threw that away. Kept in `derived`, which a reopen
    replaces, in a small LRU: a scrub through sections makes a new view per
    cut. Call with the read lock held (`_reading`)."""
    from collections import OrderedDict

    from plexora.server.volume.plane_view import plane_pyramid

    if view.spec.kind == "plane" or view.spec.default:
        return plane_pyramid(view.source, view.spec, served=True)
    with _view_pyramids_lock:
        derived = getattr(resource, "derived", None)
        if derived is None:
            derived = resource.derived = {}
        held = derived.get("view-pyramids")
        if held is None:
            held = derived["view-pyramids"] = OrderedDict()
        pyramid = held.get(view.key)
        if pyramid is not None:
            held.move_to_end(view.key)
            return pyramid
    pyramid = plane_pyramid(view.source, view.spec, served=True)
    with _view_pyramids_lock:
        held[view.key] = pyramid
        while len(held) > _VIEW_PYRAMIDS:
            held.popitem(last=False)
    return pyramid


def _volume_source(resource, zs, ts):
    """This resource's file opened as a volume under one z/t reading, once.

    None when it is planar under that reading. Kept in `derived` -- dropped,
    and closed, when the pyramid is reopened (`_opened`) -- rather than shared
    with the provider's own default open, so a reading the provider never made
    costs one handle and every reading is opened the same way.
    """
    if resource.memory is not None:
        # A notebook's array: its axes were stated when it was snapshotted, so
        # there is one reading and the snapshot holds it (no file to reopen).
        return getattr(resource.memory, "volume", None)

    def open_source():
        if not resource.path:
            return None
        from plexora.server.volume.sources import open_volume

        return open_volume(resource.path, rgb=resource.reads_colour,
                           z_semantics=zs, t_semantics=ts)

    return _cached(resource, ("volume", zs, ts), open_source)


class _Respaced:
    """A volume source drawn at another z-step-to-x-step ratio.

    Only a section's row count depends on spacing, and the primary may hold a
    z step somebody typed that the file does not state. So the primary sends
    the ratio it sizes the section's tile source with (`az`), and the section
    is drawn here at exactly that ratio: one unitless number rather than the
    project's calibration records, because the ratio is all the pixels depend
    on. Everything else is the wrapped source's.
    """

    def __init__(self, source, anisotropy):
        from dataclasses import replace

        from plexora.server.volume.axes import AxisSpacing, Spacing

        self._source = source
        unit = AxisSpacing(value=1.0, unit="µm", source="manual")
        self.volume = replace(source.volume, spacing=Spacing(
            x=unit, y=unit, z=AxisSpacing(value=float(anisotropy), unit="µm",
                                          source="manual"),
            t=source.volume.spacing.t))

    def __getattr__(self, name):
        return getattr(self._source, name)


def _close_volumes(derived):
    """Close the volume sources a reopen is about to drop with `derived`."""
    for key, value in list((derived or {}).items()):
        if isinstance(key, tuple) and key[:1] == ("volume",) and value is not None:
            try:
                value.close()
            except Exception:
                pass


def _volume_window(resource, view, index, window_class):
    """(qmin, qmax) for one channel of a volume, as a local read computes it.

    One window per channel across every plane and frame (volume/windows.py),
    by the identical function the primary runs over its own copy, so a node's
    tiles of a view are byte-for-byte the primary's. Exact or not at all: a
    provisional ceiling from one plane would be a different picture on the
    next, which is the flicker one window per volume exists to prevent. Banked
    to the window store like a plane's, so a reconnect does not rescan.
    """
    from plexora.server.models import data_model
    from plexora.server.volume.windows import std_window, volume_window

    if window_class not in ("volume", "std"):
        raise BadView(f"a volume's window class is volume or std, not {window_class!r}")
    key = f"{index}|volume|{view.reading}"

    def compute():
        stored = _stored_window(resource, key)
        if stored is not None:
            return stored
        window, _how = volume_window(view.base, index, gate=data_model._WINDOW_SCAN_GATE)
        _remember_window(resource, key, window)
        return window

    window = _cached(resource, ("qwindow", key), compute)
    return std_window(window) if window_class == "std" else window


def _volume_window_now(resource, view, index, window_class):
    """`((qmin, qmax), exact)` for a volume's channel, never waiting for a scan.

    `_volume_window`'s answer when it is known (in process or banked); else
    the provisional window a local read starts with (windows.provisional_window
    -- the same function, so the primary's provisional bytes and these agree),
    with the exact scan queued at the front of the node's one scan thread.
    Only asked for by a primary that says it can take a provisional answer
    (`provisional=1`): it names the window in every tile it asks for after,
    and asks again until it is exact."""
    from plexora.server.volume.windows import provisional_window, std_window

    if window_class not in ("volume", "std"):
        raise BadView(f"a volume's window class is volume or std, not {window_class!r}")
    key = f"{index}|volume|{view.reading}"
    window = (getattr(resource, "derived", None) or {}).get(("qwindow", key))
    if window is None:
        stored = _stored_window(resource, key)
        if stored is not None:
            window = _cached(resource, ("qwindow", key), lambda: stored)
    exact = window is not None
    if window is None:
        _scan_soon(resource, ("volume", view.zs, view.ts, index), front=True)
        # Inline (tests) the scan has finished by this line.
        window = resource.derived.get(("qwindow", key))
        exact = window is not None
    if window is None:
        window = _cached(resource, ("qwindow-provisional", key),
                         lambda: provisional_window(view.base, index)[0])
    return (std_window(window) if window_class == "std" else window), exact


def _view_overview(resource, view):
    """The mean-pooled overview a view's statistics are computed over.

    A slab's own; the default plane's for a plane or a section -- the split
    `data_model._slab_view` makes on the primary, by the same heuristic
    (`providers.local.volume_overview`). Kept on disk as well as in memory
    (`_persisted_array`): every channel's at once means opening every
    channel's dataset, and on a cluster filesystem that was the 20-odd
    seconds a node spent before the first channel switched on could show.
    """
    from plexora.server.providers.local import volume_overview
    from plexora.server.volume.plane_view import plane_pyramid

    spec = view.spec if view.spec.kind == "slab" else view.base.volume.default_view
    name = f"overview|{view.reading}|{spec.stats_key()!r}"
    return _cached(resource, ("overview", view.reading, spec.stats_key()),
                   lambda: _persisted_array(
                       resource, name, lambda: volume_overview(plane_pyramid(view.base, spec))))


def _keep_open_overview(resource):
    """Have the provider keep a volume's opening overview on disk
    (`_persisted_array`). Opening a volume builds every channel's overview of
    its default plane -- each channel's dataset opened and read, the cost
    `_view_overview` is kept on disk for -- and a node pays it on every start
    otherwise: in its warm-up, or in the first request that finds it cold."""
    provider = getattr(resource, "provider", None)
    if provider is None or not hasattr(provider, "overview_builder"):
        return
    if not getattr(resource, "path", None) or provider.overview_builder is not None:
        return
    from plexora.server.providers import local

    reading = (f"open-overview|{getattr(provider, '_z_semantics', None) or ''}"
               f"|{getattr(provider, '_t_semantics', None) or ''}"
               f"|{bool(getattr(provider, '_rgb', False))}")
    provider.overview_builder = lambda channels: _persisted_array(
        resource, reading, lambda: local.volume_overview(channels))


def _derived_dir(resource):
    from plexora import paths

    return paths.data_root() / "node-derived" / str(resource.id)


def _derived_name(fingerprint, name):
    import hashlib

    return hashlib.sha1(f"{fingerprint}|{name}".encode("utf-8")).hexdigest()[:32]


def _persisted_array(resource, name, compute):
    """`compute()`'s array, kept on disk for the file as it is now.

    Keyed by the file's fingerprint (`_file_fingerprint`) as well as `name`,
    so a rewritten file is computed afresh; best-effort both ways -- a store
    that cannot be read or written is a value computed, never an error. The
    store lives beside the window store, in the node's data root, which on a
    cluster is the user's home: it outlives the job.
    """
    import numpy as np

    fingerprint = _file_fingerprint(resource.path) if getattr(resource, "path", None) else None
    if fingerprint is None:
        return compute()
    path = _derived_dir(resource) / f"{_derived_name(fingerprint, name)}.npy"
    try:
        if path.is_file():
            return np.load(path, allow_pickle=False)
    except Exception:  # noqa: BLE001 -- unreadable: computed again below
        pass
    value = compute()
    if isinstance(value, np.ndarray):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(f"{path.stem}.{threading.get_ident()}.part")
            with open(partial, "wb") as handle:
                np.save(handle, value, allow_pickle=False)
            os.replace(partial, path)
        except Exception:  # noqa: BLE001 -- a read-only home must not break a request
            pass
    return value


_derived_store_lock = threading.Lock()


def _persisted_packet(resource, name, compute):
    """`compute()`'s JSON-able packet (a channel's stats, its mixture fit),
    kept on disk for the file as it is now -- `_persisted_array`'s rule."""
    fingerprint = _file_fingerprint(resource.path) if getattr(resource, "path", None) else None
    if fingerprint is None:
        return compute()
    path = _derived_dir(resource) / "packets.json"
    key = _derived_name(fingerprint, name)

    def load():
        try:
            stored = json.loads(path.read_text())
        except Exception:  # noqa: BLE001 -- absent or unreadable: nothing kept
            return {}
        return stored.get("packets") or {} if stored.get("fingerprint") == fingerprint else {}

    with _derived_store_lock:
        held = load().get(key)
    if held is not None:
        return held
    value = _json_safe(compute())
    try:
        with _derived_store_lock:
            packets = load()
            packets[key] = value
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(f"packets.{threading.get_ident()}.part")
            partial.write_text(json.dumps({"fingerprint": fingerprint, "packets": packets}))
            os.replace(partial, path)
    except Exception:  # noqa: BLE001 -- a read-only home must not break a request
        pass
    return value


def _volume_geometry(resource):
    """The geometry route's answer for a file read as a volume, or None.

    Under the request's `zs`/`ts` (the project's reading; the file's own
    default without them). Opened and closed here rather than through the
    resource's cache: geometry is asked before anything else is, and opening
    the default pyramid to answer it would compute an overview nobody asked
    for. Best effort, like the channel names beside it -- a file that will not
    open as a volume is described as the plane it has always been.
    """
    import zarr

    from plexora.server.volume.model import T_SEMANTICS, Z_SEMANTICS
    from plexora.server.volume.sources import open_volume

    zs = request.args.get("zs") or None
    ts = request.args.get("ts") or None
    if (zs is not None and zs not in Z_SEMANTICS) or (ts is not None and ts not in T_SEMANTICS):
        raise BadView("zs is depth, cycle or unknown; ts is time, cycle or unknown")
    if resource.memory is not None or not resource.path:
        return None
    source = None
    try:
        source = open_volume(resource.path, rgb=resource.reads_colour,
                             z_semantics=zs, t_semantics=ts)
        return _describe_volume(source)
    except Exception as exc:  # noqa: BLE001 -- described as a plane instead
        current_app.logger.warning("could not read %s as a volume: %s", resource.id, exc)
        return None
    finally:
        if source is not None:
            source.close()


def _describe_volume(source):
    """The geometry route's keys for an open volume source; None when planar."""
    import zarr

    if source is None or source.volume.is_planar:
        return None
    volume = source.volume
    default = volume.default_view
    pyramid = source.plane_pyramid(default.t, default.z)
    if isinstance(pyramid, zarr.Array):
        chunks = pyramid.chunks
        level_shapes = [[int(pyramid.shape[-2]), int(pyramid.shape[-1])]]
    else:
        chunks = (1, 1024, 1024)
        level_shapes = [[int(pyramid[key].shape[-2]), int(pyramid[key].shape[-1])]
                        for key in sorted(list(pyramid), key=int)]
    return {
        "levels": len(level_shapes),
        "num_channels": int(volume.channels),
        "height": int(volume.height),
        "width": int(volume.width),
        "tile_height": int(chunks[-2]),
        "tile_width": int(chunks[-1]),
        "level_shapes": level_shapes,
        "depth": int(volume.depth),
        "frames": int(volume.frames),
        "axes": volume.axes.spelled,
        "spacing": volume.spacing.to_dict(),
        "default_view": default.token(),
        # Everything the primary rebuilds a `Volume` from (Volume.from_dict).
        "volume": volume.to_dict(),
    }


def _viewed(response, view):
    """Stamp which view an answer is of -- what lets the primary refuse a node
    too old to have served one (providers/node.py `_served`)."""
    if view is not None:
        response.headers[VIEW_HEADER] = view.spec.token()
    return response


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
#:
#: Budgeted in BYTES, not entries. It used to be 600 tiles, sized for 8-bit
#: tiles of ~50-300 KB -- and a 16-bit tile is 2 MB (stored PNG, see
#: data_model.encode_tile_array), so the same 600 meant anything from 30 MB
#: to 1.2 GB, while a volume's planes (70 channels of 194 planes on the stack
#: this was measured on) never stayed long enough to step back to. A
#: sixteenth of the memory this process was given, 128 MB to 2 GB
#: (`PLEXORA_NODE_TILE_CACHE_MB`): a laptop node keeps what it kept, a
#: cluster job of 64 GB keeps a few hundred 16-bit tiles of planes left
#: behind.
_TILE_CACHE_MAX = 20000


def _tile_cache_budget():
    raw = os.environ.get("PLEXORA_NODE_TILE_CACHE_MB")
    try:
        if raw:
            return max(16, int(raw)) * 2**20
    except ValueError:
        pass
    try:
        from plexora._resources import allocated_memory_bytes

        total = allocated_memory_bytes() or 8 * 2**30
    except Exception:  # noqa: BLE001 -- sizing only
        total = 8 * 2**30
    return int(max(128 * 2**20, min(2 * 2**30, total // 16)))


_TILE_CACHE_BYTES = _tile_cache_budget()
_tile_cache = OrderedDict()
_tile_cache_lock = threading.Lock()
_tile_cache_bytes = 0


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
            _perf.note("cache", "hit")
            return hit
    _perf.note("cache", "miss")
    _perf.skip()
    value = encode()
    _store_tile(key, value)
    return value


def _store_tile(key, value):
    """File an encoded tile under `key`, evicting the least recently used past
    the budget."""
    global _tile_cache_bytes
    size = _tile_bytes(value)
    with _tile_cache_lock:
        if not _tile_cache:
            # Emptied from outside (a test's `clear()`): the count goes with it.
            _tile_cache_bytes = 0
        previous = _tile_cache.pop(key, None)
        if previous is not None:
            _tile_cache_bytes -= _tile_bytes(previous)
        _tile_cache[key] = value
        _tile_cache_bytes += size
        # Never the tile just stored: one bigger than the whole budget is
        # still worth answering the next request for it from.
        while len(_tile_cache) > 1 and (len(_tile_cache) > _TILE_CACHE_MAX
                                        or _tile_cache_bytes > _TILE_CACHE_BYTES):
            _, dropped = _tile_cache.popitem(last=False)
            _tile_cache_bytes -= _tile_bytes(dropped)


def _tile_bytes(value):
    try:
        return len(value[0])
    except Exception:  # noqa: BLE001 -- an entry of unknown size costs nothing
        return 0


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
            _keep_open_overview(resource)
            channels, overview, metadata = resource.provider.open()
            # `opened` last, and deliberately: `_reading` tests it without a
            # lock, so it must never be visible while its two companions are
            # still from the previous open.
            resource.opened_overview = overview
            resource.opened_metadata = metadata
            resource.opened = channels
        else:
            resource.opened = resource.provider.open()
        _close_volumes(getattr(resource, "derived", None))
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


#: Guards the read-modify-write on a window store file. One lock for all
#: resources is fine: the section is a small JSON read and write, never the
#: plane scan itself.
_window_store_lock = threading.Lock()


def _window_store_path(resource):
    from plexora import paths

    return paths.data_root() / "node-quantization" / f"{resource.id}.json"


def _file_fingerprint(path):
    """size:mtime of the served file, or None when it cannot be told.

    The same identity the primary's window store uses, for the same reason:
    hashing a file of gigabytes would cost more than the read this store
    exists to avoid, and any rewrite that could change a channel's maximum
    changes one of the two. None reads as a miss, never as a match.
    """
    import os

    try:
        stat = os.stat(path)
    except OSError:
        return None
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _stored_window(resource, index):
    """The persisted window for one channel, or None.

    A node's in-process cache dies with the process, and on a cluster the
    process dies with every job -- a saved connection starts a fresh `srun`
    each time. Without this file, every reconnect re-read every pixel of
    every channel anybody touched, which for a wide slide is the whole image
    again: minutes of a machine that answers nothing, spent recomputing
    numbers the last job already knew. The store lives in the node's own data
    root, which on a cluster is the user's home -- the one thing that
    outlives the job.
    """
    fingerprint = _file_fingerprint(resource.path)
    if fingerprint is None:
        return None
    try:
        with _window_store_lock:
            stored = json.loads(_window_store_path(resource).read_text())
        if stored.get("fingerprint") != fingerprint:
            return None
        pair = (stored.get("windows") or {}).get(str(index))
        return (float(pair[0]), float(pair[1])) if pair else None
    except Exception:
        return None


def _stored_volume_windows(resource):
    """Every persisted VOLUME window of this file as it is now:
    `{"<index>|volume|<reading>": (qmin, qmax)}` -- the keys `_volume_window`
    caches under, so warming can seed them all from one read."""
    fingerprint = _file_fingerprint(resource.path) if getattr(resource, "path", None) else None
    if fingerprint is None:
        return {}
    try:
        with _window_store_lock:
            stored = json.loads(_window_store_path(resource).read_text())
        if stored.get("fingerprint") != fingerprint:
            return {}
        return {key: (float(pair[0]), float(pair[1]))
                for key, pair in (stored.get("windows") or {}).items()
                if "|volume|" in key and pair}
    except Exception:  # noqa: BLE001 -- nothing banked is nothing to seed
        return {}


def _remember_window(resource, index, window):
    """Best-effort and never fatal: a read-only home must not break a tile."""
    fingerprint = _file_fingerprint(resource.path)
    if fingerprint is None:
        return
    try:
        with _window_store_lock:
            path = _window_store_path(resource)
            try:
                stored = json.loads(path.read_text())
            except Exception:
                stored = {}
            # A fingerprint mismatch is the file having changed under a store
            # written for the previous one; drop the lot, as the primary does.
            windows = (stored.get("windows") or {}
                       if stored.get("fingerprint") == fingerprint else {})
            windows[str(index)] = [float(window[0]), float(window[1])]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"fingerprint": fingerprint, "windows": windows}))
    except Exception:
        pass


#: Channels whose exact windows are still owed, oldest first. `appendleft` is
#: for a channel somebody is looking at right now; stragglers go to the back.
#: One queue and ONE scanner thread for every resource on the node: the scans
#: contend for the same filesystem whichever file they read, and a second
#: grinding thread buys throughput at the price of the responsiveness this
#: whole arrangement exists to protect.
_window_scan_queue = deque()
_window_scan_pending = set()
_window_scan_state = threading.Lock()
_window_scan_wake = threading.Event()
_window_scan_thread = None

#: Tests flip this so a scheduled scan runs to completion inside `_scan_soon`,
#: which restores the old synchronous semantics -- `_quantization` on a cold
#: channel returns the exact window, having read the plane. In process the
#: conftest fixture sets it; the environment variable is for the suite's
#: subprocess nodes, which a fixture cannot reach. Production keeps it False:
#: the whole point is that no request thread ever waits on a scan.
_WINDOW_SCANS_INLINE = os.environ.get("PLEXORA_WINDOW_SCANS_INLINE") == "1"


def _scan_soon(resource, index, front=False):
    """Queue one channel's exact window scan, once, without waiting for it."""
    key = (resource.id, index)
    with _window_scan_state:
        if key in _window_scan_pending:
            return
        _window_scan_pending.add(key)
        if front:
            _window_scan_queue.appendleft((resource, index))
        else:
            _window_scan_queue.append((resource, index))
    if _WINDOW_SCANS_INLINE:
        _drain_window_scans()
        return
    _ensure_window_scanner()
    _window_scan_wake.set()


def _drain_window_scans():
    """Run every queued scan in the calling thread.

    The scanner's loop body, and the whole scanner in tests. A channel stays
    in `_window_scan_pending` while its scan runs, so a request arriving
    mid-scan cannot queue it twice; it leaves on completion OR failure, so a
    failed scan can be asked for again."""
    while True:
        with _window_scan_state:
            if not _window_scan_queue:
                return
            resource, index = _window_scan_queue.popleft()
        try:
            _scan_window(resource, index)
        finally:
            with _window_scan_state:
                _window_scan_pending.discard((resource.id, index))


def _ensure_window_scanner():
    global _window_scan_thread
    with _window_scan_state:
        if _window_scan_thread is not None and _window_scan_thread.is_alive():
            return
        _window_scan_thread = threading.Thread(
            target=_window_scan_loop, name="plexora-node-window-scan", daemon=True)
        _window_scan_thread.start()


def _window_scan_loop():
    from plexora.server.volume import chunk_cache

    # In the chunk cache's background lane: a scan reads every plane of a
    # channel, and its chunks must never be what a tile on screen waits for.
    with chunk_cache.background():
        while True:
            _window_scan_wake.wait()
            _window_scan_wake.clear()
            _drain_window_scans()


def _scan_window(resource, index):
    """One channel's full-resolution read, off the request path.

    Banked to the store the moment it finishes, so it is paid once per file
    per channel EVER, not per job. Installed into the in-process cache only if
    the pyramid it read is still the one that is open: a reload mid-scan means
    these numbers describe a file nobody is serving any more, and the store's
    fingerprint check makes the same call for the persistent copy.

    A failure is logged and dropped rather than raised. The requests that
    would have carried the exception have already been answered provisionally,
    and the next request for the channel queues the scan again.
    """
    from plexora.server.models import data_model

    if isinstance(index, tuple) and index[:1] == ("volume",):
        _scan_volume_window(resource, *index[1:])
        return
    opened = resource.opened
    if opened is None:
        with resource.lock:
            opened = _opened(resource)
    try:
        window = data_model.quantization_window_of(opened, index)
    except Exception as exc:
        _log.warning("window scan failed for %s[%s]: %s", resource.id, index, exc)
        return
    _remember_window(resource, index, window)
    if resource.opened is opened:
        derived = getattr(resource, "derived", None)
        if derived is not None:
            derived[("qwindow", index)] = window
            derived.pop(("qwindow-provisional", index), None)


def _scan_volume_window(resource, zs, ts, index):
    """A volume channel's exact window, read on the scan thread: through
    `_volume_window`, so a request computing the same one shares the read,
    and banked to the store like a plane's."""
    from types import SimpleNamespace

    try:
        with _reading(resource):
            base = _volume_source(resource, zs, ts)
            if base is None:
                return
            reading = SimpleNamespace(base=base, reading=f"{zs or ''}|{ts or ''}")
            _volume_window(resource, reading, index, "volume")
    except Exception as exc:
        _log.warning("volume window scan failed for %s[%s]: %s", resource.id, index, exc)


def _provisional_window(resource, index):
    """A window to serve with while the exact one is being read.

    The mean-pooled overview is already in memory, so its ceiling costs
    nothing -- but pooling averages hotspots down, so this can sit below the
    true full-resolution max, and the brightest pixels clip until the exact
    window lands. That is the deliberate trade: a transiently clipped tile
    over a node that answers nothing at all while it reads a whole plane.
    Never persisted, and dropped the moment the exact window arrives.
    """
    import numpy as np

    overview = getattr(resource, "opened_overview", None)
    if overview is not None:
        try:
            return (0.0, max(float(np.asarray(overview[index]).max()), 1.0))
        except Exception:
            pass
    # No overview to guess from. The dtype's ceiling is dim rather than
    # clipped, which is the recoverable direction -- sliders go up.
    try:
        from plexora.server.models import data_model

        plane = (resource.opened if hasattr(resource.opened, "dtype")
                 else data_model._zarr_level(resource.opened, 0))
        dtype = np.dtype(plane.dtype)
        if np.issubdtype(dtype, np.integer):
            return (0.0, float(np.iinfo(dtype).max))
    except Exception:
        pass
    return (0.0, 1.0)


def _quantization(resource, index):
    """(qmin, qmax) for one channel: exact when known, provisional until then.

    Exact means read from every full-resolution pixel -- cached in process for
    this job, or in the node's data root where the last job banked it; see
    `_stored_window` for why that second cache is the one that matters on a
    cluster. A channel nobody has ever paid for is different: the read is
    minutes of a cluster filesystem's time, and an earlier version ran it
    right here, inside whatever request arrived first. A page restoring its
    channels puts every worker behind the first two of those reads, and the
    node spends its first minutes answering nothing -- not even /health.
    Watched live on two clusters, wearing three successive fixes.

    So a miss answers immediately with `_provisional_window` and queues the
    real read on the scan thread -- at the front, because this channel has a
    viewer waiting on it. Anything rendered with the guess goes out uncachable
    (see `_image`), so the exact rendering replaces it on the next fetch.
    """
    derived = getattr(resource, "derived", None)
    if derived is None:
        derived = resource.derived = {}
    window = derived.get(("qwindow", index))
    if window is not None:
        return window
    stored = _stored_window(resource, index)
    if stored is not None:
        return _cached(resource, ("qwindow", index), lambda: stored)
    _scan_soon(resource, index, front=True)
    # Inline mode (tests) has finished the scan by this line; anywhere the
    # exact answer exists it beats the guess. Re-read through the resource:
    # the scan may have replaced `derived` wholesale.
    window = resource.derived.get(("qwindow", index))
    if window is not None:
        return window
    return _cached(resource, ("qwindow-provisional", index),
                   lambda: _provisional_window(resource, index))


def _windowed(resource, index):
    """The window plus whether it can be cached against.

    `exact` is judged against the very pair being returned rather than by
    asking the cache again: the scan thread can land the real window between
    those two looks, and blessing a provisional pair with a year-long max-age
    because the exact one arrived a millisecond later is precisely the
    staleness the flag exists to prevent.
    """
    from plexora.server.utils import brightfield

    if index == brightfield.RGB_CHANNEL_KEY:
        # A brightfield tile has no window to be provisional about: its samples
        # are already the 8-bit colour the scanner recorded, so the bytes never
        # change and the response is cacheable from the first request.
        return (None, None), True

    window = _quantization(resource, index)
    exact = (getattr(resource, "derived", None) or {}).get(("qwindow", index))
    return window, exact is not None and tuple(exact) == tuple(window)


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


def _image(encoded, mimetype, etag, durable=True):
    """A binary answer with the same caching contract the primary's routes use.

    The ETag embeds the resource's generation rather than the process's, which
    is the whole reason generations are per resource: a table reloading must
    not make an image node's tiles look stale.

    A response rendered under a provisional quantization window is denied that
    contract entirely: `no-store`, no ETag. The browser fetches these straight
    off the node, and a year-long max-age would keep the guess on screen long
    after the exact window landed; making it uncachable is what lets the exact
    rendering take over on the next fetch.
    """
    if not durable:
        response = send_file(io.BytesIO(encoded), mimetype=mimetype)
        response.headers["Cache-Control"] = "no-store"
        return response
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
