from plexora import app, get_config, get_config_names, paths
from plexora._resources import client_concurrency
from plexora.server.models.project import (
    Project, config_transaction, read_config, write_config,
)
from plexora.server import plugins as plugin_registry
from plexora.api.plugin import Plugin
from flask import abort, render_template, send_from_directory, request
from pathlib import Path
import datetime
import json
import os
import re


@app.context_processor
def inject_server_concurrency():
    """How many per-channel requests one page may have in flight at once.

    Advertised by the SERVER because only the server knows what it is running
    on: navigator.hardwareConcurrency describes the viewer's laptop, which says
    nothing about a 2-core SLURM allocation at the other end. Restoring saved
    channels used to fan out one request per channel simultaneously, and each
    of those costs a full-resolution channel read -- which is what buried the
    worker pool on a small allocation.

    A context processor rather than a key in `template_data`, deliberately.
    This number differs from machine to machine, and everything in
    `template_data` reaches the page as `window.flaskVariables`, which
    tests/test_plugin_boundary.py compares against a checked-in golden file.
    A machine-dependent value there would make that golden unportable -- it
    would pass on whoever regenerated it and fail everywhere else.
    """
    return {'server_concurrency': client_concurrency()}


#: Set by appRouter.js on a fetch that is asking for a page's CONTENT rather
#: than navigating to it. A header rather than a query parameter on purpose: the
#: URL a fragment is fetched from has to be the same URL the address bar ends up
#: showing, or the two answers drift and a bookmarked link stops matching what
#: the router asked for.
FRAGMENT_HEADER = 'X-Plexora-Fragment'

#: Names the project a viewer page belongs to, on the response. The client-side
#: router needs this because the list of projects baked into a rendered page is
#: a snapshot: a project registered after that render -- a Quick View of a new
#: file, a project added from Jupyter or another tab -- is not in it, and the
#: router would mount the viewer template as a fragment over the live viewer
#: with the scripts already marked as run, leaving an inert shell. Asking the
#: server which project it just served is the only answer that cannot be stale.
DATASOURCE_HEADER = 'X-Plexora-Datasource'


@app.context_processor
def inject_layout():
    """Which layout every page template extends, decided per request.

    A context processor rather than an argument, so no route has to know the
    client-side router exists -- `render_template("settings.html", ...)` is
    unchanged and serves both shapes. Every page template says
    `{% extends layout %}`; this is what fills it in.

    The default matters more than the fragment case: a request without the
    header -- a bookmark, a hard reload, a browser with JavaScript off, a
    test -- gets the whole document exactly as before.
    """
    fragment = request.headers.get(FRAGMENT_HEADER) == '1'
    return {'layout': '_fragment.html' if fragment else 'base.html'}


def licensed(entitlement):
    """Whether this licence unlocks `entitlement`, for a template to decide
    whether a Pro control is drawn at all: `{% if licensed('volume:objects') %}`.

    Not granted means not shown -- no badge, no greyed button, no "needs a
    licence" text; the server still refuses the action if it is called
    directly. Never raises: licensing misbehaving reads as not granted.
    """
    if not entitlement:
        return True
    try:
        from plexora import licensing

        return bool(licensing.allows(entitlement))
    except Exception:
        return False


@app.context_processor
def inject_licensed():
    """`licensed(entitlement)` in every template. A context processor rather
    than a `template_data` key, for the reason `inject_server_concurrency`
    gives: `data` reaches the page as `window.flaskVariables` and is
    golden-tested, and this is a function of the licence on this machine."""
    return {'licensed': licensed}


def _client_node_name():
    """The node on the browser's own machine, named, or ''.

    Reads the registry and contacts nothing, because this runs on every page
    render. A node that is registered but asleep still means the Local option
    belongs on the form: the answer to "is it awake" arrives when a field
    actually asks it something, with a message about that field.
    """
    try:
        from plexora import nodes as node_api

        node = node_api.client_node()
    except Exception:
        # A missing or unreadable nodes.json is an ordinary state, and it must
        # not be able to stop a page rendering.
        return ''
    return node.name if node is not None else ''


