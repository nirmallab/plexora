"""`segment_qc_roi`: the agent outlines an artifact with magic select.

Outside a QC session, an agent that looked at a picture and saw an obvious
object the detectors missed -- or outlined loosely -- can point at it and get
a snug QC region: one include point inside the object, an exclude point on
tissue it wrongly took, a box for a large or textured one. Points are given
either in full-resolution image pixels or, more usefully, in the pixels of a
picture the agent was shown (`{artifact_id, px}` on a `render_region`
artifact), so it never has to do coordinate arithmetic.

Modes: `new` writes a region of `artifact_class`; `replace`, `union` and
`subtract` reshape an existing QC region (`roi_id`). `preview: true` writes
nothing and returns a picture of the proposed outline (and a 512 px artifact
the agent can point into again). Every write is receipted and undoable.

The guards are the same as everywhere else: an open QC session owns its
regions (refused), a locked region is the user's promise (refused), a region
the user reshaped needs `force`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.receipts import make_receipt
from plexora.agent.schemas import AgentModel, ProjectInput

MAX_POINTS = 32
#: How much of the picture round the prompts is shown to the model, as a
#: multiple of their extent, and the smallest view, in full-resolution pixels.
VIEW_PAD = 0.75
MIN_VIEW_PX = 256


class SegPoint(AgentModel):
    x: float = Field(description="Full-resolution image pixels.")
    y: float
    label: Literal[0, 1] = Field(1, description="1 inside the artifact, 0 on what is not.")


class ArtifactPoint(AgentModel):
    artifact_id: str = Field(description="A render_region artifact you looked at (art_...).")
    px: list[float] = Field(min_length=2, max_length=2,
                            description="[x, y] in that picture's own pixels.")
    label: Literal[0, 1] = 1


class SegBox(AgentModel):
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class ArtifactBox(AgentModel):
    artifact_id: str
    px: list[float] = Field(min_length=4, max_length=4,
                            description="[x0, y0, x1, y1] in that picture's own pixels.")


class SegmentRoiInput(ProjectInput):
    points: list[SegPoint | ArtifactPoint] = Field(
        default_factory=list, max_length=MAX_POINTS,
        description="Include (label 1) and exclude (label 0) points: image pixels, or "
                    "{artifact_id, px} on a picture you were shown.")
    box: SegBox | ArtifactBox | None = Field(
        None, description="A box round a large or textured object (image pixels, or on a "
                          "picture you were shown).")
    roi_id: str | None = Field(None, description="The QC region to reshape (modes other "
                                                 "than new).")
    mode: Literal["new", "replace", "union", "subtract"] = "new"
    artifact_class: str | None = Field(None, description="For mode new: what the object is "
                                                         "(an artifact class).")
    channels: list[str] | None = Field(None, max_length=8, description="Channels to show the "
                                       "model (default: the region's drawing view, else the "
                                       "picture's, else the nuclear stain).")
    from_roi: bool = Field(True, description="With roi_id: start from that region -- its "
                                             "outline is the model's starting mask and its "
                                             "box the prompt when you give none -- so a loose "
                                             "region can be tightened with no points at all.")
    force: bool = Field(False, description="Also reshape a region the user reshaped.")
    preview: bool = Field(False, description="Write nothing; return a picture of the "
                                             "proposed outline.")
    notes: str | None = Field(None, max_length=300, description="Your note on the region.")


def _artifact_frame(project, artifact_id):
    """(x0, y0, sx, sy): picture pixel -> image pixel, from a render's manifest."""
    from plexora.agent import artifacts

    try:
        _png, sidecar = artifacts.get(artifact_id)
    except KeyError:
        raise AgentError("invalid_input", f"no artifact {artifact_id!r}") from None
    manifest = sidecar.get("manifest") or {}
    if sidecar.get("project") not in (None, project) or manifest.get("project") not in (
            None, project):
        raise AgentError("invalid_input", f"{artifact_id} is a picture of another project")
    bounds = manifest.get("bounds_fullres")
    size = manifest.get("output_size")
    if not bounds or not size:
        raise AgentError("invalid_input", f"{artifact_id} does not say where its pixels are "
                         "on the image; point on a render_region picture, or give image "
                         "pixels", detail={"kind": sidecar.get("kind")})
    return (float(bounds["x"]), float(bounds["y"]), float(bounds["width"]) / float(size[0]),
            float(bounds["height"]) / float(size[1])), manifest


