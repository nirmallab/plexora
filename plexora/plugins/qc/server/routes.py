"""The QC panel's routes, mounted under /plugins/qc/.

Every route is the user's own act in the viewer, so none is licence-gated:
reading results, changing the strictness, taking in edits made in the ROI
panel, preparing a QC category to draw in (an artifact class, or one the
user names), approving a region, downloading files, and the running
session's pause / stop / take-over control. One route reaches a Paid tool:
`/regions/refine` calls `refine_qc_roi`, whose licence check still applies. They go
through the same capabilities an agent calls (`registry.invoke`), so the
receipts, the revision checks and the audit are the same whoever acts.
"""

from __future__ import annotations

import json
import math

from flask import Blueprint, abort, request, send_file

from plexora import api

qc_bp = Blueprint("qc", __name__, template_folder="../templates",
                  static_folder="../static", static_url_path="/static")

_SESSION = None


def _session():
    global _SESSION
    from plexora.agent import AgentSession, registry

    try:
        registry.get("qc.get_results")
    except Exception:
        registry.discover(["roi", "qc"])
    if _SESSION is None:
        _SESSION = AgentSession()
    return _SESSION


def _invoke(tool, arguments, *, allow_source_writes=False):
    from plexora.agent import registry
    from plexora.agent.policy import Policy

    import dataclasses

    policy = dataclasses.replace(Policy.from_flags(allow_source_writes=allow_source_writes),
                                 principal="viewer")

    def notify(project, plugin, kind, body):
        return api.notify_viewers(project, plugin, kind, body)

    answer = registry.invoke(_session(), tool, arguments, policy=policy, notify=notify)
    if not answer["ok"]:
        return api.json_response({"ok": False, "error": answer["error"]}), \
            409 if answer["error"]["code"] == "conflict" else 400
    return api.json_response({"ok": True, **(answer["result"] or {})})


def _body():
    try:
        return json.loads(request.data or b"{}")
    except ValueError:
        abort(400)


@qc_bp.route("/state", methods=["GET"])
def state():
    datasource = request.args.get("datasource")
    if not datasource:
        abort(400)
    return _invoke("get_qc_results", {"project": datasource, "include_regions": True})


def _project_arg():
    """`datasource` like every other QC route, or `project` like the tools."""
    project = request.args.get("datasource") or request.args.get("project")
    if not project:
        abort(400)
    return project


def _read(fn):
    """A read-only answer for the panel's drawing: 404 for a project that is
    not there, the error's words for anything else, never a traceback."""
    from plexora.agent.errors import AgentError

    try:
        return api.json_response({"ok": True, **fn()})
    except AgentError as exc:
        return api.json_response({"ok": False, "error": exc.to_problem()}), \
            404 if exc.code == "not_found" else 400


@qc_bp.route("/regions", methods=["GET"])
def regions():
    """Every QC region that counts now, with its outline and bounding box, for
    the panel to draw on the tissue and to fit the viewer to."""
    from plexora.plugins.qc.server import viewer_data

    project = _project_arg()
    return _read(lambda: viewer_data.regions(_session().image_data(project), project,
                                             request.args.get("result_id")))


@qc_bp.route("/cells", methods=["GET"])
def cells():
    """The flagged cells grouped by reason and status (fail / warn), with each
    group's ids and the bounding box of their centroids -- what the QC cell
    layer colours and what a click on a reason row frames."""
    from plexora.plugins.qc.server import viewer_data

    project = _project_arg()
    return _read(lambda: viewer_data.cells(_session().data(project), project,
                                           request.args.get("result_id")))


