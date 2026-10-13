from plexora import app
from flask import make_response, render_template, request, Response, jsonify, abort, send_file
import io
from pathlib import Path
from plexora import get_config
from plexora.datasource import (rename_channels, rename_layer_channels,
                                set_pixel_size as _set_pixel_size)
from plexora.server.models import data_model, layer_sources
from plexora.server.models.project import Project
# Same helper the import page's path inputs use: a path dragged in from a file
# manager, or copied on Windows, arrives wrapped in quotes.
from plexora.server.routes.import_routes import trim_filepath_quotes
from plexora.server.utils import channel_file
from plexora.telemetry import performance as _perf
import gzip
import json
import orjson
import threading
import logging
from collections import OrderedDict
from os import walk
from urllib.parse import quote

_log = logging.getLogger(__name__)


@app.route('/init_database', methods=['GET'])
def init_database():
    datasource = request.args.get('datasource')
    data_model.init(datasource)
    resp = jsonify(success=True)
    return resp


@app.route('/config')
def serve_config():
    """Every project, plus what each one's viewer draws.

    `layers` is computed, never stored: the reference image, the mask and the
    centroids are synthesized from `ImageSpec`, `SegmentationSpec` and the
    table's coordinate roles (see Project.all_layers), and registered layers
    follow. Added to a COPY of each entry so nothing that later saves a project
    can write a derived key back into config.json.

    Kept deliberately small. This route's whole response reaches the client on
    every viewer boot, for every project the user has -- so anything per-layer
    bigger than a few hundred bytes (a gene vocabulary, a category list) belongs
    in that layer's own derived manifest and not here.
    """
    from plexora.server.models.project import Project

    config = get_config()
    out = {}
    for name, entry in (config or {}).items():
        project = Project.from_entry(name, entry)
        out[name] = {**entry,
                     "layers": [layer.to_entry() for layer in project.all_layers]}
    return out


@app.route('/get_nearest_cell', methods=['GET'])
def get_nearest_cell():
    x = float(request.args.get('point_x'))
    y = float(request.args.get('point_y'))
    datasource = request.args.get('datasource')
    # The frame on screen, for a time series' object table: its cells only.
    t = request.args.get('t')
    # The planes on screen of a z-stack (inclusive; equal for one plane):
    # the cells whose extent overlaps them.
    z0, z1 = request.args.get('z0'), request.args.get('z1')
    planes = None
    if z0 not in (None, "") and z1 not in (None, ""):
        try:
            planes = tuple(sorted((int(z0), int(z1))))
        except ValueError:
            return jsonify({'error': 'z0 and z1 are plane numbers'}), 400
    resp = data_model.query_for_closest_cell(x, y, datasource,
                                             t=int(t) if t not in (None, "") else None,
                                             planes=planes)
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
    t = request.args.get('t')
    resp = data_model.get_all_cells(datasource, start_keys, data_type,
                                    t=int(t) if t not in (None, "") else None)
    content = gzip.compress(resp.tobytes('C'))
    response = make_response(content)
    response.headers.set('Content-Type', 'application/octet-stream')
    response.headers['Content-length'] = len(content)
    response.headers['Content-Encoding'] = 'gzip'
    return response


@app.route('/get_not_called', methods=['GET'])
def get_not_called():
    """One byte per table row (the order /get_all_cells sends ids in): 1 where
    QC says the cell cannot be called for `marker` -- inside a QC region, or
    flagged unreliable for it -- under the current call mode
    (`cell_exclusions.calls`); 0 elsewhere. All zeros without QC. A gate is
    drawn grey there, never positive."""
    import numpy as np

    from plexora import api

    datasource = request.args.get('datasource')
    marker = request.args.get('marker') or None
    ds = api.project_data(datasource)
    frame = ds.table.geometry() if ds.table.available else None
    n = int(frame.height) if frame is not None else 0
    mask = ds.not_called([marker] if marker else []) if n else None
    out = np.zeros(n, dtype=np.uint8)
    if mask is not None and mask.shape[0] == n:
        out[mask] = 1
    content = gzip.compress(out.tobytes('C'))
    response = make_response(content)
    response.headers.set('Content-Type', 'application/octet-stream')
    response.headers['Content-length'] = len(content)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['X-Not-Called-Count'] = int(out.sum())
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


@app.route('/get_consistency_report', methods=['GET'])
def get_consistency_report():
    """Every way this project's table, mask and image fail to describe the same
    sample -- usually none.

    Core's, not any one plugin's. Nothing in the three files says they belong
    together, so every tool that draws per-cell results over the image has this
    question; answering it here is what stops three tools answering it three
    ways. See server/models/consistency.py.
    """
    datasource = request.args.get('datasource')
    return jsonify(data_model.get_consistency_report(datasource))


