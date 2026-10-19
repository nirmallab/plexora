import json

from flask import Blueprint, Response, abort, jsonify, make_response, request
import polars as pl

from plexora import api
from plexora.plugins.gating.server import model as gating_model
# Imported for its side effect as much as anything: importing it registers this
# plugin's table operations, which the routes below name.
from plexora.plugins.gating.server import tableops  # noqa: F401
from plexora.plugins.gating.server.model import PLUGIN_NAME

# template_folder/static_folder make this plugin self-contained: Flask's
# DispatchingJinjaLoader already searches every blueprint's template folder, and
# the blueprint serves its own assets. Both were available all along and unused
# -- core previously had to know where a module's files lived.
gating_bp = Blueprint(
    'gating', __name__,
    template_folder='../templates',
    static_folder='../static',
    static_url_path='/static',
)


def _files(datasource):
    """This plugin's own file storage for a datasource -- where the uploaded
    gates CSV lands. Scoped per plugin rather than written straight into the
    datasource directory, so two plugins cannot overwrite each other's uploads
    and an uninstall knows what belongs to whom."""
    return api.store(datasource, PLUGIN_NAME).directory()


@gating_bp.route('/get_gated_cell_ids', methods=['GET'])
def get_gated_cell_ids():
    datasource = request.args.get('datasource')
    filter = json.loads(request.args.get('filter'))
    start_keys = list(request.args.get('start_keys').split(','))
    resp = gating_model.get_gated_cells(datasource, filter, start_keys)
    return api.json_response(resp)


@gating_bp.route('/get_gating_gmm', methods=['POST'])
def get_gating_gmm():
    post_data = json.loads(request.data)
    channel = post_data['channel']
    datasource = post_data['datasource']
    selection_ids = post_data['selection_ids']
    resp = gating_model.get_gating_gmm(channel, datasource, selection_ids)
    return api.json_response(resp)


@gating_bp.route('/upload_gates', methods=['POST'])
def upload_gates():
    file = request.files['file']
    if file.filename.endswith('.csv') == False:
        abort(422)
    datasource = request.form['datasource']
    file.save(_files(datasource) / 'uploaded_gates.csv')
    resp = jsonify(success=True)
    return resp


@gating_bp.route('/download_gating_csv', methods=['POST'])
def download_gating_csv():
    """The gates themselves: one row per marker, its range, and whether it was
    thresholded.

    This route used to serve a second file as well, behind a `fullCsv` flag --
    the entire cell table with every gated marker rewritten to 1/0 or to a
    kept-or-zeroed intensity. That was megabytes-to-gigabytes of text and had
    to stream; what is left is a row per marker, written in one go.
    """
    datasource = request.form['datasource']
    filename = request.form['filename']

    filter = json.loads(request.form['filter'])
    channels = json.loads(request.form['channels'])

    csv = gating_model.download_gates(datasource, filter, channels)
    return Response(
        csv.write_csv(),
        mimetype="text/csv",
        headers={"Content-disposition":
                     "attachment; filename=" + filename + ".csv"})


@gating_bp.route('/save_gating_list', methods=['POST'])
def save_gating_list():
    post_data = json.loads(request.data)

    datasource = post_data['datasource']
    filter = post_data['filter']
    channels = post_data['channels']

    # DB-only on every save -- the .h5ad file is only ever written to
    # explicitly, via the "Save Gates to AnnData" button (save_gates_to_anndata()
    # below). Writing to the source file on every debounced slider edit was
    # tried and reverted: the user wants edits to stay local/undo-able in the
    # DB until they deliberately commit them to the file.
    gating_model.save_gating_list(datasource, filter, channels)
    # A locked gate is the user's word even against their own slider: the
    # list is saved whole, so a locked marker's row is put back after it.
    reverted = _restore_locked(datasource)

    resp = jsonify(success=True, reverted=reverted)
    return resp


