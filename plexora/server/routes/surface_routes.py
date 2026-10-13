"""Core routes for surfaces: making them, previewing them, and reading them back.

A surface is a layer (plexora/server/surface/store.py), so listing, ordering,
restyling (`PATCH /project/<n>/layers/<id>`'s `render`) and removing one are
the generic layer routes. What is here is what only a surface has:

    POST /volume/<ds>/surfaces                    make one (202 pending, 200 existing)
    GET  /volume/<ds>/surfaces                    every surface with its manifest summary
    GET  /volume/<ds>/surfaces/preview            the slider's quick mesh + contours
    GET  /volume/<ds>/surfaces/suggest            thresholds and a histogram
    GET  /volume/<ds>/surfaces/estimate           what a request would build
    GET  /volume/<ds>/surfaces/<id>/manifest      how it was made
    GET  /volume/<ds>/surfaces/<id>/mesh          the mesh payload (immutable, ETag)
    GET  /volume/<ds>/surfaces/<id>/objects       one row per object
    POST /volume/<ds>/surfaces/<id>/cancel        stop a build
    GET  /volume/<ds>/surfaces/<id>/sections      where it cuts the planes on screen
    GET  /volume/<ds>/surfaces/<id>/export        PLY / OBJ / STL / GLB (+ manifest)
    POST /volume/<ds>/surfaces/<id>/refine        an edit, as a new version
    GET  /volume/<ds>/surfaces/<id>/neighbours    objects near one object
    POST /volume/<ds>/surfaces/import             a mesh file as a surface

**Licensed.** Making and analysing surfaces is Surface Studio, the
`volume:surfaces` entitlement: the POST, the preview, the suggestions and the
estimate, refinement, neighbours and import answer the Paid 403 on a
licence without it, which the page turns into the Paid modal. Reading a
surface that exists -- its manifest, mesh, objects and sections -- exporting
it, cancelling it and removing it are never guarded: what a Paid feature
produced stays readable, exportable and editable on any plan.
"""

from __future__ import annotations

import gzip
import math

from flask import jsonify, request

from plexora import app
from plexora.licensing import guards
from plexora.server.models.project import Project
from plexora.server.surface import ENTITLEMENT
from plexora.server.surface import levels as surface_levels
from plexora.server.surface import store as surface_store

paid = guards.require_entitlement(ENTITLEMENT)


def _project(datasource):
    project = Project.find(datasource)
    if project is None:
        return None, (jsonify(error=f"Unknown project: {datasource!r}"), 404)
    return project, None


def _refusal(error):
    body = {"error": str(error), "code": getattr(error, "code", "invalid_input")}
    estimate = getattr(error, "estimate", None)
    if estimate:
        body["estimate"] = estimate
    return jsonify(body), 400


def _source(datasource):
    """The z-stack a preview, a suggestion or a plan reads: the viewer's own
    for a file on this machine; for one a data node holds, its description
    plus block reads over the wire (a preview's few million voxels -- a build
    runs on the node)."""
    from plexora.server.models import views

    source = views.current_source(datasource)
    if source is None or source.volume.is_planar:
        return None
    if not getattr(source, "is_local", True):
        from plexora.server.models.project import Project
        from plexora.server.providers import node as node_provider

        project = Project.find(datasource)
        try:
            source = node_provider.project_volume_reader(project) if project else None
        except Exception:  # noqa: BLE001 -- a node that is not answering: no z-stack
            return None
        if source is None or source.volume.is_planar:
            return None
    return source


def _canonical_channel(project, datasource, name):
    """The image's own name for `name` (a channel's name or its full name)."""
    names = list(project.image.channel_names)
    if name in names:
        return name
    try:
        from plexora.server.models import data_model

        index = data_model.real_channel_index(name, datasource)
    except Exception:
        return name
    return names[index] if 0 <= index < len(names) else name


def _identity(datasource, project):
    from plexora.server.models import data_model
    from plexora.server.volume import serve

    if surface_store._node_binding(project, "image") is not None:
        return surface_store._identity(project, "image")
    return serve.source_identity(
        (data_model.config.get(datasource) or {}).get('channelFile') or project.image.src)


