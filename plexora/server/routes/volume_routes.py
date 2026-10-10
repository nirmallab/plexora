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
import threading
from collections import OrderedDict

import numpy as np
from flask import abort, jsonify, request

from plexora import app
from plexora.server.models import database_model
from plexora.server.models.project import Project
from plexora.server.volume import pin

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
    return jsonify(info)


def bricks_state(datasource) -> dict:
    """The stack's z-chunked copy (volume/bricks.py): what it holds, and the
    background copy or preparation running, if any."""
    from plexora.server.models import data_model, layer_jobs, views
    from plexora.server.volume import bricks

    source = views.current_source(datasource)
    if not isinstance(source, bricks.BrickedSource):
        return {"state": "not_needed"}
    out = bricks.state(source.store)
    jobs = layer_jobs.tasks(datasource) or {}
    for name in (data_model.BRICK_TASK, PREPARE_TASK):
        record = jobs.get(name)
        if record:
            out["copy" if name == data_model.BRICK_TASK else "prepare"] = {
                key: record.get(key) for key in ("status", "progress", "message", "error")}
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
    if not isinstance(source, bricks.BrickedSource):
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


# -- whole levels, for the volume renderer and the section panes -----------

#: The most one channel's level may cost to send. A volume renderer holds one
#: 3-D texture per channel on the GPU; 64 MB of uint8 is 64 M voxels, which is
#: past what an integrated GPU draws at an interactive rate anyway.
LEVEL_BUDGET_BYTES = 64 * 2**20

_level_cache = OrderedDict()
_level_cache_lock = threading.Lock()
_LEVEL_CACHE_BYTES = 256 * 2**20