@gating_bp.route('/clear_gates', methods=['POST'])
def clear_gates():
    """The panel's Clear and Clear all: the named markers (every marker when
    `markers` is absent) back to never gated, provenance and all
    (`model.clear_gates`). Other tabs on the project are told, so one holding
    the old gates reloads instead of saving them back over the clear."""
    post_data = json.loads(request.data or b"{}")
    datasource = post_data.get('datasource')
    markers = post_data.get('markers')
    if not datasource or (markers is not None and not isinstance(markers, list)):
        abort(400)
    ds = api.project_data(datasource)
    try:
        cleared, kept = gating_model.clear_gates(ds, markers)
    except KeyError as exc:
        return api.json_response({"error": str(exc).strip("'\"")}), 400
    if cleared:
        api.notify_viewers(datasource, PLUGIN_NAME, "gating.cleared",
                           {"markers": cleared, "origin": post_data.get('origin')})
    return api.json_response({"cleared": cleared, "kept": kept})


def _restore_locked(datasource):
    import pickle

    from plexora.plugins.gating.server import provenance

    rows = gating_model.get_saved_gating_list(datasource) or []
    merged, reverted = provenance.merge_locked(datasource, rows)
    if reverted:
        gating_model._store(datasource).put_state(pickle.dumps(merged, protocol=4))
    return reverted


def _detail_brief(row):
    """The reason and flags of a provenance row, for the status line."""
    detail = row.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = None
    detail = detail if isinstance(detail, dict) else {}
    condition = detail.get("condition") if isinstance(detail.get("condition"), dict) else None
    return {"reason": detail.get("reason"), "flags": detail.get("flags"),
            "condition": ({k: condition.get(k) for k in ("within", "n_positive_outside")}
                          if condition else None)}


@gating_bp.route('/get_gate_provenance', methods=['GET'])
def get_gate_provenance():
    """Where each gate came from (method, status, confidence) -- the marker
    list's tooltips and the status line under the gate."""
    from plexora.plugins.gating.server import provenance

    datasource = request.args.get('datasource')
    rows = provenance.read(datasource)
    slim = {marker: {**{k: row.get(k) for k in ("method", "status", "confidence", "state",
                                               "written_low", "written_high", "session_id",
                                               "timestamp", "note")},
                     **_detail_brief(row)}
            for marker, row in rows.items()}
    return api.json_response({"provenance": slim,
                              "revision": provenance.revision(datasource)})


@gating_bp.route('/set_gate_status', methods=['POST'])
def set_gate_status():
    """Approve, lock, exclude -- or undo any of those -- from the marker's own
    status line. The user's act, so nothing refuses it."""
    from plexora.plugins.gating.server import provenance

    post_data = json.loads(request.data)
    datasource = post_data['datasource']
    marker = post_data['marker']
    status = post_data['status']
    if status not in ("approved", "locked", "excluded", "unlocked", "included",
                      "unapproved"):
        abort(400)
    row = next((r for r in gating_model.get_saved_gating_list(datasource) or []
                if r.get("channel") == marker), None)
    current = (row["gate_start"], row["gate_end"]) if row else None
    before, after = provenance.set_status(datasource, marker, status, principal="viewer",
                                          current=current, note=post_data.get('note'))
    return api.json_response({"marker": marker, "status": after, "previous": before})