@qc_bp.route("/regions/delete", methods=["POST"])
def delete_region():
    """Delete a QC region -- the ROI itself, the user's explicit act from the
    panel's menu -- and take the deletion in, so the cells stop counting it."""
    body = _body()
    project = body.get("datasource")
    roi_id = body.get("roi_id")
    if not project or not roi_id:
        abort(400)
    from plexora.agent import registry
    from plexora.agent.policy import Policy

    import dataclasses

    policy = dataclasses.replace(Policy.from_flags(allow_destructive=True), principal="viewer")

    def notify(target, plugin, kind, payload):
        return api.notify_viewers(target, plugin, kind, payload)

    answer = registry.invoke(_session(), "delete_roi",
                             {"project": project, "roi_id": roi_id, "confirm": True},
                             policy=policy, notify=notify)
    if not answer["ok"]:
        return api.json_response({"ok": False, "error": answer["error"]}), \
            409 if answer["error"]["code"] == "conflict" else 400
    api.notify_viewers(project, "roi", "roi.changed", {"by": "qc"})
    return _invoke("refresh_qc", {"project": project})


@qc_bp.route("/regions/rename", methods=["POST"])
def rename_region():
    """Rename a QC region -- the ROI's own name, through the ROI plugin's
    `update_roi` -- and take it in, so the panel and the ROI panel agree."""
    body = _body()
    project = body.get("datasource")
    roi_id = body.get("roi_id")
    name = str(body.get("name") or "").strip()
    if not project or not roi_id or not name or len(name) > 120:
        abort(400)
    renamed = _invoke("update_roi", {"project": project, "roi_id": roi_id, "name": name})
    if isinstance(renamed, tuple):
        return renamed
    api.notify_viewers(project, "roi", "roi.changed", {"by": "qc"})
    return _invoke("refresh_qc", {"project": project})