def server_is_remote():
    """Whether this Plexora is running somewhere other than the user's desk.

    The fact a data field needs before it can mean anything by "Local". When
    Plexora runs on the machine the browser is on -- an ordinary desktop launch
    -- Local and the server are the same filesystem, and a path box is already
    pointing at the user's own files. When it does not, the same path box means
    a cluster's filesystem, and saying "Local" about it would be a lie.

    Four ways to be sure. Three are things the process was told rather than
    guessed: notebook/hosted mode, `--remote` (a machine reached over SSH), and
    `--ood` (an Open OnDemand portal). The fourth is evidence rather than a
    flag, and it is the strongest of them -- a node registered as the BROWSER's
    own machine. Only `plexora connect` registers one, and it does so from the
    far end of a tunnel, so its existence means the browser is somewhere this
    process is not. It is what covers the case the flags miss: a session
    somebody tunnelled by hand.

    Beyond those, nothing is guessed. What that costs is a Local option
    offering a CSV upload where it could have offered more, which is a smaller
    error than claiming to read files on a machine this process cannot see.
    """
    return bool(app.config.get('PLEXORA_NOTEBOOK_MODE')
                or app.config.get('PLEXORA_SERVER_IS_REMOTE')
                or _client_node_name())


def image_streamed(project):
    """Whether a sample's image is read from a web address with no compute
    beside it (http(s), s3, gs, az ...: `is_remote_locator`), rather than off
    a disk some Plexora reads -- this machine's, or a data node's. What the
    viewer's default tile quality turns on (main.js): from a web address
    every tile is built here from ranged reads across the link, and a 16-bit
    HD PNG is many times the bytes of a fast WebP one. A data node builds its
    tiles where the image is (the user's rule, 2026-10-19: a node does not
    count; through an ssh tunnel to HMS O2 a 2.1 MB HD tile took ~35 ms).

    Nor does a files-only server's `sftp://` image (the user's rule,
    2026-10-20, for HMS O2's transfer node). The source reads behind a tile
    are the same in HD and fast; only the encoding for the browser beside
    this server differs, so HD costs that link nothing.
    """
    from plexora.server.providers.base import is_remote_locator

    binding = project.resources.get("image") if project.resources else None
    if binding is not None and binding.is_node:
        return False
    src = str(project.image.src or "")
    if src[:7].lower() == "sftp://":
        return False
    return bool(is_remote_locator(src))