@gating_bp.route('/agent_session/<session_id>/control', methods=['POST'])
def agent_session_control(session_id):
    """The viewer's pause / resume / take-over / detach / attach for a gating session that is
    mirroring into this tab. Take-over pauses the session and locks the
    marker under review, so the session skips it."""
    from plexora.agent.sessions import control as session_control
    from plexora.plugins.gating.capabilities_session import record_limit_answers
    from plexora.plugins.gating.server.autogate import engine, provenance, schemas

    post_data = json.loads(request.data or b"{}")
    store = engine.store()
    if not store.exists(session_id):
        abort(404)

    def take_over(post, control):
        marker = post.get('marker')
        datasource = post.get('datasource')
        locked = list(control.get('locked_markers') or [])
        if marker and datasource:
            row = next((r for r in gating_model.get_saved_gating_list(datasource) or []
                        if r.get("channel") == marker), None)
            current = (row["gate_start"], row["gate_end"]) if row else None
            provenance.set_status(datasource, marker, "locked", principal="viewer",
                                  current=current, note="taken over from an agent session")
            locked.append(marker)
        return {"locked_markers": locked}

    try:
        control = session_control.handle(
            store, session_id, post_data,
            tell_tabs=lambda event, record=None, **payload: _tell_tabs(
                session_id, event, record=record, **payload),
            summary_of=engine.summary_of, record_limit_answers=record_limit_answers,
            limit_decisions=schemas.LIMIT_DECISIONS, take_over=take_over,
            replay_mirror=session_control.replayer("gating.session_status", ("gating",),
                                                   notify=api.notify_viewers))
    except session_control.BadRequest:
        abort(400)
    return api.json_response({"session_id": session_id, "control": control})


def _tell_tabs(session_id, event, /, record=None, **payload):
    """A `gating.session` event to every tab on the session's images."""
    from plexora.plugins.gating.server.autogate import engine, events

    try:
        record = record or engine.store().load(session_id)
    except Exception:
        return

    def notify(project, plugin, kind, body):
        return api.notify_viewers(project, plugin, kind, body)

    events.announce(notify, record.get("images") or [], session_id, event, **payload)


@gating_bp.route('/get_saved_gating_list', methods=['GET'])
def get_saved_gating_list():
    datasource = request.args.get('datasource')
    resp = gating_model.get_saved_gating_list(datasource)
    return api.json_response(resp)


@gating_bp.route('/save_gates_to_anndata', methods=['POST'])
def save_gates_to_anndata():
    """Writes only adata.uns[table_name] (lower gate bound per marker, one
    column per image) back into the source .h5ad -- never a full anndata
    rewrite. Gates are derived from the persisted GatingList row (the DB),
    not from anything the client sends -- the DB is the single source of
    truth here, same as CSV download/restore-on-reload.

    Important: gate_active is NOT the right signal for "was this channel
    customized" -- it only ever reflects whichever single marker is
    currently displayed (gatingList.selections is reset on every marker
    switch, by design, for the live single-marker slider/segmentation-
    outline view). The gate_start/gate_end *values* for every OTHER
    previously-gated channel are still correctly persisted though --
    save_gating_list writes them from gating_channels, which is never
    wiped -- so a channel counts as "has a gate" here by comparing its
    stored gate_start/gate_end against its own true full data range
    (get_datasource_description), exactly like the marker dropdown's
    green-dot indicator does client-side (viewerSidebar.js's
    hasCustomGate), not by trusting gate_active."""
    post_data = json.loads(request.data)
    datasource = post_data['datasource']
    table_name = post_data.get('table_name') or 'gates'

    try:
        dataset = api.project_data(datasource)
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400
    # A SpatialData datasource's gates go into the uns of the one table it
    # was imported from, using the same codec -- see anndata_gates._open_group.
    if dataset.source_kind not in ('anndata', 'spatialdata'):
        return jsonify(success=False, error="Not an AnnData or SpatialData datasource"), 400
    # The role, from the project record. This used to be a free-text box on
    # the panel defaulting to the literal 'imageid' -- the plugin asking the
    # user for something core already had a place to store. It is declared in
    # Requires as an optional role now, so the host collects it once and every
    # plugin sees the same answer.
    imageid_column = dataset.schema.image_id
    if not imageid_column:
        return jsonify(success=False, error="No image ID column recorded for this project",
                       needs="role:image_id"), 400

    # Only the lower bound travels: `uns` stores one threshold per marker.
    active_gates = {channel: low for channel, (low, _high)
                    in gating_model.active_gates(dataset).items()}

    payload = {
        "image_id": datasource,
        "gates": active_gates,
        "table_name": table_name,
        "imageid_column": imageid_column,
    }
    # Markers the Single Cell Classifier decides travel with the gates:
    # their calls and scores as layers (or obsm), their examples in uns.
    from plexora.plugins.gating.server.classifier import source_write

    classifier = source_write.payload_for(dataset)
    if classifier is not None:
        payload["classifier"] = classifier
    result = dataset.table.run("gating.save_gates", payload)
    if not result.get("ok"):
        return jsonify(success=False, error=result.get("message", "")), 400

    return jsonify(success=True, **{k: v for k, v in result.items() if k != "ok"})