def _resolve_prompts(project, inp):
    """Points and box in image pixels, and the first picture's channels."""
    points, picture_channels = [], None
    frames = {}

    def frame(artifact_id):
        nonlocal picture_channels
        if artifact_id not in frames:
            frames[artifact_id], manifest = _artifact_frame(project, artifact_id)
            if picture_channels is None:
                picture_channels = [{"name": c.get("name"), "color": c.get("color"),
                                     "range": c.get("window")}
                                    for c in manifest.get("channels") or [] if c.get("name")]
        return frames[artifact_id]

    for point in inp.points:
        if isinstance(point, ArtifactPoint):
            x0, y0, sx, sy = frame(point.artifact_id)
            points.append({"x": x0 + point.px[0] * sx, "y": y0 + point.px[1] * sy,
                           "label": point.label})
        else:
            points.append({"x": point.x, "y": point.y, "label": point.label})
    box = None
    if isinstance(inp.box, ArtifactBox):
        x0, y0, sx, sy = frame(inp.box.artifact_id)
        bx0, by0, bx1, by1 = inp.box.px
        box = {"x": x0 + min(bx0, bx1) * sx, "y": y0 + min(by0, by1) * sy,
               "width": abs(bx1 - bx0) * sx, "height": abs(by1 - by0) * sy}
    elif inp.box is not None:
        box = inp.box.model_dump()
    return points, box, picture_channels


def _view(points, box, extra_bounds, image_size):
    xs = [p["x"] for p in points]
    ys = [p["y"] for p in points]
    if box:
        xs += [box["x"], box["x"] + box["width"]]
        ys += [box["y"], box["y"] + box["height"]]
    if extra_bounds:
        xs += [extra_bounds[0], extra_bounds[2]]
        ys += [extra_bounds[1], extra_bounds[3]]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    side = max(x1 - x0, y1 - y0)
    pad = max(VIEW_PAD * side, (MIN_VIEW_PX - side) / 2, 16.0)
    width, height = image_size
    vx0, vy0 = max(0.0, x0 - pad), max(0.0, y0 - pad)
    vx1, vy1 = min(float(width), x1 + pad), min(float(height), y1 + pad)
    return {"x": vx0, "y": vy0, "width": vx1 - vx0, "height": vy1 - vy0}


