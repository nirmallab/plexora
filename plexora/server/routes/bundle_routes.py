"""Projects as one `.plexora` file, over HTTP (server/models/bundles.py).

The export and import dialogs (client views/projectExport.js,
views/projectImport.js) work through these:

    GET    /projects/export/estimate?ids=a,b[&include=][&dataset=]   sizes, from stat calls
    POST   /projects/export {ids|names, include, dest?, dataset?}   -> {job, ...}
    GET    /projects/export/<job>                                   status
    DELETE /projects/export/<job>  (POST .../cancel)                stop; the .part goes
    GET    /projects/export/<job>/file                              the file, with Range
    POST   /projects/export/plan {ids, include}                     the website's members
    POST   /projects/export/manifest {manifest, sums}               ... and its manifest
    POST   /projects/import/preview {path} | {upload} | {manifest} | a file
    POST   /projects/import {path|upload, choices, location}        -> {job, ...}
    GET    /projects/import/<job>, DELETE (POST .../cancel)
    POST   /projects/<name>/copy {location?}                        "Make a copy" -> {job}
    POST   /projects/<name>/find_missing {folder}                   one folder searched

Long work is a background task (`layer_jobs`), polled; the website's page
answers the polls itself while its Python works (the task's `mirror`), which
is why each cancel has a POST form too. A bundle is never read into memory:
an upload is streamed to a file first, and a download is served from one.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import uuid
from pathlib import Path

from flask import jsonify, request, send_file

from plexora import app

_TOKEN = re.compile(r"^[0-9a-f]{12,32}$")


def _ids(value) -> list:
    """Ids or names, from a list or a comma-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _refused(error, status=400):
    return jsonify(error=str(error).strip("'\"")), status


def _upload_path(token):
    from plexora.server.models import bundles

    if not isinstance(token, str) or not _TOKEN.match(token):
        return None
    folder = bundles.staging_root() / f"upload-{token}"
    found = sorted(folder.glob(f"*{bundles.SUFFIX}")) if folder.is_dir() else []
    return found[0] if found else None


def _save_upload():
    """The request's `.plexora` (a `file` field, or the body) written to a
    file of its own, a chunk at a time: (token, path), or (None, None)."""
    from plexora.server.models import bundles

    upload = request.files.get('file')
    if upload is None and not (request.mimetype or '').startswith(('application/zip', 'application/octet-stream')):
        return None, None
    token = uuid.uuid4().hex[:16]
    folder = bundles.staging_root() / f"upload-{token}"
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(getattr(upload, 'filename', '') or f"upload{bundles.SUFFIX}").name or f"upload{bundles.SUFFIX}"
    if not name.lower().endswith(bundles.SUFFIX):
        name += bundles.SUFFIX
    target = folder / bundles._safe_name(name)
    if upload is not None:
        upload.save(str(target))
    else:
        with target.open('wb') as out:
            shutil.copyfileobj(request.stream, out, bundles.CHUNK)
    return token, target


# -- export ------------------------------------------------------------------


@app.route('/projects/export/estimate')
def projects_export_estimate():
    """Sizes for the export dialog: `{projects, rows: {state, tables,
    captures, figures, images: {size, count, items}, derived}, include,
    total, caches, datasets, figures, left_out}`. Stat calls only."""
    from plexora.server.models import bundles

    ids = _ids(request.args.get('ids')) + _ids(request.args.get('names'))
    try:
        return jsonify(bundles.estimate(ids, request.args.get('include') or None,
                                        dataset=request.args.get('dataset') or None))
    except KeyError as error:
        return _refused(error, 404)
    except ValueError as error:
        return _refused(error)


@app.route('/projects/export', methods=['POST'])
def projects_export_start():
    """Start an export: `{ids|names, include, dest?, dataset?}` -> the job's
    first status. Without `dest` (the browser, a server reached from
    elsewhere) the file is fetched from `file.url` when it is ready."""
    from plexora.server.models import bundles

    body = request.get_json(silent=True) or {}
    ids = _ids(body.get('ids')) + _ids(body.get('names'))
    dest = body.get('dest') or None
    try:
        status = bundles.start_export(ids, body.get('include'), dest, dataset=body.get('dataset') or None)
    except KeyError as error:
        return _refused(error, 404)
    except ValueError as error:
        return _refused(error)
    return jsonify(status), 202


@app.route('/projects/export/plan', methods=['POST'])
def projects_export_plan():
    """The website's seam (bundles.plan_for_page): what to write, each
    member's source in this Python's filesystem, the tokenised entries and
    the manifest skeleton. The page's worker moves the bytes."""
    from plexora.server.models import bundles

    body = request.get_json(silent=True) or {}
    ids = _ids(body.get('ids')) + _ids(body.get('names'))
    try:
        return jsonify(bundles.plan_for_page(ids, body.get('include'), dataset=body.get('dataset') or None,
                                             work_dir=tempfile.mkdtemp(prefix="plexora-bundle-")))
    except KeyError as error:
        return _refused(error, 404)
    except ValueError as error:
        return _refused(error)


@app.route('/projects/export/manifest', methods=['POST'])
def projects_export_manifest():
    """`{manifest, sums: {bundle_path: sha256}}` -> the finished manifest the
    page writes first (bundles.finish_manifest)."""
    from plexora.server.models import bundles

    body = request.get_json(silent=True) or {}
    if not isinstance(body.get('manifest'), dict) or not isinstance(body.get('sums'), dict):
        return _refused("Send the manifest and the sums.")
    return jsonify(bundles.finish_manifest(body['manifest'], body['sums']))