@gating_bp.route('/get_gates_from_anndata', methods=['GET'])
def get_gates_from_anndata():
    """Read-only counterpart of /save_gates_to_anndata -- lets the sidebar
    pick up gates already present in adata.uns[table_name] (e.g. set up
    outside Plexora before import) the first time a datasource with no
    Plexora-side saved gating list is opened."""
    datasource = request.args.get('datasource')
    table_name = request.args.get('table_name') or 'gates'

    try:
        dataset = api.project_data(datasource)
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400
    # A SpatialData datasource's gates go into the uns of the one table it
    # was imported from, using the same codec -- see anndata_gates._open_group.
    if dataset.source_kind not in ('anndata', 'spatialdata'):
        return jsonify(success=False, error="Not an AnnData or SpatialData datasource"), 400
    imageid_column = dataset.schema.image_id
    if not imageid_column:
        # Read-only path: no image id recorded means no gates to find, which
        # is an ordinary empty result rather than something to demand of the
        # user. The save path asks; this one does not.
        return jsonify(success=True, image_id=datasource, gates={})

    result = dataset.table.run("gating.load_gates", {
        "image_id": datasource,
        "table_name": table_name,
        "imageid_column": imageid_column,
    })
    if not result.get("ok"):
        return jsonify(success=False, error=result.get("message", "")), 400

    return jsonify(success=True, **{k: v for k, v in result.items() if k != "ok"})


@gating_bp.route('/get_uploaded_gating_csv_values', methods=['GET'])
def get_gating_csv_values():
    datasource = request.args.get('datasource')
    file_path = _files(datasource) / 'uploaded_gates.csv'
    if file_path.is_file() == False:
        abort(422)
    csv = pl.read_csv(file_path)
    obj = csv.to_dicts()
    return api.json_response(obj)


# -- the Single Cell Classifier (classifier/) ------------------------------------
#
# The panel's second mode. Every route is a thin wrapper over
# classifier/panel.py, runs.py and export.py; the run itself is a capability
# job (`gating.classifier_run`) so it reports progress and can be cancelled,
# and the panel follows it through `jobs/<id>`.

_ROUTE_SESSION = {}


def _agent_session():
    """One data session for the routes that start jobs, made on first use
    and kept."""
    import threading

    from plexora.agent import AgentSession

    lock = _ROUTE_SESSION.setdefault("lock", threading.Lock())
    with lock:
        if "session" not in _ROUTE_SESSION:
            _ROUTE_SESSION["session"] = AgentSession(table_limit=2)
        return _ROUTE_SESSION["session"]


def _invoke(tool, arguments):
    """A gating capability on the viewer's behalf (a job answers `{job_id}`)."""
    import dataclasses

    from plexora.agent import registry
    from plexora.agent.policy import Policy

    try:
        registry.get("gating.classifier_run")
    except Exception:
        registry.discover(["gating"])
    policy = dataclasses.replace(Policy.from_flags(), principal="viewer")
    answer = registry.invoke(_agent_session(), tool, arguments, policy=policy,
                             notify=lambda project, plugin, kind, body:
                             api.notify_viewers(project, plugin, kind, body))
    if not answer["ok"]:
        return api.json_response({"ok": False, "error": answer["error"]}), 400
    return api.json_response({"ok": True, **(answer["result"] or {})})