def _channel_index(project, datasource, name):
    names = list(project.image.channel_names)
    canonical = _canonical_channel(project, datasource, name)
    if canonical not in names:
        raise surface_levels.SurfaceRefused(f"no channel {name!r} in this image")
    return names.index(canonical)


def _region(source, args):
    """`box=x,y,w,h` and `z=z0,z1` from a query, as a Region."""
    raw = {}
    try:
        if args.get("box"):
            raw["box"] = [float(v) for v in args["box"].split(",")]
        if args.get("planes"):
            raw["z"] = [int(v) for v in args["planes"].split(",")]
    except ValueError:
        raise surface_levels.SurfaceRefused(
            "box is x,y,width,height and planes is z0,z1") from None
    volume = source.volume
    return surface_levels.Region.from_dict(raw, width=volume.width, height=volume.height,
                                           depth=volume.depth)


def _preprocess(args):
    from plexora.server.surface.isosurface import Preprocess

    return Preprocess.from_dict({"sigma_um": args.get("sigma_um"),
                                 "background_um": args.get("background_um")})


def _float(args, key, default=None):
    value = args.get(key)
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except ValueError:
        raise surface_levels.SurfaceRefused(f"{key} is a number") from None
    if not math.isfinite(number):
        raise surface_levels.SurfaceRefused(f"{key} is a number")
    return number


# -- making ------------------------------------------------------------------------


@app.route('/volume/<string:datasource>/surfaces', methods=['POST'])
@paid
def create_surface(datasource):
    """Make a surface. The body is `store.SurfaceSpec` (type, channel, t, iso,
    preprocess, min_size, smoothing, quality, level, region, ids, inset,
    label), plus `render` (the card's first look) and `replace`.

    202 with `{layer, status: "pending", created: true, ...}` while it builds
    (the card follows `/import/status` like any layer's), 200 for a surface
    that already exists, 400 with the reason (and `estimate`) for one that
    will not be built."""
    project, missing = _project(datasource)
    if missing:
        return missing
    payload = request.get_json(silent=True) or {}
    spec = dict(payload)
    if spec.get("channel"):
        spec["channel"] = _canonical_channel(project, datasource, spec["channel"])
    source = _source(datasource) if (spec.get("type") or "isosurface") == "isosurface" else None
    try:
        answer = surface_store.create(project, spec, replace_existing=bool(payload.get("replace")),
                                      source=source, render=payload.get("render"))
    except surface_levels.SurfaceRefused as error:
        return _refusal(error)
    return jsonify(answer), 202 if answer["created"] else 200


@app.route('/volume/<string:datasource>/surfaces/preview', methods=['GET'])
@paid
def preview_surface(datasource):
    """The slider's mesh: `channel`, `t`, `iso` (raw units), optional
    `min_size`, `sigma_um`, `background_um`, `box`, `planes`, `budget`
    (voxels), and `z`, `x`, `y` for the contours through the plane on screen
    and the two sections. A mesh payload; its header has the level, the
    counts and the contours."""
    from plexora.server.surface import preview as surface_preview

    project, missing = _project(datasource)
    if missing:
        return missing
    source = _source(datasource)
    if source is None:
        return jsonify(error="this image is not a z-stack on this machine"), 400
    args = request.args
    try:
        channel = _channel_index(project, datasource, args.get("channel", ""))
        iso = _float(args, "iso")
        if iso is None:
            raise surface_levels.SurfaceRefused("iso is the value in the channel's raw units")
        t = int(_float(args, "t", 0))
        data = surface_preview.preview(
            source, _identity(datasource, project), channel=channel, t=t, iso=iso,
            region=_region(source, args), pre=_preprocess(args),
            min_size=_float(args, "min_size", 0.0) or 0.0,
            z=_float(args, "z"), x=_float(args, "x"), y=_float(args, "y"),
            budget=_float(args, "budget"))
    except surface_levels.SurfaceRefused as error:
        return _refusal(error)
    response = app.response_class(data, mimetype="application/octet-stream")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/volume/<string:datasource>/surfaces/suggest', methods=['GET'])
