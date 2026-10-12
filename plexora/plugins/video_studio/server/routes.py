"""Video Studio's HTTP surface.

Thin, like Figure Builder's: parse, hand to the repository, the presets or the
export jobs, translate the outcome. Status codes the client acts on:

    400  the request is wrong; retrying it unchanged fails the same way
    403  this licence does not include Video Studio (core's guard answers it)
    404  no such video or export
    409  somebody else saved first (revision), or frames arrived out of order
    410  the export is already over
    422  the request asks for something this video or server cannot do: a track
         that would interpolate acquired data, a format no encoder here writes,
         a stored video this build cannot read
    429  too many exports at once
    503  the encoder is behind; send the chunk again after Retry-After
"""

from __future__ import annotations

import json

from flask import Blueprint, Response, jsonify, request, send_file

from plexora import api
from plexora.plugins.video_studio.server import (
    encode_jobs, encoders, facts as facts_mod, manifest, presets, repository, schema)
from plexora.plugins.video_studio.server.repository import ConflictError, UnknownVideo

video_studio_bp = Blueprint(
    "video_studio", __name__,
    static_folder="../static",
    static_url_path="/static",
)

MAX_BODY_BYTES = 32 * 1024 * 1024


def _payload():
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        raise ValueError("request body is too large")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    return body


def _error(message, status=400, **extra):
    return jsonify(success=False, error=str(message), **extra), status


def _not_found():
    return _error("unknown_video", 404)


def _guard(fn):
    """The usual translation of repository outcomes into status codes."""
    try:
        return fn()
    except UnknownVideo:
        return _not_found()
    except ConflictError as exc:
        return _error("stale_revision", 409, revision=exc.current_revision)
    except schema.UnreadableVideo as exc:
        return _error(exc, 422, code="unreadable_video")
    except schema.IntegrityError as exc:
        return _error(exc, 422, code="integrity")
    except encode_jobs.JobError as exc:
        response = _error(exc, exc.status)
        if exc.status == 503:
            response[0].headers["Retry-After"] = "1"
        return response
    except (ValueError, KeyError, TypeError) as exc:
        return _error(exc, 400)


def _facts_for(datasource, client=None):
    server = None
    if datasource:
        try:
            server = facts_mod.dataset_facts(datasource)
        except KeyError:
            server = None
    return facts_mod.merge(server, client)


# -- what the bar needs to open -------------------------------------------------


@video_studio_bp.route("/api/state", methods=["GET"])
def state():
    """Everything the Quick Capture bar needs on opening: the presets for this
    image (with why any is unavailable), the encoders, this image's videos."""
    datasource = request.args.get("datasource") or ""
    facts = _facts_for(datasource)
    return api.json_response({
        "success": True,
        "facts": facts,
        "presets": presets.describe_all(facts),
        "groups": [{"name": n, "label": label} for n, label in presets.GROUPS],
        "default_preset": presets.default_preset(facts),
        "encoders": encoders.report(),
        "videos": repository.list_videos(datasource or None),
        "limits": {"max_side": schema.MAX_OUTPUT_SIDE, "max_duration_s": schema.MAX_DURATION_S,
                   "fps": list(schema.FPS_CHOICES), "formats": list(schema.FORMATS),
                   "qualities": list(schema.QUALITIES), "overlays": list(schema.OVERLAYS)},
    })


@video_studio_bp.route("/api/encoders", methods=["GET"])
def encoder_report():
    return api.json_response({"success": True,
                              **encoders.report(refresh=request.args.get("refresh") == "1")})


@video_studio_bp.route("/api/facts/<datasource>", methods=["GET"])
def dataset_facts(datasource):
    try:
        return api.json_response({"success": True, "facts": facts_mod.dataset_facts(datasource)})
    except KeyError:
        return _error("no such project", 404)


@video_studio_bp.route("/api/presets/build", methods=["POST"])
def build_preset():
    """A preset's project without saving it: what the bar previews while the
    user changes parameters."""
    def run():
        body = _payload()
        facts = _facts_for(body.get("datasource"), body.get("facts"))
        document = presets.build(body.get("preset"), facts, body.get("scene"),
                                 body.get("params"))
        return api.json_response({"success": True, "document": document,
                                  "warnings": schema.validate(document)})
    return _guard(run)


