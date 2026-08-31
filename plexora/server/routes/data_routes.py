from plexora import app
from flask import make_response, render_template, request, Response, jsonify, abort, send_file
import io
from pathlib import Path
from plexora import get_config
from plexora.datasource import rename_channels
from plexora.server.models import data_model
from plexora.server.models.project import Project
# Same helper the import page's path inputs use: a path dragged in from a file
# manager, or copied on Windows, arrives wrapped in quotes.
from plexora.server.routes.import_routes import trim_filepath_quotes
from plexora.server.utils import channel_file
import gzip
import json
import orjson
import threading
from collections import OrderedDict
from os import walk
from urllib.parse import quote


@app.route('/init_database', methods=['GET'])
def init_database():
    datasource = request.args.get('datasource')
    data_model.init(datasource)
    resp = jsonify(success=True)
    return resp


@app.route('/config')
def serve_config():
    return get_config()


@app.route('/get_nearest_cell', methods=['GET'])
def get_nearest_cell():
    x = float(request.args.get('point_x'))
    y = float(request.args.get('point_y'))
    datasource = request.args.get('datasource')
    resp = data_model.query_for_closest_cell(x, y, datasource)
    return serialize_and_submit_json(resp)


# Gets a row based on the index
@app.route('/get_database_row', methods=['GET'])
def get_database_row():
    datasource = request.args.get('datasource')
    row = int(request.args.get('row'))
    resp = data_model.get_row(row, datasource)
    return serialize_and_submit_json(resp)


@app.route('/get_channel_names', methods=['GET'])
def get_channel_names():
    datasource = request.args.get('datasource')
    shortnames = bool(request.args.get('shortNames'))
    resp = data_model.get_channel_names(datasource, shortnames)
    return serialize_and_submit_json(resp)


@app.route('/get_all_cells/<dtype>/', methods=['GET'])
def get_all_cells(dtype):
    datasource = request.args.get('datasource')
    data_type = int if 'integer' == dtype else float
    start_keys = list(request.args.get('start_keys').split(','))
    resp = data_model.get_all_cells(datasource, start_keys, data_type)
    content = gzip.compress(resp.tobytes('C'))
    response = make_response(content)
    response.headers.set('Content-Type', 'application/octet-stream')
    response.headers['Content-length'] = len(content)
    response.headers['Content-Encoding'] = 'gzip'
    return response


@app.route('/get_centroid_manifest', methods=['GET'])
def get_centroid_manifest():
    datasource = request.args.get('datasource')
    resp = data_model.get_centroid_manifest(datasource)
    return serialize_and_submit_json(resp)


@app.route('/get_centroid_tiles', methods=['POST'])
def get_centroid_tiles():
    post_data = json.loads(request.data)
    datasource = post_data['datasource']
    level = int(post_data.get('level', 0))
    tiles = post_data.get('tiles', [])
    filter = post_data.get('filter', {})
    max_points = post_data.get('max_points')
    resp = data_model.get_centroid_tiles(datasource, level, tiles, filter, max_points)
    content = gzip.compress(resp.tobytes('C'))
    response = make_response(content)
    response.headers.set('Content-Type', 'application/octet-stream')
    response.headers['Content-length'] = len(content)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['X-Centroid-Record-Count'] = len(resp)
    return response


@app.route('/get_channel_cell_ids', methods=['GET'])
def get_channel_cell_ids():
    datasource = request.args.get('datasource')
    filter = json.loads(request.args.get('filter'))
    resp = data_model.get_channel_cells(datasource, filter)
    return serialize_and_submit_json(resp)


@app.route('/get_database_description', methods=['GET'])
def get_database_description():
    datasource = request.args.get('datasource')
    resp = data_model.get_datasource_description(datasource)
    return serialize_and_submit_json(resp)


def _unknown_channel(channel, exc):
    """404 and a sentence, rather than a 500 and a traceback.

    Both routes below are asked for a channel BY NAME, and a name can stop
    being one of this project's -- upload_channels renames every channel at
    once, and any page, saved channel list or plugin still holding the old
    spelling asks for something that is no longer there. That is a stale
    question, not a broken server, so it is answered as one.
    """
    return jsonify(success=False, unknown_channel=True, channel=channel,
                   error=str(exc)), 404