@qc_bp.route("/color", methods=["POST"])
def set_color():
    """Recolour what the panel draws: `{datasource, class, color}` a region
    class (its `qc_<class>` ROI category, so the ROI panel follows), or
    `{datasource, reason, color}` a cell reason. `color: null` puts back the
    default. Presentation only: no call, count or receipt changes."""
    import re

    from plexora.plugins.qc.server import results, roi_link, schemas

    body = _body()
    project = body.get("datasource")
    klass = body.get("class")
    reason = body.get("reason")
    color = body.get("color")
    if not project or bool(klass) == bool(reason):
        abort(400)
    if color is not None and not (isinstance(color, str)
                                  and re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
        abort(400)
    color = color.lower() if color else None
    if klass:
        ds = _session().image_data(project)
        if klass not in schemas.ARTIFACT_CLASSES and not (
                schemas.is_custom(klass)
                and any(c["id"] == klass for c in roi_link.custom_categories(ds))):
            abort(400)
        color = roi_link.set_category_color(ds, klass, color)
        api.notify_viewers(project, "roi", "roi.changed", {"by": "qc"})
        return api.json_response({"ok": True, "class": klass, "color": color})
    if reason not in schemas.CELL_REASONS and reason != "extreme_value":
        abort(400)
    results.set_reason_color(project, reason, color)
    api.notify_viewers(project, "qc", "qc.colors", {"reason": reason})
    return api.json_response({"ok": True, "reason": reason, "color": color})


@qc_bp.route("/strictness", methods=["POST"])
def strictness():
    body = _body()
    return _invoke("set_qc_strictness", {"project": body.get("datasource"),
                                         "preset": body.get("preset"),
                                         "custom_thresholds": body.get("custom_thresholds")})


@qc_bp.route("/refresh", methods=["POST"])
def refresh():
    """Take in the ROI panel's edits. `views` ({roi_id: [{name, color, range}]})
    names the channels on screen when a region was drawn by hand."""
    body = _body()
    arguments = {"project": body.get("datasource")}
    if body.get("views"):
        arguments["views"] = body["views"]
    return _invoke("refresh_qc", arguments)


@qc_bp.route("/approve", methods=["POST"])
def approve():
    body = _body()
    return _invoke("approve_qc_roi", {"project": body.get("datasource"),
                                      "roi_id": body.get("roi_id"),
                                      "action": body.get("action"),
                                      "lock": bool(body.get("lock", True))})


@qc_bp.route("/regions/refine", methods=["POST"])
def refine_regions():
    """Trace one QC region (or all of them) at pixel level inside its outline."""
    body = _body()
    arguments = {"project": body.get("datasource"), "force": bool(body.get("force"))}
    if body.get("all"):
        arguments["all"] = True
    else:
        arguments["roi_id"] = body.get("roi_id")
    if body.get("margin_um") is not None:
        arguments["margin_um"] = body.get("margin_um")
    response = _invoke("refine_qc_roi", arguments)
    if isinstance(response, tuple):
        return response
    api.notify_viewers(body.get("datasource"), "roi", "roi.changed", {"by": "qc"})
    return response


@qc_bp.route("/categories", methods=["POST"])
def categories():
    """Make the ROI category of one artifact class (`{class}`), or of a QC
    category the user names (`{label}`), so a QC region can be drawn in it."""
    from plexora.plugins.qc.server import roi_link, schemas

    body = _body()
    klass = body.get("class")
    ds = _session().image_data(body.get("datasource"))
    if klass is None and body.get("label") is not None:
        made = roi_link.ensure_custom_category(ds, body.get("label"))
        if made is None:
            abort(400)
        api.notify_viewers(ds.name, "roi", "roi.changed", {"revision": made["revision"]})
        return api.json_response({"ok": True, "custom": True, **made})
    if klass not in schemas.ARTIFACT_CLASSES:
        abort(400)
    revision = roi_link.ensure_categories(ds, [klass])
    api.notify_viewers(ds.name, "roi", "roi.changed", {"revision": revision})
    return api.json_response({"ok": True, "key": klass,
                              "category_id": schemas.roi_category_id(klass),
                              "label": schemas.roi_category_label(klass),
                              "revision": revision})


def _category_for(ds, body):
    """The QC category a draw names -- `{class}` or `{label}` -- made when it
    does not exist yet: {key, category_id, label}, or None."""
    from plexora.plugins.qc.server import roi_link, schemas

    klass = body.get("class")
    if klass is None and body.get("label") is not None:
        return roi_link.ensure_custom_category(ds, body.get("label"))
    if klass not in schemas.ARTIFACT_CLASSES:
        return None
    roi_link.ensure_categories(ds, [klass])
    return {"key": klass, "category_id": schemas.roi_category_id(klass),
            "label": schemas.roi_category_label(klass)}


@qc_bp.route("/regions/draw", methods=["POST"])
def draw_region():
    """A region drawn by hand in the QC panel: `{datasource, class | label,
    points, views}`. Written as an ordinary ROI through the ROI plugin's own
    `create_roi` (so the ROI panel shows it, and it can be edited there like
    any other), named as the ROI panel names a region -- its category and a
    number -- and taken into QC at once, with the channels on screen when it
    was drawn (`views`, as `/refresh` takes them). The ROI tool is never
    opened: the user stays in QC."""
    from plexora.plugins.roi.server.repository import ROIRepository

    body = _body()
    project = body.get("datasource")
    points = body.get("points")
    if not project or not isinstance(points, list) or len(points) < 3:
        abort(400)
    ds = _session().image_data(project)
    made = _category_for(ds, body)
    if made is None:
        abort(400)
    from plexora.plugins.roi.server import schema as roi_schema

    state = ROIRepository(ds.name).load()
    features = roi_schema.image_entry(state, roi_schema.DEFAULT_IMAGE)["features"]
    taken = {f.get("name") for f in features if f.get("category_id") == made["category_id"]}
    number = len(taken) + 1
    while f"{made['label']} {number}" in taken:
        number += 1
    created = _invoke("create_roi", {"project": project, "category": made["label"],
                                     "points": points, "name": f"{made['label']} {number}"})
    if isinstance(created, tuple):
        return created
    roi = json.loads(created.get_data())["roi"]
    api.notify_viewers(project, "roi", "roi.changed", {"by": "qc"})
    views = body.get("views")
    arguments = {"project": project}
    if isinstance(views, list) and views:
        arguments["views"] = {roi["id"]: views}
    refreshed = _invoke("refresh_qc", arguments)
    if isinstance(refreshed, tuple):
        return refreshed
    answer = json.loads(refreshed.get_data())
    return api.json_response({**answer, "roi": roi, "category": made})


@qc_bp.route("/vocabulary", methods=["GET"])
def vocabulary():
    """The artifact classes, and -- with `datasource` -- the QC categories the
    user named on that project (`custom`)."""
    from plexora.plugins.qc.server import roi_link, schemas

    project = request.args.get("datasource")
    custom = []
    if project:
        try:
            custom = roi_link.custom_categories(_session().image_data(project))
        except Exception:  # an unknown project has no categories of its own
            custom = []
    return api.json_response({
        "custom": custom,
        "classes": [{"id": k, "words": schemas.CLASS_WORDS[k],
                     "color": schemas.CLASS_COLORS[k],
                     "label": schemas.roi_category_label(k)}
                    for k in schemas.ARTIFACT_CLASSES],
        "strictness": ["lenient", "standard", "strict"],
        "actions": list(schemas.ACTIONS)})


@qc_bp.route("/export", methods=["POST"])
def export():
    body = _body()
    return _invoke("export_qc", {"project": body.get("datasource"),
                                 "what": body.get("what") or "both"})


@qc_bp.route("/download/<project>", methods=["GET"])
def download(project):
    """A file of the project's active QC result: cells.csv, regions.geojson,
    summary.json, report.html, report.pdf."""
    from plexora.plugins.qc.server import results

    kind = request.args.get("kind", "cells.csv")
    if kind in ("report.html", "report.pdf"):
        document = results.load(project)
        result = results.active(document)
        path = ((result or {}).get("report_paths") or {}).get(kind.split(".")[1])
        if not path:
            abort(404)
        return send_file(path, as_attachment=kind.endswith("pdf"))
    answer = _invoke("export_qc", {"project": project, "what": "both"})
    response = answer[0] if isinstance(answer, tuple) else answer
    payload = json.loads(response.get_data())
    if not payload.get("ok"):
        abort(404)
    files = payload.get("files") or {}
    path = {"cells.csv": files.get("cells"), "regions.geojson": files.get("regions"),
            "summary.json": files.get("summary"), "result.json": files.get("result")}.get(kind)
    if not path:
        abort(404)
    return send_file(path, as_attachment=True, download_name=f"{project}_qc_{kind}")


@qc_bp.route("/agent_session/<session_id>/control", methods=["POST"])
def agent_session_control(session_id):
    """The viewer's pause / resume / stop / take-over for a QC session. Take
    over locks the region under review (the user's act) and the session
    closes it as the user's."""
    from plexora.agent.sessions import control as session_control
    from plexora.plugins.qc.capabilities_session import record_limit_answers
    from plexora.plugins.qc.server import engine, schemas

    post = json.loads(request.data or b"{}")
    store = engine.store()
    if not store.exists(session_id):
        abort(404)

    def take_over(post, control):
        locked = list(control.get("locked_units") or [])
        candidate = post.get("candidate_id") or post.get("unit")
        roi_id = post.get("roi_id")
        datasource = post.get("datasource")
        if roi_id and datasource:
            from plexora.plugins.roi.server import service

            try:
                service.update_roi(_session().image_data(datasource), roi_id, locked=True)
            except Exception:
                pass
        if candidate:
            locked.append(candidate)
        return {"locked_units": locked}

    try:
        control = session_control.handle(
            store, session_id, post,
            tell_tabs=lambda event, record=None, **payload: _tell_tabs(
                session_id, event, record=record, **payload),
            summary_of=engine.summary_of, record_limit_answers=record_limit_answers,
            limit_decisions=schemas.LIMIT_DECISIONS, take_over=take_over)
    except session_control.BadRequest:
        abort(400)
    return api.json_response({"session_id": session_id, "control": control})


def _tell_tabs(session_id, event, /, record=None, **payload):
    from plexora.plugins.qc.server import engine, events

    try:
        record = record or engine.store().load(session_id)
    except Exception:
        return

    def notify(project, plugin, kind, body):
        return api.notify_viewers(project, plugin, kind, body)

    events.announce(notify, record.get("images") or [], session_id, event, **payload)


# -- the free image checks (capabilities_checks.py) ------------------------------------------


def _flag(name):
    return str(request.args.get(name, "")).lower() in ("1", "true", "yes")


@qc_bp.route("/registration", methods=["GET"])
def registration_state():
    """The Registration Check's state and the last numbers for its pair."""
    return _invoke("get_registration_check", {"project": _project_arg(),
                                              "include_overlay": _flag("include_overlay"),
                                              "include_map": _flag("include_map")})


@qc_bp.route("/registration/channels", methods=["GET"])
def registration_channels():
    return _invoke("detect_nuclear_channels", {"project": _project_arg()})


def _with_project(body):
    project = body.pop("datasource", None) or body.pop("project", None)
    if not project:
        abort(400)
    return {"project": project, **body}


@qc_bp.route("/registration/set", methods=["POST"])
def registration_set():
    return _invoke("set_registration_check", _with_project(_body()))


@qc_bp.route("/registration/step", methods=["POST"])
def registration_step():
    return _invoke("step_registration_comparison", _with_project(_body()))


@qc_bp.route("/registration/compute", methods=["POST"])
def registration_compute():
    return _invoke("compute_registration_mismatch", _with_project(_body()))


def _box_arg():
    """`x0,y0,x1,y1` in full-resolution pixels, finite, or a 400."""
    try:
        box = [float(v) for v in str(request.args.get("box", "")).split(",")]
    except ValueError:
        abort(400)
    if len(box) != 4 or not all(math.isfinite(v) for v in box):
        abort(400)
    return box


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        abort(400)


@qc_bp.route("/registration/disagreement", methods=["GET"])
def registration_disagreement():
    """Where the pair's DNA stains disagree, pixel by pixel, over `box`, read
    at the pyramid level that fits it in `max_px` pixels: an RGBA PNG whose
    alpha is the disagreement. The box it covers is in `X-QC-Box` (the image
    clips it). `comparison` scores another channel without switching to it.
    The panel asks for the view on screen, so it is as sharp as the zoom."""
    from io import BytesIO

    from plexora.agent.errors import AgentError
    from plexora.plugins.qc.server import registration

    project = _project_arg()
    box = _box_arg()
    try:
        answer = registration.disagreement(
            _session(), project, registration.load_state(project), box,
            max_px=_int_arg("max_px", 1024),
            comparison=request.args.get("comparison") or None)
    except AgentError as exc:
        return api.json_response({"ok": False, "error": exc.to_problem()}), \
            404 if exc.code == "not_found" else 400
    response = send_file(BytesIO(answer["png"]), mimetype="image/png")
    response.headers["X-QC-Box"] = ",".join(str(v) for v in answer["box"])
    response.headers["X-QC-Level"] = str(answer["level"])
    response.headers["Cache-Control"] = "no-store"
    return response


@qc_bp.route("/segmentation", methods=["GET"])
def segmentation_state():
    return _invoke("get_segmentation_qc", {"project": _project_arg()})


@qc_bp.route("/segmentation/cells", methods=["GET"])
def segmentation_cells():
    """The cells Segmentation QC flagged, in `/cells`'s group shape, for the QC
    cell layer (ambiguous ones only on request). `flag_under` / `flag_over`
    re-threshold the stored scores for viewing (the panel's sliders); the
    stored calls keep theirs."""
    from plexora.plugins.qc.server.segqc import run as segqc

    project = _project_arg()
    flags = {}
    for name in ("flag_under", "flag_over"):
        raw = request.args.get(name)
        if raw in (None, ""):
            continue
        try:
            flags[name] = float(raw)
        except ValueError:
            abort(400)
        if not math.isfinite(flags[name]):
            abort(400)
    return _read(lambda: segqc.viewer_groups(project,
                                             include_ambiguous=_flag("include_ambiguous"),
                                             **flags))


@qc_bp.route("/segmentation/density", methods=["GET"])
def segmentation_density():
    """Where segmentation problems are concentrated over `box`, on a grid
    about `bins` across -- under, over, large, small, irregular, each the share
    of cells flagged per grid cell. `flag_under` / `flag_over` as `/cells`."""
    from plexora.plugins.qc.server.segqc import run as segqc

    project = _project_arg()
    box = _box_arg()
    flags = {}
    for name in ("flag_under", "flag_over"):
        raw = request.args.get(name)
        if raw in (None, ""):
            continue
        try:
            flags[name] = float(raw)
        except ValueError:
            abort(400)
        if not math.isfinite(flags[name]):
            abort(400)
    return _read(lambda: segqc.density(project, box, bins=_int_arg("bins", 128), **flags))


@qc_bp.route("/segmentation/run", methods=["POST"])
def segmentation_run():
    """Starts the job; answers `{job_id}` at once. The panel polls `/jobs/<id>`."""
    return _invoke("run_segmentation_qc", _with_project(_body()))


@qc_bp.route("/segmentation/clear", methods=["POST"])
def segmentation_clear():
    return _invoke("clear_segmentation_qc", _with_project(_body()))


@qc_bp.route("/blur", methods=["GET"])
def blur_state():
    args = {"project": _project_arg(), "include_regions": _flag("include_regions")}
    if request.args.get("channel"):
        args["channel"] = request.args["channel"]
    return _invoke("get_blur_check", args)


def _float_arg(name):
    """A finite float query argument, None when absent, or a 400."""
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except ValueError:
        abort(400)
    if not math.isfinite(value):
        abort(400)
    return value


@qc_bp.route("/blur/map", methods=["GET"])
def blur_map():
    """One channel's Blur Score of every grid cell (uint8, base64) for the
    heatmap; fetched once per result."""
    from plexora.plugins.qc.server import blur

    project = _project_arg()
    channel = request.args.get("channel") or None

    def answer():
        session = _session()
        return blur.viewer_map(project, blur.resolve(session, project, channel))

    return _read(answer)


@qc_bp.route("/blur/mask", methods=["GET"])
def blur_mask():
    """One channel's mask, regions and blurred area at `threshold` (default
    its stored one): the slider's preview. Nothing is stored."""
    from plexora.plugins.qc.server import blur

    project = _project_arg()
    threshold = _float_arg("threshold")
    size = request.args.get("min_region_tiles")
    min_region_tiles = _int_arg("min_region_tiles", 0) if size not in (None, "") else None

    channel = request.args.get("channel") or None

    def answer():
        session = _session()
        return blur.viewer_mask(project, blur.resolve(session, project, channel),
                                threshold=threshold,
                                min_region_tiles=min_region_tiles or None)

    return _read(answer)


@qc_bp.route("/blur/run", methods=["POST"])
def blur_run():
    """Starts the job; answers `{job_id}` at once. The panel polls `/jobs/<id>`."""
    return _invoke("run_blur_check", _with_project(_body()))


@qc_bp.route("/blur/set", methods=["POST"])
def blur_set():
    return _invoke("set_blur_check", _with_project(_body()))


@qc_bp.route("/blur/clear", methods=["POST"])
def blur_clear():
    return _invoke("clear_blur_check", _with_project(_body()))


@qc_bp.route("/blur/regions/write", methods=["POST"])
def blur_regions_write():
    return _invoke("write_blur_regions", _with_project(_body()))


def _qc_job(job_id):
    """A job record, only when it is one of QC's (the panel reads nothing else)."""
    from plexora.agent import jobs

    record = jobs.store().get(job_id)
    if record is None or not str(record.get("capability") or "").startswith("qc."):
        abort(404)
    return record


@qc_bp.route("/jobs/<job_id>", methods=["GET"])
def job_state(job_id):
    record = _qc_job(job_id)
    return api.json_response({"ok": True, "job": {k: record.get(k) for k in (
        "job_id", "status", "progress", "project", "capability", "error", "result")}})


@qc_bp.route("/jobs/<job_id>/cancel", methods=["POST"])
def job_cancel(job_id):
    _qc_job(job_id)
    return _invoke("job_cancel", {"job_id": job_id})