def template_data(**values):
    base_url = app.config.get('PLEXORA_BASE_URL', '')
    data = {
        'datasource': '',
        'datasources': get_config_names(),
        'is_docker': app.config.get('IS_DOCKER', False),
        # Whether this process is a notebook sidecar or behind a hosted proxy.
        # Templates use it to hide controls that act on the SERVER's machine --
        # Quit, native file dialogs -- which in that mode is not the user's.
        'notebook_mode': app.config.get('PLEXORA_NOTEBOOK_MODE', False),
        # Whether this page is inside the desktop app's window, or a browser
        # tab of the server the app started. Either way it shows "Open in
        # Browser"; desktopBridge.js decides which of the two it is.
        'desktop': app.config.get('PLEXORA_DESKTOP', False),
        # The name of the data node running on the machine the browser is on,
        # or ''. It is what lets every data-selection field offer a Local /
        # Remote choice and mean the user's own computer by "Local" -- see
        # nodes.client_node. Absent on a plain desktop launch, where the two
        # machines are the same one and the choice would be noise.
        'client_node': _client_node_name(),
        # Whether the machine running this process is the user's own. It is
        # what decides which half of a data field's Local/Remote switch needs a
        # data node behind it and which is just a path box -- see
        # server_is_remote.
        'server_is_remote': server_is_remote(),
        'base_url': base_url,
        # Menu entries contributed by installed plugins, as data rather than
        # markup -- core renders them with its own classes. Filled in here, in
        # the one place every page's context is built, because base.html's File
        # menu is on every page and a key it reads must never be absent.
        # Empty on a core-only build, where every consumer renders nothing.
        'plugin_nav_items': plugin_registry.nav_items(app, base_url),
        # Which tool this page view asked to open, and which tools the
        # navbar should offer. Both are per-request: several plugins can be
        # installed at once, and which of them apply depends on the
        # datasource, so neither can be a process-wide flag.
        'active_tool': '',
        'available_tools': [],
        # Tools whose rows belong to the View menu rather than Tools -- core's
        # Rotate and Flip (server/core_tools.py). Split server-side by
        # `Plugin.menu` so both menus render from a list of one shape.
        'view_tools': [],
        # Assets and panel templates of the active plugin, so templates never
        # name one. Empty on every page that has no tool open.
        'active_tool_scripts': [],
        'active_tool_styles': [],
        'active_tool_panels': {},
        # Plugins that are viewer LAYERS rather than tools: mounted on load,
        # never in the Tools menu, never closed. `[{name, label, panel}]`, with
        # their assets folded into the two lists above -- see
        # `plugins.layer_sections_for` and `Plugin.LAYER_SECTION_SLOT`.
        'layer_sections': [],
        # What this one page view was asked to open showing -- channels on, a
        # metadata column drawn over the cells -- rather than what the project
        # has saved. Filled only by the viewer route, from `?launch=`, and never
        # written back to the project: see _parse_launch.
        'launch': {},
        # The licence as far as drawing the page goes: plan, state and grants,
        # never the certificate. A HINT for badges and Settings -- the server
        # refuses a Pro action whatever the page believes. Read without any
        # network call, and for a Free user it is one missing-file stat.
        'license': _license_summary(),
    }
    data.update(values)
    return data


def _license_summary():
    """`{plan, state, paid, entitlements}`, or Free if licensing misbehaves.

    Licensing never breaks a page: an exception here -- a damaged file, an
    unexpected payload -- renders as Free, which is what it would resolve to.
    """
    try:
        from plexora import licensing

        state = licensing.peek()
        return {'plan': licensing.plan_of(state), 'state': licensing.state_name(state),
                'paid': state.licensed, 'entitlements': list(state.entitlements)}
    except Exception:
        return {'plan': 'free', 'state': 'free', 'paid': False, 'entitlements': []}


#: Colours a launch request may carry, as the browser will accept them.
_LAUNCH_COLOR = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')

#: Ceiling on how many channels one `?launch=` may name. The sidebar tops out
#: at 15 slots, and a request for more is a malformed or hostile URL rather than
#: something to spend page-render time normalising.
_LAUNCH_MAX_CHANNELS = 15


def _launch_channels(raw):
    """The channel entries of a launch request that survive validation.

    Every field is checked and anything unrecognised is dropped rather than
    passed along. This runs on a query parameter -- which is to say on whatever
    somebody put in a URL -- and its output is rendered into the page as
    `window.flaskVariables.launch`, so nothing arbitrary may reach it.

    A malformed entry is dropped silently and the rest are kept. The caller that
    builds these URLs (`plexora.jupyter._launch_channels`) already raises on a
    bad colour or range at the call site, where there is somebody to read it;
    by the time it is a query string there is nobody, and refusing the whole
    page over one bad field would be the worse of the two failures.
    """
    entries = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        if not isinstance(name, str) or not name:
            continue
        entry = {'name': name}
        color = item.get('color')
        if isinstance(color, str) and _LAUNCH_COLOR.match(color):
            entry['color'] = color
        span = item.get('range')
        if (isinstance(span, list) and len(span) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in span)):
            entry['range'] = [float(span[0]), float(span[1])]
        entries.append(entry)
        if len(entries) >= _LAUNCH_MAX_CHANNELS:
            break
    return entries