@app.route('/get_channel_gmm', methods=['GET'])
def get_channel_gmm():
    channel = request.args.get('channel')
    datasource = request.args.get('datasource')
    try:
        resp = data_model.get_channel_gmm(channel, datasource)
    except data_model.UnknownChannelError as exc:
        return _unknown_channel(channel, exc)
    return serialize_and_submit_json(resp)


@app.route('/get_image_channel_stats', methods=['GET'])
def get_image_channel_stats():
    channel = request.args.get('channel')
    datasource = request.args.get('datasource')
    try:
        resp = data_model.get_image_channel_stats(channel, datasource)
    except data_model.UnknownChannelError as exc:
        return _unknown_channel(channel, exc)
    return serialize_and_submit_json(resp)


@app.route('/get_segmentation_status', methods=['GET'])
def get_segmentation_status():
    datasource = request.args.get('datasource')
    return jsonify(data_model.get_segmentation_job_status(datasource))


@app.route('/resource_routing', methods=['GET'])
def resource_routing():
    """Where the BROWSER could fetch each node-backed resource from directly.

    Empty for every ordinary project -- `{"routes": {}}` -- which is what makes
    it safe for the viewer to ask unconditionally: nothing is probed, nothing is
    cached, and the page carries on exactly as it always has.

    This is a candidate, not a decision. Whether the browser can actually reach
    a node is a question only the browser can answer, and it answers it by
    probing (see `client/src/js/services/resourceRouting.js`). Three things have
    to be true at once for direct routing to work -- the address has to resolve
    from the browser's network, the node has to have been started with this
    viewer's origin in `--allow-origin`, and the token has to match -- and a
    server-side guess about any of them would be wrong in exactly the
    deployments this feature exists for. A failed probe falls back to the proxy,
    which always works when this server can reach the node.

    **The node token is in this response.** That is deliberate and it is why
    this route sits behind the app's own auth guard like everything else: the
    browser IS the user, the token rides as `?t=` rather than a header so a tile
    request stays free of a CORS preflight, and a node's token protects that
    node's files from the user's neighbours -- not from the user.
    """
    datasource = request.args.get('datasource')
    project = Project.find(datasource)
    if project is None or not project.resources:
        return jsonify(routes={})

    from plexora.server.models import nodes as node_registry

    tile_width = project.image.tile_width or 1024
    tile_height = project.image.tile_height or 1024
    routes = {}
    for kind, binding in project.resources.items():
        node = node_registry.find(binding.node)
        if node is None:
            # Registered against a node this machine has since forgotten. The
            # proxy will fail too, and `/resource_status` is what says so --
            # offering the browser an address we do not have is not better.
            continue
        query = f"t={quote(node.token)}&tw={tile_width}&th={tile_height}"
        base = node.browser_url.rstrip('/')
        routes[kind] = {
            "node": binding.node,
            "resource_id": binding.resource_id,
            # `browser_url` falls back to the primary's own endpoint, which is
            # right for a desktop or a tunnel where the two addresses are the
            # same loopback port, and is exactly what `browser_endpoint`
            # overrides for a portal.
            "endpoint": base,
            "health": f"{base}/node/v1/health?t={quote(node.token)}",
            "query": query,
            # Where a tile URL starts. The viewer appends
            # `<level>/<x>_<y>.png` to this exactly as it does to
            # `/generated/data/…/`, so `getTileUrl` never branches on which
            # kind of address it was given.
            #
            # `append_key` is the one shape difference between the two, and it
            # is a real one rather than an inconsistency: an image serves many
            # channels from one resource and names which in the path, while a
            # mask has exactly one plane and nothing to name.
            "tile_base": (f"{base}/node/v1/{'seg' if kind == 'segmentation' else 'image'}"
                          f"/{binding.resource_id}/tile/"),
            "append_key": kind != "segmentation",
        }
    return jsonify(routes=routes)