# -- the library and the document --------------------------------------------------


@video_studio_bp.route("/api/videos", methods=["GET"])
def list_videos():
    return api.json_response({"success": True, "videos": repository.list_videos(
        request.args.get("datasource") or None)})


@video_studio_bp.route("/api/videos", methods=["POST"])
def create_video():
    """Save a new video: a whole document (the bar's previewed preset, an
    imported `.plexvid`), or a preset to build here."""
    def run():
        body = _payload()
        document = body.get("document")
        if not isinstance(document, dict):
            facts = _facts_for(body.get("datasource"), body.get("facts"))
            document = presets.build(body.get("preset"), facts, body.get("scene"),
                                     body.get("params"))
        video_id = repository.create(document, title=body.get("title"))
        return api.json_response({"success": True, "video_id": video_id,
                                  "document": repository.load(video_id)})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>", methods=["GET"])
def get_video(video_id):
    def run():
        schema.validate_video_id(video_id)
        document = repository.load(video_id)
        return api.json_response({"success": True, "document": document,
                                  "warnings": schema.validate(document),
                                  "exports": repository.list_exports(video_id)})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>", methods=["PUT"])
def replace_video(video_id):
    def run():
        schema.validate_video_id(video_id)
        body = _payload()
        revision = repository.replace(video_id, body.get("base_revision"), body.get("document"))
        return api.json_response({"success": True, "revision": revision,
                                  "document": repository.load(video_id)})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>", methods=["DELETE"])
def delete_video(video_id):
    def run():
        schema.validate_video_id(video_id)
        repository.delete(video_id)
        return api.json_response({"success": True})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>/ops", methods=["POST"])
def apply_ops(video_id):
    """Edits as operations; the answer carries the inverse, which is what the
    client's undo stack keeps."""
    def run():
        schema.validate_video_id(video_id)
        body = _payload()
        result = repository.apply(video_id, body.get("base_revision"), body.get("operations"),
                                  origin=str(body.get("origin") or "ui"))
        return api.json_response({"success": True, "revision": result["revision"],
                                  "inverse": result["inverse"], "document": result["document"]})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>/duplicate", methods=["POST"])
def duplicate_video(video_id):
    def run():
        schema.validate_video_id(video_id)
        body = request.get_json(silent=True) or {}
        new_id = repository.duplicate(video_id, title=body.get("title"))
        return api.json_response({"success": True, "video_id": new_id,
                                  "document": repository.load(new_id)})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>/document", methods=["GET"])
def download_document(video_id):
    """The project as a `.plexvid` file: plain JSON, re-importable through
    `POST /api/videos`, with the source fingerprint checked on import."""
    def run():
        schema.validate_video_id(video_id)
        document = repository.load(video_id)
        body = json.dumps({"plexora_video": schema.SCHEMA_VERSION, "document": document},
                          indent=2)
        name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in document["title"])
        return Response(body, mimetype="application/json", headers={
            "Content-Disposition": f'attachment; filename="{name.strip() or video_id}.plexvid"'})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>/posters/<kf_id>", methods=["GET", "PUT"])
def poster(video_id, kf_id):
    def run():
        schema.validate_video_id(video_id)
        if request.method == "PUT":
            data = request.get_data(cache=False)
            fmt = (request.content_type or "image/webp").split("/")[-1].split(";")[0]
            repository.set_poster(video_id, kf_id, data, fmt=fmt)
            return api.json_response({"success": True})
        found = repository.get_poster(video_id, kf_id)
        if found is None:
            return _error("no poster", 404)
        fmt, data = found
        return Response(data, mimetype=f"image/{fmt}", headers={"Cache-Control": "no-store"})
    return _guard(run)


# -- exports --------------------------------------------------------------------------


def _export_name(document, output):
    stem = "".join(c if c.isalnum() or c in "-_" else "-" for c in document["title"]).strip("-")
    stem = "-".join(part for part in stem.split("-") if part)[:80] or document["video_id"]
    return f"{stem}-{output['width']}x{output['height']}-{output['fps']}fps"