def segment_roi(call, inp):
    from shapely.geometry import shape

    from plexora.plugins.qc.capabilities import (_open_session_on, _results,
                                                 apply_strictness)
    from plexora.plugins.qc.server import polygons, roi_link, schemas, strictness
    from plexora.plugins.roi.server import geometry as roi_geometry
    from plexora.server.utils import mask_polygon
    from plexora.vision import sam, sam_weights
    from plexora.vision import segment as segmenter

    if not inp.points and inp.box is None and not (inp.roi_id and inp.from_roi):
        raise AgentError("invalid_input", "give at least one point or a box (or a roi_id "
                         "to start from)")
    if inp.mode != "new" and not inp.roi_id:
        raise AgentError("invalid_input", f"mode {inp.mode} reshapes a region: give roi_id")
    if inp.mode == "new" and not inp.artifact_class:
        raise AgentError("invalid_input", "mode new needs artifact_class: what the object is",
                         detail={"classes": list(schemas.AGENT_CLASSES)})
    if inp.artifact_class and inp.artifact_class not in schemas.ARTIFACT_CLASSES:
        raise AgentError("invalid_input", f"{inp.artifact_class!r} is not an artifact class",
                         detail={"classes": list(schemas.AGENT_CLASSES)})
    if not sam.available():
        raise AgentError("capability_unavailable", "magic select is not set up on this "
                         "server (it sets itself up the first time it is used in the viewer, "
                         "or: plexora ai segment install)",
                         detail={"segment": sam.status().to_dict()})
    session_id = _open_session_on(inp.project)
    if session_id:
        raise AgentError("conflict", "a QC session is open on this project; finish it before "
                         "outlining regions outside it", detail={"session_id": session_id},
                         retryable=True)
    record = call.session.project(inp.project)
    image_size = (int(record.image.width or 0), int(record.image.height or 0))
    points, box, picture_channels = _resolve_prompts(inp.project, inp)
    if not any(p["label"] == 1 for p in points) and box is None \
            and not (inp.roi_id and inp.from_roi):
        raise AgentError("invalid_input", "at least one include point (label 1) or a box")
    ds = call.session.image_data(inp.project)
    results = _results()

    candidate = None
    live = None
    if inp.roi_id:
        with results.lock(inp.project):
            document = results.load(inp.project)
            roi_link.sync(ds, document)
            result = results.active(document)
        candidate = next((c for c in ((result or {}).get("candidates") or {}).values()
                          if c.get("roi_id") == inp.roi_id), None)
        if candidate is None:
            raise AgentError("invalid_input", f"{inp.roi_id!r} is not a QC region of the "
                             "active result")
        live = next((r for r in roi_link.live_regions(ds, result)
                     if r["roi_id"] == inp.roi_id), None)
        if live is None:
            raise AgentError("invalid_input", f"{inp.roi_id} is gone")
        user = candidate.get("user_state") or {}
        if user.get("locked"):
            raise AgentError("invalid_input", f"{inp.roi_id} is locked: unlock it in the ROI "
                             "panel to reshape it")
        if user.get("edited") and not inp.force:
            raise AgentError("invalid_input", f"{inp.roi_id} is the user's: they reshaped it; "
                             "pass force: true to reshape it anyway")

    channels = [{"name": name} for name in inp.channels] if inp.channels else None
    if channels is None and candidate is not None:
        view = candidate.get("view") or {}
        channels = [c for c in view.get("channels") or [] if c.get("visible", True)] \
            or list(candidate.get("view_channels") or []) or None
    if channels is None:
        channels = picture_channels or None

    options = {"multi": inp.mode != "new"}
    seeded = bool(live and inp.from_roi and not points and box is None)
    extra = shape(live["geometry"]).bounds if live else None
    view = _view(points, box, extra, image_size)
    if seeded:
        trace = _tighten(call, inp.project, ds, live, candidate, result)
        record = trace.to_record()
        summary = {"flags": {"empty": not trace.refined}, "iou": record["params"].get("iou"),
                   "area_um2": trace.area_um2, "timing": record.get("timing"),
                   "prompts": {"from_roi": inp.roi_id}, "view": view, "started_from_roi": True,
                   "traced_with": "magic select" if trace.method == "sam" else trace.method,
                   "guards": record.get("guards"), "kept_fraction": trace.kept_fraction,
                   # Why the model's outline was not taken, when it was not.
                   "model_check": (record.get("params") or {}).get("sam")}
        if not trace.refined:
            return {"written": False, "why": trace.reason, **summary}
        found = {"geometry": trace.geometry, "flags": summary["flags"], "iou": summary["iou"],
                 "channels": [{"name": name, "color": None, "range": None}
                              for name in trace.channels],
                 "provenance": {"prompts": summary["prompts"]}, "trace": record}
    else:
        try:
            if live and inp.from_roi and inp.mode == "subtract":
                # Taking an area away from a region: start from its outline.
                options["mask_geometry"] = live["geometry"]
            found = segmenter.segment(call.session, inp.project, view=view, points=points,
                                      channels=channels, box=box, options=options)
        except segmenter.SegmentError as exc:
            raise AgentError("invalid_input", str(exc), detail=exc.detail) from None
        summary = {"flags": found["flags"], "iou": found["iou"], "area_um2": found["area_um2"],
                   "timing": found["timing"], "prompts": found["provenance"]["prompts"],
                   "view": view, "started_from_roi": False}
    geometry = found["geometry"]
    if geometry is None:
        why = ("nothing was found there" if found["flags"].get("empty")
               else "the outline covered nearly the whole view (zoom the box in)")
        return {"written": False, "why": why, **summary}

    if live is not None and inp.mode in ("union", "subtract"):
        current = shape(live["geometry"]).buffer(0)
        drawn = shape(geometry).buffer(0)
        combined = current.union(drawn) if inp.mode == "union" else current.difference(drawn)
        combined = mask_polygon.polygonal(combined)
        if combined is None:
            raise AgentError("invalid_input", "subtracting that leaves nothing of the region")
        geometry = mask_polygon.to_geojson(combined, simplify_px=0)
    try:
        geometry = roi_geometry.validate_geometry(geometry)
    except ValueError as exc:
        raise AgentError("invalid_input", f"the outline is not a valid region: {exc}") from None
    summary["area_px2"] = round(polygons.area_of(geometry), 1)

    if inp.preview:
        return _preview(call, inp, geometry, box, view, found, summary)

    klass = inp.artifact_class or (live or {}).get("class") or candidate.get("class")
    traced = found.get("trace")
    refinement = traced if traced else {"status": "refined", "method": "sam", "kept_fraction": 1.0,
                  "reason": "outlined by the agent with magic select",
                  "params": {"model": sam_weights.MODEL_NAME,
                             "model_version": sam_weights.MODEL_VERSION,
                             "prompts": found["provenance"]["prompts"], "iou": found["iou"]}}
    agent_method = "sam_agent" if not traced or traced.get("method") == "sam" else "traced"
    view_record = {"channels": [{k: v for k, v in (("name", c["name"]), ("color", c["color"]),
                                                   ("range", c["range"])) if v is not None}
                                for c in found["channels"]],
                   "viewport": {"x": view["x"], "y": view["y"], "width": view["width"],
                                "height": view["height"]}}

    if inp.mode == "new":
        return _write_new(call, inp, ds, klass, geometry, refinement, view_record, summary)

    with results.lock(inp.project):
        document = results.load(inp.project)
        before_rev = results.revision(inp.project, document)
        result = results.active(document)
        candidate = next((c for c in ((result or {}).get("candidates") or {}).values()
                          if c.get("roi_id") == inp.roi_id), None)
        if candidate is None:
            raise AgentError("conflict", f"{inp.roi_id} changed meanwhile; read it again",
                             retryable=True)
        candidate["refinement"] = refinement
        candidate["method"] = agent_method
        if inp.notes:
            candidate["notes"] = [*list(candidate.get("notes") or []), inp.notes]
        changed = roi_link.retrace(ds, inp.roi_id, candidate, geometry)
        candidate["geometry"] = geometry
        user = candidate.get("user_state") or {}
        user.pop("edited", None)
        candidate["user_state"] = user
        candidate["action_by_strictness"] = strictness.actions_by_preset(candidate)
        results.put_result(document, result)
        results.save(inp.project, document)
        preset = dict(document.get("strictness") or {})
    roi_link.tell_roi_panel(call, inp.project, "update")
    _b, after, _renamed, _skip, _prev = apply_strictness(
        call, inp.project, preset.get("preset") or "standard", preset.get("thresholds"))
    hint, partial = None, False
    if changed is not None:
        undo, partial = roi_link.undo_arguments(inp.project, changed["before"],
                                                changed["revision_after"], reshaped=True)
        hint = {"tool": "update_roi", "arguments": undo, **({"partial": True} if partial else {})}
    receipt = make_receipt(
        call, changed=changed is not None,
        before={"roi_id": inp.roi_id, "geometry_hash": polygons.geometry_hash(
            (changed or {}).get("before", {}).get("geometry"))},
        after={"roi_id": inp.roi_id, "mode": inp.mode, "method": agent_method},
        revision_before=before_rev, revision_after=after, persistent_state="plugin_store:roi",
        reversible=changed is not None and not partial, undo_hint=hint)
    return {"written": True, "roi_id": inp.roi_id, "mode": inp.mode,
            "receipt": receipt.model_dump(mode="json"), **summary}