@paid
def suggest_threshold(datasource):
    """Threshold suggestions (Otsu, triangle, mixture split) and a histogram of
    the preview grid: `channel`, `t`, and the preview's `box`, `planes`,
    `sigma_um`, `background_um`."""
    from plexora.server.surface import preview as surface_preview

    project, missing = _project(datasource)
    if missing:
        return missing
    source = _source(datasource)
    if source is None:
        return jsonify(error="this image is not a z-stack on this machine"), 400
    args = request.args
    try:
        channel = _channel_index(project, datasource, args.get("channel", ""))
        answer = surface_preview.suggest(
            source, _identity(datasource, project), channel=channel,
            t=int(_float(args, "t", 0)), region=_region(source, args), pre=_preprocess(args),
            budget=_float(args, "budget"))
    except surface_levels.SurfaceRefused as error:
        return _refusal(error)
    answer["channel"] = list(project.image.channel_names)[channel]
    answer["units"] = "raw"
    return jsonify(answer)


@app.route('/volume/<string:datasource>/surfaces/estimate', methods=['POST'])
@paid
def estimate_surface(datasource):
    """What a POST with this body would build -- the level, grid voxels or
    object count and triangle estimate -- or the 400 it would answer."""
    project, missing = _project(datasource)
    if missing:
        return missing
    spec = dict(request.get_json(silent=True) or {})
    if spec.get("channel"):
        spec["channel"] = _canonical_channel(project, datasource, spec["channel"])
    try:
        parsed = surface_store.SurfaceSpec.from_dict(spec)
        known = surface_store.check(project, parsed, source=_source(datasource))
    except surface_levels.SurfaceRefused as error:
        return _refusal(error)
    return jsonify({"ok": True, **known,
                    "resident_triangles": surface_levels.RESIDENT_TRIANGLES})


# -- reading -----------------------------------------------------------------------


def _surface_layer(project, surface_id):
    layer = project.layer(surface_id)
    if layer is None or layer.kind != surface_store.KIND:
        return None
    return layer


@app.route('/volume/<string:datasource>/surfaces', methods=['GET'])
def list_surfaces(datasource):
    """Every surface layer of the project with its build status and, once
    built, its manifest's summary."""
    from plexora.server.models import layer_jobs

    project, missing = _project(datasource)
    if missing:
        return missing
    out = []
    for layer in surface_store.surface_layers(project):
        manifest = surface_store.read_manifest(layer) if layer.status == "ready" else None
        job = layer_jobs.get(project.name, layer.id)
        spec = (layer.extra or {}).get("surface")
        out.append({"id": layer.id, "label": layer.label, "status": layer.status,
                    "visible": layer.visible, "render": dict(layer.render or {}),
                    "spec": spec, "role": (spec or {}).get("role"),
                    "job": job, "summary": _summary(manifest),
                    "stale": _stale(project, layer)})
    return jsonify(surfaces=out, licensed=_licensed())


def _stale(project, layer) -> bool:
    """Whether what the surface was made from has changed since (the image or
    the mask rewritten, a new mask): its cache key no longer matches."""
    stored = (layer.extra or {}).get("cacheKey")
    spec = (layer.extra or {}).get("surface") or {}
    if not stored or spec.get("type") not in ("isosurface", "label_surfaces"):
        return False
    try:
        return surface_store.cache_key(project, surface_store.SurfaceSpec.from_dict(spec)) != stored
    except Exception:
        return False


def _licensed() -> bool:
    from plexora import licensing

    try:
        return bool(licensing.allows(ENTITLEMENT))
    except Exception:
        return False


def _summary(manifest):
    if not manifest:
        return None
    lods = manifest.get("lods") or []
    final = lods[-1] if lods else {}
    return {"type": manifest.get("type"), "level": final.get("level"),
            "lods": [{"level": lod.get("level"), "triangles": lod.get("triangles"),
                      "bytes": lod.get("bytes"), "sha256": lod.get("sha256")} for lod in lods],
            "triangles": final.get("triangles"), "objects": (manifest.get("objects") or {}),
            "units": (manifest.get("spacing") or {}).get("units"),
            "dims": manifest.get("dims"), "source": manifest.get("source"),
            "params": manifest.get("params"), "created_at": manifest.get("created_at"),
            "notes": manifest.get("notes")}


@app.route('/volume/<string:datasource>/surfaces/<path:surface_id>/manifest', methods=['GET'])
def surface_manifest(datasource, surface_id):
    project, missing = _project(datasource)
    if missing:
        return missing
    layer = _surface_layer(project, surface_id)
    if layer is None:
        return jsonify(error=f"{datasource} has no surface {surface_id!r}"), 404
    manifest = surface_store.read_manifest(layer)
    if manifest is None:
        return jsonify(error="this surface has not been built yet", status=layer.status), 409
    return jsonify(manifest)


