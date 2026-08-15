from plexora import app
from flask import make_response, render_template, request, Response, jsonify, abort, send_file
import csv
import io
from plexora import get_config
from plexora.datasource import rename_channels
from plexora.server.models import data_model
import gzip
import json
import orjson
import threading
from collections import OrderedDict
from os import walk


@app.route('/init_database', methods=['GET'])
def init_database():
    datasource = request.args.get('datasource')
    data_model.init(datasource)
    resp = jsonify(success=True)
    return resp


@app.route('/config')
def serve_config():
    return get_config()


@app.route('/get_nearest_cell', methods=['GET'])
def get_nearest_cell():
    x = float(request.args.get('point_x'))
    y = float(request.args.get('point_y'))
    datasource = request.args.get('datasource')
    resp = data_model.query_for_closest_cell(x, y, datasource)
    return serialize_and_submit_json(resp)


# Gets a row based on the index
@app.route('/get_database_row', methods=['GET'])
def get_database_row():
    datasource = request.args.get('datasource')
    row = int(request.args.get('row'))
    resp = data_model.get_row(row, datasource)
    return serialize_and_submit_json(resp)


@app.route('/get_channel_names', methods=['GET'])
def get_channel_names():
    datasource = request.args.get('datasource')
    shortnames = bool(request.args.get('shortNames'))
    resp = data_model.get_channel_names(datasource, shortnames)
    return serialize_and_submit_json(resp)


@app.route('/get_all_cells/<dtype>/', methods=['GET'])
def get_all_cells(dtype):
    datasource = request.args.get('datasource')
    data_type = int if 'integer' == dtype else float
    start_keys = list(request.args.get('start_keys').split(','))
    resp = data_model.get_all_cells(datasource, start_keys, data_type)
    content = gzip.compress(resp.tobytes('C'))
    response = make_response(content)
    response.headers.set('Content-Type', 'application/octet-stream')
    response.headers['Content-length'] = len(content)
    response.headers['Content-Encoding'] = 'gzip'
    return response


@app.route('/get_centroid_manifest', methods=['GET'])
def get_centroid_manifest():
    datasource = request.args.get('datasource')
    resp = data_model.get_centroid_manifest(datasource)
    return serialize_and_submit_json(resp)


@app.route('/get_centroid_tiles', methods=['POST'])
def get_centroid_tiles():
    post_data = json.loads(request.data)
    datasource = post_data['datasource']
    level = int(post_data.get('level', 0))
    tiles = post_data.get('tiles', [])
    filter = post_data.get('filter', {})
    max_points = post_data.get('max_points')
    resp = data_model.get_centroid_tiles(datasource, level, tiles, filter, max_points)
    content = gzip.compress(resp.tobytes('C'))
    response = make_response(content)
    response.headers.set('Content-Type', 'application/octet-stream')
    response.headers['Content-length'] = len(content)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['X-Centroid-Record-Count'] = len(resp)
    return response


@app.route('/get_channel_cell_ids', methods=['GET'])
def get_channel_cell_ids():
    datasource = request.args.get('datasource')
    filter = json.loads(request.args.get('filter'))
    resp = data_model.get_channel_cells(datasource, filter)
    return serialize_and_submit_json(resp)


@app.route('/get_database_description', methods=['GET'])
def get_database_description():
    datasource = request.args.get('datasource')
    resp = data_model.get_datasource_description(datasource)
    return serialize_and_submit_json(resp)


@app.route('/get_channel_gmm', methods=['GET'])
def get_channel_gmm():
    channel = request.args.get('channel')
    datasource = request.args.get('datasource')
    resp = data_model.get_channel_gmm(channel, datasource)
    return serialize_and_submit_json(resp)

