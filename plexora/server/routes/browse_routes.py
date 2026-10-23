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
# - The in-app explorer, for every machine: a directory listing (/list_dir)
#   with the sizes and dates it could afford (/stat_entries for the rest), the
#   machine's places (/fs_places), a budgeted search under one folder
#   (/search_dir) and what it remembers (/picker_prefs). It began as the
#   fallback for a compute node with no desktop, where Browse used to simply
#   refuse -- so the only way to name a file on the cluster was to know its
#   path already.
#
# None of it returns file bytes. A path, or names, sizes and dates.
#
# Every explorer route takes `node` and answers about that machine instead,
# relayed because the browser has neither its address nor its token. A node
# too old to have a route answers the same shape with `unsupported`, never an
# error: the explorer draws what it has and leaves the rest out.

import math

from flask import jsonify, request

from plexora import app, paths
from plexora.server.utils import dir_listing, dir_search, fs_places, native_dialog
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
    if _is_files_place(node):
        # A files-only server has no desktop and no process of ours to open a
        # dialog with; the listing picker is the whole of browsing it.
        return jsonify(error="Browse the list for a files-only server.",
                       fallback="list"), 400
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
    # that can do that: NSOpenPanel sets `canChooseFiles` and
    # `canChooseDirectories` together, while Tk's dialogs and the Windows
    # common dialogs are single-kind by construction.
    #
    # "kinds", not "list", and the distinction is the whole point of this
    # branch. The two refusals above mean "there is no desktop here", and the
    # in-app listing is the only picker left. This one means the opposite:
    # there IS a desktop, and a perfectly good native dialog on it -- just not
    # one that answers both questions at once. So the button asks which kind
    # and opens the real dialog for it, rather than replacing a system file
    # browser with a listing on a machine that never needed the substitute.
    #
    # Answering both with "list" is what quietly took the native picker away
    # from every Windows and Linux desktop when mode "any" arrived.
    if mode == 'any' and not native_dialog.hybrid_available():
        return jsonify(
            error="This machine's file dialog cannot take a file and a folder "
                  "at once; ask for one kind.",
            fallback="kinds",
        ), 400

    try:
        path = browse_for_path(mode=mode, file_filter=file_filter)
    except RuntimeError as exc:
        return jsonify(error=str(exc), fallback="list"), 500

    return jsonify(path=path)


