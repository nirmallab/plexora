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

from flask import jsonify, request

from plexora import app, paths
from plexora.server.utils import dir_listing, native_dialog
from plexora.server.utils.dir_listing import LIST_DIR_LIMIT  # noqa: F401 - re-export
from plexora.server.utils.native_dialog import FILTER_NAMES, browse_for_path

#: How many directories the picker offers back under "Recent", per machine.
#: Short on purpose: this is a shortcut, and a list longer than a glance is
#: another thing to search rather than a way out of searching.
RECENT_LIMIT = 8

#: How many a user may pin. Generously large -- somebody with twenty studies
#: pinning one directory each is doing exactly what this is for -- but bounded,
#: because it lives in the settings file that every start-up reads.
PINNED_LIMIT = 30


@app.route('/browse_path', methods=['POST'])
def browse_path():
    payload = request.get_json(silent=True) or {}
    mode = payload.get('mode') or 'file'
    if mode not in ('file', 'directory', 'any'):
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
    # Every path field asks for mode "any" -- one button, taking whichever of a
    # file or a folder the format happens to be. Only macOS has an OS dialog
    # that can do that; the listing picker can, on any machine, because it is
    # not one. So this is the ordinary answer on Linux and Windows rather than
    # a degradation, and it carries `fallback` for the same reason the two
    # refusals above do.
    if mode == 'any' and not native_dialog.hybrid_available():
        return jsonify(
            error="This machine's file dialog cannot take a folder; "
                  "browse the list instead.",
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

    A node with no desktop is the ORDINARY case in the other direction -- every
    cluster is one -- and it is not a gateway failure. The node answered; it
    said no. That is a 400 with `fallback`, so the button offers the listing
    picker instead of logging a 502 nobody can act on. 502 is kept for a node
    that could not be reached at all, which is a different problem.

    Which is also what handles a node too old to know mode "any": it refuses an
    unknown mode, that refusal arrives here, and the button opens the relayed
    listing picker over the node's filesystem. Nothing probes the far side's
    version -- the answer to "can you do this?" is the attempt.
    """
    from plexora import nodes as node_api
    from plexora.server.providers.base import ResourceUnavailable

    try:
        return jsonify(path=node_api.browse_on_node(name, mode, file_filter))
    except KeyError as exc:
        return jsonify(error=str(exc).strip("'\""), fallback="list"), 400
    except ResourceUnavailable as exc:
        return jsonify(error=str(exc), fallback="list"), 502
    except Exception as exc:
        return jsonify(error=str(exc), fallback="list"), 400


@app.route('/list_dir', methods=['POST'])
def list_dir():
    """One directory's contents, for the picker that stands in for a dialog.

    Same trust boundary as `/check_file_existence` next door: this server is
    one user's, guarded by the same token, and what comes back is names, sizes
    and which entries are directories -- never bytes, and never anything the
    user could not have listed in a terminal on the same machine.

    `node` names a machine to ask instead of this one. That is how a field set
    to Remote browses a cluster from a laptop: the node is on the far side and
    answers about its own filesystem, and the request is relayed rather than
    made by the browser, which has neither the address nor the token.
    """
    payload = request.get_json(silent=True) or {}
    node = (payload.get('node') or '').strip()
    raw = (payload.get('path') or '').strip()
    show_hidden = bool(payload.get('show_hidden'))
    if node:
        return _list_dir_on_node(node, raw, show_hidden)

    try:
        return jsonify(**dir_listing.listing(raw, show_hidden=show_hidden))
    except dir_listing.ListingError as exc:
        return jsonify(error=str(exc)), 400


def _list_dir_on_node(name, path, show_hidden=False):
    """A listing from the far side, with its failures kept apart.

    Same shape as `_relayed` in settings_routes.py, and for the same reason:
    "that folder does not exist" and "the node is not answering" are different
    sentences to read and different things to do next. Collapsing both to 502
    put a gateway error in the picker's error bar for an ordinary typo.
    """
    from plexora import nodes as node_api
    from plexora.server.providers.base import ResourceError, ResourceUnavailable

    try:
        return jsonify(**node_api.list_dir_on_node(
            name, path, show_hidden=show_hidden))
    except KeyError as exc:
        return jsonify(error=str(exc).strip("'\"")), 400
    except ResourceUnavailable as exc:
        return jsonify(error=str(exc)), 503
    except ResourceError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 502


# --------------------------------------------------------------------------
# Where the picker was standing last time
#
# Kept on the server rather than in the browser's storage because the thing
# being remembered is a fact about a MACHINE, not about a tab: `/n/scratch/aj`
# means nothing on the laptop and everything on the cluster, and the same user
# opens the same session from three different browsers. So the record is keyed
# by node name -- "" being this server's own filesystem -- and a user who
# browses the cluster, then their laptop, then the cluster again comes back to
# where they were on each.
#
# Paths are never checked for existence here. The viewer cannot stat a node's
# filesystem, and half of these paths are on the far side of a relay; a
# directory that has since been deleted shows up as a listing error when it is
# clicked, which is the honest place to find out.
# --------------------------------------------------------------------------


def _strings(value, limit):
    """A stored list of paths, with anything that is not one dropped.

    `read_settings` only promises a dict, and the file is a user-editable one.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item][:limit]


