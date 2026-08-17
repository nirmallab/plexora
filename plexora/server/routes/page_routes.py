from plexora import app, get_config, get_config_names, config_json_path
from plexora.server import plugins as plugin_registry
from flask import abort, render_template, send_from_directory, request
from pathlib import Path
import datetime
import json
import os


def template_data(**values):
    data = {
        'datasource': '',
        'datasources': get_config_names(),
        'is_docker': app.config.get('IS_DOCKER', False),
        'base_url': app.config.get('PLEXORA_BASE_URL', ''),
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
    opening the viewer itself."""
    try:
        with open(config_json_path, "r+") as config_file:
            config_data = json.load(config_file)
            if datasource in config_data:
                config_data[datasource]['lastOpenedAt'] = datetime.datetime.now().isoformat()
                config_file.seek(0)
                json.dump(config_data, config_file, indent=4)
                config_file.truncate()
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
    entry = get_config().get(datasource) or {}
    image_kind = entry.get('image_kind')

    # Two different lists on purpose. The menu offers every tool COMPATIBLE
    # with this datasource, including ones still missing a feature table --
    # opening those is how the user gets to the page that attaches one. A tool
    # is only ACTIVATED if it can actually run, so a stale ?tool= link to an
    # uninstalled, inapplicable or not-yet-ready tool renders the plain viewer
    # rather than a panel that cannot work.
    base_url = app.config.get('PLEXORA_BASE_URL', '')
    offered = plugin_registry.tools_for(app, entry)
    ready = plugin_registry.ready_tools(app, entry)
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
    attach_to = request.args.get('attach_to', '')
    return_tool = request.args.get('return_tool', '')
    attach_channel_file = ''
    if attach_to:
        entry = get_config().get(attach_to)
        if entry is None:
            # Stale/bookmarked link -- fall back to a plain fresh import
            # rather than prefilling from a datasource that no longer exists.
            attach_to = ''
            return_tool = ''
        else:
            attach_channel_file = entry.get('channelFile', '')
    return render_template(
        "upload.html",
        data=template_data(
            attach_to=attach_to,
            attach_return_tool=return_tool,
            attach_channel_file=attach_channel_file,
        ),
    )




@app.route('/client/<path:filename>')
def serveClient(filename):
    return send_from_directory(app.config['CLIENT_PATH'], filename, conditional=True)