@app.route('/resource_status', methods=['GET'])
def resource_status():
    """Which of this project's resources cannot be read, and why.

    Empty for every ordinary project, which is what makes it safe for a page to
    ask unconditionally. Non-empty means a layer this project needs is not
    there, and this is what turns that absence into a sentence.

    Two ways to be absent, and BOTH have to be answered here:

    - It could not be read when the project loaded. The project opened anyway
      -- see `load_datasource` -- and the layers that needed that node are
      simply missing from what is in memory.
    - The node has left the map SINCE. `_ensure_loaded` is keyed on the project
      name, so a project loaded while its node was up keeps that shape for the
      life of the process: disconnect the node, reopen the project, and the
      load is skipped, the load-time record is still clean, and this route
      said everything was fine while the viewer drew a blank page and whatever
      tiles happened to still be in cache. That is the report this half exists
      for, and it is the commonest way to hit it -- disconnecting is a thing
      people do between looking at the same project twice.

    Still no probing. Whether a registered node is ANSWERING is a different
    question, asked on the first real read where the caller can degrade; this
    route only reads the registry, which is a local file and a fact about this
    process. The asymmetry is deliberate: the second half can only ever ADD a
    failure the load did not know about yet, never clear one, so a node that
    has come back still needs `/reload_datasource` before anything says so.
    """
    datasource = request.args.get('datasource')
    # Before reading the load-time record, because otherwise there might not be
    # one. The viewer asks for this while it is still setting itself up, and
    # nothing it has called by then loads the project -- `/resource_routing`
    # reads the project record only. So this used to race the real load and
    # answer out of whatever project was loaded BEFORE, which for the first
    # project opened in a fresh server is nothing at all: a clean bill of
    # health for a project that had not been looked at yet.
    if datasource in get_config():
        try:
            data_model.ensure_loaded(datasource)
        except Exception as exc:  # noqa: BLE001 -- reported, never raised
            # A project whose image has moved on disk fails to open at all, on
            # purpose (`load_datasource` keeps the image loud). That must not
            # take this route down with it: the one job here is to say what is
            # wrong, and the version of it that answers 500 to "what is wrong?"
            # is the version that leaves a blank page unexplained. Whatever is
            # known below -- the previous load's record, and the registry --
            # is still worth answering with.
            print(f"{datasource}: could not reload while reporting its "
                  f"resources -- {exc}")
    errors = {
        kind: data_model.resource_unavailable(datasource, kind)
        for kind in ("image", "segmentation", "table")
    }
    errors = {kind: why for kind, why in errors.items() if why}
    project = Project.find(datasource)
    for kind, why in _nodes_that_have_gone(project).items():
        errors.setdefault(kind, why)
    nodes = sorted({
        binding.node for kind, binding in (project.resources.items()
                                           if project else [])
        if kind in errors and binding.node
    })
    return jsonify(unavailable=errors, nodes=nodes,
                   reconnect=_reconnect_hint(nodes),
                   profiles=_profiles_for(nodes))


def _nodes_that_have_gone(project):
    """Resources whose node is no longer on this machine's map, by kind.

    A registry read, not a probe: "is this node registered here" is answered by
    a local file, costs nothing, and cannot hang. An entry that IS there may
    still be asleep or behind a dead tunnel, and that is not this function's
    question -- it is answered by the first read that needs it.

    The sentence is `providers.node.node_for`'s, word for word. The same
    absence reaches the user down two different paths -- a load that failed
    because the node was already gone, and a load that succeeded before it went
    -- and there is no version of this where they should read differently.
    """
    from plexora.server.models import nodes as node_registry

    if project is None or not project.resources:
        return {}
    registry = node_registry.load_all()
    return {
        kind: (f"data node {binding.node!r} is not connected to this Plexora. "
               f"Connect it and reopen this project.")
        for kind, binding in project.resources.items()
        if binding.is_node and binding.node and binding.node not in registry
    }


@app.route('/reload_datasource', methods=['POST'])
def reload_datasource():
    """Read this project again from scratch, for the case a node has come back.

    The one thing a browser reload cannot do. `_ensure_loaded` is keyed on the
    project NAME, so a project that opened with its image missing -- because
    the machine holding it was not connected -- keeps exactly that shape for
    the life of the process: reopening the page finds the name already loaded
    and skips the read entirely. Connecting the node changes nothing until
    something says "again", and this is the only thing that does.

    POST rather than GET: it discards loaded state and re-reads every resource
    of a project, which is not something a prefetch, a crawler or a stale
    bookmark should be able to set off.

    Answers with what is STILL unavailable, so the caller can tell "it worked"
    from "that machine is up and this resource is still not there" without a
    second round trip -- and without reloading a page onto the same absence.
    """
    datasource = request.args.get('datasource')
    if datasource not in get_config():
        abort(404)
    data_model.load_datasource(datasource, reload=True)
    unavailable = {
        kind: data_model.resource_unavailable(datasource, kind)
        for kind in ("image", "segmentation", "table")
    }
    return jsonify(success=True,
                   unavailable={k: v for k, v in unavailable.items() if v})