#: Cell ids one launch may ask to point at. Resolved to positions here, so the
#: page is handed coordinates and never looks a cell up itself.
LAUNCH_MAX_HIGHLIGHT = 2000

#: Regions one launch may outline: the viewer's own `show_shapes` limit.
LAUNCH_MAX_REGIONS = 32

#: A tool name as `?tool=` takes it.
_LAUNCH_TOOL = re.compile(r'^[a-z][a-z0-9_]{0,63}$')

#: Ids are matched as text, and capped in length: a hostile URL must not make
#: the page render compare megabyte strings.
_LAUNCH_ID_MAX_CHARS = 128


def _launch_column(payload):
    """`color_by` (or the older `overlay`): a column name, or None."""
    for key in ('color_by', 'overlay'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and len(value) <= 256:
            return value.strip()
    return None


def _launch_ids(raw):
    if not isinstance(raw, list):
        return []
    ids = []
    for item in raw[:LAUNCH_MAX_HIGHLIGHT]:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float, str)) and len(str(item)) <= _LAUNCH_ID_MAX_CHARS:
            ids.append(str(item))
    return ids


def _launch_regions(raw):
    """GeoJSON features (a FeatureCollection or a list) as `show_shapes`
    shapes: Polygon/MultiPolygon in full-resolution pixels, at most
    LAUNCH_MAX_REGIONS, each with a short label. Anything else is dropped."""
    if isinstance(raw, dict) and isinstance(raw.get('features'), list):
        raw = raw['features']
    if not isinstance(raw, list):
        return []
    shapes = []
    for index, feature in enumerate(raw):
        if not isinstance(feature, dict):
            continue
        geometry = feature.get('geometry') if feature.get('type') == 'Feature' else feature
        if not isinstance(geometry, dict) or geometry.get('type') not in ('Polygon',
                                                                           'MultiPolygon'):
            continue
        if not isinstance(geometry.get('coordinates'), list):
            continue
        properties = feature.get('properties') if isinstance(feature.get('properties'),
                                                             dict) else {}
        label = properties.get('name') or properties.get('category') or ''
        shape = {'id': f'launch_{index}', 'geometry': {'type': geometry['type'],
                                                       'coordinates': geometry['coordinates']},
                 'label': str(label)[:24]}
        color = properties.get('category_color') or properties.get('color')
        if isinstance(color, str) and re.match(r'^#[0-9a-fA-F]{6}$', color):
            shape['color'] = color
        shapes.append(shape)
        if len(shapes) >= LAUNCH_MAX_REGIONS:
            break
    return shapes


def _launch_viewport(raw):
    if not isinstance(raw, dict):
        return None
    try:
        box = {key: float(raw[key]) for key in ('x', 'y', 'width', 'height')}
    except (KeyError, TypeError, ValueError):
        return None
    if box['width'] <= 0 or box['height'] <= 0 or not all(map(_finite, box.values())):
        return None
    return box


def _finite(value):
    return value == value and value not in (float('inf'), float('-inf'))