@app.route('/projects/export/<string:job>', methods=['GET'])
def projects_export_status(job):
    from plexora.server.models import bundles

    try:
        return jsonify(bundles.job_status(job))
    except KeyError:
        return _refused("No such export.", 404)


@app.route('/projects/export/<string:job>', methods=['DELETE'])
def projects_export_cancel(job):
    """Stop an export; its `.part` file (and a staged file) is removed."""
    from plexora.server.models import bundles

    try:
        bundles.cancel_job(job)
    except KeyError:
        return _refused("No such export.", 404)
    return jsonify(cancelled=True, job=job)


@app.route('/projects/export/<string:job>/cancel', methods=['POST'])
def projects_export_cancel_post(job):
    """The same, for the website's page (its mirrored cancels are POSTs)."""
    return projects_export_cancel(job)


@app.route('/projects/export/<string:job>/file')
def projects_export_file(job):
    """A finished export written here (no `dest`), served from its file with
    Range requests, for a server the user's browser is not beside."""
    from plexora.server.models import bundles

    path = bundles.job_file(job)
    if path is None:
        return _refused("That file is not ready.", 404)
    return send_file(path, mimetype="application/zip", as_attachment=True, download_name=path.name,
                     conditional=True)


# -- import ------------------------------------------------------------------


@app.route('/projects/import/preview', methods=['POST'])
def projects_import_preview():
    """What importing a bundle would do: `{path}` on this server, `{upload}`
    from an earlier preview, `{manifest}` the page read itself, or the file
    in the request (`file`, or the body) -- which is kept, and named by the
    `upload` in the answer for the import to use."""
    from plexora.server.models import bundles

    body = request.get_json(silent=True) or {}
    location = body.get('location') or request.args.get('location') or None
    token = None
    try:
        if isinstance(body.get('manifest'), dict):
            return jsonify(bundles.preview_manifest(body['manifest'], location,
                                                    bundle_path=body.get('path') or None))
        if body.get('upload'):
            token, path = body['upload'], _upload_path(body['upload'])
            if path is None:
                return _refused("That upload is gone; choose the file again.", 404)
        elif body.get('path'):
            path = Path(str(body['path'])).expanduser()
        else:
            token, path = _save_upload()
            if path is None:
                return _refused("Send a path, an upload, a manifest or the file.")
        answer = bundles.preview(path, location)
    except ValueError as error:
        if token and not body.get('upload'):
            shutil.rmtree(bundles.staging_root() / f"upload-{token}", ignore_errors=True)
        return _refused(error)
    answer['path'] = None if token else str(path)
    answer['upload'] = token
    return jsonify(answer)


@app.route('/projects/import', methods=['POST'])
def projects_import_start():
    """Start an import: `{path|upload, choices: {id: action}, location}` ->
    the job's first status. Actions: import, skip, update, replace,
    keep_both; each project's status's default otherwise."""
    from plexora.server.models import bundles

    body = request.get_json(silent=True)
    upload = None
    if body is None:
        upload, path = _save_upload()
        body = {k: v for k, v in request.form.items()}
        if path is None:
            return _refused("Send a path, an upload or the file.")
    elif body.get('upload'):
        upload, path = body['upload'], _upload_path(body['upload'])
        if path is None:
            return _refused("That upload is gone; choose the file again.", 404)
    elif body.get('path'):
        path = Path(str(body['path'])).expanduser()
    else:
        return _refused("Send a path, an upload or the file.")
    choices = body.get('choices') if isinstance(body.get('choices'), dict) else None
    try:
        status = bundles.start_import(path, choices, body.get('location') or None, cleanup=bool(upload))
    except ValueError as error:
        return _refused(error)
    return jsonify(status), 202


@app.route('/projects/import/<string:job>', methods=['GET'])
def projects_import_status(job):
    from plexora.server.models import bundles

    try:
        return jsonify(bundles.job_status(job))
    except KeyError:
        return _refused("No such import.", 404)


@app.route('/projects/import/<string:job>', methods=['DELETE'])
def projects_import_cancel(job):
    """Stop an import: nothing of it stays (its staging folder goes, the
    registry is never written)."""
    from plexora.server.models import bundles

    try:
        bundles.cancel_job(job)
    except KeyError:
        return _refused("No such import.", 404)
    return jsonify(cancelled=True, job=job)


@app.route('/projects/import/<string:job>/cancel', methods=['POST'])
def projects_import_cancel_post(job):
    return projects_import_cancel(job)


# -- one project -------------------------------------------------------------


@app.route('/projects/<string:name>/copy', methods=['POST'])
def project_make_copy(name):
    """"Make a copy": the project as a new one of the user's own, in
    `location` or where new projects go -- a shared project too. A job,
    polled at /projects/import/<job>."""
    from plexora.server.models import bundles

    body = request.get_json(silent=True) or {}
    try:
        status = bundles.start_copy(name, body.get('location') or None)
    except KeyError:
        return _refused(f"There is no project called {name}.", 404)
    except ValueError as error:
        return _refused(error)
    return jsonify(status), 202


@app.route('/projects/<string:name>/find_missing', methods=['POST'])
def project_find_missing(name):
    """"Find missing images...": look for the project's missing files in the
    one folder the user picked (`{folder}`), and nowhere else.
    `{found, missing}`."""
    from plexora.server.utils import relink

    body = request.get_json(silent=True) or {}
    folder = body.get('folder')
    if not isinstance(folder, str) or not folder.strip() or not Path(folder).expanduser().is_dir():
        return _refused("Choose a folder.")
    try:
        return jsonify(relink.find_in_folder(name, Path(folder).expanduser()))
    except KeyError:
        return _refused(f"There is no project called {name}.", 404)
