"""What the desktop app asks of its server, beyond what any page asks.

Two routes. `/desktop/open` is where a file or folder arrives when it was
dropped on the window, opened from Explorer or Finder with "Open with
Plexora", or handed over by a second launch: paths, never bytes, because the
server that reads them is on the same machine. `/desktop/info` is what Help >
About, "Show Data Folder" and the Tools menu are drawn from.

Both are registered in every mode, and neither is special to the shell: a
browser tab of a terminal `plexora` can call them too, and `/desktop/info`
says plainly which kind of server it is talking to. What stops a stranger
calling them is the same token guard as every other route, which the desktop
server always runs with.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from flask import jsonify, request

from plexora import app, get_config_names, paths


def _base_url():
    return app.config.get('PLEXORA_BASE_URL', '')


def _project_at(path):
    """The name of the registered project whose directory IS `path`, or None.

    Dropping a project folder on the window means "open it", not "import its
    contents as a new sample" -- which would find the project's own derived
    pyramids and propose them as images.
    """
    try:
        candidate = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    names = set(get_config_names())
    if candidate.name not in names:
        return None
    for root in paths.roots():
        try:
            if paths.project_dir(candidate.name, root).resolve() == candidate:
                return candidate.name
        except (OSError, RuntimeError):
            continue
    return None


@app.route('/desktop/open', methods=['POST'])
def desktop_open():
    """Open, or import and open, whatever these paths are.

    `{"paths": [...], "name"?, "dataset"?, "context"?}` -> `{"project", "url",
    "existing"}`. Uses the same engine as the Import Sample dialog
    (`import_sample`), so a Xenium run dropped on the window becomes exactly
    the project the dialog would have made, and data that is already
    registered is reopened rather than copied.

    `context` is a launch context (`page_routes.launch_from_dict`: channels,
    color_by, highlight_ids, regions, viewport, tool), validated here and
    carried in the returned `url`, so the page opens showing what the caller
    asked for -- SCIMAP Pro opening its table's image coloured by a phenotype,
    say -- without that view being saved over the project's own.
    """
    from plexora.server.models import import_sample as importer
    from plexora.server.routes.import_routes import _picked

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="Expected a JSON object with `paths`."), 400
    raw = payload.get('paths')
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw \
            or not all(isinstance(entry, str) for entry in raw):
        return jsonify(error="`paths` must be a non-empty list of strings."), 400

    picked = [entry for entry in _picked({'paths': raw}) if entry]
    if not picked:
        return jsonify(error="`paths` must name at least one file or folder."), 400

    if len(picked) == 1:
        project = _project_at(picked[0])
        if project:
            return jsonify(project=project, url=_with_context(project, payload),
                           existing=True)

    missing = [entry for entry in picked
               if not entry.startswith('node://') and '://' not in entry
               and not Path(entry).exists()]
    if missing:
        return jsonify(error=f"Not found: {missing[0]}",
                       url=f"{_base_url()}/open_project"), 400

    try:
        result = importer.import_sample(
            picked,
            name=(payload.get('name') or '').strip() or None,
            dataset=payload.get('dataset'))
    except importer.NameTaken as exc:
        return jsonify(error=str(exc), suggestion=exc.suggestion), 409
    except (importer.ImportError_, ValueError) as exc:
        return jsonify(error=str(exc), url=f"{_base_url()}/open_project"), 400

    name = result['name']
    return jsonify(project=name, url=_with_context(name, payload),
                   existing=bool(result.get('existing')),
                   pending=bool(result.get('pending')))


def _with_context(project, payload):
    """The project's page URL, carrying the request's launch context."""
    import json
    from urllib.parse import urlencode

    from plexora.server.routes.page_routes import launch_from_dict

    url = f"{_base_url()}/{project}"
    context = payload.get('context') if isinstance(payload, dict) else None
    launch = launch_from_dict(context) if isinstance(context, dict) else {}
    query = {}
    tool = launch.pop('tool', None)
    if tool:
        # `?tool=` rather than inside `launch`: the page activates a tool from
        # that parameter, with its own checks (installed, ready, unlocked).
        query['tool'] = tool
    if launch:
        query['launch'] = json.dumps(launch, separators=(',', ':'))
    return url + (f"?{urlencode(query)}" if query else "")


def _tools():
    from plexora.server import plugins as plugin_registry

    listed = []
    for plugin in plugin_registry.tools(app):
        # Layers are never opened from a menu; they are part of the viewer.
        is_layer = getattr(plugin, 'is_layer_section', None)
        if callable(is_layer) and is_layer():
            continue
        listed.append({
            'name': plugin.name,
            'label': getattr(plugin, 'label', plugin.name),
            'shortcut': getattr(plugin, 'shortcut', None) or None,
            'menu': getattr(plugin, 'menu', 'tools') or 'tools',
        })
    return listed


def _version():
    from plexora.updates import current_version

    return current_version() or 'unknown'


@app.route('/desktop/info', methods=['GET'])
def desktop_info():
    """About this server, for Help > About and the native menus.

    Never the token or the port: the page already knows its own origin, and
    this is exactly the kind of document somebody pastes into a bug report.
    """
    from plexora.server import plugins as plugin_registry

    try:
        settings = str(paths.settings_path())
    except Exception:
        settings = ''
    return jsonify(
        desktop=bool(app.config.get('PLEXORA_DESKTOP')),
        version=_version(),
        python=platform.python_version(),
        executable=sys.executable,
        platform=platform.platform(),
        pid=os.getpid(),
        data_root=str(paths.data_root()),
        settings_path=settings,
        config_path=str(paths.config_path()),
        log_path=os.environ.get('PLEXORA_DESKTOP_LOG') or None,
        plugins=[plugin.name for plugin in plugin_registry.installed(app)],
        tools=_tools(),
        plan=_plan(),
    )


def _plan():
    """"Free", "Paid", "Paid (trial)"... for About. Never an identifier: this
    is the document people paste into bug reports."""
    try:
        from plexora import licensing

        state = licensing.peek()
    except Exception:
        return "Free"
    if state.state == "trial":
        return "Paid (trial)"
    if state.state == "grace":
        return "Paid (in grace period)"
    return "Paid" if state.paid else "Free"
