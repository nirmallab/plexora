"""The QC panel's routes, mounted under /plugins/qc/.

Every route is the user's own act in the viewer, so none is licence-gated:
reading results, changing the strictness, taking in edits made in the ROI
panel, preparing a QC category to draw in (one of the five, a subtype of
one, or one the user names), approving a region, downloading files, and the running
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
    return _invoke("get_qc_results", {"project": datasource, "include_regions": True,
                                      "detail": "full"})


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


@qc_bp.route("/cell_at", methods=["GET"])
def cell_at():
    """QC's record of the cell under a point of the image (`x`, `y` in
    full-resolution pixels; `radius` how far from it to look): why it was
    flagged, on what value and against which bar -- the viewer's hover card."""
    import math

    from plexora.plugins.qc.server import viewer_data

    project = _project_arg()
    try:
        x, y = float(request.args["x"]), float(request.args["y"])
        radius = float(request.args.get("radius") or 0.0)
    except (KeyError, TypeError, ValueError):
        abort(400)
    if not all(math.isfinite(v) for v in (x, y, radius)):
        abort(400)
    return _read(lambda: viewer_data.cell_at(_session().data(project), project, x, y,
                                             radius=radius))


@qc_bp.route("/regions/delete", methods=["POST"])
def delete_region():
    """Delete QC regions -- the ROIs themselves, the user's explicit act from the
    panel's menu (`roi_id`, or `roi_ids` for a whole category) -- and take the
    deletion in once, so the cells stop counting them."""
    body = _body()
    project = body.get("datasource")
    roi_ids = body.get("roi_ids") or ([body["roi_id"]] if body.get("roi_id") else [])
    if not project or not roi_ids or not isinstance(roi_ids, list):
        abort(400)
    from plexora.agent import registry
    from plexora.agent.policy import Policy

    import dataclasses

    policy = dataclasses.replace(Policy.from_flags(allow_destructive=True), principal="viewer")

    def notify(target, plugin, kind, payload):
        return api.notify_viewers(target, plugin, kind, payload)

    failed = []
    for roi_id in roi_ids:
        answer = registry.invoke(_session(), "delete_roi",
                                 {"project": project, "roi_id": roi_id, "confirm": True},
                                 policy=policy, notify=notify)
        if not answer["ok"]:
            failed.append({"roi_id": roi_id, "error": answer["error"]})
    if len(failed) == len(roi_ids):
        error = failed[0]["error"]
        return api.json_response({"ok": False, "error": error}), \
            409 if error["code"] == "conflict" else 400
    api.notify_viewers(project, "roi", "roi.changed", {"by": "qc"})
    response = _invoke("refresh_qc", {"project": project})
    if failed and not isinstance(response, tuple):
        # Some were refused (a locked one, say): said beside the refresh.
        response = api.json_response({**response.get_json(), "not_deleted": failed})
    return response


