from plexora import app, get_config, get_config_names, config_json_path
from plexora.server.modules.registry import get_available_tools
from flask import render_template, send_from_directory, request
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
        # What module is installed on this process (env-var driven, fixed at
        # startup). Distinct from active_tool below, which is what the
        # current page view actually asked to see.
        'active_module': app.config.get('PLEXORA_ACTIVE_MODULE', ''),
        'active_tool': '',
        'available_tools': [],
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
        datasource = ''
    else:
        _stamp_last_opened(datasource)
    image_kind = get_config().get(datasource, {}).get('image_kind') if datasource else None

    # A tool is only ever shown if it's both requested via ?tool= and
    # actually the module installed on this process -- this is what makes
    # tool visibility per-request instead of a permanent process-wide flag.
    requested_tool = request.args.get('tool', '')
    installed_module = app.config.get('PLEXORA_ACTIVE_MODULE', '')
    active_tool = requested_tool if requested_tool and requested_tool == installed_module else ''

    return render_template(
        'index.html',
        data=template_data(
            datasource=datasource,
            datasources=datasources,
            image_kind=image_kind,
            active_tool=active_tool,
            available_tools=get_available_tools(app),
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