def _tighten(call, project, ds, live, candidate, result):
    """Redraw a region snug to the object inside it, from the region alone.

    A loose region is no prompt the model can use on its own -- a box three
    times the object comes back as the box, and its own score does not say
    so (measured on the synthetic dark patch and fold). What does say where
    the artifact is inside the region is the evidence QC's tracer reads. So
    this is the tracer with the region as its envelope: the classical trace,
    and the model's outline (peak point and box) competing under its guards
    (`refine_sam`). Needs the image scanned."""
    from plexora.plugins.qc.capabilities import _sam_any_class, _scan_of
    from plexora.plugins.qc.server import polygons
    from plexora.server.utils import pixel_scale

    scan = _scan_of(call, project, result)
    if scan is None:
        raise AgentError("precondition_missing", "tightening a region from itself reads the "
                         "image scan: run profile_image_qc first, or give a point inside the "
                         "object", detail={"project": project})
    pixel = pixel_scale.pixel_size(call.session.project(project))
    envelope = live["geometry"]
    mask = polygons.geometry_to_grid(envelope, scan.grid, touch=True)
    return _sam_any_class({**(candidate or {}), "class": live["class"]}, mask, scan, ds,
                          pixel_um=float(pixel["value"]) if pixel else None,
                          envelope=envelope, options=None)