@qc_bp.route("/findings/dismiss", methods=["POST"])
def dismiss_finding():
    """Set aside (or, `restore`, put back) a cell reason, a marker's flag or a
    channel's audit verdict the user judged wrong."""
    body = _body()
    arguments = {"project": body.get("datasource"), "finding": body.get("finding"),
                 "restore": bool(body.get("restore"))}
    for key in ("reason", "marker", "channel"):
        if body.get(key):
            arguments[key] = body[key]
    return _invoke("dismiss_qc_finding", arguments)


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
    """Recolour what the panel draws: `{datasource, category, color}` a QC
    category (one of the five, "review", or a custom key: its `qc_*` ROI
    category, so the ROI panel follows) -- `{class}` is read as its class's
    category -- or `{datasource, reason, color}` a cell reason. `color: null`
    puts back the default. Presentation only: no call, count or receipt
    changes."""
    import re

    from plexora.plugins.qc.server import results, roi_link, schemas

    body = _body()
    project = body.get("datasource")
    key = body.get("category") or body.get("class")
    reason = body.get("reason")
    color = body.get("color")
    if not project or bool(key) == bool(reason):
        abort(400)
    if color is not None and not (isinstance(color, str)
                                  and re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
        abort(400)
    color = color.lower() if color else None
    if key:
        ds = _session().image_data(project)
        if schemas.is_custom(key):
            if not any(c["id"] == key for c in roi_link.custom_categories(ds)):
                abort(400)
        else:
            key = schemas._as_key(key)
            if key is None:
                abort(400)
        color = roi_link.set_category_color(ds, key, color)
        api.notify_viewers(project, "roi", "roi.changed", {"by": "qc"})
        return api.json_response({"ok": True, "category": key, "color": color})
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
    """Make the ROI category a QC region can be drawn in: one of the five
    (`{category}`), a subtype's (`{class}`: its category's), or a QC
    category the user names (`{label}`)."""
    ds = _session().image_data(_body_cache().get("datasource"))
    made = _category_for(ds, _body_cache())
    if made is None:
        abort(400)
    api.notify_viewers(ds.name, "roi", "roi.changed", {"revision": made.get("revision")})
    return api.json_response({"ok": True, **made})


def _body_cache():
    from flask import g

    if not hasattr(g, "qc_body"):
        g.qc_body = _body()
    return g.qc_body


def _category_for(ds, body):
    """The QC category a draw names -- `{category}` (one of the five or
    "review"), `{class}` (a subtype: its category, the subtype kept) or
    `{label}` (custom) -- made when it does not exist yet: {key, category_id,
    label, class, custom, revision}, or None."""
    from plexora.plugins.qc.server import roi_link, schemas

    category = body.get("category")
    klass = body.get("class")
    if category is None and klass is None and body.get("label") is not None:
        made = roi_link.ensure_custom_category(ds, body.get("label"))
        return None if made is None else {**made, "custom": True,
                                          "class": schemas.CUSTOM_CLASS}
    if klass is not None:
        if klass not in schemas.ARTIFACT_CLASSES:
            return None
        category = schemas.category_of_class(klass)
    if category not in (*schemas.CATEGORY_IDS, schemas.REVIEW["id"]):
        return None
    revision = roi_link.ensure_categories(ds, [category])
    return {"key": category, "category_id": schemas.roi_category_id(category),
            "label": roi_link.category_label(ds, category),
            "class": klass or schemas.default_class(category), "custom": False,
            "revision": revision}


@qc_bp.route("/regions/draw", methods=["POST"])
def draw_region():
    """A region drawn by hand in the QC panel: `{datasource, category | class
    | label, points, views}`. A `class` (a subtype typed into the picker) is
    kept as the region's class through a `qc-class:` token in its notes.
    Written as an ordinary ROI through the ROI plugin's own
    `create_roi` (so the ROI panel shows it, and it can be edited there like
    any other), named as the ROI panel names a region -- its category and a
    number -- and taken into QC at once, with the channels on screen when it
    was drawn (`views`, as `/refresh` takes them). The ROI tool is never
    opened: the user stays in QC."""
    from plexora.plugins.roi.server.repository import ROIRepository

    body = _body_cache()
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
    arguments = {"project": project, "category": made["label"], "points": points,
                 "name": f"{made['label']} {number}"}
    if body.get("class") and made.get("class") and not made.get("custom"):
        arguments["notes"] = f"qc-class:{made['class']}"
    created = _invoke("create_roi", arguments)
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
    """The five categories (with what each groups, for the picker's help),
    "Needs review", the artifact classes (each with its category), and --
    with `datasource` -- the QC categories the user named on that project
    (`custom`)."""
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
        "categories": schemas.public_categories(),
        "review": {"id": schemas.REVIEW["id"], "words": schemas.REVIEW["words"],
                   "color": schemas.REVIEW["color"]},
        "classes": [{"id": k, "words": schemas.CLASS_WORDS[k],
                     "color": schemas.category_color(schemas.category_of_class(k)),
                     "category": schemas.category_of_class(k),
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
    provenance.json, findings.csv, summary.json, report.html, report.pdf."""
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
            "provenance.json": files.get("provenance"),
            "findings.csv": files.get("findings"),
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


#: The viewing thresholds a Segmentation QC read may carry: Under's and Over's
#: score bars, and the robust SDs of Large, Small and Irregular.
SEG_THRESHOLDS = ("flag_under", "flag_over", "z_large", "z_small", "z_irregular")


def _seg_thresholds(source=None):
    """The thresholds asked for, from the query (or a POST body): each a
    finite number, missing or empty for the default; anything else is a 400."""
    source = request.args if source is None else source
    out = {}
    for name in SEG_THRESHOLDS:
        raw = source.get(name)
        if raw in (None, ""):
            continue
        try:
            out[name] = float(raw)
        except (TypeError, ValueError):
            abort(400)
        if not math.isfinite(out[name]):
            abort(400)
    return out


@qc_bp.route("/segmentation/cells", methods=["GET"])
def segmentation_cells():
    """The cells Segmentation QC flagged, in `/cells`'s group shape, for the QC
    cell layer (ambiguous ones only on request): Under and Over, then Large,
    Small and Irregular. The thresholds (`SEG_THRESHOLDS`) are for viewing;
    the stored calls keep theirs."""
    from plexora.plugins.qc.server.segqc import run as segqc

    project = _project_arg()
    thresholds = _seg_thresholds()
    return _read(lambda: segqc.viewer_groups(project,
                                             include_ambiguous=_flag("include_ambiguous"),
                                             **thresholds))


@qc_bp.route("/segmentation/density", methods=["GET"])
def segmentation_density():
    """Where segmentation problems are concentrated over `box`, on a grid
    about `bins` across -- under, over, large, small, irregular, each the share
    of cells flagged per grid cell, at the thresholds as `/cells`."""
    from plexora.plugins.qc.server.segqc import run as segqc

    project = _project_arg()
    box = _box_arg()
    thresholds = _seg_thresholds()
    return _read(lambda: segqc.density(project, box, bins=_int_arg("bins", 128),
                                       **thresholds))


@qc_bp.route("/segmentation/download", methods=["GET"])
def segmentation_download():
    """Every cell of the mask, one row each: a boolean per category at the
    thresholds on the panel's sliders -- what the viewer is drawing -- then the
    status, the scores and the shapes, as CSV."""
    import io

    from plexora.plugins.qc.server.segqc import run as segqc

    project = _project_arg()
    thresholds = _seg_thresholds()
    found = segqc.calls(project, **thresholds)
    if found is None:
        abort(404)
    _summary, table, _th = found
    buffer = io.BytesIO()
    table.write_csv(buffer)
    buffer.seek(0)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(project))
    return send_file(buffer, mimetype="text/csv", as_attachment=True,
                     download_name=f"{safe}_segmentation_qc.csv")


@qc_bp.route("/segmentation/write", methods=["POST"])
def segmentation_write():
    """Segmentation QC's calls, at the panel's thresholds, into the project's
    own table file: the user pressed Save and confirmed it, so this is their
    act (the ROI panel's Save is the same), not an agent's -- which still
    needs `--allow-source-writes`. `replace` only as the user's answer to the
    conflict a first attempt returns."""
    body = _body()
    project = body.get("datasource") or body.get("project")
    if not project:
        abort(400)
    thresholds = _seg_thresholds(body)
    arguments = {"project": project, "confirm": True, "what": "segmentation",
                 "replace": bool(body.get("replace"))}
    if thresholds:
        arguments["seg_thresholds"] = thresholds
    return _invoke("write_qc_to_source", arguments, allow_source_writes=True)


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