@app.route('/volume/<string:datasource>/surfaces/<path:surface_id>/mesh', methods=['GET'])
def surface_mesh(datasource, surface_id):
    """The mesh payload (`lod=<level>`, the finest when omitted). Immutable:
    its ETag is its sha256, and a rebuilt surface is a new file. `z=gzip`
    compresses it for a slow link, as the level route does."""
    from pathlib import Path

    project, missing = _project(datasource)
    if missing:
        return missing
    layer = _surface_layer(project, surface_id)
    if layer is None:
        return jsonify(error=f"{datasource} has no surface {surface_id!r}"), 404
    manifest = surface_store.read_manifest(layer)
    if manifest is None:
        return jsonify(error="this surface has not been built yet", status=layer.status), 409
    lods = manifest.get("lods") or []
    wanted = request.args.get("lod")
    lod = lods[-1] if lods else None
    if wanted not in (None, "", "final"):
        lod = next((entry for entry in lods if str(entry.get("level")) == str(wanted)), None)
    if lod is None:
        return jsonify(error=f"no level {wanted!r} of this surface"), 404
    tag = lod.get("sha256") or ""
    if tag and request.if_none_match.contains(tag):
        response = app.response_class(status=304)
        response.set_etag(tag)
        return response
    path = Path(layer.src) / lod["path"]
    try:
        data = path.read_bytes()
    except OSError:
        return jsonify(error="the mesh file is missing; rebuild the surface"), 410
    response = app.response_class(data, mimetype="application/octet-stream")
    if tag:
        response.set_etag(tag)
    response.headers["Cache-Control"] = "private, no-cache"
    if request.args.get("z") == "gzip" and "gzip" in (request.headers.get("Accept-Encoding") or ""):
        response.set_data(gzip.compress(data, compresslevel=1))
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Vary"] = "Accept-Encoding"
    return response


@app.route('/volume/<string:datasource>/surfaces/<path:surface_id>/objects', methods=['GET'])
def surface_objects(datasource, surface_id):
    """One row per object: mesh measurements (and the voxel ones joined by id
    for cell surfaces), as `{columns, rows}`; `ids=` limits the rows."""
    from pathlib import Path

    import polars as pl

    project, missing = _project(datasource)
    if missing:
        return missing
    layer = _surface_layer(project, surface_id)
    if layer is None:
        return jsonify(error=f"{datasource} has no surface {surface_id!r}"), 404
    path = Path(layer.src) / surface_store.OBJECTS
    if not path.exists():
        return jsonify(error="this surface has not been built yet", status=layer.status), 409
    frame = pl.read_parquet(path)
    if request.args.get("ids"):
        try:
            ids = [int(v) for v in request.args["ids"].split(",") if v]
        except ValueError:
            return jsonify(error="ids are object ids"), 400
        frame = frame.filter(pl.col("object").is_in(ids))
    rows = [[_cell(v) for v in row] for row in frame.iter_rows()]
    body = {"columns": frame.columns, "rows": rows, "count": frame.height}
    response = jsonify(body)
    if "gzip" in (request.headers.get("Accept-Encoding") or "") and len(response.get_data()) > 64_000:
        response.set_data(gzip.compress(response.get_data(), compresslevel=3))
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Vary"] = "Accept-Encoding"
    return response