@app.route('/browse_capability', methods=['POST'])
def browse_capability():
    """What kind of file dialog the machine behind this field can show.

    Asked before anything is clicked, because the Browse control is a different
    control depending on the answer: a machine with one dialog for both kinds
    (macOS) gets a single button, and a machine with two single-kind dialogs
    gets the File/Folder pair, which IS the button rather than a menu that
    opens from one. That decision has to be made while the form is being drawn,
    which is too early to learn it from a refusal.

    Opens nothing and waits for nobody -- the two predicates behind
    `dialog_kind` are syntactic, and the node case is one `/hello`. Safe to ask
    on mount, which is the only time it is any use.
    """
    payload = request.get_json(silent=True) or {}
    node = (payload.get('node') or '').strip()
    if _is_files_place(node):
        return jsonify(dialogs=native_dialog.NONE)
    if node:
        from plexora import nodes as node_api
        # None is a node too old to say or one not answering just now. Both
        # mean "assume the substitute picker", the same as everywhere else.
        return jsonify(dialogs=node_api.dialogs_on_node(node) or native_dialog.NONE)
    if app.config.get('PLEXORA_NOTEBOOK_MODE'):
        # A desktop may well exist on this machine; it is not one the person
        # reading the notebook is looking at.
        return jsonify(dialogs=native_dialog.NONE)
    if app.config.get('PLEXORA_DESKTOP'):
        # The desktop window opens its own dialogs, parented to it, and asks
        # this route only so it knows it may. A browser tab of the same server
        # has no shell and reads `dialogs` exactly as it always has.
        return jsonify(dialogs=native_dialog.dialog_kind(), via="shell")
    return jsonify(dialogs=native_dialog.dialog_kind())


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

    The refusal itself does not say WHICH no it is, though. It crosses the
    network as a sentence, and by the time it arrives "there is no desktop
    here" and "my dialogs only do one kind at a time" read alike -- so this
    used to assume the first and hand a Windows laptop the listing picker
    instead of the file dialog it was sitting in front of. `_node_fallback`
    asks the node, once, after it has already said no.
    """
    from plexora import nodes as node_api
    from plexora.server.providers.base import ResourceUnavailable

    try:
        return jsonify(path=node_api.browse_on_node(name, mode, file_filter))
    except KeyError as exc:
        # Not a refusal from a machine -- there is no such node to ask.
        return jsonify(error=str(exc).strip("'\""), fallback="list"), 400
    except ResourceUnavailable as exc:
        # Nor this: the node never answered, so asking it a second question
        # would only be a second thing to wait for.
        return jsonify(error=str(exc), fallback="list"), 502
    except Exception as exc:
        return jsonify(error=str(exc),
                       fallback=_node_fallback(node_api, name, mode)), 400


def _node_fallback(node_api, name, mode):
    """Which picker to offer after a node declined to open a dialog.

    Only mode "any" can be declined for a reason worth distinguishing, so only
    mode "any" pays for the extra `/hello`. Every other mode is refused because
    the machine genuinely cannot show a dialog, and the listing is the answer.

    A node that will not say -- older than the field, or unreachable in the
    moment -- reads as None here and gets "list", which is exactly what every
    node got before it existed.
    """
    if mode != 'any':
        return "list"
    if node_api.dialogs_on_node(name) == native_dialog.KINDS:
        return "kinds"
    return "list"


@app.route('/list_dir', methods=['POST'])
def list_dir():
    """One directory's contents, for the explorer that stands in for a dialog.

    Same trust boundary as `/check_file_existence` next door: this server is
    one user's, guarded by the same token, and what comes back is names, sizes,
    dates and which entries are directories -- never bytes, and never anything
    the user could not have listed in a terminal on the same machine.

    `node` names a machine to ask instead of this one. That is how a field set
    to Remote browses a cluster from a laptop: the node is on the far side and
    answers about its own filesystem, and the request is relayed rather than
    made by the browser, which has neither the address nor the token.

    Answered through `serialize_and_submit_json`, which gzips past 256 KB: a
    50,000-entry folder is ~8 MB of JSON and ~1 MB compressed, and the slow
    link is very often the one between this server and the browser.
    """
    payload = request.get_json(silent=True) or {}
    node = (payload.get('node') or '').strip()
    raw = (payload.get('path') or '').strip()
    show_hidden = bool(payload.get('show_hidden'))
    if _is_files_place(node) or (not node and _is_files_place(raw)):
        return _list_dir_over_sftp(node or raw, raw, show_hidden)
    if node:
        return _list_dir_on_node(node, raw, show_hidden)

    try:
        return _json(dir_listing.listing(raw, show_hidden=show_hidden))
    except dir_listing.ListingError as exc:
        return jsonify(error=str(exc)), 400


def _json(answer):
    """One explorer answer as JSON, compressed when it is large and may be.

    Imported here rather than at the top: data_routes is a large module and
    this one must not depend on the order the route modules load in.
    """
    from plexora.server.routes.data_routes import serialize_and_submit_json

    return serialize_and_submit_json(answer)


def _is_files_place(value):
    """Whether a machine key names a files-only server.

    Such a place has no node, so the browser names it by the `sftp://` address
    its files are read at (`data_places`' `files.base`) -- which is also what
    every path in its listing starts with, so the picker hands back addresses
    an import takes as they are.
    """
    from plexora.server.utils import sftp

    return str(value or "").strip().lower().startswith(f"{sftp.SCHEME}://")


def _list_dir_over_sftp(base, path, show_hidden=False):
    """`/list_dir` for a files-only server, read over its SFTP connection.

    503 for a server that is not connected, as a node that is not answering
    gets: the picker shows the sentence, which names the server to connect.
    """
    from plexora.server.utils import sftp

    try:
        return _json(sftp.listing(base, path, show_hidden=show_hidden,
                                  limit=LIST_DIR_LIMIT))
    except sftp.SFTPNotConnected as exc:
        return jsonify(error=str(exc)), 503
    except dir_listing.ListingError as exc:
        return jsonify(error=str(exc)), 400
    except (ConnectionError, TimeoutError) as exc:
        return jsonify(error=str(exc)), 503


def _relay(call, answer=None):
    """Run one node call for the explorer, keeping its failures apart.

    Same shape as `_relayed` in settings_routes.py, and for the same reason:
    "that folder does not exist" and "the node is not answering" are different
    sentences to read and different things to do next. Collapsing both to 502
    put a gateway error in the picker's error bar for an ordinary typo.

    `answer` turns the call's dict into the response (`jsonify` by default,
    `_json` for a listing).
    """
    from plexora.server.providers.base import ResourceError, ResourceUnavailable

    try:
        found = call()
    except KeyError as exc:
        return jsonify(error=str(exc).strip("'\"")), 400
    except dir_search.SearchBusy as exc:
        return jsonify(error=str(exc)), 429
    except ResourceUnavailable as exc:
        return jsonify(error=str(exc)), 503
    except ResourceError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 502
    return answer(found) if answer is not None else jsonify(**found)


def _list_dir_on_node(name, path, show_hidden=False):
    """A listing from the far side, with its failures kept apart (`_relay`)."""
    from plexora import nodes as node_api

    return _relay(lambda: node_api.list_dir_on_node(
        name, path, show_hidden=show_hidden), answer=_json)


@app.route('/stat_entries', methods=['POST'])
def stat_entries():
    """Sizes, dates and kinds for up to 500 names in one folder.

    What a `partial` listing left out, asked for the rows on screen first. The
    names are bare names in `path`, never paths: this cannot stat anything the
    listing did not already name. A name that has gone since is left out.

    A node too old to have the route, and a files-only server (whose listing
    is never partial), answer `unsupported` with no entries.
    """
    payload = request.get_json(silent=True) or {}
    node = (payload.get('node') or '').strip()
    raw = (payload.get('path') or '').strip()
    names = payload.get('names')
    if not isinstance(names, list):
        return jsonify(error="names must be a list"), 400
    if len(names) > dir_listing.STAT_ENTRIES_LIMIT:
        return jsonify(error=f"At most {dir_listing.STAT_ENTRIES_LIMIT} names "
                             f"can be asked about at once."), 400
    unsupported = {"unsupported": True, "path": raw, "entries": {}}
    if _is_files_place(node) or (not node and _is_files_place(raw)):
        return jsonify(**unsupported)
    if node:
        from plexora import nodes as node_api

        return _relay(lambda: node_api.stat_entries_on_node(node, raw, names)
                      or unsupported)

    try:
        return jsonify(**dir_listing.stat_entries(raw, names))
    except dir_listing.ListingError as exc:
        return jsonify(error=str(exc)), 400


@app.route('/fs_places', methods=['GET'])
def fs_places_route():
    """Everything the explorer's sidebar shows for one machine, in one GET.

    The machine's own places -- home, Desktop/Documents/Downloads, its volumes
    with labels and free space, its cloud folders (Dropbox, OneDrive, Google
    Drive, iCloud Drive, Box), the Plexora data folder and a cluster's
    scratch and project roots -- and what the user keeps for it (`last_dir`,
    `recent`, `pinned`) with the global `view`.

    Always a 200. The remembered half comes back in every branch, so the pins
    still draw while a node is down: a node that is not answering adds
    `unavailable` (the sentence), one too old to have the route adds
    `unsupported`, and both leave the machine's own lists empty.
    """
    node = (request.args.get('node') or '').strip()
    remembered = {**_picker_record(node), "view": _view_record()}
    if _is_files_place(node):
        return jsonify(**_no_places(_files_place_host(node)), **remembered,
                       unsupported=True)
    if node:
        return jsonify(**_places_on_node(node), **remembered)
    try:
        return jsonify(**fs_places.describe(), **remembered)
    except Exception as exc:  # pragma: no cover - describe() guards each part
        return jsonify(**_no_places(""), **remembered, unavailable=str(exc))


def _no_places(hostname, platform=""):
    """The machine half of `/fs_places` when there is nothing to say."""
    return {"platform": platform or "", "hostname": hostname, "home": "",
            "known": [], "volumes": [], "cloud": [], "project": []}


def _files_place_host(base):
    """The host part of a files-only server's `sftp://` address."""
    from plexora.server.utils import sftp

    try:
        return sftp.target_of(base)
    except Exception:
        return base