def launch_from_dict(payload, project=None):
    """A launch context as a validated dict; {} for anything unusable.

    One page view's state, never persisted: which channels to turn on, which
    column to colour the cells by, which cells to point at, which regions to
    outline, where to look and which tool to open. The client applies it
    (`viewerSidebar.js` the channels, `services/launchContext.js` the rest)
    in place of the project's saved arrangement without writing it back -- so
    a notebook, SCIMAP Pro's `hl.viewImage` or a desktop deep link can open the
    same project a dozen times with a dozen different views of it, and the
    project still remembers whatever the user last arranged by hand.

    Every field is checked and anything unrecognised is dropped rather than
    passed along: this runs on whatever somebody put in a URL, and its output is
    rendered into the page as `window.flaskVariables.launch`.

    `highlight_ids` are cell ids as the table names them; with `project` they
    are resolved here to `{id, x, y}` in full-resolution pixels (ids the table
    does not have are dropped). Without a project they are left as ids, for a
    caller that only validates (`/desktop/open`, before the page exists).
    """
    if not isinstance(payload, dict):
        return {}
    launch = {}
    column = _launch_column(payload)
    if column:
        # `color_by` is the bridge's word, `overlay` the one Cell Explorer has
        # always read on open; the page is handed one name for one thing.
        launch['overlay'] = column
    channels = _launch_channels(payload.get('channels'))
    if channels:
        launch['channels'] = channels
    ids = _launch_ids(payload.get('highlight_ids'))
    if ids:
        if project is None:
            launch['highlight_ids'] = ids
        else:
            cells = _resolve_cells(project, ids)
            if cells:
                launch['highlight'] = cells
    regions = _launch_regions(payload.get('regions'))
    if regions:
        launch['regions'] = regions
    viewport = _launch_viewport(payload.get('viewport'))
    if viewport:
        launch['viewport'] = viewport
    tool = payload.get('tool')
    if isinstance(tool, str) and _LAUNCH_TOOL.match(tool):
        launch['tool'] = tool
    return launch


_LAUNCH_SESSION = None


def _launch_session():
    """The table reads a launch link resolves ids with: an agent session of its
    own, so resolving them neither loads the image nor swaps the datasource the
    viewer has loaded (see plexora/agent/session.py)."""
    global _LAUNCH_SESSION
    if _LAUNCH_SESSION is None:
        from plexora.agent.session import AgentSession

        _LAUNCH_SESSION = AgentSession(table_limit=1)
    return _LAUNCH_SESSION


def _resolve_cells(project, ids):
    """`[{id, x, y}]` for the ids this project's table has, in its own
    coordinates (full-resolution pixels). [] when the table cannot be read --
    the page then opens without the highlight, which is a viewer rather than
    an error page."""
    try:
        import polars as pl

        data = _launch_session().data(project)
        schema = data.schema
        if schema is None or not data.table.available:
            return []
        frame = data.table.geometry()
        column = schema.cell_id if schema.cell_id in frame.columns else 'id'
        wanted = set(ids)
        match = frame.filter(pl.col(column).cast(pl.Utf8).is_in(list(wanted)))
        cells = []
        for row in match.select([column, schema.x, schema.y]).iter_rows():
            cells.append({'id': row[0] if isinstance(row[0], (int, str)) else str(row[0]),
                          'x': float(row[1]), 'y': float(row[2])})
        return cells[:LAUNCH_MAX_HIGHLIGHT]
    except Exception:
        return []


def _parse_launch(raw, project=None):
    """`?launch=<json>` as a validated dict, or {} for anything unusable; see
    `launch_from_dict`.

    Unparseable input is {}, not an error: the page renders exactly as it would
    have without the parameter, which is a viewer rather than a stack trace.
    """
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return launch_from_dict(payload, project)


@app.route("/")
def my_index():
    return render_template("index.html",
                           data=template_data(recent_projects=_recent_rows()))


#: The words a library card puts on each image kind. openProjectPage.js's
#: IMAGE_KINDS is the list -- change that one first, then this. Anything
#: unlisted is "Image", which is true of every kind and misleads about none.
_IMAGE_KIND_WORDS = {
    "ome_tiff": "OME-TIFF", "tiff": "TIFF", "ome_zarr": "OME-Zarr",
    "brightfield": "H&E", "rgb": "Image", "dicom": "DICOM", "blank": "No image",
}