def _gating_job(job_id):
    """A job record, only when it is one of gating's (the panel reads nothing else)."""
    from plexora.agent import jobs

    record = jobs.store().get(job_id)
    if record is None or not str(record.get("capability") or "").startswith("gating."):
        abort(404)
    return record


@gating_bp.route('/jobs/<job_id>', methods=['GET'])
def job_state(job_id):
    record = _gating_job(job_id)
    return api.json_response({"ok": True, "job": {k: record.get(k) for k in (
        "job_id", "status", "progress", "project", "capability", "error", "result")}})


@gating_bp.route('/jobs/<job_id>/cancel', methods=['POST'])
def job_cancel(job_id):
    _gating_job(job_id)
    return _invoke("job_cancel", {"job_id": job_id})


def _body():
    try:
        return json.loads(request.data or b"{}")
    except ValueError:
        abort(400)


def _classifier_ds(datasource):
    if not datasource:
        abort(400)
    try:
        return api.project_data(datasource)
    except KeyError:
        abort(404)


def _classifier_answer(work, datasource, marker=None, notify=True):
    """Run `work()`; a refusal is a 400 with the panel's message. A change is
    told to the project's other tabs."""
    from plexora.plugins.gating.server.classifier import ClassifierRefused

    try:
        result = work()
    except ClassifierRefused as exc:
        return api.json_response({"ok": False, "error": {"message": str(exc)}}), 400
    if notify:
        api.notify_viewers(datasource, PLUGIN_NAME, "classifier.changed", {"marker": marker})
    return api.json_response({"ok": True, **(result or {})})


@gating_bp.route('/classifier/state', methods=['GET'])
def classifier_state():
    from plexora.plugins.gating.server.classifier import panel

    datasource = request.args.get('datasource')
    marker = request.args.get('marker') or None
    ds = _classifier_ds(datasource)
    return _classifier_answer(lambda: panel.state(ds, marker), datasource, notify=False)


@gating_bp.route('/classifier/mode', methods=['POST'])
def classifier_mode():
    from plexora.plugins.gating.server.classifier import panel

    body = _body()
    ds = _classifier_ds(body.get('datasource'))
    return _classifier_answer(lambda: {"mode": panel.set_mode(ds, body.get('mode'))},
                              ds.name, notify=False)


@gating_bp.route('/classifier/cell_at', methods=['GET'])
def classifier_cell_at():
    from plexora.plugins.gating.server.classifier import panel

    ds = _classifier_ds(request.args.get('datasource'))
    try:
        x, y = float(request.args['x']), float(request.args['y'])
        radius = float(request.args.get('radius') or panel.PICK_RADIUS_PX)
    except (KeyError, ValueError):
        abort(400)
    return _classifier_answer(lambda: panel.cell_at(ds, x, y, radius), ds.name, notify=False)


@gating_bp.route('/classifier/review', methods=['GET'])
def classifier_review():
    from plexora.plugins.gating.server.classifier import panel

    ds = _classifier_ds(request.args.get('datasource'))
    try:
        count = int(request.args.get('count') or panel.REVIEW_COUNT)
    except ValueError:
        abort(400)
    return _classifier_answer(
        lambda: panel.review_cells(ds, request.args.get('marker'), count), ds.name,
        notify=False)


@gating_bp.route('/classifier/annotate', methods=['POST'])
def classifier_annotate():
    from plexora.plugins.gating.server.classifier import panel

    body = _body()
    ds = _classifier_ds(body.get('datasource'))
    marker = body.get('marker')
    return _classifier_answer(
        lambda: panel.annotate(ds, marker, body.get('cell_id'), body.get('label'),
                               toggle=body.get('toggle', True) is not False),
        ds.name, marker)


