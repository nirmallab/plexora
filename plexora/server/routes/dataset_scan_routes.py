"""A dataset from a folder, over HTTP: scan, correct, create.

The New Dataset dialog's routes. A scan is a background task the dialog polls
(`GET /datasets/scan/<token>`); every poll answers with the scan's progress
AND its current plan, so the table fills in while the walk is still going.
Corrections are posted whole (`/plan`) and kept on the scan, so the next poll,
the saved spec and the build all see the same plan.

`/datasets/scan` is a static rule, so werkzeug matches it ahead of
`/datasets/<dataset_id>` -- tests/test_dataset_scan_routes.py holds it to that.
"""

from flask import Response, jsonify, request

from plexora import app
from plexora.server.models import dataset_build, dataset_scan
from plexora.server.routes.import_routes import _json_errors, tidy_pick


def _unknown(token):
    return jsonify(error=f"No scan {token}; it may have expired. Scan the folder again."), 404


def _payload():
    return request.get_json(silent=True) or {}


@app.route('/datasets/scan', methods=['POST'])
@_json_errors
def dataset_scan_start():
    payload = _payload()
    node = str(payload.get("node") or "").strip() or None
    # A path on a node is that machine's spelling, never tidied against this
    # one's filesystem.
    paths = [str(path).strip() if node else tidy_pick(path)
             for path in payload.get("paths") or ()]
    paths = [path for path in paths if path]
    try:
        found = dataset_scan.start_scan(paths, token=payload.get("token") or None,
                                        limits=payload.get("limits"), node=node)
    except KeyError:
        return _unknown(payload.get("token"))
    except (ValueError, ConnectionError) as error:
        # ConnectionError: a files-only server this Plexora is not connected
        # to, which says so in a sentence (`sftp.SFTPNotConnected`).
        return jsonify(error=str(error)), 400
    return jsonify(found), 202


@app.route('/datasets/scan/<token>')
@_json_errors
def dataset_scan_get(token):
    try:
        return jsonify(dataset_scan.snapshot(token))
    except KeyError:
        return _unknown(token)


@app.route('/datasets/scan/<token>/plan', methods=['POST'])
@_json_errors
def dataset_scan_plan(token):
    try:
        dataset_scan.set_overrides(token, _payload().get("overrides") or {})
        return jsonify(dataset_scan.snapshot(token, verified=True))
    except KeyError:
        return _unknown(token)
    except ValueError as error:
        return jsonify(error=str(error)), 400


@app.route('/datasets/scan/<token>/cancel', methods=['POST'])
@_json_errors
def dataset_scan_cancel(token):
    try:
        stopped = dataset_scan.cancel_scan(token)
    except KeyError:
        return _unknown(token)
    return jsonify(cancelled=True, stopped=stopped)


@app.route('/datasets/scan/<token>/spec')
@_json_errors
def dataset_scan_spec(token):
    import json

    try:
        plan = dataset_scan.plan_for(token)
    except KeyError:
        return _unknown(token)
    name = (request.args.get("name") or "").strip() or "Dataset"
    document = dataset_build.spec_document(plan, name,
                                           request.args.get("description") or "")
    return Response(json.dumps(document, indent=2), mimetype="application/json")


@app.route('/datasets/scan/<token>/create', methods=['POST'])
@_json_errors
def dataset_scan_create(token):
    payload = _payload()
    try:
        if payload.get("overrides") is not None:
            dataset_scan.set_overrides(token, payload["overrides"])
        plan = dataset_scan.plan_for(token)
    except KeyError:
        return _unknown(token)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    if dataset_scan.status(token)["pending"]:
        return jsonify(error="The folder is still being read."), 409
    only = payload.get("only")
    if only is not None and not isinstance(only, list):
        return jsonify(error="`only` is a list of project ids"), 400
    try:
        found = dataset_build.start_build(
            token, plan, name=payload.get("name"),
            description=payload.get("description") or "",
            exist_ok=bool(payload.get("exist_ok")), only=only)
    except FileExistsError as error:
        return jsonify(error=str(error), taken=True), 409
    except ValueError as error:
        return jsonify(error=str(error)), 400
    return jsonify(found), 202


@app.route('/datasets/scan/<token>/create')
@_json_errors
def dataset_scan_create_status(token):
    try:
        return jsonify(dataset_build.build_status(token))
    except KeyError:
        return jsonify(error=f"Nothing is being created for {token}."), 404


@app.route('/datasets/scan/<token>/create/cancel', methods=['POST'])
@_json_errors
def dataset_scan_create_cancel(token):
    dataset_build.cancel_build(token)
    return jsonify(cancelled=True)