def _profiles_for(names):
    """Which saved profile, if any, this Plexora could bring each node back with.

    The other half of `_reconnect_hint` below, and the half that can be a
    button: when the connection belongs to a profile saved HERE, this server
    can open it -- `POST /settings/remotes/<profile>/connect?kind=node` -- and
    telling somebody to go and run a command instead would be advice to do by
    hand what the page is already able to do.

    Resolved from the PROFILES rather than out of the `managed_by` marker. The
    marker names the NODE (`connect:<node_name>`, because a profile with a
    `node_name` sets both from that), so reading a profile name out of it is
    right only when the two happen to be equal -- and the profile is what the
    Connect button posts to.

    A node missing from the map entirely is the ordinary case here rather than
    a disqualification: Disconnect forgets the entry on purpose, which is why
    the project can be pointing at a name the registry no longer has. What DOES
    disqualify a profile is an entry that is present and not marked as this
    connection's own -- that is somebody's hand-registered node under a
    colliding name, pointing at an address they maintain.
    """
    from plexora.server.models import nodes as node_registry
    from plexora.server.models import remotes as remote_store

    registry = node_registry.load_all()
    owners = {}
    for remote in sorted(remote_store.load_all().values(),
                         key=lambda item: item.name):
        node_name = remote.node_name or remote.name
        entry = registry.get(node_name)
        if entry is None or entry.managed_by == f"connect:{node_name}":
            owners.setdefault(node_name, remote.name)
    return [{"node": name, "profile": owners[name]}
            for name in names if name in owners]


def _reconnect_hint(names):
    """How to bring these nodes back, when a saved connection is what does it.

    A node that `plexora connect` set up has its address and token rewritten
    every session, so "check the address in Settings" is advice that cannot
    work -- the entry is not wrong, the tunnel is gone. Naming the command is
    the only actionable thing to say, and this server cannot run it: the
    command belongs on the machine that opened the tunnel, which is the user's
    own, which is precisely what is not reachable from here.
    """
    from plexora.server.models import nodes as node_registry

    for name in names:
        node = node_registry.find(name)
        managed = (node.extra or {}).get("managed_by") if node else None
        if managed and str(managed).startswith("connect:"):
            profile = str(managed).split(":", 1)[1]
            return (f"Reconnect with `plexora connect {profile}` on the "
                    f"computer you started it from." if profile else
                    "Reconnect with `plexora connect` on the computer you "
                    "started it from.")
    return None

def _channel_file_source():
    """The file the user chose, however they chose it.

    Two ways in, deliberately. A browser upload is the ordinary one. A path is
    for the case the upload cannot cover: on a cluster the browser is on a
    laptop and the marker list is beside the image on the remote filesystem,
    so there is nothing local to upload -- see the path row in
    views/channelNamesUpload.js. Both arrive as form fields, so the route reads
    one request shape rather than branching on content type.

    @returns (data, path, filename), the three arguments read_grid takes.
             Exactly one of `data` (uploaded bytes) and `path` (a file on the
             server's own disk) is set; `filename` decides the format either
             way.
    """
    upload = request.files.get('file')
    if upload is not None and upload.filename:
        return upload.read(), None, upload.filename

    raw = (request.form.get('path') or '').strip()
    if not raw:
        raise channel_file.ChannelFileError("Choose a file, or paste the path to one.")
    path = Path(trim_filepath_quotes(raw)).expanduser()
    if not path.is_file():
        raise channel_file.ChannelFileError(f"There is no file at {raw}")
    return None, path, path.name