@app.route('/fix_table', methods=['POST'])
def fix_table():
    """Correct how this project's table is read, so its cells sit on the mask.

    The Fix beside a consistency finding (`fix: "table"`). The correction is
    found by trying candidates against the mask (models/table_correction.py),
    recorded on the project and applied every time the table is read -- the
    file is never written, and `/undo_table_fix` removes the record.

    Answers `{success, applied, message, reloaded}`. Finding nothing to apply
    is a success with `applied: false` and the reason: "the table already lines
    up" and "nothing tried does" are answers the user should read, not errors.
    """
    from plexora.server.models import table_correction
    from plexora.server.models.project import Project
    from plexora.server.routes.tool_routes import _reload_or_restore

    payload = request.get_json(silent=True) or {}
    datasource = payload.get('datasource') or request.args.get('datasource')
    project = Project.find(datasource)
    if project is None or not project.has_table:
        return jsonify(success=False, error="This project has no table to correct."), 404
    proposal = data_model.propose_table_fix(datasource)
    correction = proposal.get("correction")
    if not correction:
        return jsonify(success=True, applied=False, reloaded=False,
                       message=proposal.get("message") or "")
    Project.mutate(datasource, lambda p: table_correction.corrected_project(p, correction))
    error = _reload_or_restore(datasource, project)
    if error:
        return jsonify(success=False, error=error), 400
    return jsonify(success=True, applied=True, reloaded=True,
                   message=proposal.get("message") or "", correction=correction)


@app.route('/undo_table_fix', methods=['POST'])
def undo_table_fix():
    """Read this project's table exactly as its file says again."""
    from plexora.server.models import table_correction
    from plexora.server.models.project import Project
    from plexora.server.routes.tool_routes import _reload_or_restore

    payload = request.get_json(silent=True) or {}
    datasource = payload.get('datasource') or request.args.get('datasource')
    project = Project.find(datasource)
    if project is None or not project.dataset:
        return jsonify(success=False, error="This project has no table."), 404
    if not project.dataset.correction:
        return jsonify(success=True, reloaded=False)
    Project.mutate(datasource, table_correction.uncorrected_project)
    error = _reload_or_restore(datasource, project)
    if error:
        return jsonify(success=False, error=error), 400
    return jsonify(success=True, reloaded=True)


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


#: Both routes below take `view=` (a slab projection has statistics of its own;
#: see data_model._slab_view). Absent is exactly the call it always was.
@app.route('/get_channel_gmm', methods=['GET'])
def get_channel_gmm():
    from plexora.server.volume.viewspec import BadView

    channel = request.args.get('channel')
    datasource = request.args.get('datasource')
    view = request.args.get('view') or ''
    try:
        resp = (data_model.get_channel_gmm(channel, datasource, view) if view
                else data_model.get_channel_gmm(channel, datasource))
    except data_model.UnknownChannelError as exc:
        return _unknown_channel(channel, exc)
    except BadView as exc:
        return jsonify({'error': str(exc)}), 400
    return serialize_and_submit_json(resp)


@app.route('/get_image_channel_stats', methods=['GET'])
def get_image_channel_stats():
    from plexora.server.volume.viewspec import BadView

    channel = request.args.get('channel')
    datasource = request.args.get('datasource')
    view = request.args.get('view') or ''
    try:
        resp = (data_model.get_image_channel_stats(channel, datasource, view) if view
                else data_model.get_image_channel_stats(channel, datasource))
    except data_model.UnknownChannelError as exc:
        return _unknown_channel(channel, exc)
    except BadView as exc:
        return jsonify({'error': str(exc)}), 400
    return serialize_and_submit_json(resp)


@app.route('/get_segmentation_status', methods=['GET'])
def get_segmentation_status():
    datasource = request.args.get('datasource')
    return jsonify(data_model.get_segmentation_job_status(datasource))


@app.route('/get_table_status', methods=['GET'])
def get_table_status():
    """Where preparing this project's cell table has got to.

    Polled by the browser WHILE the save that is doing the work is still
    outstanding, which is why the load itself stays synchronous: the routes
    keep validating a user's answer inline and restoring the previous project
    when it is wrong, and this only adds somewhere for the work to report from.
    Waitress is multi-threaded, so this is answered while that request runs.
    """
    datasource = request.args.get('datasource')
    return jsonify(data_model.get_table_job_status(datasource))


@app.route('/get_load_status', methods=['GET'])
def get_load_status():
    """Where opening this project has got to -- table, mask, image.

    The viewer's navbar chip (views/loadProgress.js) polls this while its first
    request is waiting on the load, the same arrangement as get_table_status,
    which it folds in when the table is the step that is running.
    """
    datasource = request.args.get('datasource')
    return jsonify(data_model.get_load_status(datasource))


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
        if not node.browser_reachable:
            # A node the browser is known not to be able to reach -- today that
            # means a notebook kernel's, whose loopback address means the user's
            # own laptop from where the browser stands. Skipped rather than
            # probed, because the probe would carry this node's token to
            # whatever is listening on that port over there.
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
        if kind == "image" and project.image.is_volumetric:
            # A node has no project, so a volume's tiles carry the project's
            # z/t reading as well as their view. Fetched direct, the viewer
            # appends `view=<token>` (the default plane spelled as itself, never
            # empty), this, and `az=<anisotropy>` for a section -- the
            # parameters the proxy sends (providers/node.py `_view_params`).
            # Only for a volume: a planar image's route is unchanged.
            from urllib.parse import urlencode

            routes[kind]["volume_query"] = urlencode(
                {key: value for key, value in (("zs", project.image.z_semantics),
                                               ("ts", project.image.t_semantics))
                 if value})
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
            _log.warning("%s: could not reload while reporting its resources -- %s",
                         datasource, exc)
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
    masks = [] if 'segmentation' in errors else (
        _node_mask_report(project) or _local_mask_report(datasource))
    return jsonify(unavailable=errors, nodes=nodes,
                   reconnect=_reconnect_hint(nodes),
                   profiles=_profiles_for(nodes),
                   masks=masks)


