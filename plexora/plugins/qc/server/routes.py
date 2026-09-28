"""The QC panel's routes, mounted under /plugins/qc/.

Every route is the user's own act in the viewer, so none is licence-gated:
reading results, changing the strictness, taking in edits made in the ROI
panel, preparing a QC category to draw in, approving a region, downloading
files, and the running session's pause / stop / take-over control. They go
through the same capabilities an agent calls (`registry.invoke`), so the
receipts, the revision checks and the audit are the same whoever acts.
"""

from __future__ import annotations

import json

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


@qc_bp.route("/strictness", methods=["POST"])
def strictness():
    body = _body()
    return _invoke("set_qc_strictness", {"project": body.get("datasource"),
                                         "preset": body.get("preset"),
                                         "custom_thresholds": body.get("custom_thresholds")})


@qc_bp.route("/refresh", methods=["POST"])
def refresh():
    body = _body()
    return _invoke("refresh_qc", {"project": body.get("datasource")})


@qc_bp.route("/approve", methods=["POST"])
def approve():
    body = _body()
    return _invoke("approve_qc_roi", {"project": body.get("datasource"),
                                      "roi_id": body.get("roi_id"),
                                      "action": body.get("action"),
                                      "lock": bool(body.get("lock", True))})


@qc_bp.route("/categories", methods=["POST"])
def categories():
    """Make the ROI category of one artifact class, so the user can draw a QC
    region in the ROI panel."""
    from plexora.plugins.qc.server import roi_link, schemas

    body = _body()
    klass = body.get("class")
    if klass not in schemas.ARTIFACT_CLASSES:
        abort(400)
    ds = _session().image_data(body.get("datasource"))
    revision = roi_link.ensure_categories(ds, [klass])
    api.notify_viewers(ds.name, "roi", "roi.changed", {"revision": revision})
    return api.json_response({"ok": True, "category_id": schemas.roi_category_id(klass),
                              "label": schemas.roi_category_label(klass),
                              "revision": revision})


@qc_bp.route("/vocabulary", methods=["GET"])
def vocabulary():
    from plexora.plugins.qc.server import schemas

    return api.json_response({
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
