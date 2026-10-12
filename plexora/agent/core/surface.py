"""Surfaces as capabilities: make, inspect, measure, refine and show 3-D surfaces.

Every call maps onto a recorded, reversible operation of
plexora/server/surface -- the AI never draws or invents geometry. A surface's
manifest says how it was made (source, isovalue in raw units, smoothing,
level, spacing), and every answer here repeats what an agent must not claim
from it: an isosurface is a chosen boundary, not a validated one; smoothed
mesh measurements describe the drawing, the mask's voxel measurements are
the quantitative ones; nearness is geometry, not interaction.

Licensed: making, refining and analysing surfaces is Surface Studio
(`surfaces`); listing, reading, restyling, exporting and deleting a surface
that exists are Free, like everything else a Paid feature produced. Over MCP
a Paid tool also needs `mcp` (plexora/licensing/guards.py).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel, ProjectInput

ENTITLEMENT = "surfaces"
TAGS = ("surface", "surfaces", "mesh", "isosurface", "3d", "volume", "z-stack", "render",
        "segmentation", "cells", "objects", "shape")

NOTES = ("An isosurface is the boundary at one chosen value, not a validated object "
         "boundary; smoothed meshes describe the drawing -- the mask's voxel measurements "
         "are the quantitative ones.")


def _surface_layer(record, surface_id):
    from plexora.server.surface import store

    layer = record.layer(surface_id)
    if layer is None or layer.kind != store.KIND:
        raise AgentError("invalid_input", f"{record.name} has no surface {surface_id!r} "
                                      "(surface_list names them)")
    return layer


def _summary(layer):
    from plexora.server.surface import store

    manifest = store.read_manifest(layer) or {}
    lods = manifest.get("lods") or []
    final = lods[-1] if lods else {}
    return {"surface": layer.id, "label": layer.label, "status": layer.status,
            "type": manifest.get("type") or ((layer.extra or {}).get("surface") or {}).get("type"),
            "params": manifest.get("params") or (layer.extra or {}).get("surface"),
            "level": final.get("level"), "triangles": final.get("triangles"),
            "objects": (manifest.get("objects") or {}).get("count"),
            "touching_bounds": (manifest.get("objects") or {}).get("touching_bounds"),
            "units": (manifest.get("spacing") or {}).get("units"),
            "versions": len(manifest.get("versions") or []),
            "render": dict(layer.render or {}), "visible": layer.visible}


# -- surface_create --------------------------------------------------------------


class CreateInput(ProjectInput):
    type: Literal["isosurface", "label_surfaces"] = Field(
        "isosurface", description="isosurface: where a channel crosses `iso`; label_surfaces: "
                                  "one closed surface per cell of the 3-D mask, under its id.")
    channel: str | None = Field(None, description="The channel (isosurface).")
    iso: float | None = Field(None, description="The isovalue in the channel's RAW units "
                              "(surface_suggest_threshold proposes some). Never a display "
                              "window's 0..1.")
    t: int = Field(0, ge=0, description="Frame of a time series.")
    smoothing: Literal["none", "light", "medium", "strong"] | None = Field(
        None, description="Taubin smoothing, half a voxel at most (default light for an "
                          "isosurface, medium for cells).")
    quality: Literal["preview", "standard", "full"] = Field(
        "standard", description="preview: quick and coarse; standard: up to 3 M triangles; "
                                "full: level 0 where it fits.")
    min_size: float = Field(0.0, ge=0, description="Drop pieces enclosing less (µm³, or "
                            "voxels³ without a z step).")
    sigma_um: float = Field(0.0, ge=0, description="Gaussian blur before thresholding, µm.")
    background_um: float = Field(0.0, ge=0, description="Subtract the local background at "
                                 "this object diameter, µm (0: off).")
    box: list[float] | None = Field(None, min_length=4, max_length=4,
                                    description="Only this area: [x, y, width, height], "
                                                "full-resolution pixels.")
    planes: list[int] | None = Field(None, min_length=2, max_length=2,
                                     description="Only planes [z0, z1], inclusive.")
    ids: list[int] | None = Field(None, description="label_surfaces: only these cells.")
    label: str | None = Field(None, description="The card's name (default: what it is).")
    color: str | None = Field(None, description="Its colour, #rrggbb.")
    replace: bool = Field(False, description="Build again when the same surface exists.")


def surface_create(call, inp):
    """Make a surface and wait for it."""
    from plexora.agent.receipts import make_receipt
    from plexora.server.models import layer_jobs
    from plexora.server.models.project import Project
    from plexora.server.surface import store

    record = call.session.project(inp.project)
    spec = {"type": inp.type, "channel": inp.channel, "iso": inp.iso, "t": inp.t,
            "quality": inp.quality, "min_size": inp.min_size, "label": inp.label}
    if inp.smoothing:
        spec["smoothing"] = inp.smoothing
    if inp.sigma_um or inp.background_um:
        spec["preprocess"] = {"sigma_um": inp.sigma_um, "background_um": inp.background_um}
    region = {}
    if inp.box:
        region["box"] = list(inp.box)
    if inp.planes:
        region["z"] = list(inp.planes)
    if region:
        spec["region"] = region
    if inp.ids:
        spec["ids"] = list(inp.ids)
    # A data node's z-stack is described here and built on the node.
    source = store.open_volume_for(record) if inp.type == "isosurface" else None
    try:
        answer = store.create(record, spec, replace_existing=inp.replace, source=source,
                              render={"color": inp.color} if inp.color else None)
    except store.SurfaceRefused as refused:
        raise AgentError(refused.code, str(refused), detail=refused.estimate or None) from None
    finally:
        if source is not None:
            source.close()
    layer_id = answer["layer"]
    if call.notify is not None:
        call.notify(record.name, "core", "layers", {})
    while True:
        job = layer_jobs.get(record.name, layer_id) or {}
        status = job.get("status")
        if status is None:
            found = Project.find(record.name)
            layer = found.layer(layer_id) if found is not None else None
            status = getattr(layer, "status", None) or "ready"
        if status != "pending":
            break
        if call.cancelled():
            store.cancel(record.name, layer_id)
            raise InterruptedError("cancelled")
        call.progress(job.get("progress"), 100, job.get("message") or "extracting")
        time.sleep(0.25)
    if status == "failed":
        raise AgentError("invalid_input", f"the surface could not be built: "
                         f"{job.get('error') or 'see the server log'}")
    call.session.invalidate(inp.project)
    if call.notify is not None:
        call.notify(record.name, "core", "layers", {})
    layer = Project.find(record.name).layer(layer_id)
    receipt = make_receipt(call, changed=bool(answer["created"]), before=None,
                           after={"layer": layer_id}, persistent_state="project_config")
    return {**_summary(layer), "created": answer["created"], "note": NOTES,
            "next": "viewer_set_volume_mode(mode='volume') then viewer_capture to look at it",
            "receipt": receipt.model_dump(mode="json")}


# -- suggest / list / inspect / measure ------------------------------------------------


class SuggestInput(ProjectInput):
    channel: str = Field(description="The channel to threshold.")
    t: int = Field(0, ge=0)
    sigma_um: float = Field(0.0, ge=0)
    background_um: float = Field(0.0, ge=0)


def surface_suggest_threshold(call, inp):
    """Otsu, triangle and mixture thresholds in raw units, over a coarse grid."""
    from plexora.server.surface import levels, preview
    from plexora.server.surface import store
    from plexora.server.surface.isosurface import Preprocess

    record = call.session.project(inp.project)
    names = list(record.image.channel_names)
    if inp.channel not in names:
        raise AgentError("invalid_input", f"no channel {inp.channel!r}; the channels are "
                                          f"{', '.join(names)}")
    source = store.open_volume_for(record)
    if source is None:
        raise AgentError("invalid_input", f"{record.name}'s image is not a z-stack")
    try:
        volume = source.volume
        answer = preview.suggest(
            source, f"agent|{store._identity(record, 'image')}",
            channel=names.index(inp.channel), t=inp.t,
            region=levels.Region(), pre=Preprocess(sigma_um=inp.sigma_um,
                                                   background_um=inp.background_um))
    finally:
        source.close()
    histogram = answer.pop("histogram", None) or {}
    answer["histogram_range"] = [histogram.get("lo"), histogram.get("hi")]
    answer["units"] = "raw"
    answer["note"] = ("Suggestions from the data's histogram at a coarse level; look at the "
                      "result (and its contours in the planes) before relying on one.")
    return answer


class ListInput(ProjectInput):
    pass


def surface_list(call, inp):
    from plexora.server.surface import store

    record = call.session.project(inp.project)
    return {"surfaces": [_summary(layer) for layer in store.surface_layers(record)]}


class SurfaceInput(ProjectInput):
    surface: str = Field(description="The surface's layer id (surf-...).")


def surface_inspect(call, inp):
    """Quality questions about a surface: fragments, cut-off objects, how far
    smoothing moved it, open pieces -- with what each means."""
    import polars as pl

    from plexora.server.surface import store

    record = call.session.project(inp.project)
    layer = _surface_layer(record, inp.surface)
    manifest = store.read_manifest(layer)
    if manifest is None:
        raise AgentError("invalid_input", "this surface has not been built yet")
    frame = pl.read_parquet(Path(layer.src) / store.OBJECTS)
    unit = "um" if (manifest.get("spacing") or {}).get("units") == "um" else "vox"
    volume_col = f"mesh_volume_{unit}3"
    volumes = np.asarray(frame[volume_col], dtype=np.float64) if volume_col in frame.columns \
        else np.zeros(frame.height)
    largest = float(np.nanmax(volumes)) if len(volumes) else 0.0
    tiny = int(np.sum(volumes < 0.01 * largest)) if largest > 0 else 0
    smoothing = (manifest.get("build") or {}).get("report", {}).get("smoothing") or {}
    flags = []
    if tiny:
        flags.append(f"{tiny} piece(s) under 1% of the largest: noise or fragments; "
                     "surface_refine remove_small, or raise min_size")
    touching = int(np.sum(np.asarray(frame["touches_bounds"]))) if "touches_bounds" in frame.columns else 0
    if touching:
        flags.append(f"{touching} object(s) touch the edge of the volume: their volumes are "
                     "of what is inside it")
    open_count = int(np.sum(~np.asarray(frame["closed"], dtype=bool))) if "closed" in frame.columns else 0
    if open_count:
        flags.append(f"{open_count} object(s) are open: no volume is reported for them")
    if smoothing.get("max_displacement", 0) > 0:
        flags.append(f"smoothing moved vertices up to {smoothing['max_displacement']:.3g} "
                     f"{'µm' if unit == 'um' else 'voxels'} (capped at half a voxel)")
    if "voxel_volume_um3" in frame.columns and volume_col in frame.columns:
        ratio = np.asarray(frame[volume_col], dtype=np.float64) / np.asarray(
            frame["voxel_volume_um3"], dtype=np.float64)
        ratio = ratio[np.isfinite(ratio)]
        if len(ratio):
            flags.append(f"mesh / voxel volume: median {np.median(ratio):.3f} "
                         f"(5th-95th {np.percentile(ratio, 5):.3f}-{np.percentile(ratio, 95):.3f})")
    return {**_summary(layer), "pieces_under_1pct": tiny, "touching_bounds": touching,
            "open_objects": open_count, "smoothing": smoothing or None,
            "largest_volume": largest, "volume_units": f"{unit}³", "findings": flags,
            "note": NOTES}


def surface_measure(call, inp):
    """The distribution of every measurement over a surface's objects."""
    import polars as pl

    from plexora.server.surface import store

    record = call.session.project(inp.project)
    layer = _surface_layer(record, inp.surface)
    path = Path(layer.src) / store.OBJECTS
    if not path.exists():
        raise AgentError("invalid_input", "this surface has not been built yet")
    frame = pl.read_parquet(path)
    summary = {}
    for name in frame.columns:
        if name == "object" or name.startswith("bbox_") or frame[name].dtype == pl.Boolean:
            continue
        values = np.asarray(frame[name], dtype=np.float64)
        values = values[np.isfinite(values)]
        if not len(values):
            continue
        p5, p50, p95 = np.percentile(values, [5, 50, 95])
        summary[name] = {"n": int(len(values)), "min": float(values.min()), "p5": float(p5),
                         "median": float(p50), "p95": float(p95), "max": float(values.max())}
    flags = {name: int(np.sum(np.asarray(frame[name], dtype=bool)))
             for name in ("closed", "touches_bounds") if name in frame.columns}
    return {"surface": layer.id, "objects": frame.height, "measurements": summary,
            "counts": flags,
            "note": "mesh_* columns are of the surface; voxel_* columns (cell surfaces) are "
                    "the mask's own and are the ones to report. Per-object rows: "
                    "surface_objects."}


