# Backs the "Browse..." buttons that open a native OS file/folder picker
# server-side (see server/utils/native_dialog.py for why this has to be a
# subprocess). Returns a path only -- never file bytes.

from flask import jsonify, request

from plexora import app
from plexora.server.utils.native_dialog import FILTER_NAMES, browse_for_path


@app.route('/browse_path', methods=['POST'])
def browse_path():
    # A native file dialog opens on the machine running the SERVER, which in
    # notebook and hosted mode is not the machine with the user's screen -- and
    # frequently has no display at all. What that produced was not an error but
    # a hang: the osascript/tkinter subprocess waits for input from a desktop
    # nobody can see, holding a waitress thread until it is killed.
    if app.config.get('PLEXORA_NOTEBOOK_MODE'):
        return jsonify(
            error="Native file dialogs are unavailable in notebook/hosted mode; "
                  "type the path instead."
        ), 400

    payload = request.get_json(silent=True) or {}
    mode = payload.get('mode') or 'file'
    if mode not in ('file', 'directory'):
        return jsonify(error="Invalid mode."), 400

    # Validated against the dialog module's own table, not a copy: a filter it
    # knows about but this list did not made the button dead rather than
    # unfiltered, and silently -- attachBrowseButton had nowhere to show a 400.
    file_filter = payload.get('filter') or 'any'
    if file_filter not in FILTER_NAMES:
        return jsonify(error=f"Unknown file filter: {file_filter}"), 400

    try:
        path = browse_for_path(mode=mode, file_filter=file_filter)
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 500

    return jsonify(path=path)