def _write_new(call, inp, ds, klass, geometry, refinement, view_record, summary):
    import hashlib

    from plexora.plugins.qc.capabilities import _results, apply_strictness
    from plexora.plugins.qc.server import roi_link, strictness
    from plexora.plugins.roi.server.repository import ConflictError
    from plexora.vision import sam_weights

    results = _results()
    key = hashlib.sha1(repr((klass, geometry)).encode()).hexdigest()[:10]
    record = {"id": f"cand_{key}", "detector": "sam", "detector_version": sam_weights.MODEL_VERSION,
              "class": klass, "class_alternatives": [], "scope": "all_channels",
              "channels": [], "cycles": [], "geometry": geometry, "envelope_geometry": geometry,
              "refinement": refinement, "method": "sam_agent", "severity": "moderate",
              "score": None, "metrics": {}, "measurement": {},
              "ai_decision": {"verdict": "artifact", "artifact_class": klass,
                              "severity": "moderate", "confidence": "fairly_sure",
                              "boundary": "covers", "source": "segment_qc_roi"},
              "evidence_artifacts": [], "notes": [inp.notes] if inp.notes else [],
              "origin": "agent", "view": view_record,
              "view_channels": view_record["channels"], "state": "confirmed",
              "created_by": "agent", "user_state": {}, "created_at": results.now_iso()}
    with results.lock(inp.project):
        document = results.load(inp.project)
        roi_link.sync(ds, document)
        result = results.ensure_active(document, inp.project)
        preset = dict(document.get("strictness") or {})
        thresholds = strictness.thresholds(preset.get("preset") or "standard")
        action = strictness.action_for(record, thresholds)
        record["action"] = action
        try:
            before, after, feature = roi_link.create(ds, record, action=action)
        except ConflictError:
            before, after, feature = roi_link.create(ds, record, action=action)
        record["roi_id"] = feature["id"]
        record["action_by_strictness"] = strictness.actions_by_preset(record)
        result.setdefault("candidates", {})[record["id"]] = record
        results.upsert_roi_meta(inp.project, [roi_link.meta_row(
            record, feature, result=result, session_id=None, action=action,
            strictness=preset.get("preset"), agent=None, operation_id=call.operation_id,
            created_by="agent")])
        results.put_result(document, result)
        results.save(inp.project, document)
    roi_link.tell_roi_panel(call, inp.project, "create")
    apply_strictness(call, inp.project, preset.get("preset") or "standard",
                     preset.get("thresholds"))
    receipt = make_receipt(
        call, changed=True, before=None,
        after={"roi_id": feature["id"], "name": feature["name"], "class": klass,
               "method": "sam_agent"},
        revision_before=before, revision_after=after, persistent_state="plugin_store:roi",
        undo_hint={"tool": "delete_roi", "arguments": {
            "project": inp.project, "roi_id": feature["id"], "confirm": True}})
    return {"written": True, "roi_id": feature["id"], "name": feature["name"],
            "class": klass, "action": action, "receipt": receipt.model_dump(mode="json"),
            **summary}


def _preview(call, inp, geometry, box, view, found, summary):
    from plexora.agent import render
    from plexora.agent.core.visual import with_image
    from plexora.agent.render_spec import Bounds, ChannelSpec, OutputSpec, RenderInput, ShapeSpec

    shapes = [ShapeSpec(id="proposed", geometry=geometry, color="#22d3ee", width=2,
                        fill_alpha=0.15, label="proposed")]
    if box:
        shapes.append(ShapeSpec(id="box", bounds=Bounds(**box), color="#fbbf24", width=1,
                                dash=True))
    colours = ("#ffffff", "#00ff00", "#ff00ff", "#00ffff")
    channels = [ChannelSpec(name=c["name"], color=c.get("color") or colours[i % 4],
                            window=list(c["range"]) if c.get("range") else "auto")
                for i, c in enumerate(found["channels"][:4])] or None
    spec = RenderInput(project=inp.project, bounds=Bounds(**view), channels=channels,
                       segmentation="none", shapes=shapes, output=OutputSpec(width=512))
    rendered = render.render_region(call.session, spec)
    rendered["manifest"]["kind_detail"] = "plexora.qc_segment_preview"
    return with_image({"written": False, "preview": True, "geometry": geometry,
                       "artifact": rendered["artifact"], **summary}, rendered["png"])


def capability(paid):
    """The registry entry (Paid, like `refine_qc_roi`)."""
    return paid(
        name="qc.segment_roi", tool_name="segment_qc_roi",
        purpose="Outline an artifact with magic select: point inside it (and, if needed, on "
                "tissue it should not take, or a box round it) and get a snug QC region -- "
                "new, or replacing, growing or carving an existing one. Given a roi_id, the "
                "region itself is the starting point (its outline and box), so a loose "
                "detector region is tightened with mode replace and no points. Point on a picture "
                "you were shown ({artifact_id, px}) or in image pixels. preview: true writes "
                "nothing and returns the proposed outline drawn on the picture. Refused "
                "while a QC session is open on the project; locked regions are never "
                "reshaped. Each write is receipted and undoable.",
        permission="reversible_write", input_model=SegmentRoiInput, handler=segment_roi,
        visual_output=True, egress="rendered_pixels", writes=("qc", "rois"),
        persistent=True, reads=("image", "qc", "rois"))
