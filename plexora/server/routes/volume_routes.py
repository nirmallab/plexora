"""Core routes for volumes: what the image is, and where the viewer was in it.

`/volume/<ds>/info` is read once at boot by the viewer (services/viewState.js)
and answers for a planar image too -- `{"volumetric": false}` -- so the client
needs no second code path to find out there is nothing to show. Planes,
projections and sections themselves come down the ordinary tile route with
`view=` (data_routes.generate_png); nothing here serves pixels.

`/view_state/<ds>` is the viewer's position in the stack, stored per project
like `/view_transform/<ds>`: a shared, read-only project still reopens on the
plane this user left.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

from flask import abort, jsonify, request

from plexora import app, paths
from plexora.server.models import database_model
from plexora.server.models.project import Project
from plexora.server.volume import pin, serve
from plexora.telemetry import performance

#: What the viewer can be showing a volume as.
MODES = ("plane", "slab", "sections", "volume")
LAYOUTS = ("single", "split-h", "quad", "strip", "sections")
COMPOSITES = ("mip", "additive")
VIEW_STATE_VERSION = 1


def _project_or_404(datasource):
    project = Project.find(datasource)
    if project is None:
        abort(404)
    return project


@app.route('/volume/<string:datasource>/info', methods=['GET'])
def volume_info(datasource):
    """The loaded image's volume: extent, spacing (with its evidence), levels,
    default view -- or `{"volumetric": false}` for a planar image."""
    project = _project_or_404(datasource)
    from plexora.server.models import views

    try:
        info = views.volume_info(datasource)
    except Exception as exc:  # the image failed to load; the viewer says so
        return jsonify({"volumetric": False, "error": str(exc)}), 200
    if info is None:
        return jsonify({"volumetric": False,
                        "depth": 1, "frames": 1,
                        "z_semantics": project.image.z_semantics})
    info["pixel_size_z"] = project.image.pixel_size_z
    info["frame_interval"] = project.image.frame_interval
    info["segmentation"] = _segmentation_info(project)
    info["bricks"] = bricks_state(datasource)
    # A time series' object table names each row's frame: the viewer then
    # reads only the frame on screen's rows (ImageViewer.setTableFrame).
    info["table_t"] = project.roles.get("t") if project.has_table else None
    # Its z column (a measured 3-D object table): the viewer then sends the
    # planes on screen with a pick, which keeps to the cells on them.
    info["table_z"] = project.roles.get("z") if project.has_table else None
    return jsonify(info)


def bricks_state(datasource) -> dict:
    """The stack's z-chunked copy (volume/bricks.py): what it holds, and the
    background copy or preparation running, if any -- and, under `levels`,
    the derivation of an OME-Zarr stack's missing coarse levels."""
    from plexora.server.models import data_model, layer_jobs, views
    from plexora.server.volume import bricks

    source = views.current_source(datasource)
    jobs = layer_jobs.tasks(datasource) or {}
    if isinstance(source, bricks.BrickedSource):
        out = bricks.state(source.store, source)
    else:
        out = {"state": "not_needed"}
    names = {data_model.EXTENSION_TASK: "levels"}
    if isinstance(source, bricks.BrickedSource):
        names.update({data_model.BRICK_TASK: "copy", PREPARE_TASK: "prepare"})
    for name, key in names.items():
        record = jobs.get(name)
        if record:
            out[key] = {field: record.get(field)
                        for field in ("status", "progress", "message", "error")}
    return out


PREPARE_TASK = "volume-prepare"
PREPARE_STAGES = {"preparing": (0, 100, "Preparing the area in 3-D")}


