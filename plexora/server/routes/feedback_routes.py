"""Bug reports and feature requests from the navbar's feedback menu
(client services/feedbackMenu.js, views/feedbackDialog.js), sent to the
BioCognia platform (`POST /v1/feedback`), where staff read them.

- `GET  /feedback/context` -- what the form needs before it is filled in:
  whether this device is signed in (so the platform will reply to the
  account's address), the version and build, the platform, and the platform
  API address the page may send to itself if this server cannot. Never the
  email: the page asks `GET /settings/license/account` for that, the one
  route that may carry it.
- `POST /feedback/submit` -- multipart: `report` (JSON: kind, description,
  diagnostics, and email when not signed in) and up to three `files`. Sent on
  with this device's certificate and binding, so the proof of who is asking
  never enters the page, then each file with the upload token the platform
  answered. `{id, files: [{name, ok, error?}], upload}` -- the upload token
  so the page can send a file that failed again.

When this server cannot reach the platform -- a data node with no internet,
an older `biocognia` without `Licensing.feedback`, offline mode -- it says so
(`unreachable`, or `client_too_old`), and the page sends the report itself. Nothing here stores the report, and the body, which may hold the
person's email, is never logged (the rule at license_routes.py's head).

On the website the page sends everything itself (it holds the licence there),
so these answer only that.
"""

from __future__ import annotations

import json
import os
import platform

from flask import Blueprint, jsonify, request

feedback_bp = Blueprint("feedback", __name__)

#: The whole multipart body: three 5 MB files and the report.
MAX_BODY = 16 * 1024 * 1024
MAX_FILES = 3
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_REPORT = 64 * 1024


def _on_the_website() -> bool:
    return os.environ.get("PLEXORA_BACKEND") == "browser"


def _refuse(code: str, error: str, status: int):
    reply = jsonify(success=False, code=code, error=error)
    reply.status_code = status
    reply.headers["Cache-Control"] = "no-store"
    return reply


def _api_base() -> str:
    """The platform's /v1, as this server is configured to reach it, or ""."""
    try:
        from biocognia import store

        base = store.server_url() or ""
    except Exception:
        base = ""
    return f"{base.rstrip('/')}/v1" if base else ""


def _signed_in() -> bool:
    """Whether a device certificate is held here, so the platform will reply to
    the account's address. No network."""
    try:
        from plexora.licensing import LICENSING

        state = LICENSING.current(network=False)
    except Exception:
        return False
    return bool(getattr(state, "certificate", None)) and getattr(state, "source", None) in ("cache", "token") \
        and getattr(state, "certificate_type", None) in (None, "env")


@feedback_bp.route("/feedback/context", methods=["GET"])
def feedback_context():
    from plexora import __version__
    from plexora._build import build_id

    reply = jsonify(success=True, signed_in=_signed_in(), version=__version__, build=build_id(),
                    platform=platform.platform(terse=True), python=platform.python_version(),
                    website=_on_the_website(), api=_api_base(),
                    relay=not _on_the_website() and callable(getattr(_licensing(), "feedback", None)))
    reply.headers["Cache-Control"] = "no-store"
    return reply


def _licensing():
    try:
        from plexora.licensing import LICENSING

        return LICENSING
    except Exception:
        return None


@feedback_bp.route("/feedback/submit", methods=["POST"])
def feedback_submit():
    if _on_the_website():
        return _refuse("website_page", "On the website the page sends feedback itself.", 409)
    # This machine only, unless the server has an auth token, as for the
    # account (license_routes.py): a neighbour who can reach the tiles must not
    # send reports in this licence holder's name. Refused, the page sends the
    # report itself, from the address typed in.
    from plexora.server.routes.license_routes import _local_or_token

    refused = _local_or_token()
    if refused is not None:
        return refused
    if (request.content_length or 0) > MAX_BODY:
        return _refuse("too_large", "Attach at most three files of 5 MB each.", 413)
    licensing = _licensing()
    send = getattr(licensing, "feedback", None)
    if send is None:
        # `Licensing.feedback` arrived after biocognia 0.1.2: the page sends
        # the report itself.
        return _refuse("client_too_old", "This Plexora's BioCognia client cannot send feedback.", 501)
    try:
        report = json.loads(request.form.get("report") or "{}")
    except ValueError:
        report = None
    if not isinstance(report, dict) or len(request.form.get("report") or "") > MAX_REPORT:
        return _refuse("invalid_request", "The report could not be read.", 400)
    files = request.files.getlist("files")
    if len(files) > MAX_FILES:
        return _refuse("invalid_request", "Attach at most three files.", 400)
    blobs = []
    for upload in files:
        data = upload.stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            return _refuse("invalid_request", f"{upload.filename or 'A file'} is over 5 MB.", 400)
        blobs.append((os.path.basename(upload.filename or "file"), data))
    report = {key: report.get(key) for key in ("kind", "description", "diagnostics", "email")}

    from biocognia.errors import LicenseError

    try:
        created = send(report)
    except LicenseError as exc:
        code = getattr(exc, "code", None) or "license_error"
        # What the platform refused stays a refusal the person can fix; the
        # rest (unreachable, offline, no server, a 5xx) the page sends itself.
        if code in ("invalid_request", "rate_limited", "forged_certificate"):
            return _refuse(code, str(exc), 429 if code == "rate_limited" else 400)
        return _refuse("unreachable", str(exc), 502)

    results = []
    token = (created.get("upload") or {}).get("token") or ""
    attach = getattr(licensing, "feedback_attachment", None)
    for name, data in blobs:
        try:
            if attach is None:
                raise LicenseError("This Plexora's BioCognia client cannot send files.")
            attach(created["id"], token, name, data)
            results.append({"name": name, "ok": True})
        except LicenseError as exc:
            results.append({"name": name, "ok": False, "error": str(exc)})
    # The upload token too, so a file that did not go can be sent again from
    # the page: it adds files to this one report, for a few minutes.
    reply = jsonify(success=True, id=created["id"], files=results, upload=created.get("upload") or {})
    reply.status_code = 201
    reply.headers["Cache-Control"] = "no-store"
    return reply