def _places_on_node(name):
    """A node's own places, or the empty answer that says why there are none.

    Never raises: a node that is down, unregistered, refusing (`--dynamic`
    missing) or older than the route all leave the sidebar with Home and the
    pins, which is what it had before this existed. `platform` comes from the
    last answer that node gave this process, when there was one -- no probe is
    spent on it.
    """
    from plexora import nodes as node_api

    try:
        found = node_api.fs_places_on_node(name)
    except Exception as exc:
        return {**_no_places(name, node_api.known_platform(name)),
                "unavailable": str(exc).strip("'\"")}
    if found is None:
        return {**_no_places(name, node_api.known_platform(name)),
                "unsupported": True}
    return found


@app.route('/search_dir', methods=['POST'])
def search_dir():
    """Names matching `query` anywhere under one folder, within a budget.

    Breadth-first, so the nearest matches come first, and bounded three ways
    (`limit` matches, `budget_ms`, `max_depth`), each clamped to the module's
    caps whatever is asked; `stopped` says which one ended it. Two searches run
    at once per process and a third is told 429, which the explorer reads as
    "try again" rather than as a failure. Matches carry no size or date.

    Not cancellable here: the explorer drops a stale answer by its own epoch
    and aborts the fetch, and the walk ends on its budget regardless.
    """
    payload = request.get_json(silent=True) or {}
    node = (payload.get('node') or '').strip()
    raw = (payload.get('path') or '').strip()
    show_hidden = bool(payload.get('show_hidden'))
    try:
        query = dir_search.clean_query(payload.get('query'))
        limit = _bounded(payload, 'limit', dir_search.SEARCH_LIMIT,
                         1, dir_search.SEARCH_LIMIT)
        budget_ms = _bounded(payload, 'budget_ms', dir_search.SEARCH_BUDGET_MS,
                             1, dir_search.SEARCH_BUDGET_MS)
        max_depth = _bounded(payload, 'max_depth', dir_search.SEARCH_MAX_DEPTH,
                             1, dir_search.SEARCH_MAX_DEPTH)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if _is_files_place(node) or (not node and _is_files_place(raw)):
        return jsonify(unsupported=True, path=raw, query=query, matches=[],
                       visited=0, stopped=None)
    if node:
        from plexora import nodes as node_api

        return _relay(lambda: node_api.search_on_node(
            node, raw, query, show_hidden=show_hidden, limit=limit,
            budget_ms=budget_ms, max_depth=max_depth) or {
                "unsupported": True, "path": raw, "query": query,
                "matches": [], "visited": 0, "stopped": None})

    try:
        return jsonify(**dir_search.search(
            raw, query, show_hidden=show_hidden, limit=limit,
            budget_ms=budget_ms, max_depth=max_depth))
    except dir_search.SearchBusy as exc:
        return jsonify(error=str(exc)), 429
    except (dir_listing.ListingError, ValueError) as exc:
        return jsonify(error=str(exc)), 400


