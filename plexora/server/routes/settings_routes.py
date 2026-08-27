"""The Settings page, and the one section it currently has.

A page rather than a dialog because the list of things worth setting is only
going to grow, and the left rail is the shape that absorbs that without a
redesign: a section is a `<button>` in the rail plus a `<section>` in the body,
and nothing else on the page has to know it arrived.

**Changing the data directory does not move this process.** `paths.data_root()`
is resolved once per process and everything downstream is holding the result --
an open TIFF or zarr handle in data_model, a SQLite connection per figure, a
tile pyramid path resolved at import. Calling `paths.reset()` here would
repoint the next request at a directory the loaded datasource does not live in,
which fails as a stack trace from whichever tile read got there first. So the
new directory is RECORDED and takes effect at the next start, and the page says
so rather than pretending otherwise. That is also why the state below reports
`in_use` and `pending` separately: between the write and the restart they are
genuinely different answers, and showing only one of them would be a lie in one
direction or the other.
"""

from flask import jsonify, render_template, request
from pathlib import Path
import os

from plexora import app, paths
from plexora.server.models import data_migration
from plexora.server.routes.page_routes import template_data

#: Sections of the settings page, in rail order. One entry, one tab; the
#: template renders both the rail button and the panel shell from this, so
#: adding a section is this list plus a `{% block %}` of markup -- no new route
#: and no change to the page's JavaScript.
SECTIONS = (
    {"id": "data", "label": "Data", "icon": "fa-folder-tree",
     "blurb": "Where Plexora keeps your projects and figures."},
    {"id": "nodes", "label": "Data nodes", "icon": "fa-network-wired",
     "blurb": "Other machines that hold image or cell data."},
)


@app.route('/settings')
def settings_page():
    # `sections` is a render argument rather than part of `template_data`,
    # which reaches the page as window.flaskVariables and is golden-tested.
    # Nothing in the browser needs this list -- the rail is server-rendered --
    # so putting it there would only widen a pinned payload.
    return render_template('settings.html', data=template_data(), sections=SECTIONS)