class MeasureInput(SurfaceInput):
    sort_by: str | None = Field(None, description="A column to sort by, largest first.")
    limit: int = Field(50, ge=1, le=5000)
    ids: list[int] | None = Field(None, description="Only these objects.")


def surface_objects(call, inp):
    import polars as pl

    from plexora.server.surface import store

    record = call.session.project(inp.project)
    layer = _surface_layer(record, inp.surface)
    path = Path(layer.src) / store.OBJECTS
    if not path.exists():
        raise AgentError("invalid_input", "this surface has not been built yet")
    frame = pl.read_parquet(path)
    if inp.ids:
        frame = frame.filter(pl.col("object").is_in(list(inp.ids)))
    if inp.sort_by:
        if inp.sort_by not in frame.columns:
            raise AgentError("invalid_input", f"no column {inp.sort_by!r}; columns are "
                                              f"{', '.join(frame.columns)}")
        frame = frame.sort(inp.sort_by, descending=True, nulls_last=True)
    rows = frame.head(inp.limit).to_dicts()
    for row in rows:
        for key, value in list(row.items()):
            if isinstance(value, float) and not np.isfinite(value):
                row[key] = None
    return {"surface": layer.id, "objects": frame.height, "columns": frame.columns,
            "rows": rows, "note": "mesh_* columns are of the surface; voxel_* columns (cell "
                                  "surfaces) are the mask's own and are the ones to report."}