def _bounded(payload, field, default, low, high):
    """An optional whole number from the request, clamped to `[low, high]`.

    Clamped rather than refused when it is merely too big or too small: the
    caps are this server's to set, and a client asking for more is told less
    by the answer's `stopped`. Something that is not a number is a caller bug
    and is refused, naming the field.
    """
    value = payload.get(field)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise ValueError(f"{field} must be a number")
    return max(low, min(high, int(value)))


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


def _store_picker_record(node, record, view=None):
    """Write one machine's record back, leaving every other setting alone.

    `view`, when given, is written in the same read-modify-write
    (`_store_view`): one settings write per request, whatever it carried.
    """
    settings = paths.read_settings()
    picker = settings.get("path_picker")
    if not isinstance(picker, dict):
        picker = {}
    places = picker.get("places")
    if not isinstance(places, dict):
        places = {}
    places[node] = record
    picker["places"] = places
    if view is not None:
        _store_view(picker, view)
    settings["path_picker"] = picker
    paths.write_settings(settings)


# --------------------------------------------------------------------------
# How the explorer is laid out
#
# One record for every machine, unlike the places above: a sort order and a
# column width are about the person reading the list, not about the disk it
# lists. Stored once under settings["path_picker"]["view"], read leniently
# (a damaged key is its default) and written strictly (a wrong type is a 400
# naming the key, because it is a client bug and silently keeping the old
# value would hide it).
# --------------------------------------------------------------------------

