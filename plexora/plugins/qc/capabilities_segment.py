"""`segment_qc_roi`: the agent outlines an artifact with magic select.

An agent that looked at a picture and saw an obvious object the detectors
missed -- or outlined loosely -- can point at it and get a snug QC region:
one include point inside the object, an exclude point on tissue it wrongly
took, a box for a large or textured one. Points are given either in
full-resolution image pixels or, more usefully, in the pixels of a picture
the agent was shown (`{artifact_id, px}` on a `render_region` picture, the
visual overview, a channel sheet or an earlier preview -- `frames.py` maps
them), so it never has to do coordinate arithmetic.

Modes: `new` writes a region of `artifact_class`; `replace`, `union` and
`subtract` reshape an existing QC region (`roi_id`). `preview: true` writes
nothing and returns the proposed outline drawn twice -- snug on the prompt
view, and in context beside the regions already written -- with the
embedding's `sam` token, so the next round (`continue_from`) starts from
this mask instead of afresh.

A region is held to what the session's own regions are held to. It is cut to
the tissue (holes filled, feathered) and refused when it lies on the glass
(`schemas.VISUAL["min_on_tissue"]`); a region of the same class already over
the same place (`ENGINE["merge_iou"]`, `ENGINE["merge_contain"]`) or an
exclusion it lies inside refuses it as a duplicate (`force_duplicate` writes
anyway, and says so); smaller overlaps are written and recorded. Its action
is strictness's, from the agent's `confidence` and `severity` and the trace's
support; `unsure` lands it in Needs review as a warning, never an exclusion.
`reasoning` and `evidence_artifacts` are kept as its provenance.

Outside a session the region is written to the active result. Inside one,
during its visual pass (`session_id`, while a `visual_scan` packet is
outstanding), it becomes a candidate unit of the session: written as a child
receipt of it, merged with what the session already confirmed, and
consolidated with the rest at the close. An open session owns its regions at
every other moment (refused), a locked region is the user's promise
(refused), a region the user reshaped needs `force`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.receipts import make_receipt
from plexora.agent.schemas import AgentModel, ProjectInput
from plexora.plugins.qc.server import schemas

MAX_POINTS = 32
#: How much of the picture round the prompts is shown to the model, as a
#: multiple of their extent, and the smallest view, in full-resolution pixels.
VIEW_PAD = 0.75
MIN_VIEW_PX = 256
#: A point on a picture means the place within this many of its pixels: the
#: view round it is at least twice this, at the picture's own scale.
POINT_PICTURE_PX = 48
VISUAL = schemas.VISUAL
#: Overlaps reported to the agent, at most.
MAX_OVERLAPS = 8


class SegPoint(AgentModel):
    x: float = Field(description="Full-resolution image pixels.")
    y: float
    label: Literal[0, 1] = Field(1, description="1 inside the artifact, 0 on what is not.")


class ArtifactPoint(AgentModel):
    artifact_id: str = Field(description="A picture you looked at (art_...): a render_region "
                                         "picture, the visual overview, a channel sheet or a "
                                         "preview.")
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
                            description="[x0, y0, x1, y1] in that picture's own pixels, both "
                                        "corners on the same tile.")


class ContinueFrom(AgentModel):
    token: str = Field(description="The `sam.token` a preview returned: its embedding.")
    prev_key: str = Field(description="Its `sam.prev_key`: the mask it decoded, which this "
                                      "round starts from.")


_CONFIDENCE = ("How sure you are that this is an artifact of that class: sure, fairly_sure, "
               "or unsure -- unsure writes it to Needs review as a warning, never an "
               "exclusion. Strictness turns the word into the action.")
_SEVERITY = ("minor: cells there are still readable; moderate: some markers unreliable; "
             "severe: nothing there can be trusted.")


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
    mode: Literal["new", "replace", "union", "subtract"] = Field(
        "new", description="new: write a region of artifact_class; replace: the region "
                           "becomes the outline; union: the outline is added to it; "
                           "subtract: the outline is taken away from it.")
    artifact_class: str | None = Field(None, description="For mode new: what the object is "
                                                         "(an artifact class).")
    channels: list[str] | None = Field(None, max_length=8, description="Channels to show the "
                                       "model (default: the region's drawing view, else the "
                                       "picture's, else the nuclear stain). Two to four that "
                                       "show the artifact are best (inspect_artifact_channels).")
    from_roi: bool = Field(True, description="With roi_id: start from that region -- its "
                                             "outline is the model's starting mask and its "
                                             "box the prompt when you give none -- so a loose "
                                             "region can be tightened with no points at all.")
    force: bool = Field(False, description="Also reshape a region the user reshaped.")
    preview: bool = Field(False, description="Write nothing; return a picture of the "
                                             "proposed outline, snug and in context, with "
                                             "the overlaps and the on-tissue share.")
    notes: str | None = Field(None, max_length=300, description="Your note on the region.")
    confidence: Literal["sure", "fairly_sure", "unsure"] = Field("fairly_sure",
                                                                 description=_CONFIDENCE)
    severity: Literal["minor", "moderate", "severe"] = Field("moderate", description=_SEVERITY)
    reasoning: str | None = Field(None, max_length=500,
                                  description="What you saw and in which channels: kept with "
                                              "the region as its explanation, shown to the "
                                              "user and in the report.")
    evidence_artifacts: list[str] = Field(
        default_factory=list, max_length=8,
        description="The pictures you judged it on (artifact ids: the overview, the channel "
                    "sheet, the preview), kept as the region's evidence.")
    continue_from: ContinueFrom | None = Field(
        None, description="A preview's `sam` {token, prev_key}: this round decodes from that "
                          "mask on the same embedding instead of starting afresh (give every "
                          "point again, plus the new ones).")
    force_duplicate: bool = Field(False, description="Write even where a region of the same "
                                                     "class already covers the place.")
    session_id: str | None = Field(
        None, description="Inside a QC session's visual pass (its visual_scan packet names "
                          "the session): the region joins the session as one of its own "
                          "findings. Mode new only.")


# -- prompts on pictures -----------------------------------------------------------


def _resolve_prompts(project, inp):
    """Points and box in image pixels, the first picture's channels, and the
    least view side the points ask for: a point on a picture stands for what
    lies within `POINT_PICTURE_PX` picture pixels of it, at that picture's
    scale -- on a whole-slide sheet that is millimetres, not a 256 px crop."""
    from plexora.plugins.qc.server import frames

    points, picture_channels, min_side = [], None, 0.0
    for point in inp.points:
        if isinstance(point, ArtifactPoint):
            (x, y), manifest, scale = frames.locate_point_scaled(project, point.artifact_id,
                                                                 point.px)
            points.append({"x": x, "y": y, "label": point.label})
            min_side = max(min_side, POINT_PICTURE_PX * scale)
            if picture_channels is None:
                picture_channels = frames.picture_channels(manifest)
        else:
            points.append({"x": point.x, "y": point.y, "label": point.label})
    box = None
    if isinstance(inp.box, ArtifactBox):
        box, manifest = frames.locate_box(project, inp.box.artifact_id, inp.box.px)
        if picture_channels is None:
            picture_channels = frames.picture_channels(manifest)
    elif inp.box is not None:
        box = inp.box.model_dump()
    return points, box, picture_channels, min_side


def _view(points, box, extra_bounds, image_size, min_side=0.0):
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
    pad = max(VIEW_PAD * side, (max(MIN_VIEW_PX, min_side) - side) / 2, 16.0)
    width, height = image_size
    vx0, vy0 = max(0.0, x0 - pad), max(0.0, y0 - pad)
    vx1, vy1 = min(float(width), x1 + pad), min(float(height), y1 + pad)
    return {"x": vx0, "y": vy0, "width": vx1 - vx0, "height": vy1 - vy0}


def _evidence(project, ids):
    from plexora.plugins.qc.server import frames

    for art in ids or []:
        frames._load(project, art)   # exists, and is this project's
    return list(dict.fromkeys(ids or []))


# -- the session's visual pass ---------------------------------------------------------


def _visual_unit(engine, project):
    """The session's open visual-pass unit, or an error saying why the tool
    may not write into the session now."""
    record = engine.record
    if record.get("state") in schemas.FINISHED_STATES:
        raise AgentError("conflict", f"session {engine.id} is {record.get('state')}",
                         detail={"state": record.get("state")})
    if project not in (record.get("images") or []):
        raise AgentError("invalid_input", f"session {engine.id} is not on {project!r}",
                         detail={"images": record.get("images")})
    if record.get("outstanding_kind") != "visual_scan":
        raise AgentError("conflict", "the session is not in its visual pass: no visual_scan "
                         "packet is outstanding, so its regions are its own; answer the "
                         "packet in hand, or outline without session_id once it is finished",
                         detail={"outstanding_kind": record.get("outstanding_kind")},
                         retryable=True)
    unit = next((u for u in engine.units_of("check", project)
                 if u.get("check") == "visual" and u["state"] == "awaiting_visual_scan"), None)
    if unit is None:
        raise AgentError("conflict", "the session's visual pass is not open",
                         retryable=True)
    if len(unit.get("written") or []) >= int(VISUAL["max_regions"]):
        raise AgentError("invalid_input", f"this pass has written its {VISUAL['max_regions']} "
                         "regions; answer the packet (done) and say what was left",
                         detail={"written": list(unit.get("written") or [])})
    return unit


def _session_gate(call, inp):
    """(result_id) of the session the write joins, after the checks."""
    from plexora.plugins.qc.server import engine as engines

    if inp.mode != "new":
        raise AgentError("invalid_input", "inside a session only mode new is taken: the "
                         "session's own regions are reshaped by its packets")
    with engines.engine_for(call, inp.session_id, save=False) as engine:
        _visual_unit(engine, inp.project)
        return engine.record.get("result_id")


# -- guards ------------------------------------------------------------------------------


def _tissue_guard(call, project, geometry):
    """(geometry cut to the tissue, measurement) -- or (None, measurement)
    when too little of it lies on the tissue."""
    from plexora.plugins.qc.server import tissue

    found = tissue.for_project(call.session, project)
    share = tissue.share_on_tissue(found, geometry)
    measurement = {"on_tissue_fraction": round(share["on_tissue_fraction"], 4),
                   "tissue_fraction": share["tissue_fraction"]}
    if share["on_tissue_fraction"] < float(VISUAL["min_on_tissue"]):
        return None, measurement
    clipped = tissue.clip_to_tissue(found, geometry)
    if clipped is None:
        return None, measurement
    if clipped is not geometry:
        after = tissue.share_on_tissue(found, clipped)
        measurement["tissue_fraction"] = after["tissue_fraction"]
        measurement["clipped_to_tissue"] = True
    if measurement.get("tissue_fraction") is not None:
        measurement["tissue_fraction"] = round(float(measurement["tissue_fraction"]), 6)
        measurement["refined_fraction"] = measurement["tissue_fraction"]
    return clipped, measurement


def _overlaps(ds, result, geometry, klass, *, exclude_roi=None):
    """(rows, duplicate, explained): the live regions this outline shares a
    place with, strongest first; the one that makes it a duplicate (same
    class, the session's merge rule); the exclusion it lies inside."""
    from plexora.plugins.qc.server import consolidate, polygons, roi_link

    rows = []
    for region in roi_link.live_regions(ds, result):
        if exclude_roi and region["roi_id"] == exclude_roi:
            continue
        o = polygons.overlap(geometry, region["geometry"])
        # Either way round: a small region the outline swallows overlaps it
        # as much as an outline lying inside a large one.
        if max(o["iou"], o["share_a_in_b"], o["share_b_in_a"]) < consolidate.MIN_OVERLAP_SHARE:
            continue
        rows.append({"roi_id": region["roi_id"], "candidate_id": region.get("candidate_id"),
                     "class": region["class"],
                     "action": region.get("action"), "iou": round(o["iou"], 3),
                     "inside_share": round(o["share_a_in_b"], 3),
                     "covers_share": round(o["share_b_in_a"], 3)})
    rows.sort(key=lambda r: -max(r["iou"], r["inside_share"], r["covers_share"]))
    engine = schemas.ENGINE
    duplicate = next((r for r in rows if r["class"] == klass
                      and (r["iou"] >= engine["merge_iou"]
                           or r["inside_share"] >= engine["merge_contain"])), None)
    explained = next((r for r in rows if r["action"] == "exclude"
                      and r["inside_share"] >= engine["merge_contain"]), None)
    return rows[:MAX_OVERLAPS], duplicate, explained


def _result_for(results, project, result_id=None):
    document = results.load(project)
    if result_id:
        return results.get_result(project, document, result_id)
    return results.active(document)


# -- the tool --------------------------------------------------------------------------


def segment_roi(call, inp):
    from shapely.geometry import shape

    from plexora.plugins.qc.capabilities import (_open_session_on, _results,
                                                 apply_strictness)
    from plexora.plugins.qc.server import polygons, roi_link, strictness
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
    if inp.artifact_class and inp.artifact_class not in schemas.AGENT_CLASSES:
        raise AgentError("invalid_input", f"{inp.artifact_class!r} is not an artifact class",
                         detail={"classes": list(schemas.AGENT_CLASSES)})
    if not sam.available():
        raise AgentError("capability_unavailable", "magic select is not set up on this "
                         "server (it sets itself up the first time it is used in the viewer, "
                         "or: plexora ai segment install)",
                         detail={"segment": sam.status().to_dict()})
    evidence = _evidence(inp.project, inp.evidence_artifacts)
    result_id = None
    if inp.session_id:
        result_id = _session_gate(call, inp)
    else:
        session_id = _open_session_on(inp.project)
        if session_id:
            raise AgentError("conflict", "a QC session is open on this project; finish it "
                             "before outlining regions outside it -- or, during its visual "
                             "pass, pass its session_id", detail={"session_id": session_id},
                             retryable=True)
    record = call.session.project(inp.project)
    image_size = (int(record.image.width or 0), int(record.image.height or 0))
    points, box, picture_channels, min_side = _resolve_prompts(inp.project, inp)
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
    token = None
    if inp.continue_from is not None:
        token = inp.continue_from.token
        options.update(use_prev_mask=True, prev_key=inp.continue_from.prev_key)
    seeded = bool(live and inp.from_roi and not points and box is None)
    extra = shape(live["geometry"]).bounds if live else None
    view = _view(points, box, extra, image_size, min_side)
    if seeded:
        trace = _tighten(call, inp.project, ds, live, candidate, result)
        record_ = trace.to_record()
        summary = {"flags": {"empty": not trace.refined}, "iou": record_["params"].get("iou"),
                   "area_um2": trace.area_um2, "timing": record_.get("timing"),
                   "prompts": {"from_roi": inp.roi_id}, "view": view, "started_from_roi": True,
                   "traced_with": "magic select" if trace.method == "sam" else trace.method,
                   "guards": record_.get("guards"), "kept_fraction": trace.kept_fraction,
                   # Why the model's outline was not taken, when it was not.
                   "model_check": (record_.get("params") or {}).get("sam")}
        if not trace.refined:
            return {"written": False, "why": trace.reason, **summary}
        found = {"geometry": trace.geometry, "flags": summary["flags"], "iou": summary["iou"],
                 "channels": [{"name": name, "color": None, "range": None}
                              for name in trace.channels],
                 "provenance": {"prompts": summary["prompts"]}, "trace": record_}
    else:
        try:
            if live and inp.from_roi and inp.mode == "subtract":
                # Taking an area away from a region: start from its outline.
                options["mask_geometry"] = live["geometry"]
            found = segmenter.segment(call.session, inp.project, view=view, points=points,
                                      channels=channels, box=box, token=token,
                                      options=options)
        except segmenter.SegmentError as exc:
            raise AgentError("invalid_input", str(exc), detail=exc.detail) from None
        summary = {"flags": found["flags"], "iou": found["iou"], "area_um2": found["area_um2"],
                   "timing": found["timing"], "prompts": found["provenance"]["prompts"],
                   "view": view, "started_from_roi": False,
                   "sam": {"token": found.get("token"), "prev_key": found.get("prev_key")}}
        if inp.continue_from is not None:
            # Afresh when the embedding changed (a prompt left its crop) or
            # the named mask is gone (an older preview, evicted).
            summary["restarted"] = (found.get("token") != inp.continue_from.token
                                    or not found.get("used_prev_mask"))
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

    measurement = {}
    if inp.mode == "new":
        # The tissue: a new outline on the glass is refused, one over the
        # tissue's edge is cut. A region reshaped keeps its place -- that was
        # the user's or the detector's to choose.
        clipped, measurement = _tissue_guard(call, inp.project, geometry)
        summary["on_tissue_fraction"] = measurement["on_tissue_fraction"]
        if clipped is None:
            return {"written": False, "why": "that lies on the glass, not the tissue: only "
                                             "artifacts on the tissue are regions",
                    "measurement": measurement, **summary}
        geometry = clipped
    summary["area_px2"] = round(polygons.area_of(geometry), 1)

    klass = inp.artifact_class or (live or {}).get("class") or candidate.get("class")
    # The regions already there: a duplicate is refused, an overlap recorded.
    result_now = _result_for(results, inp.project, result_id)
    overlaps, duplicate, explained = _overlaps(ds, result_now, geometry, klass,
                                               exclude_roi=inp.roi_id)
    summary["overlaps"] = overlaps
    if duplicate is not None:
        summary["duplicate_of"] = duplicate
    elif explained is not None:
        summary["explained_by"] = explained

    if inp.preview:
        return _preview(call, inp, geometry, points, box, view, found, summary, result_now)

    if inp.mode == "new" and not inp.force_duplicate:
        if duplicate is not None:
            return {"written": False,
                    "why": (f"{duplicate['roi_id']} already covers this place as the same "
                            "class; reshape it with roi_id and mode replace or union if your "
                            "outline is snugger, or pass force_duplicate"),
                    **summary}
        if explained is not None:
            return {"written": False,
                    "why": (f"this lies inside {explained['roi_id']}, which already excludes "
                            "its cells; nothing more to write, or pass force_duplicate"),
                    **summary}

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
    notes = [n for n in (inp.reasoning, inp.notes) if n]

    if inp.mode == "new":
        decision = {"verdict": "artifact", "artifact_class": klass, "severity": inp.severity,
                    "confidence": inp.confidence, "boundary": "covers", "scope": "all_channels",
                    "exclude_recommended": True, "manual_review": inp.confidence == "unsure",
                    "source": "visual_scan" if inp.session_id else "segment_qc_roi",
                    "reasoning": inp.reasoning}
        extra_record = {"overlaps": overlaps, "forced_duplicate": bool(
            inp.force_duplicate and (duplicate or explained))}
        if inp.session_id:
            return _write_session(call, inp, klass, geometry, refinement, view_record, summary,
                                  decision, measurement, evidence, notes, extra_record)
        return _write_new(call, inp, ds, klass, geometry, refinement, view_record, summary,
                          decision, measurement, evidence, notes, extra_record)

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
        if notes:
            candidate["notes"] = [*list(candidate.get("notes") or []), *notes]
        if evidence:
            candidate["evidence_artifacts"] = list(dict.fromkeys(
                [*list(candidate.get("evidence_artifacts") or []), *evidence]))
        candidate.setdefault("measurement", {}).update(
            {k: v for k, v in measurement.items() if v is not None})
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
    _propagate_cells(call, inp.project)
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


def _propagate_cells(call, project):
    """The cells' calls, with the regions propagated again: a region just
    written or reshaped has no stored membership yet, which a derivation
    from the stored pairs alone would miss."""
    if not call.session.project(project).has_table:
        return
    from plexora.plugins.qc.server.cells import calls

    calls.write_for_active(call, project)


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


def _candidate_id(klass, geometry):
    import hashlib

    return "cand_" + hashlib.sha1(repr((klass, geometry)).encode()).hexdigest()[:10]


def _write_new(call, inp, ds, klass, geometry, refinement, view_record, summary, decision,
               measurement, evidence, notes, extra):
    """Outside a session: the region joins the active result (a new manual
    one when there is none), its action strictness's."""
    from plexora.plugins.qc.capabilities import _results
    from plexora.plugins.qc.server import consolidate, roi_link, strictness
    from plexora.plugins.roi.server.repository import ConflictError
    from plexora.vision import sam_weights

    results = _results()
    written_class = klass
    alternatives = []
    if inp.confidence == "unsure":
        # Not sure enough to decide: Needs review, a warning under every preset
        # (the class a flag alone would let Strict's floor exclude on).
        written_class = "uncertain_manual_review"
        alternatives = [klass]
        decision = {**decision, "artifact_class": written_class, "manual_review": True}
    record = {"id": _candidate_id(written_class, geometry), "detector": "sam",
              "detector_version": sam_weights.MODEL_VERSION,
              "class": written_class, "class_alternatives": alternatives,
              "scope": "all_channels", "channels": [], "cycles": [], "geometry": geometry,
              "envelope_geometry": geometry, "refinement": refinement, "method": "sam_agent",
              "severity": inp.severity, "score": None, "metrics": {},
              "measurement": dict(measurement), "ai_decision": decision,
              "evidence_artifacts": evidence, "notes": notes, "origin": "agent",
              "view": view_record, "view_channels": view_record["channels"],
              "created_by": "agent", "user_state": {}, "created_at": results.now_iso(), **extra}
    record["measurement"]["support"] = strictness.measured_support(record)
    with results.lock(inp.project):
        document = results.load(inp.project)
        roi_link.sync(ds, document)
        result = results.ensure_active(document, inp.project)
        held = result.setdefault("candidates", {})
        base, n = record["id"], 2
        while record["id"] in held:
            record["id"] = f"{base}_{n}"
            n += 1
        preset = dict(document.get("strictness") or {})
        thresholds = strictness.thresholds(preset.get("preset") or "standard")
        if written_class == "uncertain_manual_review":
            record["state"] = "manual_review_recommended"
            action = strictness.manual_review_action(record["measurement"])["action"]
        else:
            action = strictness.action_for(record, thresholds)
            record["state"] = consolidate.ACTION_STATE.get(action, "confirmed_noted")
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
    _propagate_cells(call, inp.project)
    receipt = make_receipt(
        call, changed=True, before=None,
        after={"roi_id": feature["id"], "name": feature["name"], "class": written_class,
               "method": "sam_agent", "action": action},
        revision_before=before, revision_after=after, persistent_state="plugin_store:roi",
        undo_hint={"tool": "delete_roi", "arguments": {
            "project": inp.project, "roi_id": feature["id"], "confirm": True}})
    return {"written": True, "roi_id": feature["id"], "name": feature["name"],
            "class": written_class, "action": action, "candidate_id": record["id"],
            "measurement": record["measurement"], "receipt": receipt.model_dump(mode="json"),
            **summary}


def _write_session(call, inp, klass, geometry, refinement, view_record, summary, decision,
                   measurement, evidence, notes, extra):
    """Inside the visual pass: the region is a candidate unit of the session,
    merged with what it already confirmed, decided under its strictness and
    written through its own writer (a child receipt; proposed, in propose
    mode)."""
    from plexora.plugins.qc.server import candidates as cand
    from plexora.plugins.qc.server import consolidate, polygons, strictness
    from plexora.plugins.qc.server import engine as engines
    from plexora.vision import sam_weights

    project = inp.project
    with engines.engine_for(call, inp.session_id) as engine:
        visual = _visual_unit(engine, project)
        scan = engine.scan(project)
        mask = cand.encode_mask(polygons.geometry_to_grid(geometry, scan.grid, touch=True))
        bbox = None
        try:
            from shapely.geometry import shape

            bbox = [round(v, 1) for v in shape(geometry).bounds]
        except Exception:  # noqa: BLE001
            pass
        n = len(visual.get("written") or []) + 1
        unit = {"type": "candidate", "project": project,
                "id": _candidate_id(klass, geometry), "origin": "agent", "trace": "object",
                "detector": "sam", "detector_version": sam_weights.MODEL_VERSION,
                "class_hint": klass, "class": klass, "alternatives": [],
                "scope_hint": "all_channels", "channels": [], "cycles": [],
                "audit_channel": None, "channel": None, "score": None,
                "severity": inp.severity, "label": f"v{n}", "mask": mask, "bbox": bbox,
                "geometry": geometry, "envelope_geometry": geometry,
                "refinement": refinement, "decision": decision,
                "measurement": dict(measurement), "artifacts": list(evidence),
                "notes": list(notes), "method": "sam_agent", "view": view_record,
                "state": "awaiting_confirm", "level": 0, "visual_unit": visual["id"], **extra}
        key = engine.unit_key_of(engine.unit_ref(unit))
        if key in engine.record["units"]:
            held = engine.record["units"][key]
            return {"written": False, "why": "this outline of this class is already one of "
                                             "the session's regions",
                    "duplicate_of": {"candidate_id": held["id"], "roi_id": held.get("roi_id")},
                    "session_id": inp.session_id, **summary}
        engine.record["units"][key] = unit
        merged = engine._merge_target(unit)
        if merged is not None:
            merged.setdefault("merged", []).append(unit["id"])
            engine.close(unit, "merged", f"the same region as {merged['id']}, already "
                                         "confirmed by the session")
            return {"written": False,
                    "why": f"{merged['id']} already covers this place as the same class in "
                           "this session",
                    "duplicate_of": {"candidate_id": merged["id"],
                                     "roi_id": merged.get("roi_id")},
                    "session_id": inp.session_id, **summary}
        if inp.confidence == "unsure":
            engine.manual_review(unit, "the agent outlined it on the overview but was not "
                                       "sure enough to decide")
            op = (unit.get("receipts") or [None])[-1]
        else:
            unit["measurement"]["support"] = strictness.measured_support(unit)
            action = strictness.decide_artifact(decision, unit["measurement"],
                                                engine.table())["action"]
            unit["action"] = action
            engine.close(unit, consolidate.ACTION_STATE.get(action, "confirmed_noted"),
                         f"outlined by the agent on the overview: {schemas.CLASS_WORDS.get(klass, klass)}; "
                         f"{action}")
            op = engine.write_candidate(unit, klass=klass, action=action)
        visual.setdefault("written", []).append(unit["id"])
        out = {"written": bool(unit.get("roi_id")), "proposed": bool(unit.get("proposed")),
               "roi_id": unit.get("roi_id"), "candidate_id": unit["id"],
               "class": unit.get("class"), "action": unit.get("action"),
               "state": unit["state"], "session_id": inp.session_id,
               "measurement": unit["measurement"], "label": unit["label"],
               "written_so_far": len(visual["written"]),
               "max_regions": int(VISUAL["max_regions"])}
    receipt = make_receipt(
        call, changed=bool(op), before=None,
        after={"roi_id": out["roi_id"], "candidate_id": unit["id"], "class": out["class"],
               "action": out["action"], "session_id": inp.session_id},
        persistent_state="plugin_store:roi", reversible=bool(op),
        undo_hint={"note": "the session wrote it as a child receipt: undo_operation on that "
                           "deletes the region, and the session's rollback undoes them all",
                   "children": [op] if op else []},
        extra={"qc_session": inp.session_id, "children": [op] if op else []})
    return {**out, "receipt": receipt.model_dump(mode="json"), **summary}


def _preview(call, inp, geometry, points, box, view, found, summary, result):
    from shapely.geometry import shape

    from plexora.agent.core.visual import with_image
    from plexora.plugins.qc.server import overview

    record = call.session.project(inp.project)
    image_size = (int(record.image.width or 0), int(record.image.height or 0))
    rendered = overview.preview_sheet(call.session, inp.project, geometry=geometry,
                                      points=points, box=box, view=view,
                                      channels=found["channels"], image_size=image_size,
                                      result=result)
    manifest = rendered["manifest"]
    # The picture is the outline: its vertices stay on the server (the write
    # recomputes them), only its size and place come back.
    drawn = shape(geometry)
    parts = getattr(drawn, "geoms", [drawn])
    outline = {"parts": len(parts),
               "vertices": sum(len(p.exterior.coords) for p in parts),
               "bounds": [round(b, 1) for b in drawn.bounds]}
    return with_image({"written": False, "preview": True, "outline": outline,
                       "artifact": rendered["artifact"], "frames": manifest["frames"],
                       "context": manifest["context"],
                       "next": ("wrong? add an exclude point on tissue it took, an include "
                                "point on what it missed, or a box, give continue_from its "
                                "sam token, and preview again; right? write it with "
                                "artifact_class, confidence, severity and reasoning"),
                       **summary}, rendered["image"])


def capability(paid):
    """The registry entry (Paid, like `refine_qc_roi`)."""
    return paid(
        name="qc.segment_roi", tool_name="segment_qc_roi",
        purpose="Outline an artifact with magic select: point inside it (and, if needed, on "
                "tissue it should not take, or a box round it) and get a snug QC region -- "
                "new, or replacing, growing or carving an existing one. Given a roi_id, the "
                "region itself is the starting point (its outline and box), so a loose "
                "detector region is tightened with mode replace and no points. Point on a picture "
                "you were shown ({artifact_id, px}: a render, the visual overview, a channel "
                "sheet, a preview) or in image pixels. preview: true writes nothing and "
                "returns the proposed outline snug and in context, with the overlaps and "
                "the on-tissue share; continue_from retries from that mask. Cut to the "
                "tissue; a duplicate of a region already there is refused. Writes the "
                "agent's class, confidence, severity and reasoning as provenance. Refused "
                "while a QC session is open on the project, except into its visual pass "
                "(session_id); locked regions are never reshaped. Each write is receipted "
                "and undoable.",
        permission="reversible_write", input_model=SegmentRoiInput, handler=segment_roi,
        visual_output=True, egress="rendered_pixels", writes=("qc", "rois"),
        persistent=True, reads=("image", "qc", "rois"))