@app.route('/volume/<string:datasource>/prepare', methods=['GET', 'POST'])
def volume_prepare(datasource):
    """Bricks of a finer level for an area, so sections and the 3-D view of
    it read across planes fast (volume/bricks.py `prepare`).

    POST `{box: {x, y, width, height}}` in full-resolution pixels, optionally
    `level` (default: the finest at which the area's every plane fits the
    preparation budget) and `channels` (names; default all). Runs in the
    background: 202, then GET says how it went.
    """
    _project_or_404(datasource)
    from plexora.server.models import data_model, layer_jobs, views
    from plexora.server.volume import bricks

    if request.method == 'GET':
        return jsonify(bricks_state(datasource))
    source = views.current_source(datasource)
    if not isinstance(source, bricks.BrickedSource) or not source.copies:
        return jsonify(error="this stack is read across planes from its own chunks; "
                             "there is nothing to prepare"), 409
    body = request.get_json(silent=True) or {}
    box = body.get("box") or {}
    try:
        x0, y0 = float(box["x"]), float(box["y"])
        x1, y1 = x0 + float(box["width"]), y0 + float(box["height"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="box is {x, y, width, height} in full-resolution pixels"), 400
    volume = source.volume
    names = body.get("channels")
    try:
        channels = (None if not names else
                    [data_model.real_channel_index(name, datasource) for name in names])
    except Exception:
        return jsonify(error="channels are this image's channel names"), 400
    count = volume.channels if channels is None else len(channels)
    level = body.get("level")
    if level is None:
        # The finest level whose whole shards over the area, through every
        # plane, fit what one preparation may write.
        level = volume.levels[-1].index
        for info in volume.levels:
            factor = volume.width / max(1, info.width)
            box_l = (x0 / factor, y0 / factor, x1 / factor, y1 / factor)
            if bricks.prepare_cost(volume, info.index, box_l, count) <= bricks.PREPARE_BUDGET:
                level = info.index
                break
    level = int(level)
    if not 0 <= level < len(volume.levels):
        return jsonify(error=f"level {level} does not exist"), 400
    factor = volume.width / max(1, volume.levels[level].width)
    level_box = (x0 / factor, y0 / factor, x1 / factor, y1 / factor)
    store = source.store

    def work(stage, report, cancel):
        stage("preparing")
        bricks.prepare(store, source.base, level, level_box, channels=channels,
                       report=lambda percent, _text: report(percent, 100), cancel=cancel)

    record = layer_jobs.start_task(datasource, PREPARE_TASK, work, stages=PREPARE_STAGES,
                                   message="Preparing the area in 3-D")
    return jsonify(level=level, task=record), 202


def _segmentation_info(project):
    """Whether the mask is a 3-D label volume (one id per cell across planes)
    or one 2-D mask drawn on every plane -- which decides whether the viewer
    asks for its outlines plane by plane."""
    from plexora.server.volume import labels as volume_labels

    seg = project.segmentation
    if not seg.available:
        return {"ndim": 0}
    return {"ndim": 3 if volume_labels.is_label_volume(seg.derived) else 2}


def _int(value, default=0, low=None, high=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def normalize_view_state(value, depth=None, frames=None):
    """A stored or posted view state, repaired against the image's extent.

    Lenient by design, like `normalize_view_transform`: a field that does not
    parse reads as its default, and a plane past the end of the stack (a
    re-registered image with fewer planes) is clamped rather than refused --
    the cost of being wrong is opening on a nearby plane.
    """
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    top_z = (depth - 1) if depth else None
    top_t = (frames - 1) if frames else None
    out = {"version": VIEW_STATE_VERSION}
    out["t"] = _int(value.get("t"), 0, 0, top_t)
    out["z"] = _int(value.get("z"), (depth // 2) if depth else 0, 0, top_z)
    mode = value.get("mode")
    out["mode"] = mode if mode in MODES else "plane"
    slab = value.get("slab") if isinstance(value.get("slab"), dict) else {}
    op = str(slab.get("op") or "max")
    if op not in ("max", "mean", "min", "std", "med", "pct"):
        op = "max"
    z0 = _int(slab.get("z0"), 0, 0, top_z)
    z1 = _int(slab.get("z1"), top_z if top_z is not None else 0, 0, top_z)
    if z1 < z0:
        z0, z1 = z1, z0
    out["slab"] = {"z0": z0, "z1": z1, "op": op, "follow": bool(slab.get("follow", True))}
    if op == "pct":
        try:
            q = float(slab.get("q", 95))
        except (TypeError, ValueError):
            q = 95.0
        out["slab"]["q"] = min(100.0, max(0.0, q))
    try:
        out["slab"]["thickness"] = max(1, int(slab.get("thickness", z1 - z0 + 1)))
    except (TypeError, ValueError):
        out["slab"]["thickness"] = z1 - z0 + 1
    section = value.get("section") if isinstance(value.get("section"), dict) else {}
    out["section"] = {"x": _int(section.get("x"), 0, 0), "y": _int(section.get("y"), 0, 0)}
    layout = value.get("layout")
    out["layout"] = layout if layout in LAYOUTS else "single"
    aux = value.get("aux")
    out["aux"] = aux if aux in ("slab", "volume") else None
    volume = value.get("volume") if isinstance(value.get("volume"), dict) else {}
    composite = volume.get("composite")
    opacity = volume.get("opacity") if isinstance(volume.get("opacity"), dict) else {}
    clean_opacity = {}
    for key, level in list(opacity.items())[:64]:
        try:
            clean_opacity[str(key)[:120]] = min(1.0, max(0.0, float(level)))
        except (TypeError, ValueError):
            continue
    camera = volume.get("camera") if isinstance(volume.get("camera"), dict) else {}
    clean_camera = {}
    for key in ("yaw", "pitch", "dist"):
        try:
            clean_camera[key] = float(camera.get(key))
        except (TypeError, ValueError):
            pass
    if camera.get("preset") in ("xy", "xz", "yz", "iso"):
        clean_camera["preset"] = camera["preset"]
    out["volume"] = {
        "composite": composite if composite in COMPOSITES else "mip",
        "opacity": clean_opacity,
        "camera": clean_camera,
        "quality": volume.get("quality") if volume.get("quality") in (
            "auto", "1/8", "1/4", "1/2", "1") else "auto",
    }
    return out


def get_view_state(datasource, depth=None, frames=None):
    try:
        row = database_model.get(database_model.ViewState, datasource=datasource)
        if row is None:
            return None
        return normalize_view_state(json.loads(row.cells), depth, frames)
    except Exception:
        return None


def save_view_state(datasource, value, depth=None, frames=None):
    stored = normalize_view_state(value, depth, frames)
    database_model.save_list(database_model.ViewState, datasource=datasource,
                             cells=json.dumps(stored).encode("utf-8"))
    return stored


def _extent(project):
    image = project.image
    return (image.depth if image.depth > 1 else None,
            image.frames if image.frames > 1 else None)


@app.route('/view_state/<string:datasource>', methods=['GET'])
def get_view_state_route(datasource):
    """The viewer's last position in this volume, or `{}` when never saved."""
    project = _project_or_404(datasource)
    depth, frames = _extent(project)
    return jsonify(get_view_state(datasource, depth, frames) or {})


@app.route('/view_state/<string:datasource>', methods=['PUT'])
def put_view_state(datasource):
    """Store it -- debounced client-side, so a scrub arrives as one request."""
    project = _project_or_404(datasource)
    depth, frames = _extent(project)
    try:
        stored = save_view_state(datasource, request.get_json(silent=True), depth, frames)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    return jsonify(success=True, state=stored)


# -- how this user likes the viewer, for every image ----------------------

#: A section pane's saved size, as a share of the plane's height (XZ) or width
#: (YZ). The panes lie over the plane, so past the upper bound too little of
#: it is left to cut through; `None` sizes a pane to fit, up to its default.
SECTION_SHARE = (0.05, 0.75)
#: "off" stops planes fading into each other and the 3-D view coasting after
#: a drag, on top of the system's own reduced-motion setting.
MOTION = ("on", "off")


def normalize_viewer_prefs(value):
    """The viewer's preferences, repaired: a share outside its bounds is
    clamped, and anything that does not parse reads as its default."""
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    sections = value.get("sections") if isinstance(value.get("sections"), dict) else {}
    out_sections = {}
    for axis in ("xz", "yz"):
        share = sections.get(axis)
        try:
            share = None if share is None or isinstance(share, bool) else float(share)
        except (TypeError, ValueError):
            share = None
        out_sections[axis] = (min(SECTION_SHARE[1], max(SECTION_SHARE[0], share))
                              if share is not None and math.isfinite(share) else None)
    motion = value.get("motion")
    return {"sections": out_sections, "motion": motion if motion in MOTION else "on"}


@app.route('/viewer_prefs', methods=['GET'])
def get_viewer_prefs():
    """The section panes' sizes and whether the viewer animates, for every
    image. Kept in the settings file rather than the browser: the desktop
    app's port -- and so the page's origin, and its storage -- can change
    from one run to the next."""
    stored = paths.read_settings().get("viewer")
    return jsonify(normalize_viewer_prefs(stored if isinstance(stored, dict) else {}))


@app.route('/viewer_prefs', methods=['PUT'])
def put_viewer_prefs():
    """Merge what was sent over what is stored: a pane resized here leaves the
    other pane's size and the motion setting as they were."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(success=False, error="expected an object"), 400
    settings = paths.read_settings()
    current = settings.get("viewer") if isinstance(settings.get("viewer"), dict) else {}
    sections = current.get("sections") if isinstance(current.get("sections"), dict) else {}
    if isinstance(payload.get("sections"), dict):
        sections = {**sections, **payload["sections"]}
    stored = normalize_viewer_prefs({**current, **payload, "sections": sections})
    settings["viewer"] = stored
    paths.write_settings(settings)
    return jsonify(success=True, prefs=stored)


# -- whole levels, for the volume renderer and the section panes -----------

# The level helpers live with the payload builder (server/volume/serve.py),
# which a data node runs too; named here for the callers that import them.
from plexora.server.volume.serve import (  # noqa: E402,F401
    LEVEL_BUDGET_BYTES, area_at, choose_area_level, choose_level, z_pool)


@app.route('/volume/<string:datasource>/level', methods=['GET'])
def volume_level(datasource):
    """One channel's whole level as uint8 bytes, shaped (Z, H, W), C order --
    Z after max-pooling `X-Plexora-ZPool` planes into each texel (`z_pool`).

    What the browser's volume renderer uploads as a 3-D texture: quantized with
    the same window the tiles use (server/volume/windows.py), so a voxel and the
    tile pixel above it are the same byte. `level=auto` (the default) picks the
    finest level under `budget` bytes and `max_side`; the headers say which:
    `X-Plexora-Shape: Z,H,W`, `X-Plexora-Level`, `X-Plexora-Window: qmin,qmax`.

    `X-Plexora-Hint: lo,hi` is the default contrast for what the renderer will
    draw -- `channel_stats_of`'s hints over this block's maximum along z, in raw
    units. A coarse level's voxels are averages, so its maxima sit well below a
    full-resolution projection's, and the projection's window draws it black.

    `box=x,y,width,height` (full-resolution pixels) is an area instead of the
    whole plane: the 3-D view of what is on screen. The level is then the
    finest at which that area, through every plane, fits the budget -- read
    through the stack's 3-D copy, so an area prepared at a fine level
    (`/volume/<ds>/prepare`) reads from bricks. `X-Plexora-Box: x0,y0,x1,y1`
    is the area covered, in full-resolution pixels.

    `format=f16` is the HD counterpart: the same [qmin, qmax] scaled to 0..1 as
    little-endian float16, two bytes a voxel (the budget counts them). Eight
    bits over a window that runs to the stack's brightest voxel leave a dim
    stack a handful of grey levels -- a light-sheet volume whose tissue sits
    under 1000 with saturated voxels at 65535 gets four; half floats keep
    about three significant digits at every brightness.
    """
    from plexora.server.models import data_model
    from plexora.server.volume.viewspec import BadView

    context = _level_context(datasource)
    if not isinstance(context, SimpleNamespace):
        return context
    source, volume, index, name, ask = (context.source, context.volume, context.index,
                                        context.name, context.ask)
    window, exact = context.window, context.exact
    rows, cols, covered, pool = context.rows, context.cols, context.covered, context.pool
    remote, identity = context.remote, context.identity
    span = () if ask.planes == volume.depth else (ask.z0, ask.z1)
    tag = serve.level_etag(identity, index, ask.t, ask.level, window, ask.voxel_format,
                           tuple(covered) + span, pool)
    headers = serve.level_headers(ask, covered, pool, window)
    # A request that names the window it was made for (`w=`) is an address no
    # other bytes will ever have: kept for a year. Without it, the browser
    # asks again and is told "not modified".
    headers['Cache-Control'] = ('private, max-age=31536000, immutable' if ask.named is not None
                                else ('private, no-cache' if exact else 'no-store'))
    if not exact:
        headers['X-Plexora-Window-Provisional'] = '1'
    # Revalidation costs nothing: the tag is known before a voxel is read.
    if request.if_none_match.contains(tag):
        response = app.response_class(status=304)
        response.set_etag(tag)
        response.headers.update(headers)
        performance.note("cache", "hit")
        return response
    key = (identity, datasource, index, ask.t, ask.level, tuple(window), ask.voxel_format,
           covered, pool, ask.z0, ask.z1)
    hint_key = (identity, datasource, index, ask.t, ask.level, tuple(window), covered, pool,
                ask.z0, ask.z1)
    performance.note("cache", "miss" if serve.cached(key) is None else "hit")
    if remote:
        # A volume on a data node: built there by the same functions, with
        # every choice made here sent explicitly -- the level, the area, the
        # planes, the pooling (this project's z step, which the node may not
        # know) and the window -- so its bytes are a local read's.
        node = data_model._remote_image()
        if node is None:
            return jsonify(error="this volume's data node is not attached"), 503

        def fetch():
            got = node.level(name, ask, covered, pool, window)
            performance.mark("node")
            return got

        try:
            data, shape, hint = serve.forwarded_payload(key, fetch, performance.mark)
        except BadView as error:
            return jsonify(error=str(error)), 400
    else:
        data, shape, hint = serve.level_payload(
            key, hint_key, lambda: serve.read_level(source, ask, index, rows, cols, pool),
            window, ask.voxel_format, data_model.channel_stats_of, performance.mark)
    response = app.response_class(data, mimetype='application/octet-stream')
    response.set_etag(tag)
    response.headers.update(headers)
    response.headers.update(serve.payload_headers(shape, hint))
    return _maybe_gzip(response)


def _level_context(datasource):
    """What the level and hint routes share: the source, the channel, the
    parsed ask, the window, the area and the pooling -- or the error answer."""
    _project_or_404(datasource)
    from plexora.server.models import data_model, views

    source = views.current_source(datasource)
    if source is None or source.volume.is_planar:
        return jsonify(error="this image has a single plane"), 400
    volume = source.volume
    channel = request.args.get('channel', '')
    try:
        index = data_model.real_channel_index(channel, datasource)
    except Exception:
        return jsonify(error=f"no channel {channel!r}"), 400
    try:
        ask = serve.parse_level(volume, request.args)
    except serve.BadLevel as error:
        return jsonify(error=str(error)), 400
    name = data_model.real_channels(datasource)[index]['fullname']
    performance.skip()
    # The window the request names (`w=`): the bytes are fixed by the address.
    # Without one, the window there is now -- the provisional one while the
    # exact scan runs, which no cache may keep.
    if ask.named is not None:
        window, exact = ask.named, True
    else:
        window, exact = data_model.volume_window_state(name, datasource)
    performance.mark("lut")
    rows, cols, covered = serve.area_at(volume, ask.level, ask.box)
    pool = ask.pool or serve.z_pool(volume, ask.level)
    remote = not getattr(source, "is_local", True)
    identity = serve.source_identity(
        (data_model.config.get(datasource) or {}).get('channelFile'))
    if remote:
        # The project's own record of where the pixels are, not a file here.
        identity = f"node|{identity}|{data_model.load_generation}"
    return SimpleNamespace(source=source, volume=volume, index=index, name=name, ask=ask,
                           window=window, exact=exact, rows=rows, cols=cols, covered=covered,
                           pool=pool, remote=remote, identity=identity)


@app.route('/volume/<string:datasource>/hint', methods=['GET'])
def volume_hint(datasource):
    """The default contrast for some planes of a level the 3-D view drew:
    `{hint: [lo, hi] | null, level, z: [z0, z1]}` in raw units.

    The same numbers as the level's `X-Plexora-Hint`, over planes `z=z0,z1`
    only. The 3-D view asks when its clip range is narrower than the stack,
    naming the `level` (and `box`, `w`) it drew, so a thin clip range through
    a dim part of the stack is levelled by what it shows rather than by the
    brightest plane outside it. Takes the level route's query."""
    from plexora.server.models import data_model
    from plexora.server.volume.viewspec import BadView

    context = _level_context(datasource)
    if not isinstance(context, SimpleNamespace):
        return context
    ask, window = context.ask, context.window
    hint_key = (context.identity, datasource, context.index, ask.t, ask.level, tuple(window),
                context.covered, context.pool, ask.z0, ask.z1)
    if context.remote:
        node = data_model._remote_image()
        if node is None:
            return jsonify(error="this volume's data node is not attached"), 503
        key = (context.identity, datasource, context.index, ask.t, ask.level, tuple(window),
               ask.voxel_format, context.covered, context.pool, ask.z0, ask.z1)
        try:
            _, _, hint = serve.forwarded_payload(
                key, lambda: node.level(context.name, ask, context.covered, context.pool, window))
        except BadView as error:
            return jsonify(error=str(error)), 400
    else:
        hint = serve.hint_over(
            hint_key,
            lambda: serve.read_level(context.source, ask, context.index, context.rows,
                                     context.cols, context.pool),
            window, data_model.channel_stats_of)
    response = jsonify(hint=list(hint) if hint else None, level=ask.level, z=[ask.z0, ask.z1])
    # Fixed by the address when it names its window.
    response.headers['Cache-Control'] = ('private, max-age=31536000, immutable'
                                         if ask.named is not None else 'no-store')
    return response


def _maybe_gzip(response, started=None):
    """`z=gzip` in the query, and a browser that takes gzip: the body
    compressed at gzip's fastest setting. The 3-D view asks for it once it has
    measured its link slower than compressing (views/volumeAtlas.js
    `LINK`): voxels compress about fivefold, which on a slow link is five
    times the speed, and on localhost is CPU time spent for nothing -- hence
    only when asked. The query is part of the address, so a compressed body
    never answers for an uncompressed one in any cache."""
    import gzip
    import time

    if request.args.get('z') == 'gzip' and 'gzip' in (request.headers.get('Accept-Encoding') or ''):
        response.set_data(gzip.compress(response.get_data(), compresslevel=1))
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Vary'] = 'Accept-Encoding'
    if started is not None:
        response.headers['Server-Timing'] = f"total;dur={(time.perf_counter() - started) * 1000:.1f}"
    return response


def _brick_source(datasource):
    """The source bricks are read from, or the error answer: a planar image
    has none, and a data node's volume is not served as bricks yet (the 3-D
    view then refines with a finer area instead)."""
    _project_or_404(datasource)
    from plexora.server.models import views

    source = views.current_source(datasource)
    if source is None or source.volume.is_planar:
        return None, (jsonify(error="this image has a single plane"), 400)
    if not getattr(source, "is_local", True):
        return None, (jsonify(error="bricks of an image on a data node are not served; the "
                                    "3-D view refines the area it rests on instead",
                              available=False), 501)
    return source, None


@app.route('/volume/<string:datasource>/bricks/manifest', methods=['GET'])
def bricks_manifest(datasource):
    """The brick grid of every level the 3-D view's atlas can draw from
    (server/volume/brick_serve.py): brick size, apron, and per level its
    planes pooled per voxel, its shape in voxels and its grid of bricks."""
    from plexora.server.volume import brick_serve

    source, error = _brick_source(datasource)
    if error:
        return error
    return jsonify(brick_serve.manifest(source.volume))


@app.route('/volume/<string:datasource>/bricks', methods=['GET'])
def volume_bricks(datasource):
    """Bricks of one level for the 3-D view's atlas, several at once.

    `level`, `t`, `format` (`u8` | `f16`), `at=z.y.x,z.y.x,...` (up to
    `MAX_PER_REQUEST`), and `channel` with its `w=qmin,qmax` repeated per
    channel (the window the bytes are made with, as the level route takes
    it; without one, the channel's window now). The body is
    `brick_serve.payload`'s: header length, JSON header, voxels. A request
    whose every channel names its window is an address no other bytes have:
    kept for a year."""
    import hashlib
    import time

    from plexora.server.models import data_model
    from plexora.server.volume import brick_serve
    from plexora.server.volume.windows import parse_stamp

    started = time.perf_counter()
    source, error = _brick_source(datasource)
    if error:
        return error
    volume = source.volume
    try:
        level = int(request.args.get('level', ''))
        t = int(request.args.get('t', 0))
    except ValueError:
        return jsonify(error="level and t are integers"), 400
    voxel_format = request.args.get('format', 'u8')
    if voxel_format not in ('u8', 'f16'):
        return jsonify(error="format is u8 or f16"), 400
    if not 0 <= t < volume.frames:
        return jsonify(error=f"t {t} is outside this image's {volume.frames} frames"), 400
    names = request.args.getlist('channel')
    stamps = request.args.getlist('w')
    if not names or len(names) > brick_serve.MAX_CHANNELS:
        return jsonify(error=f"name 1 to {brick_serve.MAX_CHANNELS} channels"), 400
    try:
        entry = brick_serve.level_of(volume, level)
        at = brick_serve.parse_at(request.args.get('at'), entry["grid"])
    except brick_serve.BadBricks as exc:
        return jsonify(error=str(exc)), 400
    performance.skip()
    channels, exact = [], True
    for position, name in enumerate(names):
        try:
            index = data_model.real_channel_index(name, datasource)
        except Exception:
            return jsonify(error=f"no channel {name!r}"), 400
        full = data_model.real_channels(datasource)[index]['fullname']
        named = parse_stamp(stamps[position]) if position < len(stamps) else None
        if named is not None:
            window = named
        else:
            window, settled = data_model.volume_window_state(full, datasource)
            exact = exact and settled
            named = None
        channels.append((index, full, tuple(float(v) for v in window)))
    every_named = len(stamps) >= len(names) and all(parse_stamp(s) for s in stamps[:len(names)])
    identity = serve.source_identity(
        (data_model.config.get(datasource) or {}).get('channelFile'))
    tag = "bk-" + hashlib.sha1("|".join(str(part) for part in (
        serve.PAYLOAD_VERSION, identity, level, entry["pool"], t, voxel_format,
        [(c[0], c[2]) for c in channels], at)).encode("utf-8")).hexdigest()[:24]
    cache = ('private, max-age=31536000, immutable' if every_named
             else ('private, no-cache' if exact else 'no-store'))
    if request.if_none_match.contains(tag):
        response = app.response_class(status=304)
        response.set_etag(tag)
        response.headers['Cache-Control'] = cache
        return response
    with serve.LEVEL_GATE:
        performance.mark("gate")
        body = brick_serve.payload(source, entry, t, channels, at, voxel_format, identity)
        performance.mark("read")
    response = app.response_class(body, mimetype='application/octet-stream')
    response.set_etag(tag)
    response.headers['Cache-Control'] = cache
    return _maybe_gzip(response, started)


@app.route('/volume/<string:datasource>/window', methods=['GET'])
def volume_window(datasource):
    """Each named channel's volume window, `{windows: {name: {qmin, qmax, exact}}}`.

    Answered at once: the exact window when it is known, else the provisional
    one (server/volume/windows.py) with the exact scan put first in the
    background worker's line. The viewer quantizes every tile and level it
    asks for with the window it got here (`w=`), and asks again until each is
    exact."""
    _project_or_404(datasource)
    from plexora.server.models import data_model, views

    source = views.current_source(datasource)
    if source is None or source.volume.is_planar:
        return jsonify(error="this image has a single plane"), 400
    out = {}
    for name in request.args.getlist('channel'):
        try:
            window, exact = data_model.volume_window_state(name, datasource)
        except data_model.UnknownChannelError:
            continue
        out[name] = {"qmin": float(window[0]), "qmax": float(window[1]), "exact": bool(exact)}
    response = jsonify(windows=out)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/volume/<string:datasource>/perf', methods=['GET'])
def volume_perf(datasource):
    """What the volume caches hold now, for the 3-D benchmark and for a person
    asking why a stack is slow: the level payload cache and, once the stack
    has been read, its decoded-chunk cache and window state."""
    _project_or_404(datasource)
    from plexora.server.models import data_model
    from plexora.server.volume import chunk_cache

    out = {"level_cache": serve.stats(), "chunk_cache": chunk_cache.CACHE.stats(),
           "exact_window_queue": len(data_model._exact_window_queue),
           "provisional_windows": sorted(name for (_scope, name)
                                         in list(data_model._provisional_windows))}
    for name, provider in _PERF_PROVIDERS.items():
        try:
            out[name] = provider(datasource)
        except Exception as error:  # a stat must never fail the request
            out[name] = {"error": repr(error)}
    return jsonify(out)


#: Other caches report here: `{name: fn(datasource) -> dict}`.
_PERF_PROVIDERS: dict = {}


@app.route('/volume/<string:datasource>/section', methods=['GET'])
def section_geometry(datasource):
    """What a section pane's tile source needs for `view=xzy<y>` / `yzx<x>`:
    its width, height and level shapes (rows = z, drawn at the z step)."""
    _project_or_404(datasource)
    from plexora.server.models import views
    from plexora.server.volume.plane_view import section_geometry as geometry
    from plexora.server.volume.viewspec import BadView

    try:
        spec = views.resolve(datasource, request.args.get('view', ''))
    except BadView as exc:
        return jsonify(error=str(exc)), 400
    if spec is None or spec.kind != "section":
        return jsonify(error="name a section, e.g. view=xzy1200"), 400
    return jsonify(geometry(views.current_source(datasource).volume, spec))


__all__ = ["normalize_view_state", "get_view_state", "save_view_state", "choose_level"]


# -- pinned projections (plexora/server/volume/pin.py) -----------------------

pin.register()


@app.route('/volume/<string:datasource>/pin', methods=['POST'])
def pin_projection(datasource):
    """Compute a projection exactly at full resolution and add it as a layer.

    `{"view": "z4-19max"}` -> 202 with `{layer, status, created}`; the layer
    is `pending` until built and the viewer follows it on `/import/status`
    like any other layer. 400 says why a view cannot be pinned.
    """
    project = _project_or_404(datasource)
    payload = request.get_json(silent=True) or {}
    try:
        answer = pin.pin(project, payload.get("view"),
                         replace_existing=bool(payload.get("replace")))
    except pin.PinRefused as error:
        return jsonify(error=str(error)), 400
    return jsonify(answer), 202 if answer["created"] else 200


# -- 3-D cells (plexora/server/volume/objects.py) ----------------------------

OBJECTS_TASK = "objects"
OBJECTS_STAGES = {"measuring": (0, 97, "Measuring cells")}
TRACKS_TASK = "tracks"
TRACKS_STAGES = {"linking": (0, 97, "Linking cells across frames")}
#: The last finished measurement's summary per project, for the card that
#: started it (the object table's manifest outlives this; see `objects_state`).
_objects_results: dict = {}


def objects_state(project) -> dict:
    """What the Depth card shows about measuring a 3-D mask's cells."""
    from plexora.server.models import layer_jobs
    from plexora.server.volume import labels as volume_labels
    from plexora.server.volume import objects as volume_objects

    seg = project.segmentation
    label_volume = bool(seg.available and seg.derived
                        and volume_labels.is_label_volume(seg.derived))
    table, manifest_path = volume_objects.object_paths(project)
    manifest = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
    return {
        "label_volume": label_volume,
        "z_step_known": _z_step_known(project),
        "task": layer_jobs.tasks(project.name).get(OBJECTS_TASK),
        "result": _objects_results.get(project.name),
        "measured": ({"objects": manifest.get("objects"), "created": manifest.get("created"),
                      "units": manifest.get("units"), "table": str(table),
                      "attached": str(project.dataset.src if project.dataset else "")
                      == str(table),
                      # A time series: rows per frame, and whether they were
                      # linked across frames (server/volume/tracking.py).
                      "frames": manifest.get("frames"),
                      "labels_per_frame": manifest.get("labels_per_frame"),
                      "tracking": manifest.get("tracking")} if manifest else None),
        "tracks_task": layer_jobs.tasks(project.name).get(TRACKS_TASK),
    }


def _z_step_known(project) -> bool:
    """Whether physical 3-D measurement can run: x, y and z steps known --
    read off the loaded volume, which carries any step somebody typed."""
    from plexora.server.models import views

    if not project.image.is_volumetric:
        return False
    source = views.current_source(project.name)
    return bool(source is not None and source.volume.spacing.volumetric_ok())


@app.route('/volume/<string:datasource>/tracks', methods=['POST'])
def track_objects_route(datasource):
    """Link a time series' measured cells from frame to frame (a `track`
    column in the object table). `{"max_distance": number}` in the table's
    units (µm, or voxels), optional -> 202 and the task record; the Depth
    card polls GET /objects. 400 says why it cannot run."""
    from plexora.server.models import layer_jobs
    from plexora.server.volume import tracking

    project = _project_or_404(datasource)
    payload = request.get_json(silent=True) or {}
    limit = payload.get("max_distance")
    if limit is not None:
        try:
            limit = float(limit)
        except (TypeError, ValueError):
            return jsonify(error="max_distance is a number"), 400
        if not limit > 0:
            return jsonify(error="max_distance is above zero"), 400
    measured = objects_state(project).get("measured") or {}
    if not measured:
        return jsonify(error="Measure this project's cells first: there is nothing to "
                             "link."), 400
    if (measured.get("frames") or 1) < 2:
        return jsonify(error="The object table has one frame: there is nothing to link "
                             "across."), 400
    name = project.name

    def work(stage, report, cancel):
        stage("linking")
        tracking.track_project_objects(Project.load(name), max_distance=limit,
                                       progress=lambda done, total: report(done, total))

    record = layer_jobs.start_task(name, TRACKS_TASK, work, stages=TRACKS_STAGES)
    return jsonify(task=record), 202


@app.route('/volume/<string:datasource>/objects', methods=['GET'])
def get_objects(datasource):
    return jsonify(objects_state(_project_or_404(datasource)))


@app.route('/volume/<string:datasource>/objects', methods=['POST'])
def measure_objects_route(datasource):
    """Measure every cell of the project's 3-D mask once, whole.

    `{"units": "physical" | "voxel"}` -> 202 and the task record; the card
    polls GET. 400 says why it cannot run (no 3-D mask; no z step for
    physical units).
    """
    from plexora.server.models import layer_jobs
    from plexora.server.volume import objects as volume_objects

    project = _project_or_404(datasource)
    payload = request.get_json(silent=True) or {}
    units = payload.get("units") or "physical"
    if units not in ("physical", "voxel"):
        return jsonify(error="units is physical or voxel"), 400
    state = objects_state(project)
    if not state["label_volume"]:
        return jsonify(error="This project's mask is not a 3-D label volume, so there are "
                             "no whole cells to measure."), 400
    if units == "physical" and not state["z_step_known"]:
        return jsonify(error="Measuring in µm needs the distance between planes. Set the "
                             "z step, or measure in voxels.", missing="spacing:z"), 400
    name = project.name
    _objects_results.pop(name, None)

    def work(stage, report, cancel):
        stage("measuring")
        summary = volume_objects.measure_project_objects(
            Project.load(name), units=units,
            progress=lambda done, total: report(done, total),
            cancelled=cancel.is_set)
        _objects_results[name] = summary

    record = layer_jobs.start_task(name, OBJECTS_TASK, work, stages=OBJECTS_STAGES)
    return jsonify(task=record), 202