@app.route('/upload_channels', methods=['POST'])
def upload_channels():
    """Rename an already-registered datasource's image channels from an
    uploaded single-column CSV (one name per row, in channel order) -- lets
    users fix gating/channel auto-matching without re-registering the whole
    datasource. If the row count is exactly one more than the channel count,
    the first row is assumed to be a header and dropped.
    """
    file = request.files['file']
    if file.filename.endswith('.csv') == False:
        abort(422)
    datasource = request.form['datasource']
    config = get_config()
    if datasource not in config:
        abort(422)

    n_channels = sum(1 for c in config[datasource]['imageData'] if c['name'] != 'Area')

    text = file.read().decode('utf-8-sig', errors='replace')
    names = []
    for row in csv.reader(io.StringIO(text)):
        cells = [cell.strip() for cell in row if cell.strip()]
        if cells:
            names.append(cells[0])
    if len(names) == n_channels + 1:
        names = names[1:]

    try:
        rename_channels(datasource, names)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    data_model.load_datasource(datasource, reload=True)
    return jsonify(success=True)

@app.route('/get_ome_metadata', methods=['GET'])
def get_ome_metadata():
    datasource = request.args.get('datasource')
    resp = data_model.get_ome_metadata(datasource)
    if hasattr(resp, "model_dump"):
        resp = resp.model_dump(mode="json")
    elif hasattr(resp, "dict"):
        resp = resp.dict()
    elif not resp:
        resp = {}
    # OME-Types handles jsonify itself, so skip the orjson conversion
    response = app.response_class(
        response=json.dumps(resp),
        mimetype='application/json'
    )
    return response


@app.route('/save_channel_list', methods=['POST'])
def save_channel_list():
    post_data = json.loads(request.data)

    datasource = post_data['datasource']
    map_channels = post_data['map_channels']
    active_channels = post_data['active_channels']
    list_colors = post_data['list_colors']
    list_ranges = post_data['list_ranges']
    list_channels = post_data['list_channels']

    data_model.save_channel_list(datasource, map_channels, active_channels, list_colors, list_ranges, list_channels)

    resp = jsonify(success=True)
    return resp

@app.route('/get_saved_channel_list', methods=['GET'])
def get_saved_channel_list():
    datasource = request.args.get('datasource')
    resp = data_model.get_saved_channel_list(datasource)
    return serialize_and_submit_json(resp)

_tile_png_cache = OrderedDict()
_tile_png_cache_lock = threading.Lock()
_TILE_PNG_CACHE_MAX = 1500


def _get_tile_png_bytes(datasource, channel, level, tile, quality):
    # Keyed on data_model.load_generation so a datasource reload (which may
    # regenerate segmentation, per ensure_outline_segmentation) naturally
    # invalidates previously cached tiles without cross-module cache access.
    # `quality` is part of the key so default/hd/legacy variants of the same
    # tile don't collide.
    key = (data_model.load_generation, datasource, channel, level, tile, quality)
    with _tile_png_cache_lock:
        cached = _tile_png_cache.get(key)
        if cached is not None:
            _tile_png_cache.move_to_end(key)
            return cached

    encoded, mimetype = data_model.encode_tile(datasource, channel, level, tile, quality)

    with _tile_png_cache_lock:
        _tile_png_cache[key] = (encoded, mimetype)
        _tile_png_cache.move_to_end(key)
        while len(_tile_png_cache) > _TILE_PNG_CACHE_MAX:
            _tile_png_cache.popitem(last=False)
    return encoded, mimetype


# E.G /generated/data/melanoma/channel_00_files/13/16_18.png
# ?q=hd requests the full-precision 16-bit path for channel tiles; anything
# else (including the param being absent) uses the fast default WebP path.
# Segmentation tiles ignore `q` entirely -- see data_model.encode_tile.
@app.route('/generated/data/<string:datasource>/<string:channel>/<string:level>/<string:tile>')
def generate_png(datasource, channel, level, tile):
    # Frontend can now decode WebP (createImageBitmap-based u8 path, verified
    # against real data) -- an absent `q` is the true default: fast/small
    # WebP. `q=legacy` is kept as an explicit escape hatch back to the
    # original uncompressed PNG behavior if ever needed.
    quality = request.args.get('q', 'webp')
    encoded, mimetype = _get_tile_png_bytes(datasource, channel, level, tile, quality)
    return send_file(io.BytesIO(encoded), mimetype=mimetype)

def serialize_and_submit_json(data):
    response = app.response_class(
        response=orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY),
        mimetype='application/json'
    )
    return response

