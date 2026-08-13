# "Quick view" landing page: register a datasource from just a local image
# path (no CSV/segmentation/h5ad) and hand the client a redirect straight
# into the viewer. Paths only, never bytes -- see datasource.py's
# register_image_datasource/register_rgb_datasource docstrings.

from pathlib import Path

from flask import jsonify, request, send_file

from plexora import app, get_config, get_config_names
from plexora.datasource import (
    _dedupe_dataset_name,
    _derive_dataset_name_from_path,
    _sniff_quick_view_kind,
    register_image_datasource,
    register_rgb_datasource,
)
from plexora.server.routes.import_routes import trim_filepath_quotes


@app.route('/quick_view', methods=['POST'])
def quick_view():
    payload = request.get_json(silent=True) or {}
    path = trim_filepath_quotes((payload.get('path') or '').strip())

    if not path or not Path(path).is_file():
        return jsonify(success=False, error="File does not exist."), 400

    try:
        kind = _sniff_quick_view_kind(path)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    name = _dedupe_dataset_name(_derive_dataset_name_from_path(path), get_config_names())

    try:
        if kind == 'ome_tiff':
            register_image_datasource(name=name, image=path)
        else:
            register_rgb_datasource(name=name, image=path)
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 400

    base_url = app.config.get('PLEXORA_BASE_URL', '')
    return jsonify(success=True, name=name, redirect=f"{base_url}/{name}")


@app.route('/generated/rgb/<string:datasource>')
def generate_rgb_image(datasource):
    config = get_config()
    entry = config.get(datasource)
    if not entry or entry.get('image_kind') != 'rgb':
        return jsonify(error="Not an RGB quick-view datasource."), 404
    return send_file(entry['channelFile'])