# -- refine / neighbours / export / update / delete --------------------------------------


class RefineInput(SurfaceInput):
    op: Literal["remove_small", "keep_largest", "delete", "keep", "smooth", "decimate", "undo"]
    min_size: float | None = Field(None, ge=0)
    n: int | None = Field(None, ge=1)
    ids: list[int] | None = None
    smoothing: Literal["light", "medium", "strong"] | None = None
    ratio: float | None = Field(None, gt=0, lt=1)


def surface_refine(call, inp):
    from plexora.agent.receipts import make_receipt
    from plexora.server.surface import refine, store

    record = call.session.project(inp.project)
    layer = _surface_layer(record, inp.surface)
    params = {k: v for k, v in inp.model_dump().items()
              if k in ("min_size", "n", "ids", "smoothing", "ratio") and v is not None}
    scale, physical = store.project_scale(record)
    manifest = store.read_manifest(layer)
    try:
        updated = refine.refine(layer.src, inp.op, params, scale=scale, physical=physical,
                                frame_for=lambda m: store.frame_for(record, layer, m, manifest))
    except refine.RefineRefused as refused:
        raise AgentError("invalid_input", str(refused),
                         detail={"install": refused.install} if refused.install else None) from None
    if call.notify is not None:
        call.notify(record.name, "core", "layers", {})
    receipt = make_receipt(call, changed=True, before=None,
                           after={"surface": layer.id, "op": inp.op,
                                  "versions": len(updated.get("versions") or [])},
                           persistent_state="project_config")
    return {"surface": layer.id, "note": updated.get("last_edit"),
            "versions": updated.get("versions"), "undo": "surface_refine op='undo'",
            "receipt": receipt.model_dump(mode="json")}