#: The answer for a user who never changed anything. Name takes the slack, so
#: only the other three columns have widths.
DEFAULT_VIEW = {
    "sort_by": "name",
    "sort_dir": "asc",
    "dirs_first": True,
    "show_hidden": False,
    "layout": "list",
    "columns": {"mtime": 150, "kind": 120, "size": 80},
    "sidebar": {"collapsed": []},
}

#: The allowed values of each enumerated `view` key.
VIEW_CHOICES = {
    "sort_by": ("name", "mtime", "kind", "size"),
    "sort_dir": ("asc", "desc"),
    "layout": ("list", "grid"),
}

#: Pixels. A width outside this is clamped, not refused: it came from a drag.
COLUMN_WIDTHS = (40, 2000)

#: The sidebar sections that can be collapsed, in the order they are drawn.
SIDEBAR_SECTIONS = ("favorites", "locations", "recent")


def _fresh_view():
    return {**DEFAULT_VIEW, "columns": dict(DEFAULT_VIEW["columns"]),
            "sidebar": {"collapsed": list(DEFAULT_VIEW["sidebar"]["collapsed"])}}


def _clean_view(raw, base, strict=True):
    """`raw` merged over `base`, each key checked; unknown keys dropped.

    A shallow merge -- a key `raw` does not carry keeps `base`'s value -- with
    `columns` merged per column too, so a drag that resized one column sends
    only that one. `strict` raises ValueError naming the first bad key, for a
    write; otherwise a bad key keeps `base`'s value, for a read of a file the
    user may have edited.
    """
    view = {**base, "columns": dict(base["columns"]),
            "sidebar": {"collapsed": list(base["sidebar"]["collapsed"])}}
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("view must be an object")
        return view
    for key, value in raw.items():
        try:
            if key in VIEW_CHOICES:
                if value not in VIEW_CHOICES[key]:
                    raise ValueError(f"view.{key} must be one of "
                                     f"{', '.join(VIEW_CHOICES[key])}")
                view[key] = value
            elif key in ("dirs_first", "show_hidden"):
                if not isinstance(value, bool):
                    raise ValueError(f"view.{key} must be true or false")
                view[key] = value
            elif key == "columns":
                view["columns"].update(_clean_columns(value))
            elif key == "sidebar":
                view["sidebar"] = _clean_sidebar(value)
        except ValueError:
            if strict:
                raise
    return view