#: (load_generation, node, resource id) of each node mask this process has
#: already asked to convert again. Once per load, not once per poll: a failure
#: that recurs -- a full disk, an unreadable file -- would otherwise be retried
#: every two seconds for as long as the viewer is open. Reopening the project
#: (a new load) tries again.
_mask_retries = set()
_mask_retries_lock = threading.Lock()


def _local_mask_report(datasource):
    """The one thing worth saying about a mask on this machine: that its
    pyramid is not beside it because its folder could not be written.

    Inferred from where the pyramid ended up, which `refresh_segmentation_
    mapping` already decided: beside the mask when that folder takes writes,
    under this project's derived root when not. Silent when the user asked
    for project-side output, since then it is where they put it.
    """
    from plexora import paths

    entry = (get_config() or {}).get(datasource) or {}
    served, source = entry.get('segmentation'), entry.get('segmentationSource')
    if not served or not source or served == source:
        return []
    if paths.mask_output_preference() == 'project':
        return []
    served_dir, source_dir = Path(served).parent, Path(source).parent
    # Probed rather than inferred alone: projects imported before pyramids
    # were written beside their masks keep theirs in the derived root, and
    # telling those users their folder is read-only would be untrue.
    if served_dir == source_dir or paths.is_writable(source_dir):
        return []
    return [{
        'node': None, 'id': None, 'state': 'ready',
        'warning': (f"{source_dir} is read-only for this account, so the "
                    f"cell-mask pyramid is kept in {served_dir}."),
    }]


def _node_mask_report(project):
    """How this project's node-served cell mask is doing, as a list of one.

    The one exception to "no probing" above, and a narrow one: a single status
    GET with a short timeout, for a mask on a node that is on the map. It is
    what lets the viewer say "Preparing the cell mask on hms-o2" instead of
    drawing the unconverted mask with nothing to explain why it is slow -- and
    what lets opening the project retry a conversion that failed, which before
    needed somebody to reshare the file by hand.

    Unreachable is not reported here: that is `unavailable`'s job, decided on
    a real read.
    """
    from plexora import nodes as node_api
    from plexora.server.providers.base import ResourceUnavailable

    binding = project.resources.get('segmentation') if project else None
    if binding is None or not getattr(binding, 'node', None):
        return []
    node, resource_id = binding.node, binding.resource_id
    row = {'node': node, 'id': resource_id}
    try:
        described = node_api.resource_status(node, resource_id, timeout=5.0)
    except ResourceUnavailable:
        return []
    except Exception as exc:  # noqa: BLE001 -- reported, never raised
        text = str(exc)
        if 'does not serve that resource' in text:
            text = (f"{node} no longer serves this mask -- the file it was "
                    f"shared from may have moved. Import it again.")
        return [dict(row, state='error', error=text)]

    if described.get('state') == 'error':
        key = (data_model.load_generation, node, resource_id)
        with _mask_retries_lock:
            first = key not in _mask_retries
            _mask_retries.add(key)
        if first:
            try:
                described = node_api.prepare_again(node, resource_id, timeout=10.0)
            except Exception as exc:  # noqa: BLE001 -- the old error still stands
                described = dict(described, error=f"{described.get('error')} "
                                                  f"(retrying failed: {exc})")
    # Ready rows too: a viewer that watched this mask convert learns from one
    # that it is done, and which version to fetch.
    state = described.get('state') or 'ready'
    return [dict(
        row, state=state,
        error=described.get('error'),
        warning=described.get('warning'),
        progress=described.get('progress'),
        mode=described.get('mask_mode'),
        # What the viewer puts on the label tile URL once this is ready, so
        # the pyramid's tiles never collide with the raw mask's in the
        # browser's year-long cache.
        version=f"{described.get('generation', 0)}-{described.get('mask_mode') or ''}",
    )]


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
    # Every other tab on this project re-reads too -- this is what makes a
    # notebook's `PlexoraViewer.refresh()` visibly refresh an embedded viewer.
    from plexora.server.models import viewer_sessions
    viewer_sessions.publish(datasource, "core", "reload", origin="server")
    unavailable = {
        kind: data_model.resource_unavailable(datasource, kind)
        for kind in ("image", "segmentation", "table")
    }
    return jsonify(success=True,
                   unavailable={k: v for k, v in unavailable.items() if v})


