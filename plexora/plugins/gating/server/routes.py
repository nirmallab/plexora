import json

from flask import Blueprint, Response, abort, jsonify, request, stream_with_context
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
    datasource = request.form['datasource']
    filename = request.form['filename']

    filter = json.loads(request.form['filter'])
    channels = json.loads(request.form['channels'])
    selection_ids = json.loads(request.form['selection_ids'])
    fullCsv = json.loads(request.form['fullCsv'])
    encoding = request.form['encoding']
    if fullCsv:
        # A stream operation, so the chunking happens wherever the table is and
        # this route only forwards what arrives -- see model.stream_csv.
        chunks = api.dataset(datasource).table.stream("gating.export_csv", {
            "gates": filter,
            "channels": channels,
            "selection_ids": selection_ids,
            "encoding": encoding,
        })
        return Response(
            stream_with_context(chunks),
            mimetype="text/csv",
            headers={"Content-disposition":
                         "attachment; filename=" + filename + ".csv"})
    else:
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

    resp = jsonify(success=True)
    return resp


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
        dataset = api.dataset(datasource)
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

    saved_rows = gating_model.get_saved_gating_list(datasource) or []
    description = dataset.table.describe()
    active_gates = {}
    for row in saved_rows:
        channel = row.get('channel')
        if not channel:
            continue
        gate_start = row.get('gate_start')
        gate_end = row.get('gate_end')
        if gate_start is None or gate_end is None:
            continue
        desc = description.get(channel) or {}
        if gate_start == desc.get('min') and gate_end == desc.get('max'):
            continue  # still at the full default range -- never customized
        active_gates[channel] = gate_start

    result = dataset.table.run("gating.save_gates", {
        "image_id": datasource,
        "gates": active_gates,
        "table_name": table_name,
        "imageid_column": imageid_column,
    })
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
        dataset = api.dataset(datasource)
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