@video_studio_bp.route("/api/videos/<video_id>/exports", methods=["GET"])
def list_exports(video_id):
    def run():
        schema.validate_video_id(video_id)
        return api.json_response({"success": True,
                                  "exports": repository.list_exports(video_id)})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>/exports", methods=["POST"])
def start_export(video_id):
    """Open a server-side writer. The document is frozen here: editing the
    video while it exports changes the next export, not this one."""
    def run():
        schema.validate_video_id(video_id)
        body = _payload()
        document = repository.load(video_id)
        requested = body.get("output") if isinstance(body.get("output"), dict) else {}
        output = schema.normalize_output({**document["output"], **requested})
        if requested.get("writer"):
            output["writer"] = str(requested["writer"])
        frames = schema.frame_count(document["duration_s"], output["fps"])
        job = encode_jobs.start(video_id, document, output, frames=frames,
                                name=_export_name(document, output))
        return api.json_response({"success": True, **job})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>/exports/<export_id>/frames", methods=["POST"])
def push_frames(video_id, export_id):
    def run():
        schema.validate_video_id(video_id)
        first = int(request.headers.get("X-Plexora-First-Frame", "-1"))
        count = int(request.headers.get("X-Plexora-Frames", "0"))
        frame_format = request.headers.get("X-Plexora-Frame-Format", "rgba")
        job = encode_jobs.push(video_id, export_id, first, count,
                               request.get_data(cache=False),
                               frame_format="png" if frame_format == "png" else "rgba")
        return api.json_response({"success": True, **job})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>/exports/<export_id>/finish", methods=["POST"])
def finish_export(video_id, export_id):
    def run():
        schema.validate_video_id(video_id)
        body = request.get_json(silent=True) or {}
        job = encode_jobs.finish(video_id, export_id, body.get("manifest") or {})
        return api.json_response({"success": True, **job})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>/exports/<export_id>", methods=["GET"])
def export_status(video_id, export_id):
    job = encode_jobs.get(video_id, export_id)
    if job is None:
        return _error("no such export", 404)
    return api.json_response({"success": True, **job})


@video_studio_bp.route("/api/videos/<video_id>/exports/<export_id>", methods=["DELETE"])
def cancel_export(video_id, export_id):
    if not encode_jobs.cancel(video_id, export_id):
        return _error("no such export", 404)
    return api.json_response({"success": True})


@video_studio_bp.route("/api/videos/<video_id>/exports/<export_id>/download", methods=["GET"])
def download_export(video_id, export_id):
    def run():
        schema.validate_video_id(video_id)
        path = encode_jobs.download_path(video_id, export_id)
        if path is None or not path.is_file():
            return _error("this export is not ready", 404)
        return send_file(path, as_attachment=True, download_name=path.name)
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>/exports/<export_id>/manifest", methods=["GET"])
def export_manifest(video_id, export_id):
    def run():
        schema.validate_video_id(video_id)
        record = repository.export_manifest(video_id, export_id)
        if record is None:
            return _error("no manifest for this export", 404)
        return api.json_response({"success": True, "manifest": record})
    return _guard(run)


@video_studio_bp.route("/api/videos/<video_id>/exports/record", methods=["POST"])
def record_client_export(video_id):
    """An export the browser encoded itself (WebCodecs): the file stays where
    the user saved it; its manifest is kept with the video."""
    def run():
        schema.validate_video_id(video_id)
        body = _payload()
        document = repository.load(video_id)
        output = schema.normalize_output({**document["output"], **(body.get("output") or {})})
        draft = body.get("manifest") or {}
        frames = int(body.get("frames") or 0)
        record = manifest.build(document, draft, output=output,
                                writer=draft.get("encoder") or {"encoder": "browser"},
                                frames=frames, complete=bool(body.get("complete", True)))
        export_id = "exp_" + schema.new_id("c").split("_", 1)[1]
        repository.record_export(video_id, export_id,
                                 status="done" if record["output"]["complete"] else "failed",
                                 output=output, manifest=record, size=body.get("bytes"))
        return api.json_response({"success": True, "export_id": export_id, "manifest": record})
    return _guard(run)