@app.route('/upload_channels', methods=['POST'])
def upload_channels():
    """Rename an already-registered datasource's image channels from a list the
    user supplies -- lets them fix gating/channel auto-matching without
    re-registering the whole datasource (and re-running pyramid generation).

    Takes a CSV/TSV/TXT or an .xlsx/.xlsm, uploaded or named by path, and
    answers one of three ways:

      - **applied**, when the file says which names it holds without being
        asked: one column, and a length that is either the channel count or
        one more than it (a header row).
      - **needs_column**, when it does not -- a table of several columns has
        no such thing as "the" column, so the description of the file comes
        back for the picker in views/channelNamesUpload.js, and the second
        request names `column` and `has_header`.
      - **mismatch**, when the names are read but there is the wrong number of
        them. Nothing is applied: half a panel renamed and half left on
        Channel_12 is worse than the original, and impossible to see.

    `column`/`has_header` are the picker's answer. Absent, the file is read the
    way autodetect reads it.
    """
    datasource = (request.form.get('datasource') or '').strip()
    config = get_config()
    if datasource not in config:
        abort(422)

    # The mask's "Area" channel is not one of the image's -- it is inserted
    # when a segmentation mask is attached -- so it is not something the user
    # supplies a name for, and counting it would make every correct file look
    # one short.
    before = [c for c in config[datasource]['imageData'] if c['name'] != 'Area']
    n_channels = len(before)

    try:
        data, path, filename = _channel_file_source()
        grid = channel_file.read_grid(data=data, path=path, filename=filename)

        chosen = request.form.get('column')
        if chosen is None or chosen == '':
            has_header = channel_file.autodetect(grid, n_channels)
            if has_header is None and channel_file.width(grid) > 1:
                # The one answer this route cannot guess at. Everything the
                # picker draws goes back in the same response rather than in a
                # second inspect request: it is the same parse, and a file read
                # twice is a file that can be edited in between.
                return jsonify(
                    success=False,
                    needs_column=True,
                    **channel_file.describe(grid, n_channels, filename),
                )
            # A single column with a count that matches neither reading is not
            # a question -- there is nothing to pick. Read it the plain way and
            # let the length check below say so.
            column, has_header = 0, bool(has_header)
        else:
            column, has_header = int(chosen), request.form.get('has_header') == 'true'

        names = channel_file.names(grid, column, has_header)
    except channel_file.ChannelFileError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except ValueError:
        return jsonify(success=False, error="That column is not in the file."), 400

    try:
        rename_channels(datasource, names)
    except ValueError as exc:
        # The counts as numbers, beside the sentence. The modal states them in
        # its own words ("N names, M channels") and must not have to parse a
        # message to do it.
        return jsonify(
            success=False,
            error=str(exc),
            mismatch=True,
            marker_count=len(names),
            channel_count=n_channels,
            filename=filename,
        ), 400

    # Everything else that stored a channel by NAME has to move with it. The
    # saved channel list is the one that bites: it is what the sidebar rebuilds
    # its slots from on the next page load, so leaving it behind puts a slot on
    # screen for a channel that no longer exists. `before` is read above, from
    # the config as it was BEFORE rename_channels rewrote it.
    renames = {}
    for channel, renamed in zip(before, names):
        renames[channel['name']] = renamed
        renames[channel['fullname']] = renamed
    data_model.rename_saved_channels(datasource, renames)

    data_model.load_datasource(datasource, reload=True)
    # `names` goes back so the page can take the new names on in place --
    # main.js's adoptChannelNames. They are in imageData order, the one order
    # every index in the viewer is keyed on.
    return jsonify(success=True, names=names, channel_count=n_channels)

@app.route('/get_ome_metadata', methods=['GET'])
def get_ome_metadata():
    datasource = request.args.get('datasource')
    resp = data_model.get_ome_metadata(datasource)
    if hasattr(resp, "model_dump"):
        resp = resp.model_dump(mode="json")
    elif hasattr(resp, "dict"):
        resp = resp.dict()
    elif not resp:
        resp = {}
    # OME-Types handles jsonify itself, so skip the orjson conversion
    response = app.response_class(
        response=json.dumps(resp),
        mimetype='application/json'
    )
    return response


@app.route('/save_channel_list', methods=['POST'])
def save_channel_list():
    post_data = json.loads(request.data)

    datasource = post_data['datasource']
    map_channels = post_data['map_channels']
    active_channels = post_data['active_channels']
    list_colors = post_data['list_colors']
    list_ranges = post_data['list_ranges']
    list_channels = post_data['list_channels']

    data_model.save_channel_list(datasource, map_channels, active_channels, list_colors, list_ranges, list_channels)

    resp = jsonify(success=True)
    return resp