def z_pool(volume, level: int) -> int:
    """How many planes the level route max-pools into one texel at `level`.

    As many as fit in one of the level's pixels, so the texture's voxels are
    about as deep as they are wide -- what Imaris does to its own coarse
    levels. A light-sheet stack of 1937 planes 5 µm apart is 1937 texels deep
    at every level otherwise: a ray marched across it samples one plane in four
    and misses most of what a MIP should show, and the budget goes on depth
    instead of on detail. A maximum keeps every peak. 1 without a stated z step.
    """
    spacing = volume.spacing
    sx, sz = spacing.x.value, spacing.z.value
    if not (sx and sz) or spacing.z.source not in ("metadata", "manual"):
        return 1
    info = volume.levels[level]
    pixel = float(sx) * volume.width / max(1, info.width)
    return max(1, int(pixel // float(sz)))


def choose_level(volume, budget_bytes=LEVEL_BUDGET_BYTES, max_side=2048):
    """The finest level whose (Z, H, W) uint8 stack -- Z after `z_pool` -- fits
    the budget and whose every side fits a 3-D texture (WebGL2 guarantees 256;
    real GPUs 2048)."""
    for info in volume.levels:
        depth = -(-volume.depth // z_pool(volume, info.index))
        voxels = depth * info.height * info.width
        if voxels <= budget_bytes and max(info.height, info.width, depth) <= max_side:
            return info.index
    return volume.levels[-1].index


def area_at(volume, level, box):
    """(rows, cols) at `level` covering a full-resolution `box` (the whole
    level when None), and the full-resolution box they cover."""
    info = volume.levels[level]
    if box is None:
        return (slice(0, info.height), slice(0, info.width),
                (0, 0, volume.width, volume.height))
    fx = volume.width / max(1, info.width)
    fy = volume.height / max(1, info.height)
    x0, y0, x1, y1 = box
    c0, r0 = int(x0 // fx), int(y0 // fy)
    c1 = max(c0 + 1, min(info.width, int(math.ceil(x1 / fx))))
    r1 = max(r0 + 1, min(info.height, int(math.ceil(y1 / fy))))
    covered = (int(round(c0 * fx)), int(round(r0 * fy)),
               min(volume.width, int(round(c1 * fx))), min(volume.height, int(round(r1 * fy))))
    return slice(r0, r1), slice(c0, c1), covered


def choose_area_level(volume, box, budget_bytes, max_side=2048):
    """`choose_level` for an area: the finest level at which `box`, through
    every plane (after `z_pool`), fits the budget and a 3-D texture."""
    for info in volume.levels:
        rows, cols, _ = area_at(volume, info.index, box)
        depth = -(-volume.depth // z_pool(volume, info.index))
        height, width = rows.stop - rows.start, cols.stop - cols.start
        if depth * height * width <= budget_bytes and max(height, width, depth) <= max_side:
            return info.index
    return volume.levels[-1].index


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
        t = int(request.args.get('t', 0))
        budget = int(request.args.get('budget', LEVEL_BUDGET_BYTES))
        max_side = int(request.args.get('max_side', 2048))
    except ValueError:
        return jsonify(error="t, budget and max_side are integers"), 400
    voxel_format = request.args.get('format', 'u8')
    if voxel_format not in ('u8', 'f16'):
        return jsonify(error="format is u8 or f16"), 400
    voxel_bytes = 2 if voxel_format == 'f16' else 1
    if not 0 <= t < volume.frames:
        return jsonify(error=f"t {t} is outside this image's {volume.frames} frames"), 400
    budget = max(2**20, min(budget, 512 * 2**20))
    box = None
    if request.args.get('box'):
        try:
            bx, by, bw, bh = (float(v) for v in request.args['box'].split(","))
        except ValueError:
            return jsonify(error="box is x,y,width,height in full-resolution pixels"), 400
        x0, y0 = max(0.0, bx), max(0.0, by)
        x1, y1 = min(float(volume.width), bx + bw), min(float(volume.height), by + bh)
        if x1 - x0 < 1 or y1 - y0 < 1:
            return jsonify(error="the box is outside the image"), 400
        box = (int(x0), int(y0), int(math.ceil(x1)), int(math.ceil(y1)))
    raw_level = request.args.get('level', 'auto')
    if raw_level == 'auto' and box is not None:
        level = choose_area_level(volume, box, budget // voxel_bytes, max_side)
    elif raw_level == 'auto':
        level = choose_level(volume, budget // voxel_bytes, max_side)
    else:
        try:
            level = int(raw_level)
        except ValueError:
            return jsonify(error="level is an integer or auto"), 400
        if not 0 <= level < len(volume.levels):
            return jsonify(error=f"level {level} does not exist"), 400
        rows, cols, _covered = area_at(volume, level, box)
        pooled = -(-volume.depth // z_pool(volume, level))
        if (pooled * (rows.stop - rows.start) * (cols.stop - cols.start) * voxel_bytes
                > 512 * 2**20):
            return jsonify(error="that level is too large to send whole"), 400
    name = data_model.real_channels(datasource)[index]['fullname']
    window = data_model.get_channel_quantization_window(name, datasource, "volume")
    rows, cols, covered = area_at(volume, level, box)
    key = (data_model.load_generation, datasource, index, t, level, tuple(window),
           voxel_format, covered)
    with _level_cache_lock:
        payload = _level_cache.get(key)
        if payload is not None:
            _level_cache.move_to_end(key)
    if payload is None:
        pool = z_pool(volume, level)
        if pool == 1:
            block = source.read_block(level, t, index, 0, volume.depth - 1, rows, cols)
        else:
            block = np.stack([
                np.asarray(source.read_block(level, t, index, z0,
                                             min(volume.depth, z0 + pool) - 1,
                                             rows, cols)).max(axis=0)
                for z0 in range(0, volume.depth, pool)])
        qmin, qmax = window
        span = max(1.0, float(qmax) - float(qmin))
        unit = np.clip((block.astype(np.float32) - qmin) / span, 0.0, 1.0)
        if voxel_format == 'f16':
            voxels = unit.astype('<f2')
        else:
            voxels = np.rint(unit * 255).astype(np.uint8)
        try:
            stats = data_model.channel_stats_of(np.asarray(block.max(axis=0), dtype=np.float64),
                                                qmin, qmax)
            hint = (stats['vmin_hint'], stats['vmax_hint'])
        except Exception:  # an all-zero level has no positive voxel to log
            hint = None
        payload = (np.ascontiguousarray(voxels).tobytes(), block.shape, hint)
        with _level_cache_lock:
            _level_cache[key] = payload
            total = sum(len(entry[0]) for entry in _level_cache.values())
            while total > _LEVEL_CACHE_BYTES and len(_level_cache) > 1:
                _, dropped = _level_cache.popitem(last=False)
                total -= len(dropped[0])
    data, shape, hint = payload
    response = app.response_class(data, mimetype='application/octet-stream')
    if hint is not None:
        response.headers['X-Plexora-Hint'] = f"{hint[0]:g},{hint[1]:g}"
    response.headers['X-Plexora-Shape'] = ",".join(str(int(side)) for side in shape)
    response.headers['X-Plexora-Box'] = ",".join(str(int(v)) for v in covered)
    response.headers['X-Plexora-Level'] = str(level)
    response.headers['X-Plexora-ZPool'] = str(z_pool(volume, level))
    response.headers['X-Plexora-Window'] = f"{window[0]:g},{window[1]:g}"
    response.headers['X-Plexora-Format'] = voxel_format
    response.headers['Access-Control-Expose-Headers'] = \
        'X-Plexora-Shape, X-Plexora-Level, X-Plexora-Window, X-Plexora-Format, ' \
        'X-Plexora-Hint, X-Plexora-ZPool, X-Plexora-Box'
    response.headers['Cache-Control'] = 'no-store'
    return response


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
                      == str(table)} if manifest else None),
    }


def _z_step_known(project) -> bool:
    """Whether physical 3-D measurement can run: x, y and z steps known --
    read off the loaded volume, which carries any step somebody typed."""
    from plexora.server.models import views

    if not project.image.is_volumetric:
        return False
    source = views.current_source(project.name)
    return bool(source is not None and source.volume.spacing.volumetric_ok())


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