def _opened_phrase(iso, now=None):
    """openProjectPage.js's `timeAgo`, worked out here: "3 hours ago", "2 days
    ago", then the date.

    Here rather than in the browser because `lastOpenedAt` is this machine's
    local time with no zone on it, and a browser on another machine would be
    measuring it against its own clock.
    """
    try:
        then = datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    now = now or datetime.datetime.now(then.tzinfo)
    seconds = (now - then).total_seconds()
    if seconds < 60:
        return "just now"
    for size, unit, limit in ((60, "minute", 60), (3600, "hour", 24), (86400, "day", 30)):
        count = round(seconds / size)
        if count < limit:
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return f"{then:%b} {then.day}, {then.year}"


def _recent_rows(limit=3):
    """The home page's Recent list: the projects opened last, each as the line
    a library card would say about it.

    Rendered into the page rather than fetched after it, so the three rows are
    there on the first paint -- the landing column is centred, and a list that
    arrived late would push the card up under the pointer.
    """
    # Imported here: project_routes imports this module for template_data.
    from plexora.server.routes.project_routes import recent_projects

    rows = []
    for card in recent_projects(limit):
        kind = card.get("imageKind")
        layers = (card.get("layers") or {}).get("count") or 0
        facts = []
        if card.get("dataset"):
            facts.append(card["dataset"]["name"])
        if kind:
            facts.append(_IMAGE_KIND_WORDS.get(kind, "Image"))
        if card.get("segmentation") == "present":
            facts.append("Mask")
        if card.get("table") == "present":
            facts.append("Data")
        if layers:
            facts.append(f"{layers} layer{'' if layers == 1 else 's'}")
        if card.get("needsSetup"):
            facts.append("Needs setup")
        # The tile is a hint at what opens, not a badge: a slide, a sample
        # whose picture is mostly its layers, or channels.
        if kind in ("brightfield", "rgb"):
            tone, icon = "slide", "fa-image"
        elif layers or kind == "blank":
            tone, icon = "spatial", "fa-table-cells"
        else:
            tone, icon = "channels", "fa-layer-group"
        rows.append({
            "name": card["name"],
            # What the row says; `name` is still what it links to.
            "title": card.get("displayName") or card["name"],
            "meta": " · ".join(facts),
            "opened": card.get("lastOpenedAt") or "",
            "when": _opened_phrase(card.get("lastOpenedAt")),
            "remote": ", ".join(card.get("remote") or []),
            "tone": tone,
            "icon": icon,
        })
    return rows


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        Path(app.config['CLIENT_PATH']) / 'src' / 'img', 'favicon.ico', conditional=True
    )


def _stamp_last_opened(datasource):
    """Record when a project was last opened, for the Open Project page's
    "Recently Opened" sort. Best-effort: a failure here should never break
    opening the viewer itself.

    Skipped outright for a project on a read-only shared root. Catching the
    failure would be correct but slow: write_config retries for two seconds
    past what looks like a transient Windows sharing violation, and paying that
    on every open of a shared project would be a visible stall for a sort key.
    """
    config_file = Project.config_path_for(datasource)
    if not paths.is_writable(config_file.parent):
        return
    try:
        with config_transaction():
            config_data = read_config(config_file)
            if datasource in config_data:
                config_data[datasource]['lastOpenedAt'] = datetime.datetime.now().isoformat()
                write_config(config_file, config_data)
    except (OSError, ValueError):
        pass