def _cell(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@app.route('/volume/<string:datasource>/surfaces/<path:surface_id>/cancel', methods=['POST'])
def cancel_surface(datasource, surface_id):
    project, missing = _project(datasource)
    if missing:
        return missing
    layer = _surface_layer(project, surface_id)
    if layer is None:
        return jsonify(error=f"{datasource} has no surface {surface_id!r}"), 404
    surface_store.cancel(project.name, layer.id)
    return jsonify(success=True, layer=layer.id)



# -- views, export, edits, neighbours, import (Phase 2) -----------------------------


def _current(project, surface_id):
    """`(layer, mesh, manifest)` or an error answer."""
    layer = _surface_layer(project, surface_id)
    if layer is None:
        return None, (jsonify(error=f"{project.name} has no surface {surface_id!r}"), 404)
    surface, manifest = surface_store.current_mesh(layer)
    if surface is None:
        return None, (jsonify(error="this surface has not been built yet",
                              status=layer.status), 409)
    return (layer, surface, manifest), None


@app.route('/volume/<string:datasource>/surfaces/<path:surface_id>/sections', methods=['GET'])
def surface_sections(datasource, surface_id):
    """Where the surface crosses plane `z` (XY), row `y` (XZ) and column `x`
    (YZ), level-0 voxels: `{xy: {z, objects: {id: [a0, b0, a1, b1, ...]}}, ...}`."""
    from plexora.server.surface import sections

    project, missing = _project(datasource)
    if missing:
        return missing
    found, error = _current(project, surface_id)
    if error:
        return error
    layer, surface, manifest = found
    tag = (manifest.get("lods") or [{}])[-1].get("sha256", "")
    out = {}
    for name, axis, key in (("xy", "z", "z"), ("xz", "y", "y"), ("yz", "x", "x")):
        raw = request.args.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except ValueError:
            return jsonify(error=f"{key} is a number"), 400
        cut = sections.cached((tag, axis, value), lambda a=axis, v=value: sections.section(surface, a, v))
        out[name] = {key: value, "objects": cut}
    return jsonify(out)


@app.route('/volume/<string:datasource>/surfaces/<path:surface_id>/export', methods=['GET'])
def export_surface(datasource, surface_id):
    """The mesh as `format` = ply | obj | stl | glb, in microns when the
    spacing is known; `ids=` exports only those objects; zipped with its
    manifest unless `bundle=0`."""
    from plexora.server.surface import export as surface_export

    project, missing = _project(datasource)
    if missing:
        return missing
    found, error = _current(project, surface_id)
    if error:
        return error
    layer, surface, manifest = found
    ids = None
    if request.args.get("ids"):
        try:
            ids = [int(v) for v in request.args["ids"].split(",") if v]
        except ValueError:
            return jsonify(error="ids are object ids"), 400
    scale = (manifest.get("spacing") or {}).get("scale") or [1.0, 1.0, 1.0]
    units = (manifest.get("spacing") or {}).get("units") or "voxel"
    if units != "um":
        scale = [1.0, 1.0, 1.0]
    try:
        data, name, mimetype = surface_export.export(
            surface, request.args.get("format", "ply"), scale=scale, units=units,
            manifest=manifest, ids=ids, bundle=request.args.get("bundle", "1") != "0",
            name=layer.label or layer.id)
    except surface_export.ExportRefused as refused:
        return jsonify(error=str(refused)), 400
    response = app.response_class(data, mimetype=mimetype)
    response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


@app.route('/volume/<string:datasource>/surfaces/<path:surface_id>/refine', methods=['POST'])
@paid
def refine_surface(datasource, surface_id):
    """An edit as a new version of the mesh: `{op, ...params}` (refine.OPS);
    `undo` drops the last one. The mask is never touched."""
    from plexora.server.surface import refine

    project, missing = _project(datasource)
    if missing:
        return missing
    found, error = _current(project, surface_id)
    if error:
        return error
    layer, _surface, manifest = found
    payload = request.get_json(silent=True) or {}
    op = payload.get("op")
    params = {k: v for k, v in payload.items() if k != "op"}
    scale, physical = surface_store.project_scale(project)
    try:
        updated = refine.refine(layer.src, op, params, scale=scale, physical=physical,
                                frame_for=lambda m: surface_store.frame_for(project, layer, m,
                                                                            manifest))
    except refine.RefineRefused as refused:
        body = {"error": str(refused)}
        if refused.install:
            body["install"] = refused.install
        return jsonify(body), 400
    return jsonify({"layer": layer.id, "note": updated.get("last_edit"),
                    "summary": _summary(updated), "versions": updated.get("versions")})


@app.route('/volume/<string:datasource>/surfaces/<path:surface_id>/neighbours', methods=['GET'])
@paid
def surface_neighbours(datasource, surface_id):
    """Objects near `object`: `mode=surface` (shortest surface-to-surface
    distance, the default) or `centroid`, within `within` (µm, or voxels
    without a z step). For cell surfaces each answer also carries the contact
    area the two cells share in the mask. Geometry, not interaction."""
    from pathlib import Path

    import polars as pl

    from plexora.server.surface import contacts

    project, missing = _project(datasource)
    if missing:
        return missing
    found, error = _current(project, surface_id)
    if error:
        return error
    layer, surface, manifest = found
    try:
        target = int(request.args.get("object"))
        within = float(request.args.get("within", 5))
    except (TypeError, ValueError):
        return jsonify(error="object is an id and within a distance"), 400
    if not within > 0:
        return jsonify(error="within is above zero"), 400
    mode = request.args.get("mode", "surface")
    spacing = manifest.get("spacing") or {}
    physical = spacing.get("units") == "um"
    scale = spacing.get("scale") if physical else [1.0, 1.0, 1.0]
    if mode == "centroid":
        frame = pl.read_parquet(Path(layer.src) / surface_store.OBJECTS)
        pairs = contacts.centroid_neighbours(frame, target, within, physical=physical)
    elif mode == "surface":
        pairs = contacts.surface_neighbours(surface, target, within, scale=scale)
    else:
        return jsonify(error="mode is surface or centroid"), 400
    rows = [{"object": int(o), "distance": round(float(d), 4)} for o, d in pairs]
    contact_note = None
    spec = (layer.extra or {}).get("surface") or {}
    if spec.get("type") == "label_surfaces":
        from plexora.server.volume import labels as volume_labels

        table = surface_store.object_table(project, int(spec.get("t") or 0))
        box = None
        if table is not None:
            mine = table.filter(pl.col("label") == target)
            if mine.height:
                r = mine.row(0, named=True)
                box = (r["bbox_z0"], r["bbox_z1"], r["bbox_y0"], r["bbox_y1"],
                       r["bbox_x0"], r["bbox_x1"])
        # A data node's mask is read through it, one block around the cell.
        labels = volume_labels.project_label_volume(project, int(spec.get("t") or 0)) \
            if box is not None else None
        if labels is not None:
            areas = contacts.contact_areas(labels, target, box, spacing=scale)
            for row in rows:
                row["contact_area"] = round(float(areas.get(row["object"], 0.0)), 4)
            contact_note = "contact area: voxel faces the two cells share in the mask"
    return jsonify({"object": target, "mode": mode, "within": within,
                    "units": "um" if physical else "voxel", "neighbours": rows,
                    "contact": contact_note,
                    "note": "Distances between surfaces; nearness is not interaction."})


@app.route('/volume/<string:datasource>/surfaces/import', methods=['POST'])
@paid
def import_surface(datasource):
    """A mesh file (PLY, OBJ, STL, GLB, or a zip Plexora exported) as a
    surface: multipart `file`, `units` = um | voxel (read from the file when
    it says), `offset` = x,y,z in those units, `label`."""
    from plexora.server.surface import importers

    project, missing = _project(datasource)
    if missing:
        return missing
    upload = request.files.get("file")
    if upload is None:
        return jsonify(error="send the mesh as the multipart field file"), 400
    name = upload.filename or "mesh"
    data = upload.read(importers.MAX_BYTES + 1)
    given = None
    try:
        fmt = importers.sniff(name)
        if fmt == "zip":
            data, fmt, given = importers.from_bundle(data, name)
        positions, triangles, ids, comments = importers.read(data, fmt)
        units = (request.form.get("units") or (given or {}).get("export", {}).get("units")
                 or importers.units_from_comments(comments) or "um")
        try:
            offset = [float(v) for v in (request.form.get("offset") or "0,0,0").split(",")]
            assert len(offset) == 3
        except (ValueError, AssertionError):
            return jsonify(error="offset is x,y,z"), 400
        scale, physical = surface_store.project_scale(project)
        if units == "um" and not physical:
            return jsonify(error="this image's z step is not known, so a mesh in microns "
                                 "cannot be placed; set the z step, or import in voxels"), 400
        surface = importers.place(positions, triangles, ids, units=units, spacing_um=scale,
                                  offset=offset)
        label = (request.form.get("label") or name.rsplit(".", 1)[0])[:120]
        answer = surface_store.create_imported(project, surface, label=label, units=units,
                                               file_name=name, given=given)
    except importers.ImportRefused as refused:
        return jsonify(error=str(refused)), 400
    return jsonify(answer), 201


surface_store.register()