class NeighboursInput(SurfaceInput):
    object: int = Field(description="The object (cell id for cell surfaces).")
    within: float = Field(5.0, gt=0, description="Distance, µm (voxels without a z step).")
    mode: Literal["surface", "centroid"] = "surface"


def surface_neighbours(call, inp):
    import polars as pl

    from plexora.server.surface import contacts, store

    record = call.session.project(inp.project)
    layer = _surface_layer(record, inp.surface)
    surface, manifest = store.current_mesh(layer)
    if surface is None:
        raise AgentError("invalid_input", "this surface has not been built yet")
    spacing = manifest.get("spacing") or {}
    physical = spacing.get("units") == "um"
    scale = spacing.get("scale") if physical else [1.0, 1.0, 1.0]
    if inp.mode == "centroid":
        frame = pl.read_parquet(Path(layer.src) / store.OBJECTS)
        pairs = contacts.centroid_neighbours(frame, inp.object, inp.within, physical=physical)
    else:
        pairs = contacts.surface_neighbours(surface, inp.object, inp.within, scale=scale)
    return {"surface": layer.id, "object": inp.object, "mode": inp.mode,
            "units": "um" if physical else "voxel",
            "neighbours": [{"object": int(o), "distance": round(float(d), 4)} for o, d in pairs],
            "note": "Distances between surfaces; nearness is not interaction."}


