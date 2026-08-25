from plexora import app, get_config, get_config_names, paths
from plexora._resources import client_concurrency
from plexora.server.models.project import (
    Project, config_transaction, read_config, write_config,
)
from plexora.server import plugins as plugin_registry
from flask import abort, render_template, send_from_directory, request
from pathlib import Path
import datetime
import os


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
    }
    data.update(values)
    return data


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
    return render_template(
        'index.html',
        data=template_data(
            datasource=datasource,
            datasources=datasources,
            image_kind=image_kind,
            active_tool=active_tool,
            available_tools=[p.describe() for p in offered],
            active_tool_scripts=active.asset_urls('scripts', base_url) if active else [],
            active_tool_styles=active.asset_urls('styles', base_url) if active else [],
            active_tool_panels=dict(active.panels) if active else {},
        ),
    )



@app.route("/upload_page")
def upload_page():
    """The one import screen: image, optional mask, optional data.

    It has no "attach to an existing project" mode any more. Adding data to a
    project that already exists is an edit, and the edit page does it -- so
    there is one place that knows how to change a project rather than an
    import form with a second personality.
    """
    return render_template("upload.html", data=template_data())




@app.route('/client/<path:filename>')
def serveClient(filename):
    return send_from_directory(app.config['CLIENT_PATH'], filename, conditional=True)