def _picker_record(node):
    """One machine's remembered places, normalized to the full shape."""
    settings = paths.read_settings()
    picker = settings.get("path_picker")
    places = picker.get("places") if isinstance(picker, dict) else None
    record = places.get(node) if isinstance(places, dict) else None
    if not isinstance(record, dict):
        record = {}
    last_dir = record.get("last_dir")
    return {
        "last_dir": last_dir if isinstance(last_dir, str) else "",
        "recent": _strings(record.get("recent"), RECENT_LIMIT),
        "pinned": _strings(record.get("pinned"), PINNED_LIMIT),
    }


def _store_picker_record(node, record):
    """Write one machine's record back, leaving every other setting alone."""
    settings = paths.read_settings()
    picker = settings.get("path_picker")
    if not isinstance(picker, dict):
        picker = {}
    places = picker.get("places")
    if not isinstance(places, dict):
        places = {}
    places[node] = record
    picker["places"] = places
    settings["path_picker"] = picker
    paths.write_settings(settings)


def _asked(payload, field):
    """One optional path field, or None when it was not sent.

    A field that is present but is not a string is a caller bug rather than a
    user one, and is refused rather than coerced -- `str(None)` written into
    the recent list as "None" would sit there being clicked forever.
    """
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value.strip()


@app.route('/picker_prefs', methods=['GET'])
def picker_prefs():
    """Where the listing picker should open, for one machine."""
    return jsonify(**_picker_record((request.args.get('node') or '').strip()))


@app.route('/picker_prefs', methods=['POST'])
def save_picker_prefs():
    """Record a directory the user actually chose, or (un)pin one.

    Written once per successful pick -- `last_dir` and `add_recent` together --
    rather than on every step through the tree: this is a file on disk, and
    walking six directories deep should not be six read-modify-writes.
    """
    payload = request.get_json(silent=True) or {}
    node = payload.get('node') or ''
    if not isinstance(node, str):
        return jsonify(error="node must be a string"), 400
    node = node.strip()

    try:
        last_dir = _asked(payload, 'last_dir')
        add_recent = _asked(payload, 'add_recent')
        pin = _asked(payload, 'pin')
        unpin = _asked(payload, 'unpin')
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    record = _picker_record(node)
    if last_dir:
        record["last_dir"] = last_dir
    if add_recent:
        # Newest first, and each directory only once: choosing three files out
        # of the same folder is one place, not three lines of the same name.
        recent = [p for p in record["recent"] if p != add_recent]
        record["recent"] = [add_recent, *recent][:RECENT_LIMIT]
    if pin and pin not in record["pinned"]:
        record["pinned"] = [*record["pinned"], pin][:PINNED_LIMIT]
    if unpin:
        # A no-op when it was not pinned, which is what an un-pin of something
        # already gone should be.
        record["pinned"] = [p for p in record["pinned"] if p != unpin]

    _store_picker_record(node, record)
    return jsonify(**record)
