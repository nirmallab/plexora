from pathlib import Path
import json

from flask import Blueprint, Response, abort, jsonify, request, stream_with_context
import polars as pl

from plexora import data_path, get_config
from plexora.server.models import data_model
from plexora.server.modules.gating import anndata_gates
from plexora.server.modules.gating import model as gating_model
from plexora.server.routes.data_routes import serialize_and_submit_json

gating_bp = Blueprint('gating', __name__)


@gating_bp.route('/get_gated_cell_ids', methods=['GET'])
def get_gated_cell_ids():
    datasource = request.args.get('datasource')
    filter = json.loads(request.args.get('filter'))
    start_keys = list(request.args.get('start_keys').split(','))
    resp = gating_model.get_gated_cells(datasource, filter, start_keys)
    return serialize_and_submit_json(resp)


@gating_bp.route('/get_gated_cell_ids_custom', methods=['GET'])
def get_gated_cell_ids_custom():
    datasource = request.args.get('datasource')
    filter = json.loads(request.args.get('filter'))
    start_keys = list(request.args.get('start_keys').split(','))
    resp = gating_model.get_gated_cells_custom(datasource, filter, start_keys)
    return serialize_and_submit_json(resp)


@gating_bp.route('/get_gating_gmm', methods=['POST'])
def get_gating_gmm():
    post_data = json.loads(request.data)
    channel = post_data['channel']
    datasource = post_data['datasource']
    selection_ids = post_data['selection_ids']
    resp = gating_model.get_gating_gmm(channel, datasource, selection_ids)
    return serialize_and_submit_json(resp)


@gating_bp.route('/upload_gates', methods=['POST'])
def upload_gates():
    file = request.files['file']
    if file.filename.endswith('.csv') == False:
        abort(422)
    datasource = request.form['datasource']
    save_path = data_path / datasource
    if save_path.is_dir() == False:
        abort(422)

    filename = 'uploaded_gates.csv'
    file.save(Path(save_path / filename))
    resp = jsonify(success=True)
    return resp


def _stream_csv(df, chunksize=100_000):
    """Yield a large DataFrame as CSV in row chunks instead of materializing
    the full serialized string (and holding it alongside the DataFrame) in
    memory at once, as df.write_csv() would for a multi-million-row gating
    export. Polars has no built-in chunked-string-generator, so this slices
    and writes each chunk by hand."""
    header = True
    for start in range(0, df.height, chunksize):
        yield df.slice(start, chunksize).write_csv(include_header=header)
        header = False


@gating_bp.route('/download_gating_csv', methods=['POST'])
def download_gating_csv():
    datasource = request.form['datasource']
    filename = request.form['filename']

    filter = json.loads(request.form['filter'])
    channels = json.loads(request.form['channels'])
    lassos = json.loads(request.form['lassos'])
    selection_ids = json.loads(request.form['selection_ids'])
    fullCsv = json.loads(request.form['fullCsv'])
    encoding = request.form['encoding']
    if fullCsv:
        csv = gating_model.download_gating_csv(datasource, filter, channels, selection_ids, encoding)
        return Response(
            stream_with_context(_stream_csv(csv)),
            mimetype="text/csv",
            headers={"Content-disposition":
                         "attachment; filename=" + filename + ".csv"})
    else:
        csv = gating_model.download_gates(datasource, filter, channels, lassos)
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
    lassos = post_data['lassos']

    # DB-only on every save -- the .h5ad file is only ever written to
    # explicitly, via the "Save Gates to AnnData" button (save_gates_to_anndata()
    # below). Writing to the source file on every debounced slider edit was
    # tried and reverted: the user wants edits to stay local/undo-able in the
    # DB until they deliberately commit them to the file.
    gating_model.save_gating_list(datasource, filter, channels, lassos)

    resp = jsonify(success=True)
    return resp


@gating_bp.route('/get_saved_gating_list', methods=['GET'])
def get_saved_gating_list():
    datasource = request.args.get('datasource')
    resp = gating_model.get_saved_gating_list(datasource)
    return serialize_and_submit_json(resp)


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
    imageid_column = post_data.get('imageid_column') or 'imageid'

    config = get_config()
    entry = config.get(datasource)
    if not entry or entry.get('data_type') != 'anndata':
        return jsonify(success=False, error="Not an AnnData datasource"), 400

    saved_rows = gating_model.get_saved_gating_list(datasource) or []
    description = data_model.get_datasource_description(datasource)
    active_gates = {}
    for row in saved_rows:
        channel = row.get('channel')
        if not channel or channel == 'Lasso':
            continue
        gate_start = row.get('gate_start')
        gate_end = row.get('gate_end')
        if gate_start is None or gate_end is None:
            continue
        desc = description.get(channel) or {}
        if gate_start == desc.get('min') and gate_end == desc.get('max'):
            continue  # still at the full default range -- never customized
        active_gates[channel] = gate_start

    try:
        result = anndata_gates.save_gates_to_anndata(
            entry['featureData'][0], datasource, active_gates,
            table_name=table_name, imageid_column=imageid_column)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    return jsonify(success=True, **result)


@gating_bp.route('/get_gates_from_anndata', methods=['GET'])
def get_gates_from_anndata():
    """Read-only counterpart of /save_gates_to_anndata -- lets the sidebar
    pick up gates already present in adata.uns[table_name] (e.g. set up
    outside Plexora before import) the first time a datasource with no
    Plexora-side saved gating list is opened."""
    datasource = request.args.get('datasource')
    table_name = request.args.get('table_name') or 'gates'
    imageid_column = request.args.get('imageid_column') or 'imageid'

    config = get_config()
    entry = config.get(datasource)
    if not entry or entry.get('data_type') != 'anndata':
        return jsonify(success=False, error="Not an AnnData datasource"), 400

    try:
        result = anndata_gates.load_gates_from_anndata(
            entry['featureData'][0], datasource,
            table_name=table_name, imageid_column=imageid_column)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    return jsonify(success=True, **result)


@gating_bp.route('/get_uploaded_gating_csv_values', methods=['GET'])
def get_gating_csv_values():
    datasource = request.args.get('datasource')
    file_path = data_path / datasource / 'uploaded_gates.csv'
    if file_path.is_file() == False:
        abort(422)
    csv = pl.read_csv(file_path)
    obj = csv.to_dicts()
    return serialize_and_submit_json(obj)