@app.route('/image_status', methods=['GET'])
def image_status():
    """Whether this project's image can be read, and how it fails if not.

    What a blank canvas cannot say for itself. Every way an image can be
    unreadable reaches the browser as a 500 per tile, or -- when the load
    fails before any tile is asked for -- as nothing at all, and the three
    causes want three different sentences: a file that has moved, one the
    process may not read, and one whose bytes are not an image.

    GET and side-effect-free in the sense that matters: it may complete a load
    that was going to happen anyway on the next tile, and it never discards
    loaded state the way /reload_datasource does.
    """
    datasource = request.args.get('datasource')
    if datasource not in get_config():
        abort(404)
    return jsonify(success=True, **data_model.image_status(datasource))


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

    `layer` names a REGISTERED LAYER of the same project instead of its
    reference image. One route for both because it is one flow -- the file,
    the column, the count -- and a second copy of it would be a second place
    for a spreadsheet to be read differently. Only the two ends differ: how
    many channels there are to name, and which record is rewritten.
    """
    datasource = (request.form.get('datasource') or '').strip()
    layer_id = (request.form.get('layer') or '').strip()
    config = get_config()
    if datasource not in config:
        abort(422)

    if layer_id:
        project = Project.find(datasource)
        layer = project.layer(layer_id) if project else None
        if layer is None:
            abort(404)
        # Every plane the layer has. There is no "Area" here: that channel is
        # inserted into the REFERENCE image when a mask is attached, and a
        # registered layer never carries one.
        before = [dict(channel) for channel in layer.channels]
    else:
        before = _reference_channels(config, datasource)
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
        if layer_id:
            rename_layer_channels(datasource, layer_id, names)
        else:
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

    if layer_id:
        # Nothing else to move. A layer's saved channel list lives in its own
        # `render.channels`, where every row carries the INDEX beside the name
        # and the panel resolves by index first -- so it survives a rename
        # untouched (layerChannelPanel.savedRowsFor). There is no datasource to
        # reload either: the layer's tiles are addressed by the key in `src`,
        # which a rename does not touch.
        return jsonify(success=True, names=names, channel_count=n_channels)

    return _finish_reference_rename(datasource, before, names)


def _reference_channels(config, datasource):
    """The reference image's channels a user names, in imageData order.

    The mask's "Area" channel is not one of the image's -- it is inserted when
    a segmentation mask is attached -- so it is not something the user supplies
    a name for, and counting it would make every correct list look one short.
    """
    return [c for c in config[datasource]['imageData'] if c['name'] != 'Area']


def _finish_reference_rename(datasource, before, names):
    """What follows a rename of the reference image's channels, and the answer.

    Everything else that stored a channel by NAME has to move with it. The
    saved channel list is the one that bites: it is what the sidebar rebuilds
    its slots from on the next page load, so leaving it behind puts a slot on
    screen for a channel that no longer exists. `before` must be read from the
    config as it was BEFORE rename_channels rewrote it.
    """
    renames = {}
    for channel, renamed in zip(before, names):
        renames[channel['name']] = renamed
        renames[channel['fullname']] = renamed
    data_model.rename_saved_channels(datasource, renames)

    data_model.load_datasource(datasource, reload=True)
    # `names` goes back so the page can take the new names on in place --
    # main.js's adoptChannelNames. They are in imageData order, the one order
    # every index in the viewer is keyed on.
    return jsonify(success=True, names=names, channel_count=len(before))


@app.route('/rename_channels', methods=['POST'])
def rename_channels_json():
    """Rename the reference image's channels from a list, as JSON.

    The Image card's "Paste channel names" (views/layerManager.js): the names
    come from another image's copy rather than from a file, so there is no file
    to read and no column to choose, and this is /upload_channels without its
    front half. The list is COMPLETE -- one name per non-Area channel, in
    imageData order -- because the page has already merged the copied names
    onto this image's own (services/renderClipboard.js mergeNames); a partial
    paste is the page's decision, never a guess made here.

        {"datasource": "<name>", "names": ["DAPI", "CD3", ...]}

    Refused with 400, and nothing renamed, for anything that is not a list of
    distinct non-blank names of the right length.
    """
    body = request.get_json(silent=True) or {}
    datasource = str(body.get('datasource') or '').strip()
    config = get_config()
    if datasource not in config:
        abort(422)
    names = body.get('names')
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return jsonify(success=False, error="names must be a list of strings."), 400
    names = [n.strip() for n in names]
    if any(not n for n in names):
        return jsonify(success=False, error="A channel name cannot be blank."), 400
    if len(set(names)) != len(names):
        return jsonify(success=False, error="Two channels cannot have the same name."), 400

    before = _reference_channels(config, datasource)
    try:
        rename_channels(datasource, names)
    except ValueError as exc:
        return jsonify(
            success=False,
            error=str(exc),
            mismatch=True,
            marker_count=len(names),
            channel_count=len(before),
        ), 400
    return _finish_reference_rename(datasource, before, names)

@app.route('/get_ome_metadata', methods=['GET'])
def get_ome_metadata():
    datasource = request.args.get('datasource')
    # Already a plain dict, and already carries `pixel_size_source` -- the
    # model does the ome_types conversion now, because laying the project's own
    # calibration over the file's needed to happen somewhere that could read
    # the values. The isinstance ladder that used to be here is kept below only
    # so a reader that hands back a model still serializes.
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


@app.route('/set_pixel_size', methods=['POST'])
def set_pixel_size():
    """Record (or clear) what one pixel is worth, for a project whose image
    file never said.

    The viewer's calibration control posts here and then re-reads
    `/get_ome_metadata`, rather than being handed the new number back and
    trusting it: the merge that decides `manual` vs `metadata` lives in one
    place, and a control that drew its own answer could disagree with the
    scale bar beside it.

    An empty or non-positive value clears the calibration, which is what the
    control's X does -- back to a bar counted in pixels, rather than a number
    nobody stands behind.
    """
    payload = request.get_json(silent=True) or {}
    datasource = str(payload.get('datasource') or '').strip()
    config = get_config()
    if datasource not in config:
        abort(422)

    raw = payload.get('value')
    try:
        value = float(raw) if raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        return jsonify(success=False,
                       error="That is not a number of microns per pixel."), 400
    if value < 0 or value != value or value in (float("inf"), float("-inf")):
        return jsonify(success=False,
                       error="Microns per pixel has to be a positive number."), 400

    axis = str(payload.get('axis') or 'xy')
    if axis not in ('xy', 'z', 't'):
        return jsonify(success=False, error="axis is xy, z or t"), 400
    pixel_size = _set_pixel_size(
        datasource, value or None, unit=payload.get('unit'), axis=axis)
    data_model.load_datasource(datasource, reload=True)
    return jsonify(success=True, pixel_size=pixel_size, axis=axis)


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

@app.route('/view_transform/<string:datasource>', methods=['GET'])
def get_view_transform(datasource):
    """How this image is turned and mirrored in the viewer.

    The default for an image that was never turned, so the client never has to
    tell "unsaved" apart from "upright". Read once at boot (main.js).
    """
    if Project.find(datasource) is None:
        abort(404)
    return jsonify(data_model.get_view_transform(datasource))


@app.route('/view_transform/<string:datasource>', methods=['PUT'])
def put_view_transform(datasource):
    """Store the orientation core's Rotate and Flip tools set.

    Written by services/viewTransform.js, debounced there, so a slider drag
    arrives as one request rather than one per frame.
    """
    if Project.find(datasource) is None:
        abort(404)
    try:
        data_model.save_view_transform(datasource, request.get_json(silent=True))
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    return jsonify(success=True)


@app.route('/get_saved_channel_list', methods=['GET'])
def get_saved_channel_list():
    datasource = request.args.get('datasource')
    resp = data_model.get_saved_channel_list(datasource)
    return serialize_and_submit_json(resp)

class _EncodedTiles(OrderedDict):
    """Encoded tiles, least recently used first; `nbytes` is what they hold
    (kept by `_keep_tile`, zeroed by `clear`)."""

    def __init__(self):
        super().__init__()
        self.nbytes = 0

    def clear(self):
        super().clear()
        self.nbytes = 0


def _tile_cache_budget():
    """`PLEXORA_TILE_CACHE_MB`, else a sixteenth of the memory this process
    was given, 128 MB to 2 GB -- a data node's rule for its own tile cache
    (node/api.py `_tile_cache_budget`)."""
    import os

    raw = os.environ.get("PLEXORA_TILE_CACHE_MB")
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


#: Encoded tiles already sent, so a pan back or a plane stepped back to is
#: answered from memory.
#:
#: Budgeted in BYTES, not entries. It used to be 1500 tiles, which is 75-450
#: MB of 8-bit WebP tiles and 3 GB of 16-bit ones (a stored PNG of a 1024 px
#: tile is 2 MB). `_TILE_PNG_CACHE_MAX` remains as a ceiling on the count.
_tile_png_cache = _EncodedTiles()
_tile_png_cache_lock = threading.Lock()
_TILE_PNG_CACHE_MAX = 20000
_TILE_PNG_CACHE_BYTES = _tile_cache_budget()
_tile_png_stats = {"hits": 0, "misses": 0}


def _tile_key(generation, datasource, channel, level, tile, quality, view="", window=None):
    """The tile cache's key for one tile. The view only when there is one, so
    a planar project's key is the tuple it has always been."""
    return ((generation, datasource, channel, level, tile, quality) + ((view,) if view else ())
            + ((window,) if window else ()))


