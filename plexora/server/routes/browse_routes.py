# Backs the "Browse..." buttons that open a native OS file/folder picker
# server-side (see server/utils/native_dialog.py for why this has to be a
# subprocess). Returns a path only -- never file bytes.

from flask import jsonify, request

from plexora import app
from plexora.server.utils.native_dialog import browse_for_path


@app.route('/browse_path', methods=['POST'])
def browse_path():
    payload = request.get_json(silent=True) or {}
    mode = payload.get('mode') or 'file'
    if mode not in ('file', 'directory'):
        return jsonify(error="Invalid mode."), 400

    file_filter = payload.get('filter') or 'any'
    if file_filter not in ('image', 'csv', 'h5ad', 'any'):
        return jsonify(error="Invalid filter."), 400

    try:
        path = browse_for_path(mode=mode, file_filter=file_filter)
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 500

    return jsonify(path=path)
