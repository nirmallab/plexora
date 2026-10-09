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

from flask import abort, jsonify, request

from plexora import app
from plexora.server.models import database_model
from plexora.server.models.project import Project

#: What the viewer can be showing a volume as.
MODES = ("plane", "slab", "sections", "volume")
LAYOUTS = ("single", "split-h", "quad", "strip")
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
    return jsonify(info)


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


__all__ = ["normalize_view_state", "get_view_state", "save_view_state"]