@gating_bp.route('/classifier/examples/clear', methods=['POST'])
def classifier_clear_examples():
    from plexora.plugins.gating.server.classifier import panel

    body = _body()
    ds = _classifier_ds(body.get('datasource'))
    markers = body.get('markers')
    if markers is not None and not isinstance(markers, list):
        abort(400)
    return _classifier_answer(lambda: panel.clear_examples(ds, markers), ds.name)


@gating_bp.route('/classifier/run', methods=['POST'])
def classifier_run():
    """Start a run (a job). One at a time per project: while one runs, a
    second press answers with the running job instead of starting another."""
    from plexora.plugins.gating.server.classifier import ClassifierRefused, runs

    body = _body()
    ds = _classifier_ds(body.get('datasource'))
    marker = body.get('marker')
    held = runs.running(ds.name)
    if held is not None:
        return api.json_response({"ok": True, "job_id": held["job_id"], "running": held,
                                  "already_running": True})
    auto = body.get('auto') is True
    try:
        if auto:
            # The wand picks its own examples in the job; only the marker
            # is checked here.
            if marker not in ds.table.markers:
                raise ClassifierRefused(f"{marker!r} is not a marker of this project")
        else:
            runs.check_ready(ds, marker)
    except ClassifierRefused as exc:
        return api.json_response({"ok": False, "error": {"message": str(exc)}}), 400
    return _invoke("classifier_run", {"project": ds.name, "marker": marker, "auto_pick": auto,
                                      "pixels": body.get('pixels', True) is not False})


@gating_bp.route('/classifier/revert', methods=['POST'])
def classifier_revert():
    from plexora.plugins.gating.server.classifier import panel, runs

    body = _body()
    ds = _classifier_ds(body.get('datasource'))
    marker = body.get('marker')
    return _classifier_answer(lambda: (panel.marker_of(ds, marker), runs.revert(ds, marker))[1],
                              ds.name, marker)


@gating_bp.route('/classifier/reset', methods=['POST'])
def classifier_reset():
    from plexora.plugins.gating.server.classifier import runs

    body = _body()
    ds = _classifier_ds(body.get('datasource'))
    return _classifier_answer(lambda: runs.reset(ds), ds.name)


@gating_bp.route('/classifier/calls', methods=['GET'])
def classifier_calls():
    """One byte per table row (the order /get_all_cells sends ids in): 0
    negative, 1 positive, 2 not classified, 3 uncertain. Headers carry the
    state (ready, stale, not_run) and the counts."""
    import gzip

    from plexora.plugins.gating.server.classifier import calls

    ds = _classifier_ds(request.args.get('datasource'))
    marker = request.args.get('marker')
    if marker not in ds.table.markers:
        abort(404)
    codes, info = calls.viewer_codes(ds, marker)
    content = gzip.compress(codes.tobytes('C'))
    response = make_response(content)
    response.headers.set('Content-Type', 'application/octet-stream')
    response.headers['Content-length'] = len(content)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Classifier-State'] = info["state"]
    response.headers['X-Classifier-Run'] = info["run"]
    response.headers['X-Classifier-Positive'] = info["positive"]
    response.headers['X-Classifier-Eligible'] = info["eligible"]
    response.headers['X-Classifier-Uncertain'] = info["uncertain"]
    response.headers['X-Classifier-Not-Classified'] = info["not_classified"]
    return response