def _keep_tile(key, value):
    """File `(bytes, mimetype)` under `key`, evicting the least recently used
    past the budget -- never the tile just kept: one bigger than the whole
    budget is still worth answering the next request for it from."""
    size = len(value[0])
    with _tile_png_cache_lock:
        if not _tile_png_cache:
            _tile_png_cache.nbytes = 0
        previous = _tile_png_cache.pop(key, None)
        if previous is not None:
            _tile_png_cache.nbytes -= len(previous[0])
        _tile_png_cache[key] = value
        _tile_png_cache.nbytes += size
        while len(_tile_png_cache) > 1 and (len(_tile_png_cache) > _TILE_PNG_CACHE_MAX
                                            or _tile_png_cache.nbytes > _TILE_PNG_CACHE_BYTES):
            _, dropped = _tile_png_cache.popitem(last=False)
            _tile_png_cache.nbytes -= len(dropped[0])


def tile_cache_stats() -> dict:
    """What the encoded tile cache holds (`/volume/<ds>/perf`)."""
    with _tile_png_cache_lock:
        return {"entries": len(_tile_png_cache), "bytes": _tile_png_cache.nbytes,
                "budget": _TILE_PNG_CACHE_BYTES, **_tile_png_stats}


def _tiles_ahead(generation, datasource, channel, level, tile, quality, view, window, hit):
    """Make this tile at the planes ahead of the hand (prefetch.py
    `TILE_PREFETCHER`) when it is a plane of a stack decoded a plane at a time
    (`data_model.tiles_ahead_of`): encoded in the background and filed under
    the key this route would file it under, so the browser's standby request
    for the next plane is answered from memory. Never waited on."""
    from dataclasses import replace

    from plexora.server.volume.prefetch import TILE_PREFETCHER

    plan = data_model.tiles_ahead_of(datasource, channel, view)
    if plan is None:
        return
    spec, depth = plan

    def view_at(z):
        return replace(spec, z=int(z)).token()

    def held(z):
        key = _tile_key(generation, datasource, channel, level, tile, quality, view_at(z), window)
        with _tile_png_cache_lock:
            return key in _tile_png_cache

    def encode(z):
        at = view_at(z)
        key = _tile_key(generation, datasource, channel, level, tile, quality, at, window)
        if held(z) or data_model.load_generation != generation:
            return False
        try:
            with data_model.reading_ahead():
                value = data_model.encode_tile(datasource, channel, level, tile, quality, at,
                                               window=window)
        except data_model.NotLoaded:
            return False
        if data_model.load_generation != generation:
            return False
        _keep_tile(key, value)
        return True

    region = (generation, datasource, channel, level, tile, quality, window, spec.t)
    try:
        TILE_PREFETCHER.after_plane_tile(region, spec.z, depth, encode, held, hit=hit)
    except Exception:  # noqa: BLE001 -- a read ahead is never the tile's error
        _log.debug("tile read-ahead not queued", exc_info=True)


