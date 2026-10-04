"""`/ai/v1`: Plexora AI runs from the viewer.

    POST /ai/v1/runs                  start a run (ai.run_session, a job)
    GET  /ai/v1/runs                  the recent runs (ai.run_status)
    GET  /ai/v1/runs/<id>             one run, by run id or job id
    POST /ai/v1/runs/<id>/control     {action: pause | resume | stop} (ai.run_control)
    GET  /ai/v1/balance               credit, prices, and ?project=&kind= an estimate

Every route calls `registry.invoke` on the `ai.*` capabilities
(plexora/ai/harness/capabilities.py), so receipts and audit lines are the
ones an MCP agent's calls leave. The job a run executes on is handed this
server's notifier, so the session's events -- and the harness's own usage
and pause events -- reach the open tabs, where the agent panel shows them.

Guarded twice, like the agent control plane: without a server token only
this machine is answered (a run spends the account's credit), and the whole
blueprint needs the `ai` entitlement (`guards.guard_blueprint`).
"""

from __future__ import annotations

import ipaddress

from flask import Blueprint, current_app, jsonify, request

from plexora.licensing import guards

ai_bp = Blueprint("ai_v1", __name__)

#: How an `ai.*` refusal reads over HTTP.
STATUS = {"invalid_input": 400, "unknown_project": 404, "precondition_missing": 409,
          "unsupported_modality": 409, "conflict": 409, "license_required": 403,
          "permission_required": 403, "resource_unavailable": 503}

_SESSION = None


@ai_bp.before_request
def _loopback_unless_token():
    if current_app.config.get("PLEXORA_AUTH_TOKEN"):
        return None
    try:
        if ipaddress.ip_address(request.remote_addr or "").is_loopback:
            return None
    except ValueError:
        pass
    return jsonify(success=False, error="Plexora AI answers this machine only"), 403


guards.guard_blueprint(ai_bp, "ai")


def _session():
    global _SESSION
    from plexora.agent import AgentSession, registry

    try:
        registry.get("ai.run_session")
    except Exception:
        registry.register_core()
    if _SESSION is None:
        _SESSION = AgentSession()
    return _SESSION


def _invoke(name, arguments, *, created=False, add=None):
    import dataclasses

    from plexora import api
    from plexora.agent import registry
    from plexora.agent.policy import Policy

    policy = dataclasses.replace(Policy.from_flags(), principal="viewer")

    def notify(project, plugin, kind, body):
        return api.notify_viewers(project, plugin, kind, body)

    answer = registry.invoke(_session(), name, arguments, policy=policy, notify=notify)
    if not answer["ok"]:
        error = answer["error"]
        return jsonify(success=False, error=error.get("message"), problem=error),             STATUS.get(error.get("code"), 500)
    result = dict(answer["result"] or {})
    if add is not None:
        result.update(add(result))
    result.setdefault("operation_id", answer.get("operation_id"))
    return jsonify(success=True, **result), 201 if created else 200


def _body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _names(value):
    if isinstance(value, str):
        value = [v for v in value.split(",")]
    return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else None


@ai_bp.route("/runs", methods=["POST"])
def start_run():
    body = _body()
    arguments = {k: body[k] for k in ("kind", "project", "mode", "resume_session", "dev", "model",
                                       "units_per_worker", "parallel_markers", "start_options", "context")
                 if body.get(k) is not None}
    arguments.setdefault("project", body.get("datasource"))
    for key in ("markers", "channels"):
        if body.get(key):
            arguments[key] = _names(body[key])
    from plexora.ai.harness.capabilities import run_id_for

    return _invoke("ai.run_session", arguments, created=True,
                   add=lambda result: {"run_id": run_id_for(result["job_id"])})


@ai_bp.route("/runs", methods=["GET"])
def list_runs():
    return _invoke("ai.run_status", {"limit": request.args.get("limit", 20, type=int)})


@ai_bp.route("/runs/<run_id>", methods=["GET"])
def get_run(run_id):
    return _invoke("ai.run_status", {"run_id": run_id})


@ai_bp.route("/runs/<run_id>/control", methods=["POST"])
def control_run(run_id):
    return _invoke("ai.run_control", {"run_id": run_id, "action": _body().get("action")})


@ai_bp.route("/balance", methods=["GET"])
def balance():
    arguments = {"kind": request.args.get("kind") or "both", "dev": request.args.get("dev") in ("1", "true")}
    project = request.args.get("project") or request.args.get("datasource")
    if project:
        arguments["project"] = project
    for key in ("markers", "channels"):
        if request.args.get(key):
            arguments[key] = _names(request.args[key])
    return _invoke("ai.balance", arguments)