@gating_bp.route('/classifier/scores', methods=['GET'])
def classifier_scores():
    """What the hover card reads: the run's score per table row (float32,
    NaN where not classified), then each row's agreement as a whole percent
    (uint8) -- one block of 5 bytes a row, gzipped. Empty without a run."""
    import gzip

    import numpy as np

    from plexora.plugins.gating.server.classifier import calls

    ds = _classifier_ds(request.args.get('datasource'))
    marker = request.args.get('marker')
    if marker not in ds.table.markers:
        abort(404)
    record, arrays = calls.run_arrays(ds, marker)
    body = b""
    if arrays is not None:
        scores = np.where(arrays["calls"] >= 0, arrays["scores"], np.nan).astype(np.float32)
        agreement = arrays.get("agreement")
        if agreement is None:
            agreement = np.zeros(scores.shape[0], dtype=np.uint8)
        body = scores.tobytes() + np.asarray(agreement, dtype=np.uint8).tobytes()
    content = gzip.compress(body)
    response = make_response(content)
    response.headers.set('Content-Type', 'application/octet-stream')
    response.headers['Content-length'] = len(content)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Classifier-Run'] = record["run_id"] if record else ""
    response.headers['X-Classifier-Score-Name'] = (record or {}).get("score_name") or "score"
    return response


def _download(chunks, filename, mimetype):
    """Stream `chunks` (text) as an attachment, gzipped when `gz=1`."""
    import zlib

    if request.args.get('gz') in ('1', 'true'):
        def zipped():
            packer = zlib.compressobj(6, zlib.DEFLATED, 31)
            for chunk in chunks:
                data = packer.compress(chunk.encode('utf-8'))
                if data:
                    yield data
            yield packer.flush()
        return Response(zipped(), mimetype='application/gzip', headers={
            "Content-disposition": f"attachment; filename={filename}.gz"})
    return Response((c.encode('utf-8') for c in chunks), mimetype=mimetype, headers={
        "Content-disposition": f"attachment; filename={filename}"})


@gating_bp.route('/classifier/export/<kind>.csv', methods=['GET'])
def classifier_export_csv(kind):
    from plexora.plugins.gating.server.classifier import export, store

    if kind not in ('binary', 'scores'):
        abort(404)
    ds = _classifier_ds(request.args.get('datasource'))
    markers, chunks = export.stream_csv(ds, kind)
    if not markers:
        return api.json_response({"ok": False, "error": {
            "message": "No marker has a classifier run to download"}}), 409
    return _download(chunks, f"{store.safe_name(ds.name)}_classifier_{kind}.csv", 'text/csv')


@gating_bp.route('/classifier/export/annotations.json', methods=['GET'])
def classifier_export_annotations():
    from plexora.plugins.gating.server.classifier import export, store

    ds = _classifier_ds(request.args.get('datasource'))
    text = json.dumps(export.annotations_document(ds), indent=1)
    return _download([text], f"{store.safe_name(ds.name)}_classifier_annotations.json",
                     'application/json')


#: The largest annotations file the panel accepts.
MAX_ANNOTATIONS_BYTES = 64 * 1024 * 1024


@gating_bp.route('/classifier/annotations/preview', methods=['POST'])
def classifier_annotations_preview():
    from plexora.plugins.gating.server.classifier import ClassifierRefused, export

    ds = _classifier_ds(request.form.get('datasource'))
    upload = request.files.get('file')
    if upload is None:
        abort(400)
    data = upload.read(MAX_ANNOTATIONS_BYTES + 1)
    if len(data) > MAX_ANNOTATIONS_BYTES:
        return api.json_response({"ok": False, "error": {
            "message": "This annotations file is too large"}}), 400
    try:
        document = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return api.json_response({"ok": False, "error": {
            "message": "This file is not JSON"}}), 400
    try:
        report = export.preview(ds, document)
    except ClassifierRefused as exc:
        return api.json_response({"ok": False, "error": {"message": str(exc)}}), 400
    return api.json_response({"ok": True, **report})


@gating_bp.route('/classifier/annotations/apply', methods=['POST'])
def classifier_annotations_apply():
    from plexora.plugins.gating.server.classifier import export

    body = _body()
    ds = _classifier_ds(body.get('datasource'))
    return _classifier_answer(lambda: export.apply(ds, body.get('token'), body.get('mode')),
                              ds.name)
