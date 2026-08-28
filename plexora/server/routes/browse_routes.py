# Backs the "Browse..." buttons that pick a path on one of the two machines a
# session can involve.
#
# Two pickers, because there are two machines and only one of them reliably has
# a screen:
#
# - A native OS dialog (server/utils/native_dialog.py), opened on whichever
#   machine actually has a desktop. `node` says which: absent means this
#   server's own machine, and a node name means that node's -- which for the
#   node `plexora connect` starts is the user's laptop, the one they are
#   sitting in front of. The request is relayed rather than made by the
#   browser, because the browser has neither the address nor the token.
#
# - A directory listing (/list_dir), for the machine that has no desktop at
#   all. That is the ordinary state of a compute node, and it used to leave
#   the Browse button simply refusing -- so the only way to name a file on the
#   cluster was to know its path already.
#
# Neither returns file bytes. A path, or a list of names and sizes.

import os
from pathlib import Path

from flask import jsonify, request

from plexora import app
from plexora.server.utils import native_dialog
from plexora.server.utils.native_dialog import FILTER_NAMES, browse_for_path

#: How many entries one listing hands back. A scratch directory with a hundred
#: thousand files is an ordinary thing on a cluster, and this picker is for
#: finding one file rather than for reading a directory whole.
LIST_DIR_LIMIT = 2000


@app.route('/browse_path', methods=['POST'])
def browse_path():
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

    node = (payload.get('node') or '').strip()
    if node:
        return _browse_on_node(node, mode, file_filter)

    # A native file dialog opens on the machine running the SERVER, which in
    # notebook and hosted mode is not the machine with the user's screen -- and
    # frequently has no display at all. What that produced was not an error but
    # a hang: the osascript/tkinter subprocess waits for input from a desktop
    # nobody can see, holding a waitress thread until it is killed.
    #
    # Both refusals carry `fallback` so the button can offer the listing picker
    # instead of printing a refusal at somebody. Before that there was no way
    # to browse the server's filesystem at all in these modes: the only option
    # was to already know the path and type it.
    if app.config.get('PLEXORA_NOTEBOOK_MODE'):
        return jsonify(
            error="Native file dialogs are unavailable in notebook/hosted mode; "
                  "browse the list or type the path instead.",
            fallback="list",
        ), 400
    if not native_dialog.available():
        return jsonify(
            error="This machine has no desktop to open a file dialog on.",
            fallback="list",
        ), 400

    try:
        path = browse_for_path(mode=mode, file_filter=file_filter)
    except RuntimeError as exc:
        return jsonify(error=str(exc), fallback="list"), 500

    return jsonify(path=path)


def _browse_on_node(name, mode, file_filter):
    """The dialog opens on the node's machine, not here.

    Which is the point: on the layout this exists for, "here" is a compute node
    with no display and the node is the laptop the user is looking at.
    """
    from plexora import nodes as node_api

    try:
        return jsonify(path=node_api.browse_on_node(name, mode, file_filter))
    except KeyError as exc:
        return jsonify(error=str(exc).strip("'\"")), 400
    except Exception as exc:
        return jsonify(error=str(exc), fallback="list"), 502


@app.route('/list_dir', methods=['POST'])
def list_dir():
    """One directory's contents, for the picker that stands in for a dialog.

    Same trust boundary as `/check_file_existence` next door: this server is
    one user's, guarded by the same token, and what comes back is names, sizes
    and which entries are directories -- never bytes, and never anything the
    user could not have listed in a terminal on the same machine.

    An empty path means the user's home directory, which is where somebody
    starting to look for their data almost always is.
    """
    payload = request.get_json(silent=True) or {}
    raw = (payload.get('path') or '').strip()
    try:
        directory = Path(raw).expanduser() if raw else Path.home()
        directory = directory.resolve()
    except (OSError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400

    if not directory.is_dir():
        return jsonify(error=f"Not a folder: {directory}"), 400

    entries = []
    truncated = False
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                if entry.name.startswith('.'):
                    # Dotfiles are noise in a picker for scientific data, and a
                    # user who genuinely wants one can still type its path.
                    continue
                if len(entries) >= LIST_DIR_LIMIT:
                    truncated = True
                    break
                entries.append(_described(entry))
    except OSError as exc:
        return jsonify(error=f"Cannot read {directory}: {exc}"), 400

    # Directories first, then by name: a .zarr store is a directory and the
    # single Data input takes one, so the two kinds have to be equally easy to
    # reach rather than one buried under the other.
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    parent = str(directory.parent) if directory.parent != directory else None
    return jsonify(path=str(directory), parent=parent, entries=entries,
                   truncated=truncated)


def _described(entry):
    """One directory entry, as the picker draws it.

    Every stat is guarded: a scratch mount routinely holds broken symlinks and
    directories the user cannot enter, and one of them must not blank the
    listing that contains it.
    """
    try:
        is_dir = entry.is_dir()
    except OSError:
        is_dir = False
    size = None
    if not is_dir:
        try:
            size = entry.stat().st_size
        except OSError:
            size = None
    return {"name": entry.name, "is_dir": is_dir, "size": size}