@app.route('/<string:datasource>')
def image_viewer(datasource):
    datasources = get_config_names()
    if datasource not in datasources:
        # This rule matches any single path segment, so it is the last thing
        # standing between a wrong URL and a 404. It used to render the empty
        # viewer instead, which meant a request for a route that does not exist
        # -- a removed plugin's endpoint, a typo, a probe -- came back 200 with
        # a full HTML page. Callers expecting JSON got HTML and no error.
        abort(404)
    _stamp_last_opened(datasource)
    project = Project.load(datasource)
    image_kind = project.image.kind

    # Two different lists on purpose. The menu offers every tool COMPATIBLE
    # with this datasource, including ones still missing an input -- opening
    # those is how the user gets asked for it. A tool is only ACTIVATED if it
    # can actually run, so a stale ?tool= link to an uninstalled, inapplicable
    # or not-yet-ready tool renders the plain viewer rather than a panel that
    # cannot work.
    base_url = app.config.get('PLEXORA_BASE_URL', '')
    offered = plugin_registry.tools_for(app, project)
    ready = plugin_registry.ready_tools(app, project)
    requested_tool = request.args.get('tool', '')
    # A Pro tool this licence does not unlock is not in `ready`, so a `?tool=`
    # link to it renders the plain viewer (plugins.unlocked).
    active_tool = requested_tool if any(p.name == requested_tool for p in ready) else ''

    active = next((p for p in ready if p.name == active_tool), None)

    # Layer sections are not tools and do not go through any of the above.
    # They are part of what the viewer is for this sample, so they are
    # rendered and their assets loaded on every view of it -- no ?tool=, no
    # lazy fetch, no close button. `applies_to` rather than `satisfied_by`:
    # see plugins.layer_sections_for.
    # A Pro layer plugin is not mounted on a licence that does not unlock
    # it (plugins.unlocked); its data stays on disk untouched.
    sections = plugin_registry.layer_sections_for(app, project)
    section_scripts, section_styles = [], []
    section_entries = []
    for section in sections:
        section_scripts.extend(section.asset_urls('scripts', base_url))
        section_styles.extend(section.asset_urls('styles', base_url))
        # Which LAYER this section belongs to. The panel is the body of that
        # layer's card in the Layers list, and the card is built from /config
        # before the plugin's own JavaScript runs -- so core needs the answer
        # here. The modality is carried as well as the id because a layer
        # imported mid-session arrives after this page was rendered: its id
        # was not known to write down, and the card then matches the mount by
        # what the layer IS. Both empty for a section whose layer is absent,
        # which is a section showing its own "nothing to show" state.
        layer = section.requires.first_layer(project)
        section_entries.append({
            'name': section.name,
            'label': section.label,
            'panel': section.panels[Plugin.LAYER_SECTION_SLOT],
            'layer_id': layer.id if layer else '',
            'modality': (layer.modality or '') if layer else '',
        })

    # Scripts a plugin wants on every viewer page, its tool open or not (a
    # store core's bookmarks talk to; Plugin.viewer_scripts). First, and once:
    # a script also in the open tool's list is the same URL, and a second
    # <script> of a file that declares a class is a SyntaxError.
    viewer_scripts = [url for plugin in plugin_registry.viewer_scripts_for(app, project)
                      for url in plugin.asset_urls('viewer_scripts', base_url)]
    tool_scripts = list(dict.fromkeys(
        viewer_scripts + (active.asset_urls('scripts', base_url) if active else []) + section_scripts))

    return render_template(
        'index.html',
        data=template_data(
            datasource=datasource,
            datasources=datasources,
            image_kind=image_kind,
            # Whether the image comes over a network: the viewer starts in
            # HD only when it does not (main.js).
            image_streamed=image_streamed(project),
            active_tool=active_tool,
            available_tools=[p.describe() for p in offered if p.menu == 'tools'],
            view_tools=[p.describe() for p in offered if p.menu == 'view'],
            active_tool_scripts=tool_scripts,
            active_tool_styles=(active.asset_urls('styles', base_url)
                                if active else []) + section_styles,
            active_tool_panels=dict(active.panels) if active else {},
            layer_sections=section_entries,
            launch=_parse_launch(request.args.get('launch', ''), datasource),
        ),
        # The display name, beside `data` rather than in it: `datasource`
        # stays the key every script fetches by, and this is only what the
        # page's title and the canvas's corner label say.
        project_title=project.display_name,
    ), {DATASOURCE_HEADER: datasource}



@app.route('/client/<path:filename>')
def serveClient(filename):
    return send_from_directory(app.config['CLIENT_PATH'], filename, conditional=True)
