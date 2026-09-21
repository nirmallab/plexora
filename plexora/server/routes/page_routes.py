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
    }
    data.update(values)
    return data


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


def _parse_launch(raw):
    """`?launch=<json>` as a validated dict, or {} for anything unusable.

    This is state for ONE page view: which channels to turn on and which
    metadata column to draw. It is deliberately not persisted anywhere -- the
    client applies it in place of the project's saved channels and saved
    overlay, without writing it back -- so a notebook can open the same project
    a dozen times with a dozen different views of it and the project still
    remembers whatever the user last arranged by hand in the browser.

    Unparseable input is {}, not an error: the page renders exactly as it would
    have without the parameter, which is a viewer rather than a stack trace.
    """
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    launch = {}
    overlay = payload.get('overlay')
    if isinstance(overlay, str) and overlay:
        launch['overlay'] = overlay
    channels = _launch_channels(payload.get('channels'))
    if channels:
        launch['channels'] = channels
    return launch


@app.route("/")
def my_index():
    return render_template("index.html", data=template_data())


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
    active_tool = requested_tool if any(p.name == requested_tool for p in ready) else ''

    active = next((p for p in ready if p.name == active_tool), None)

    # Layer sections are not tools and do not go through any of the above.
    # They are part of what the viewer is for this sample, so they are
    # rendered and their assets loaded on every view of it -- no ?tool=, no
    # lazy fetch, no close button. `applies_to` rather than `satisfied_by`:
    # see plugins.layer_sections_for.
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

    return render_template(
        'index.html',
        data=template_data(
            datasource=datasource,
            datasources=datasources,
            image_kind=image_kind,
            active_tool=active_tool,
            available_tools=[p.describe() for p in offered],
            active_tool_scripts=(active.asset_urls('scripts', base_url)
                                 if active else []) + section_scripts,
            active_tool_styles=(active.asset_urls('styles', base_url)
                                if active else []) + section_styles,
            active_tool_panels=dict(active.panels) if active else {},
            layer_sections=section_entries,
            launch=_parse_launch(request.args.get('launch', '')),
        ),
    ), {DATASOURCE_HEADER: datasource}



@app.route('/client/<path:filename>')
def serveClient(filename):
    return send_from_directory(app.config['CLIENT_PATH'], filename, conditional=True)