def _clean_columns(value):
    """`{column: width}` for the known columns, clamped to `COLUMN_WIDTHS`."""
    if not isinstance(value, dict):
        raise ValueError("view.columns must be an object")
    low, high = COLUMN_WIDTHS
    widths = {}
    for column, width in value.items():
        if column not in DEFAULT_VIEW["columns"]:
            continue
        if isinstance(width, bool) or not isinstance(width, (int, float)) \
                or not math.isfinite(width):
            raise ValueError(f"view.columns.{column} must be a number")
        widths[column] = max(low, min(high, int(round(width))))
    return widths


def _clean_sidebar(value):
    """`{collapsed: [...]}`, the known sections only, each once, in order."""
    if not isinstance(value, dict):
        raise ValueError("view.sidebar must be an object")
    collapsed = value.get("collapsed", [])
    if not isinstance(collapsed, list) or not all(
            isinstance(item, str) for item in collapsed):
        raise ValueError("view.sidebar.collapsed must be a list of names")
    return {"collapsed": [name for name in SIDEBAR_SECTIONS if name in collapsed]}


def _view_record():
    """The stored `view`, every key valid, defaults for anything missing."""
    picker = paths.read_settings().get("path_picker")
    stored = picker.get("view") if isinstance(picker, dict) else None
    return _clean_view(stored, _fresh_view(), strict=False)


def _store_view(picker, view):
    """Put a cleaned `view` into the `path_picker` settings dict."""
    picker["view"] = view


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
    """Where the listing picker should open, for one machine -- and, for this
    machine when it is a Windows one, its drives.

    The listing is the Browse of every "file or folder" field on Windows (the
    system dialogs there take one kind at a time; see browsePicker.js), and a
    listing that starts in the home folder has no way to a `D:\\` or a mapped
    share except typing it. `os.listdrives()` only NAMES them -- a mapped
    drive that is not answering is not touched until somebody opens it.
    """
    node = (request.args.get('node') or '').strip()
    return jsonify(**_picker_record(node), view=_view_record(),
                   drives=[] if node else _drives())


def _drives():
    """This machine's drive roots (`C:\\`, `D:\\`), or [] off Windows."""
    import os
    import sys

    if not sys.platform.startswith("win") or not hasattr(os, "listdrives"):
        return []
    try:
        return sorted(str(drive) for drive in os.listdrives())
    except OSError:
        return []


@app.route('/picker_prefs', methods=['POST'])
def save_picker_prefs():
    """Record a directory the user actually chose, (un)pin or reorder pins, or
    change how the explorer is laid out.

    Written once per successful pick -- `last_dir` and `add_recent` together --
    rather than on every step through the tree: this is a file on disk, and
    walking six directories deep should not be six read-modify-writes.

    `pinned_order` is the pins as the user dragged them: the ones it names
    that are pinned come first, in its order, and any pin it does not name
    keeps its place after them -- so an order sent from a stale sidebar can
    neither drop a pin nor bring back an unpinned one. `view` is merged over
    the stored one (see `_clean_view`) and is the same for every machine.
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
        order = payload.get('pinned_order')
        if order is not None and (not isinstance(order, list) or not all(
                isinstance(item, str) for item in order)):
            raise ValueError("pinned_order must be a list of paths")
        raw_view = payload.get('view')
        if raw_view is not None:
            _clean_view(raw_view, _fresh_view())   # refuse a bad key before writing
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    # Read, merge and write as one step: a pin and the explorer closing land
    # together, and merging into a record read before the other's write
    # landed dropped the pin.
    with paths.settings_lock():
        view = None if raw_view is None else _clean_view(raw_view, _view_record())
        record = _update_picker_record(node, last_dir, add_recent, pin, unpin,
                                       order, view)
    return jsonify(**record, view=view if view is not None else _view_record())


def _update_picker_record(node, last_dir, add_recent, pin, unpin, order, view):
    """Apply one request's changes to one machine's record and store it."""
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
    if order is not None:
        pinned = record["pinned"]
        ordered = list(dict.fromkeys(p for p in order if p in pinned))
        record["pinned"] = ordered + [p for p in pinned if p not in ordered]

    _store_picker_record(node, record, view=view)
    return record