@app.route('/get_saved_channel_list', methods=['GET'])
def get_saved_channel_list():
    datasource = request.args.get('datasource')
    resp = data_model.get_saved_channel_list(datasource)
    return serialize_and_submit_json(resp)

_tile_png_cache = OrderedDict()
_tile_png_cache_lock = threading.Lock()
_TILE_PNG_CACHE_MAX = 1500


def _get_tile_png_bytes(datasource, channel, level, tile, quality):
    # ensure_loaded() must run BEFORE sampling load_generation: loading is what
    # bumps the generation, so sampling first would file the encoded bytes under
    # the pre-load generation and every later request would miss. Keying on the
    # generation means a datasource reload (which may regenerate segmentation,
    # per refresh_segmentation_mapping) naturally invalidates cached tiles
    # without cross-module cache access. `quality` is part of the key so
    # default/hd/legacy variants of the same tile don't collide.
    generation = data_model.ensure_loaded(datasource)
    key = (generation, datasource, channel, level, tile, quality)
    with _tile_png_cache_lock:
        cached = _tile_png_cache.get(key)
        if cached is not None:
            _tile_png_cache.move_to_end(key)
            return cached

    encoded, mimetype = data_model.encode_tile(datasource, channel, level, tile, quality)

    with _tile_png_cache_lock:
        _tile_png_cache[key] = (encoded, mimetype)
        _tile_png_cache.move_to_end(key)
        while len(_tile_png_cache) > _TILE_PNG_CACHE_MAX:
            _tile_png_cache.popitem(last=False)
    return encoded, mimetype


# E.G /generated/data/melanoma/channel_00_files/13/16_18.png
# ?q=hd requests the full-precision 16-bit path for channel tiles; anything
# else (including the param being absent) uses the fast default WebP path.
# Segmentation tiles ignore `q` entirely -- see data_model.encode_tile.
@app.route('/generated/data/<string:datasource>/<string:channel>/<string:level>/<string:tile>')
def generate_png(datasource, channel, level, tile):
    # Frontend can now decode WebP (createImageBitmap-based u8 path, verified
    # against real data) -- an absent `q` is the true default: fast/small
    # WebP. `q=legacy` is kept as an explicit escape hatch back to the
    # original uncompressed PNG behavior if ever needed.
    quality = request.args.get('q', 'webp')
    encoded, mimetype = _get_tile_png_bytes(datasource, channel, level, tile, quality)

    # Tile bytes are immutable for a given load_generation, so let the browser
    # keep them: without this Flask's send_file() emits `Cache-Control:
    # no-cache` and every pan back over visited ground is a fresh round trip
    # through the (globally serialized) zarr/tifffile reader. The generation is
    # in the ETag rather than the URL so a reload invalidates without the
    # frontend having to rewrite tile URLs -- a stale conditional request gets
    # a 200 with fresh bytes instead of a wrong 304.
    etag = f'"{data_model.load_generation}-{datasource}-{channel}-{level}-{tile}-{quality}"'
    if request.headers.get('If-None-Match') == etag:
        response = app.response_class(status=304)
    else:
        response = send_file(io.BytesIO(encoded), mimetype=mimetype)
    response.headers['ETag'] = etag
    response.headers['Cache-Control'] = 'private, max-age=31536000'
    return response

# The viewer mini-map's source: one channel's whole tissue, ~200-400 px, in the
# same [0, 255] domain as the WebP tiles. Separate from the tile route on
# purpose -- see data_model.generate_channel_overview for why no tile level is
# reliably a single whole-image tile.
@app.route('/generated/overview/<string:datasource>/<string:channel>')
def generate_overview(datasource, channel):
    # Before sampling load_generation, for the same reason as the tile path:
    # loading is what bumps the generation.
    generation = data_model.ensure_loaded(datasource)

    etag = f'"{generation}-overview-{datasource}-{channel}"'
    if request.headers.get('If-None-Match') == etag:
        response = app.response_class(status=304)
    else:
        encoded = data_model.generate_channel_overview(datasource, channel)
        if encoded is None:
            abort(404)
        response = send_file(io.BytesIO(encoded), mimetype='image/webp')
    response.headers['ETag'] = etag
    response.headers['Cache-Control'] = 'private, max-age=31536000'
    return response


def serialize_and_submit_json(data):
    response = app.response_class(
        response=orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY),
        mimetype='application/json'
    )
    return response