def _get_tile_png_bytes(datasource, channel, level, tile, quality, view="", window=None):
    # ensure_loaded() must run BEFORE sampling load_generation: loading is what
    # bumps the generation, so sampling first would file the encoded bytes under
    # the pre-load generation and every later request would miss. Keying on the
    # generation means a datasource reload (which may regenerate segmentation,
    # per refresh_segmentation_mapping) naturally invalidates cached tiles
    # without cross-module cache access. `quality` is part of the key so
    # default/hd/legacy variants of the same tile don't collide.
    generation = data_model.ensure_loaded(datasource)
    from plexora.server.volume.plane_view import served_final, served_steps

    # This thread's previous tile is not this one's -- a cached tile included,
    # which would otherwise be sent with the last section's drawn-up flags.
    served_steps()
    key = _tile_key(generation, datasource, channel, level, tile, quality, view, window)
    with _tile_png_cache_lock:
        cached = _tile_png_cache.get(key)
        if cached is not None:
            _tile_png_cache.move_to_end(key)
            _tile_png_stats["hits"] += 1
        else:
            _tile_png_stats["misses"] += 1
    if cached is not None:
        _perf.note("cache", "hit")
        if view:
            _tiles_ahead(generation, datasource, channel, level, tile, quality, view, window,
                         hit=True)
        return cached

    _perf.note("cache", "miss")
    encoded, mimetype = data_model.encode_tile(datasource, channel, level, tile, quality,
                                               view, window=window)
    if served_steps(reset=False):
        _perf.note("served", str(served_steps(reset=False)))
        # A section drawn up from a coarser level (its read was over budget):
        # neither kept here nor by the browser -- the sharp one replaces it
        # once the area is prepared in 3-D. A drawn-up projection is final.
        if not served_final():
            return encoded, mimetype

    _keep_tile(key, (encoded, mimetype))
    if view:
        _tiles_ahead(generation, datasource, channel, level, tile, quality, view, window,
                     hit=False)
    return encoded, mimetype


def _tile_etag(generation, datasource, channel, level, tile, quality, view="", window=None):
    etag = f'"{generation}-{datasource}-{channel}-{level}-{tile}-{quality}"'
    if view:
        # Unchanged for the default view, so a planar project's browser cache
        # survives; a view's tiles are different pixels under their own tag.
        etag = etag[:-1] + f'-{view}"'
    if window:
        etag = etag[:-1] + f'-w{window[0]:g},{window[1]:g}"'
    return etag


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
    # `view=` picks a plane, projection or section of a volume (`z12`,
    # `z4-19max`, `xzy1200` -- plexora/server/volume/viewspec.py). Absent is
    # the image's default view, which is the path every planar project takes.
    view = request.args.get('view') or ''
    from plexora.server.volume.viewspec import BadView

    if view:
        from plexora.server.models import views
        from plexora.server.volume.viewspec import ViewSpec

        try:
            view = views.canonical(datasource, view)
            if view and not ViewSpec.parse(view).served:
                raise BadView("a sum projection is exported, not served as tiles: "
                              "summed planes overflow a 16-bit tile")
        except BadView as error:
            return jsonify({'error': str(error)}), 400
    # `w=qmin,qmax`: the window a volume's 8-bit tile is to be quantized with
    # (server/volume/windows.py -- the viewer starts with a provisional one).
    # The bytes are then fixed by the address. HD tiles are raw values, and a
    # planar image has one window: neither takes one.
    window = None
    if view and quality not in ('hd', 'legacy') and request.args.get('w'):
        from plexora.server.volume.windows import parse_stamp

        window = parse_stamp(request.args.get('w'))
    # A browser that already has these bytes is told so before a pixel is
    # read: the tag is known from the request alone (the bytes depend only on
    # the generation, the address, the view and the window it names).
    if view and request.headers.get('If-None-Match'):
        early = _tile_etag(data_model.ensure_loaded(datasource),
                           datasource, channel, level, tile, quality, view, window)
        if request.headers.get('If-None-Match') == early:
            response = app.response_class(status=304)
            response.headers['ETag'] = early
            response.headers['Cache-Control'] = 'private, max-age=31536000'
            return response
    try:
        encoded, mimetype = _get_tile_png_bytes(datasource, channel, level, tile, quality,
                                                view, window)
    except BadView as error:
        # A volume on a data node whose Plexora cannot serve its planes (see
        # providers/node.py): refused in its own words rather than served as
        # the default plane under this view's URL.
        return jsonify({'error': str(error)}), 400

    # Tile bytes are immutable for a given load_generation, so let the browser
    # keep them: without this Flask's send_file() emits `Cache-Control:
    # no-cache` and every pan back over visited ground is a fresh round trip
    # through the (globally serialized) zarr/tifffile reader. The generation is
    # in the ETag rather than the URL so a reload invalidates without the
    # frontend having to rewrite tile URLs -- a stale conditional request gets
    # a 200 with fresh bytes instead of a wrong 304.
    etag = _tile_etag(data_model.load_generation, datasource, channel, level, tile, quality,
                      view, window)
    from plexora.server.volume.plane_view import served_final, served_steps

    final = served_final()
    steps = served_steps()
    if steps and not final:
        response = send_file(io.BytesIO(encoded), mimetype=mimetype)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Plexora-Served-Steps'] = str(steps)
        response.headers['Access-Control-Expose-Headers'] = 'X-Plexora-Served-Steps'
        return response
    if request.headers.get('If-None-Match') == etag:
        response = app.response_class(status=304)
    else:
        response = send_file(io.BytesIO(encoded), mimetype=mimetype)
    response.headers['ETag'] = etag
    response.headers['Cache-Control'] = 'private, max-age=31536000'
    if steps:
        # A projection computed coarser and drawn up: final, so cached, and
        # said all the same.
        response.headers['X-Plexora-Served-Steps'] = str(steps)
        response.headers['Access-Control-Expose-Headers'] = 'X-Plexora-Served-Steps'
    return response


