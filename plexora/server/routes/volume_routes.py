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
    return jsonify(info)


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


def choose_level(volume, budget_bytes=LEVEL_BUDGET_BYTES, max_side=2048):
    """The finest level whose (Z, H, W) uint8 stack fits the budget and whose
    every side fits a 3-D texture (WebGL2 guarantees 256; real GPUs 2048)."""
    for info in volume.levels:
        voxels = volume.depth * info.height * info.width
        if voxels <= budget_bytes and max(info.height, info.width, volume.depth) <= max_side:
            return info.index
    return volume.levels[-1].index


@app.route('/volume/<string:datasource>/level', methods=['GET'])
def volume_level(datasource):
    """One channel's whole level as uint8 bytes, shaped (Z, H, W), C order.

    What the browser's volume renderer uploads as a 3-D texture: quantized with
    the same window the tiles use (server/volume/windows.py), so a voxel and the
    tile pixel above it are the same byte. `level=auto` (the default) picks the
    finest level under `budget` bytes and `max_side`; the headers say which:
    `X-Plexora-Shape: Z,H,W`, `X-Plexora-Level`, `X-Plexora-Window: qmin,qmax`.
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
    if not 0 <= t < volume.frames:
        return jsonify(error=f"t {t} is outside this image's {volume.frames} frames"), 400
    budget = max(2**20, min(budget, 512 * 2**20))
    raw_level = request.args.get('level', 'auto')
    if raw_level == 'auto':
        level = choose_level(volume, budget, max_side)
    else:
        try:
            level = int(raw_level)
        except ValueError:
            return jsonify(error="level is an integer or auto"), 400
        if not 0 <= level < len(volume.levels):
            return jsonify(error=f"level {level} does not exist"), 400
        info = volume.levels[level]
        if volume.depth * info.height * info.width > 512 * 2**20:
            return jsonify(error="that level is too large to send whole"), 400
    name = data_model.real_channels(datasource)[index]['fullname']
    window = data_model.get_channel_quantization_window(name, datasource, "volume")
    key = (data_model.load_generation, datasource, index, t, level, tuple(window))
    with _level_cache_lock:
        payload = _level_cache.get(key)
        if payload is not None:
            _level_cache.move_to_end(key)
    if payload is None:
        height, width = source.level_shape(level)
        block = source.read_block(level, t, index, 0, volume.depth - 1,
                                  slice(0, height), slice(0, width))
        qmin, qmax = window
        span = max(1.0, float(qmax) - float(qmin))
        scaled = np.clip(np.rint((block.astype(np.float32) - qmin) / span * 255), 0, 255)
        payload = (np.ascontiguousarray(scaled.astype(np.uint8)).tobytes(), block.shape)
        with _level_cache_lock:
            _level_cache[key] = payload
            total = sum(len(entry[0]) for entry in _level_cache.values())
            while total > _LEVEL_CACHE_BYTES and len(_level_cache) > 1:
                _, dropped = _level_cache.popitem(last=False)
                total -= len(dropped[0])
    data, shape = payload
    response = app.response_class(data, mimetype='application/octet-stream')
    response.headers['X-Plexora-Shape'] = ",".join(str(int(side)) for side in shape)
    response.headers['X-Plexora-Level'] = str(level)
    response.headers['X-Plexora-Window'] = f"{window[0]:g},{window[1]:g}"
    response.headers['Access-Control-Expose-Headers'] = \
        'X-Plexora-Shape, X-Plexora-Level, X-Plexora-Window'
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
