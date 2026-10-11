"""`/segment/v1`: magic select for the viewer.

    GET  /segment/v1/status          what is installed, downloading, or wrong
    POST /segment/v1/install         start the one-time setup (idempotent)
    POST /segment/v1/install/cancel  stop a running setup
    POST /segment/v1/segment         view + prompts -> one polygon
    POST /segment/v1/install3d       start fetching the through-planes model
    POST /segment/v1/carry           an outline on one plane -> one per plane

Core, not a plugin's: the ROI panel and the QC panel both call it, and core
JavaScript may only call core routes. Nothing here writes a region -- the
caller commits the polygon through its own undoable store.

`segment` answers 409 `not_ready` carrying the status fields when the model
is not set up, so a client never has to ask `status` first: it clicks, and if
the answer is 409 it starts the setup and shows the progress.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

segment_bp = Blueprint("segment_v1", __name__)

_SESSION = None


def _session():
    global _SESSION
    from plexora.agent import AgentSession

    if _SESSION is None:
        _SESSION = AgentSession()
    return _SESSION


def _error(code, message, status, **extra):
    return jsonify(ok=False, success=False, error={"code": code, "message": message, **extra}), status


def _body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@segment_bp.route("/status", methods=["GET"])
def status():
    from plexora.vision import sam, sam_video

    # `through_planes`: the optional model that carries an outline through a
    # z-stack (plexora/vision/sam_video.py) -- a find_spec and a stat.
    return jsonify(ok=True, **sam.status().to_dict(),
                   through_planes=sam_video.status().to_dict())


@segment_bp.route("/install3d", methods=["POST"])
def install3d():
    from plexora.vision import sam_video

    current = sam_video.status()
    if current.state in ("ready", "downloading"):
        return jsonify(ok=True, **current.to_dict()), 200
    if current.state in ("disabled", "not_installed_runtime"):
        return _error(current.state, current.message, 409, **current.to_dict())
    try:
        started = sam_video.start_install()
    except PermissionError as exc:
        return _error("dir_not_writable", str(exc), 400, **current.to_dict())
    return jsonify(ok=True, **started.to_dict()), 202


@segment_bp.route("/install", methods=["POST"])
def install():
    from plexora.vision import sam

    current = sam.status()
    if current.state == "ready":
        return jsonify(ok=True, **current.to_dict()), 200
    if current.state == "disabled":
        return _error("disabled", current.hint, 409, **current.to_dict())
    if current.state == "not_installed_runtime":
        return _error("not_installed_runtime",
                      "Magic select is not available on this server.", 409,
                      **current.to_dict())
    try:
        started = sam.start_install()
    except PermissionError as exc:
        return _error("dir_not_writable", str(exc), 400, **current.to_dict())
    return jsonify(ok=True, **started.to_dict()), 202


@segment_bp.route("/install/cancel", methods=["POST"])
def cancel():
    from plexora.vision import sam

    return jsonify(ok=True, **sam.cancel_install().to_dict())


@segment_bp.route("/segment", methods=["POST"])
def segment():
    from plexora.agent.errors import AgentError
    from plexora.vision import sam, sam_backend, sam_weights
    from plexora.vision import segment as segmenter

    body = _body()
    project = body.get("datasource") or body.get("project")
    if not project:
        return _error("invalid_input", "datasource is required", 400)
    current = sam.status()
    if current.state != "ready" and not sam.available():
        return _error("not_ready", "Magic select is not set up yet.", 409, **current.to_dict())
    if not sam.QUEUE.acquire(blocking=False):
        return _error("busy", "Still outlining the last click.", 429, retry_after_ms=400)
    try:
        result = segmenter.segment(
            _session(), str(project), view=body.get("view"), points=body.get("points"),
            channels=body.get("channels"), box=body.get("box"), token=body.get("token"),
            options=body.get("options") if isinstance(body.get("options"), dict) else None,
            image_view=body.get("image_view") or None)
    except segmenter.SegmentError as exc:
        status_code = {"invalid_input": 400, "unsupported_modality": 409}.get(exc.code, 400)
        return _error(exc.code, str(exc), status_code, **exc.detail)
    except AgentError as exc:
        status_code = {"unknown_project": 404, "invalid_input": 400}.get(exc.code, 409)
        return _error(exc.code, str(exc), status_code)
    except (sam_backend.SamRuntimeMissing, sam_weights.SamWeightsMissing):
        return _error("not_ready", "Magic select is not set up yet.", 409,
                      **sam.status().to_dict())
    except Exception as exc:
        from plexora.server.providers.base import ResourceUnavailable

        if isinstance(exc, ResourceUnavailable):
            raise  # the app's handler answers 503 for a sleeping node
        return _error("inference_failed", f"Magic select failed: {exc}", 500)
    finally:
        sam.QUEUE.release()
    return jsonify(ok=True, success=True, **result)


@segment_bp.route("/carry", methods=["POST"])
def carry():
    """Carry an outline on plane `z` up and down the stack.

    Body: `datasource`, `z`, `view` ({x, y, width, height}, the screen), the
    prompt -- `geometry` (an outline, kept as-is on its own plane) or
    `points`/`box` as for `segment` -- and optionally `t`, `channels`,
    `z_range` [first, last] and `engine` (auto | sam2 | mobile). Answers
    plexora/vision/propagate.py's result, `footprint` included (the outline
    covering every plane's, what the region stores as its geometry).

    `auto` takes SAM 2 when the 3-D extra is installed: its weights are then
    fetched on first use, answered 409 `not_ready` with the progress, exactly
    as magic select's own setup is.
    """
    from plexora.agent.errors import AgentError
    from plexora.vision import propagate as carrier
    from plexora.vision import sam, sam_backend, sam_video, sam_weights
    from plexora.vision import segment as segmenter

    body = _body()
    project = body.get("datasource") or body.get("project")
    if not project:
        return _error("invalid_input", "datasource is required", 400)
    try:
        z = int(body.get("z"))
        t = int(body.get("t") or 0)
    except (TypeError, ValueError):
        return _error("invalid_input", "z (and t) are plane indices", 400)
    engine = body.get("engine") or "auto"
    third = sam_video.status()
    if engine in ("auto", "sam2") and third.state in ("weights_missing", "downloading", "error"):
        if third.state == "weights_missing":
            third = sam_video.start_install()
        return _error("not_ready", third.message, 409, **third.to_dict(), engine="sam2")
    if engine == "mobile" or (engine == "auto" and not third.ready):
        current = sam.status()
        if current.state != "ready" and not sam.available():
            return _error("not_ready", "Magic select is not set up yet.", 409,
                          **current.to_dict())
    if not sam.QUEUE.acquire(blocking=False):
        return _error("busy", "Still outlining the last click.", 429, retry_after_ms=400)
    try:
        z_range = body.get("z_range")
        result = carrier.carry(
            _session(), str(project), z=z, t=t, view=body.get("view"),
            geometry=body.get("geometry") or None, points=body.get("points"),
            box=body.get("box"), channels=body.get("channels"),
            z_range=tuple(z_range) if isinstance(z_range, (list, tuple)) and len(z_range) == 2
            else None, engine=engine,
            options=body.get("options") if isinstance(body.get("options"), dict) else None)
        result["footprint"] = carrier.footprint(result["planes"]) if result["planes"] else None
    except segmenter.SegmentError as exc:
        status_code = {"invalid_input": 400, "unsupported_modality": 409,
                       "precondition_missing": 409}.get(exc.code, 400)
        return _error(exc.code, str(exc), status_code, **exc.detail)
    except AgentError as exc:
        status_code = {"unknown_project": 404, "invalid_input": 400}.get(exc.code, 409)
        return _error(exc.code, str(exc), status_code)
    except (sam_backend.SamRuntimeMissing, sam_weights.SamWeightsMissing, sam_video.Sam2Missing):
        return _error("not_ready", "The model is not set up yet.", 409,
                      **sam_video.status().to_dict())
    except Exception as exc:
        from plexora.server.providers.base import ResourceUnavailable

        if isinstance(exc, ResourceUnavailable):
            raise
        return _error("inference_failed", f"Carrying the outline failed: {exc}", 500)
    finally:
        sam.QUEUE.release()
    return jsonify(ok=True, success=True, **result)