class ExportInput(SurfaceInput):
    format: Literal["ply", "obj", "stl", "glb"] = "ply"
    ids: list[int] | None = Field(None, description="Only these objects.")


def surface_export(call, inp):
    from plexora.server.surface import export, store

    record = call.session.project(inp.project)
    layer = _surface_layer(record, inp.surface)
    surface, manifest = store.current_mesh(layer)
    if surface is None:
        raise AgentError("invalid_input", "this surface has not been built yet")
    spacing = manifest.get("spacing") or {}
    units = spacing.get("units") or "voxel"
    scale = spacing.get("scale") if units == "um" else [1.0, 1.0, 1.0]
    try:
        data, name, _ = export.export(surface, inp.format, scale=scale, units=units,
                                      manifest=manifest, ids=inp.ids, name=layer.label)
    except export.ExportRefused as refused:
        raise AgentError("invalid_input", str(refused)) from None
    folder = Path(layer.src) / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return {"surface": layer.id, "path": str(path), "bytes": len(data), "units": units,
            "format": inp.format, "note": "Zipped with its manifest (how it was made)."}


class UpdateInput(SurfaceInput):
    visible: bool | None = None
    opacity: float | None = Field(None, ge=0, le=1)
    color: str | None = Field(None, description="#rrggbb")
    style: Literal["solid", "glass", "wireframe", "solid+wire", "outline",
                   "illustration"] | None = None
    material: Literal["matte", "soft", "glossy", "glass"] | None = None
    color_by: Literal["uniform", "id", "cells"] | None = None
    label: str | None = None


def surface_update(call, inp):
    """How a surface is drawn (saved with it): its look, never its geometry."""
    from dataclasses import replace

    from plexora.agent.receipts import make_receipt
    from plexora.server.models.project import Project

    record = call.session.project(inp.project)
    layer = _surface_layer(record, inp.surface)
    patch = {"opacity": inp.opacity, "color": inp.color, "style": inp.style,
             "material": inp.material, "colorBy": inp.color_by}
    patch = {k: v for k, v in patch.items() if v is not None}
    before = dict(layer.render or {})

    def change(current):
        found = current.layer(layer.id)
        if found is None:
            return current
        updated = replace(found, render={**dict(found.render or {}), **patch},
                          visible=found.visible if inp.visible is None else inp.visible,
                          label=inp.label or found.label)
        return current.with_layer(updated)

    Project.mutate(record.name, change)
    call.session.invalidate(inp.project)
    if call.notify is not None:
        call.notify(record.name, "core", "layers", {})
    receipt = make_receipt(call, changed=True, before={"render": before},
                           after={"render": {**before, **patch}}, persistent_state="project_config")
    return {"surface": layer.id, "render": {**before, **patch},
            "receipt": receipt.model_dump(mode="json")}


class DeleteInput(SurfaceInput):
    confirm: bool = Field(False, description="Must be true: the mesh files are deleted "
                          "(the image and mask are not).")


def surface_delete(call, inp):
    from plexora.agent.receipts import make_receipt
    from plexora.server.models.project import Project
    from plexora.server.surface import store

    record = call.session.project(inp.project)
    layer = _surface_layer(record, inp.surface)
    Project.mutate(record.name, lambda current: current.without_layer(layer.id))
    store.forget(record, layer)
    call.session.invalidate(inp.project)
    if call.notify is not None:
        call.notify(record.name, "core", "layers", {})
    receipt = make_receipt(call, changed=True, before={"layer": layer.id}, after=None,
                           persistent_state="project_config", reversible=False)
    return {"surface": layer.id, "deleted": True,
            "note": "The mesh files were Plexora's and are gone; the image and mask are "
                    "unchanged. surface_create with the same parameters makes it again.",
            "receipt": receipt.model_dump(mode="json")}