def _resolve(raw):
    """A user-typed path as an absolute one, or None if it is not usable.

    `expanduser` before `resolve` so `~/plexora-data` works, which is what
    anyone types first. Resolve rather than absolute: a relative path here
    would be relative to the SERVER's working directory, which on a cluster is
    wherever the batch script happened to start.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _record(path):
    """Persist the chosen directory, without disturbing this process.

    Deliberately does NOT call `paths.reset()` -- see the module docstring.
    """
    settings = paths.read_settings()
    settings["data_dir"] = str(path)
    paths.write_settings(settings)


def _env_override():
    """The environment variable that is overriding the settings file, or "".

    Checked by name rather than by comparing resolved paths: the notebook
    sidecar and `plexora --data-dir` both export it, and in both cases a change
    made here would be recorded correctly, ignored completely, and look like a
    bug. Saying which variable is winning is the whole point.
    """
    value = (os.environ.get(paths.ENV_DATA_PATH) or "").strip()
    return paths.ENV_DATA_PATH if value else ""


def _state():
    resolution = paths.data_root_resolution()
    stored = paths.read_settings().get("data_dir")
    stored = stored.strip() if isinstance(stored, str) else ""
    override = _env_override()
    # "Pending" only when the recorded value differs from what this process is
    # actually serving from. Equal values are the ordinary case and must not
    # draw a restart banner.
    pending = stored if stored and Path(stored) != resolution.path else ""
    return {
        "in_use": str(resolution.path),
        "rule": resolution.rule,
        "pending": pending,
        "settings_file": str(paths.settings_path()),
        "env_override": override,
        "notebook_mode": bool(app.config.get('PLEXORA_NOTEBOOK_MODE')),
        "shared_roots": [str(path) for path in paths.shared_roots()],
        "entry_count": len(data_migration.migratable(resolution.path)),
    }


@app.route('/settings/data')
def settings_data():
    return jsonify(_state())


@app.route('/settings/data/check', methods=['POST'])
def settings_data_check():
    """What choosing this directory would mean, before anything is written.

    Every question the confirm step needs is answered here rather than in the
    browser, because all of them -- does it exist, can it be written to, is it
    on the same filesystem, what is already in it -- are questions about the
    SERVER's filesystem, and the page has no access to that at all.
    """
    payload = request.get_json(silent=True) or {}
    target = _resolve(payload.get('path'))
    if target is None:
        return jsonify(error="Enter a directory path."), 400

    current = paths.data_root()
    result = data_migration.plan(current, target).describe()
    result.update({
        "path": str(target),
        "exists": target.is_dir(),
        "is_current": target == current,
        # data_migration.can_write, never paths.is_writable: this route is a
        # preview and must not create the directory it is asked about.
        "writable": data_migration.can_write(target),
        "entries_here": len(data_migration.migratable(target)),
    })
    return jsonify(result)


@app.route('/settings/data', methods=['POST'])
def settings_data_set():
    payload = request.get_json(silent=True) or {}
    target = _resolve(payload.get('path'))
    mode = payload.get('migrate') or data_migration.MODE_NONE

    if target is None:
        return jsonify(error="Enter a directory path."), 400
    if mode not in data_migration.MODES:
        return jsonify(error=f"Unknown migration mode: {mode}"), 400

    override = _env_override()
    if override:
        # Refused rather than written-and-ignored. Recording a preference that
        # provably cannot take effect is worse than declining: the user would
        # restart, land in the same directory, and have no way to tell why.
        return jsonify(
            error=f"{override} is set for this server, and it overrides the "
                  f"stored setting. Unset it and restart Plexora to choose a "
                  f"directory here."
        ), 409
    if data_migration.is_running():
        return jsonify(error="A migration is already running."), 409

    current = paths.data_root()
    if mode == data_migration.MODE_NONE:
        # Covers "point me at a root that already holds my projects", which is
        # the ordinary case on a second machine, and the no-op of re-choosing
        # the current directory. Neither touches a file.
        _record(target)
        return jsonify(_state())

    proposal = data_migration.plan(current, target)
    if not proposal.can_migrate:
        return jsonify(error="This directory cannot be migrated into.",
                       **proposal.describe()), 409

    # The write is the job's LAST step, not its first -- see data_migration's
    # module docstring. A migration that dies half way leaves the setting
    # pointing at the root that still holds the data.
    data_migration.start(current, target, mode, lambda: _record(target))
    return jsonify(_state() | {"migration": data_migration.status()}), 202


@app.route('/settings/data/migration')
def settings_data_migration():
    return jsonify(data_migration.status())


# -- data nodes -------------------------------------------------------------
#
# A node is a machine that holds image or cell data this Plexora reads over the
# network. It is registered here rather than per project because one node
# routinely serves several, and because the token is rotated in one place.
#
# Nothing here ever sends a token back to the browser. The page needs to know
# WHICH nodes exist and whether they answer; the secret is the server's, and a
# settings page that displayed it would put it in a screenshot the first time
# somebody asked for help.


def _node_view(node, resources=None, error=None):
    return {
        "name": node.name,
        "endpoint": node.endpoint,
        "browser_endpoint": node.browser_endpoint,
        "node_id": node.node_id,
        "plexora_version": node.plexora_version,
        "last_seen": node.last_seen,
        "has_token": bool(node.token),
        "reachable": error is None and resources is not None,
        "error": error,
        "resources": resources or [],
    }


@app.route('/settings/nodes')
def settings_nodes():
    """Every registered node, and what each one is serving right now.

    Contacted on load rather than reported from the record, because "is it
    reachable" is the question this page exists to answer and a stored
    `last_seen` answers a different one. Each is probed independently so one
    sleeping laptop does not blank the list.
    """
    from plexora.server.models import nodes as node_registry
    from plexora.server.providers import http as node_http

    listed = []
    for node in node_registry.load_all().values():
        try:
            hello = node_http.hello(node, timeout=2.5)
            listed.append(_node_view(node, hello.get("resources") or []))
        except Exception as exc:
            listed.append(_node_view(node, None, str(exc)))
    return jsonify(nodes=listed)


@app.route('/settings/nodes', methods=['POST'])
def settings_nodes_add():
    """Register a node, after checking that it answers.

    The check is not optional here, unlike in the programmatic API: somebody
    typing an address into a form has no other way to find out they mistyped
    it, and a node that is recorded but wrong produces its first error later,
    in a project, wearing the costume of a broken project.
    """
    from plexora import nodes as node_api

    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    endpoint = (payload.get("endpoint") or "").strip()
    if not name:
        return jsonify(error="Give this node a name -- projects point at it by name."), 400
    if not endpoint:
        return jsonify(error="Enter the address the node is serving on."), 400
    try:
        node = node_api.register_node(
            name, endpoint,
            token=(payload.get("token") or "").strip(),
            browser_endpoint=(payload.get("browser_endpoint") or "").strip() or None,
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(
            error=f"Could not reach a Plexora data node at {endpoint}: {exc}"), 400
    return jsonify(node=_node_view(node, []))


@app.route('/settings/nodes/<name>', methods=['DELETE'])
def settings_nodes_remove(name):
    """Forget a node.

    Projects that read from it keep their bindings and will report the node
    unreachable, which is the honest outcome: forgetting an address is not the
    same as deciding a project no longer has a table, and silently unbinding
    every project here would be a much larger action than the button says.
    """
    from plexora import nodes as node_api
    from plexora.server.models.project import all_projects

    using = sorted(
        project.name for project in all_projects()
        if any(binding.node == name for binding in project.resources.values())
    )
    node_api.forget_node(name)
    return jsonify(ok=True, projects_affected=using)