# One route for every layer that is not the reference image. Core rather than a
# plugin, because it is the LAYER MODEL and not any one modality: it serves a
# second slide, an H&E registered alongside a Xenium morphology image, and a
# transcript density raster, which is a uint16 channel like any other.
#
# Everything transcript-SPECIFIC -- the gene vocabulary, the point tiles, the
# build job -- lives in the transcripts plugin's own blueprint, which is what
# keeps pyarrow and the Xenium reader out of a core build. That boundary is the
# whole purpose of tests/test_plugin_boundary.py, and this route costs it
# nothing.
@app.route('/generated/layer/<string:datasource>/<string:layer>/'
           '<string:channel>/<string:level>/<string:tile>')
def generate_layer_tile(datasource, layer, channel, level, tile):
    quality = request.args.get('q', 'webp')
    # Absent for a layer whose channels are drawn through the client's GL
    # colorize pass, which is every layer with channel controls: those tiles
    # are the plain quantized plane and their colour is a repaint, not a
    # refetch. A colour here is for what that pass does not touch -- an rgb
    # layer, and a points layer's density raster.
    style = layer_sources.parse_style({
        'color': request.args.get('color'),
        'lo': request.args.get('lo'),
        'hi': request.args.get('hi'),
        # A points layer's density, per gene: which genes and what colour
        # each. `minq` rather than `q` -- `q` is this route's own encoding
        # quality above, and the collision would have been a quality slider
        # that quietly switched the tiles to lossless.
        'genes': request.args.get('genes'),
        'colors': request.args.get('colors'),
        'minq': request.args.get('minq'),
        # The density map's own three: how coarse the bins are (in layer
        # pixels -- the panel asks for microns and converts), which colour
        # ramp reads the field when it is one field rather than a gene per
        # colour, and the window as fractions of the automatic one.
        'bin': request.args.get('bin'),
        'ramp': request.args.get('ramp'),
        'dlo': request.args.get('dlo'),
        'dhi': request.args.get('dhi'),
        # A bin layer's counts through log1p before the window, how its
        # heatmap combines several genes (mean/sum/max/min), and its
        # composition: which genes are grouped and how each group combines.
        'log': request.args.get('log'),
        'agg': request.args.get('agg'),
        'comp': request.args.get('comp'),
    })
    try:
        served = layer_sources.layer_tile(datasource, layer, channel, level,
                                          tile, quality, style=style)
    except layer_sources.BadStyle as error:
        # A 400 and not a 404 (which a layer card reads as "Preparing...") or
        # a fallback picture (which would be cached for a year).
        return jsonify({'error': str(error)}), 400
    if served is None:
        abort(404)
    encoded, mimetype, etag = served

    # The ETag carries the CONFIG generation, not data_model's load_generation:
    # this tile is a function of the project record and the file it names, and
    # nothing about which datasource happens to be open in the viewer. Keying it
    # on the other counter would both evict these for an unrelated project's
    # load and fail to evict them when a layer was re-registered.
    if request.headers.get('If-None-Match') == etag:
        response = app.response_class(status=304)
    else:
        response = send_file(io.BytesIO(encoded), mimetype=mimetype)
    response.headers['ETag'] = etag
    response.headers['Cache-Control'] = 'private, max-age=31536000'
    return response


# The other half of a registered layer's channel controls: the two packets the
# sidebar needs before it can draw a contrast slider at all.
#
# Deliberately the SAME SHAPE as `/get_image_channel_stats` and
# `/get_channel_gmm` above, because they feed the same widget. A registered
# layer's channel panel is a second `ViewerSidebar` instance over the same
# markup -- not a second channel widget -- so anything these answered
# differently would be a difference the user has to learn.
#
# Four segments after the prefix where the tile route has five, so the two
# cannot be confused: `<layer>/<channel>/stats` against
# `<layer>/<channel>/<level>/<tile>`.
@app.route('/generated/layer/<string:datasource>/<string:layer>/'
           '<string:channel>/stats')
def generate_layer_channel_stats(datasource, layer, channel):
    packet = layer_sources.layer_channel_stats(datasource, layer, channel)
    if packet is None:
        abort(404)
    return serialize_and_submit_json(packet)


@app.route('/generated/layer/<string:datasource>/<string:layer>/'
           '<string:channel>/gmm')
def generate_layer_channel_gmm(datasource, layer, channel):
    """The GaussianMixture fit, which is 0.2-1.9 s of CPU per channel and is
    cached on the open layer for that reason (see `layer_channel_gmm`)."""
    packet = layer_sources.layer_channel_gmm(datasource, layer, channel)
    if packet is None:
        abort(404)
    return serialize_and_submit_json(packet)