def capabilities():
    def paid(**kwargs):
        return Capability(owner="core", entitlement=ENTITLEMENT, tags=TAGS, **kwargs)

    def free(**kwargs):
        return Capability(owner="core", tags=TAGS, **kwargs)

    return [
        paid(name="surface.create", tool_name="surface_create",
             purpose="Make a 3-D surface of a z-stack and add it as a layer: the isosurface "
                     "where a channel crosses a raw value, or one closed surface per cell of "
                     "the 3-D mask (under the cell's id). Records how it was made; never "
                     "edits the mask.",
             permission="reversible_write", input_model=CreateInput, handler=surface_create,
             execution="job", writes=("project_config",), reads=("image", "segmentation")),
        paid(name="surface.suggest_threshold", tool_name="surface_suggest_threshold",
             purpose="Threshold suggestions (Otsu, triangle, mixture split) for a channel's "
                     "isosurface, in raw intensity units, from a coarse level of the stack.",
             permission="read", input_model=SuggestInput, handler=surface_suggest_threshold,
             egress="aggregates", reads=("image",)),
        paid(name="surface.inspect", tool_name="surface_inspect",
             purpose="Check a surface: fragments, objects cut by the volume's edge, open "
                     "objects, how far smoothing moved it, and mesh versus voxel volume.",
             permission="read", input_model=SurfaceInput, handler=surface_inspect,
             egress="aggregates"),
        paid(name="surface.refine", tool_name="surface_refine",
             purpose="Edit a surface as a new version (remove small pieces, keep the largest, "
                     "delete or keep objects, smooth again, decimate; undo the last edit). "
                     "The mask is never touched.",
             permission="reversible_write", input_model=RefineInput, handler=surface_refine,
             writes=("project_config",)),
        paid(name="surface.neighbours", tool_name="surface_neighbours",
             purpose="Objects near one object of a surface: shortest surface-to-surface or "
                     "centroid distance within a radius. Geometry, not interaction.",
             permission="read", input_model=NeighboursInput, handler=surface_neighbours,
             egress="aggregates"),
        free(name="surface.list", tool_name="surface_list",
             purpose="The project's surfaces: what each is (source, isovalue, level, "
                     "triangles, objects), its build status and how it is drawn.",
             permission="read", input_model=ListInput, handler=surface_list),
        free(name="surface.measure", tool_name="surface_measure",
             purpose="How a surface's objects measure, as distributions: mesh area, volume, "
                     "sphericity, elongation (and the mask's voxel volume for cell "
                     "surfaces), with how many are closed or cut by the volume's edge.",
             permission="read", input_model=SurfaceInput, handler=surface_measure,
             egress="aggregates"),
        free(name="surface.objects", tool_name="surface_objects",
             purpose="A surface's objects table, one row per object (sortable): mesh "
                     "measurements, centroid and bounds, and the voxel ones for cells.",
             permission="read", input_model=MeasureInput, handler=surface_objects,
             egress="row_level"),
        free(name="surface.export", tool_name="surface_export",
             purpose="Write a surface as PLY, OBJ, STL or glTF (µm when the spacing is "
                     "known), zipped with its manifest, into the surface's folder.",
             permission="reversible_write", input_model=ExportInput, handler=surface_export,
             writes=("files",)),
        free(name="surface.update", tool_name="surface_update",
             purpose="How a surface is drawn: visible, opacity, colour, style (solid, glass, "
                     "wireframe, outline), material, colour by id or cell colours. Saved.",
             permission="reversible_write", input_model=UpdateInput, handler=surface_update,
             writes=("project_config",)),
        free(name="surface.delete", tool_name="surface_delete",
             purpose="Remove a surface layer and the mesh files Plexora made for it.",
             permission="destructive", input_model=DeleteInput, handler=surface_delete,
             writes=("project_config",)),
    ]