# The reference frame of a sample that has no image. Its own route, not a
# branch inside `generate_png`: that function is the hot path of the entire
# viewer -- one call per tile per pan -- and a sample with a real image must
# not pay a comparison per tile for a case it can never be in. The bytes are
@app.route('/generated/batch', methods=['POST'])
def generate_batch():
    """Several of this server's tiles in one request: the viewer's scheduler
    batches when the link to it is slow (server/tile_batch.py). Body
    `{"tiles": ["/generated/data/...png?...", ...]}`; each tile is its own
    GET's answer, bytes and headers alike."""
    from plexora.server import tile_batch

    return tile_batch.respond(app, request, tile_batch.PRIMARY)


# the same for every level and every tile, so this is a dictionary lookup and a
# send_file, and the browser is told it can keep them for a year.
@app.route('/generated/blank/<string:datasource>/<string:level>/<string:tile>')
def generate_blank_tile(datasource, level, tile):
    project = Project.find(datasource)
    if project is None or not project.image.is_blank:
        abort(404)
    encoded = data_model.encode_blank_tile(project.image.tile_width or 1024,
                                           project.image.tile_height or 1024)

    # Constant, and deliberately not keyed on any generation counter: these
    # bytes are a function of the tile size alone. Re-registering the project,
    # reloading the datasource and adding a layer all leave them identical, and
    # an ETag that churned would re-fetch a transparent square for nothing.
    etag = f'"blank-{len(encoded)}"'
    if request.headers.get('If-None-Match') == etag:
        response = app.response_class(status=304)
    else:
        response = send_file(io.BytesIO(encoded), mimetype='image/png')
    response.headers['ETag'] = etag
    response.headers['Cache-Control'] = 'private, max-age=31536000'
    return response


# A flat picture, served whole rather than tiled. `image_kind == "rgb"` is a
# screenshot, a figure panel, a photograph -- something with no pyramid and no
# channels, which `RgbImageViewer` pans and zooms with OpenSeadragon's own
# single-image tile source. Moved here from quick_view_routes.py when the
# quick-view flow was replaced by Import Sample: the flow went, this did not,
# because a flat picture is still a thing somebody imports.
@app.route('/generated/rgb/<string:datasource>')
def generate_rgb_image(datasource):
    config = get_config()
    entry = config.get(datasource)
    if not entry or entry.get('image_kind') != 'rgb':
        return jsonify(error="Not a flat-picture datasource."), 404
    src = entry['channelFile']
    from plexora.server.providers.base import RemoteUnreachable, is_remote_locator

    if is_remote_locator(src):
        # Out of the chunk cache as bytes, not as the cached file: clearing
        # the cache (or evicting to make room) may unlink that file while the
        # response is still being sent.
        import io
        import mimetypes

        from plexora.server.utils import remote_store

        try:
            data = remote_store.read_bytes(src)
        except (FileNotFoundError, PermissionError):
            return jsonify(error="The picture is not at its address any more."), 404
        except RemoteUnreachable as exc:
            return jsonify(error=str(exc)), 503
        mimetype = mimetypes.guess_type(remote_store.url_name(src))[0] \
            or 'application/octet-stream'
        return send_file(io.BytesIO(data), mimetype=mimetype)
    return send_file(src)


# The viewer mini-map's source: one channel's whole tissue, ~200-400 px, in the
# same [0, 255] domain as the WebP tiles. Separate from the tile route on
# purpose -- see data_model.generate_channel_overview for why no tile level is
# reliably a single whole-image tile.
@app.route('/generated/overview/<string:datasource>/<string:channel>')
def generate_overview(datasource, channel):
    # Before sampling load_generation, for the same reason as the tile path:
    # loading is what bumps the generation.
    generation = data_model.ensure_loaded(datasource)
    # `view=`: the plane or projection of a z-stack on screen, so the map
    # shows what is being looked at. Canonical, so one view is one address.
    view = request.args.get('view') or ''
    if view:
        from plexora.server.models import views
        from plexora.server.volume.viewspec import BadView

        try:
            view = views.canonical(datasource, view)
        except BadView:
            view = ''

    etag = f'"{generation}-overview-{datasource}-{channel}' + (f'-{view}"' if view else '"')
    if request.headers.get('If-None-Match') == etag:
        response = app.response_class(status=304)
    else:
        encoded = data_model.generate_channel_overview(datasource, channel, view or None)
        if encoded is None:
            abort(404)
        response = send_file(io.BytesIO(encoded), mimetype='image/webp')
    response.headers['ETag'] = etag
    response.headers['Cache-Control'] = 'private, max-age=31536000'
    return response


#: Past this many bytes a JSON answer is gzipped for a client that accepts it.
#: A whole-transcriptome description is 18,000 histograms -- 20 MB of JSON,
#: 1 MB compressed -- and it is fetched on every viewer boot.
GZIP_JSON_BYTES = 256 * 1024


def serialize_and_submit_json(data):
    body = orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY)
    if (len(body) > GZIP_JSON_BYTES
            and "gzip" in (request.headers.get("Accept-Encoding") or "")):
        import gzip

        response = app.response_class(
            response=gzip.compress(body, compresslevel=3),
            mimetype='application/json')
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Vary'] = 'Accept-Encoding'
        return response
    response = app.response_class(
        response=body,
        mimetype='application/json'
    )
    return response

