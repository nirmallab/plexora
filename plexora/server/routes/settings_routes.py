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
    {"id": "remotes", "label": "Remote servers", "icon": "fa-server",
     "blurb": "Clusters and workstations to run Plexora on."},
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


# -- remote servers ---------------------------------------------------------
#
# The same thing `plexora connect` does, driven from a page instead of a
# terminal: start Plexora on another machine, tunnel to it, open it here. The
# saved profiles are shared between the two front ends -- one remotes.json --
# so a server set up here is reachable as `plexora connect <name>` and the
# other way round.
#
# Nothing here ever holds a password. Credentials travel through the askpass
# relay at the bottom of this section and live in memory for the seconds
# between the user typing one and ssh consuming it; see plexora/askpass.py.


def _remote_view(remote, session=None):
    """A saved profile plus whatever its live connection is doing."""
    view = {
        "name": remote.name,
        "target": remote.target,
        "remote_command": remote.remote_command,
        "datasource": remote.datasource,
        "data_dir": remote.data_dir,
        "srun": remote.srun,
        "bind_node": remote.bind_node,
        "jump": remote.jump,
        "forwards": list(remote.forwards),
        "serve": list(remote.serve),
        "local_serve": list(remote.local_serve),
        "node_name": remote.node_name,
        "state": "idle",
        "data_nodes": [],
        "node_errors": [],
        "phase": "",
        "error": None,
        "url": None,
        "prompt": None,
        "log": [],
    }
    if session is not None:
        view.update(session.status())
        view["name"] = remote.name
    return view


def _askpass_base():
    """Where the askpass helper posts a prompt back to.

    Built from the port this request arrived on, never from `request.host_url`.
    The helper is a child of this process on this machine, so loopback is both
    correct and the only address guaranteed to work: a hostname that came in
    through a reverse proxy would send the password out onto the network and
    back, if it resolved from here at all.
    """
    port = request.environ.get("SERVER_PORT") or "8000"
    prefix = app.config.get("PLEXORA_BASE_URL") or ""
    return f"http://127.0.0.1:{port}{prefix}/settings/remotes/_askpass"


def _remote_payload(payload, name):
    from plexora.server.models.remotes import Remote

    def listed(key):
        value = payload.get(key) or []
        if isinstance(value, str):
            value = [part.strip() for part in value.splitlines()]
        return tuple(str(item).strip() for item in value if str(item).strip())

    def optional(key):
        value = (payload.get(key) or "").strip()
        return value or None

    # The checkbox and the arguments are separate answers: "run it inside a
    # job" with no arguments is a real and common choice on a site whose
    # defaults are already right, and it has to be distinguishable from "do
    # not use a scheduler at all".
    srun = None
    if payload.get("use_srun"):
        srun = (payload.get("srun") or "").strip()

    return Remote(
        name=name,
        target=(payload.get("target") or "").strip(),
        remote_command=(payload.get("remote_command") or "").strip() or "plexora",
        datasource=optional("datasource"),
        data_dir=optional("data_dir"),
        plugins=optional("plugins"),
        srun=srun,
        bind_node=bool(payload.get("bind_node")),
        jump=optional("jump"),
        ssh_opts=listed("ssh_opts"),
        forwards=listed("forwards"),
        serve=listed("serve"),
        local_serve=listed("local_serve"),
        node_name=optional("node_name"),
    )


@app.route('/settings/remotes')
def settings_remotes():
    from plexora.server.models import remote_sessions, remotes as remote_store

    listed = []
    for remote in remote_store.load_all().values():
        listed.append(_remote_view(remote, remote_sessions.get(remote.name)))
    return jsonify(remotes=sorted(listed, key=lambda item: item["name"]))


@app.route('/settings/remotes', methods=['POST'])
def settings_remotes_save():
    """Create or update one saved server.

    Nothing is contacted here. Unlike adding a data node -- where a typo can
    only be caught by asking the node -- the test of a remote server is
    pressing Connect, which the user is about to do anyway, and which reports
    far more than a reachability check could.
    """
    from plexora.server.models import remotes as remote_store

    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify(error="Give this server a short name, e.g. “hpc”."), 400
    if "/" in name or name.startswith("_"):
        return jsonify(error="Use a plain name -- letters, digits and dashes."), 400
    remote = _remote_payload(payload, name)
    if not remote.target:
        return jsonify(error="Enter the address to connect to, e.g. "
                             "you@login.cluster.edu."), 400
    remote_store.save(remote)
    return jsonify(remote=_remote_view(remote))


@app.route('/settings/remotes/<name>', methods=['DELETE'])
def settings_remotes_remove(name):
    """Forget a saved server, disconnecting it first if it is up."""
    from plexora.server.models import remote_sessions, remotes as remote_store

    remote_sessions.forget(name)
    remote_store.remove(name)
    return jsonify(ok=True)


@app.route('/settings/remotes/<name>/connect', methods=['POST'])
def settings_remotes_connect(name):
    """Start connecting, and answer immediately.

    202 and a poll, never a blocking request: a `--srun` connection waits in
    the scheduler's queue, legitimately and sometimes for a quarter of an
    hour, and a route that waited with it would pin a Waitress worker and time
    out in the browser long before the job started.
    """
    from plexora.server.models import remote_sessions, remotes as remote_store

    remote = remote_store.find(name)
    if remote is None:
        return jsonify(error=f"No saved server named “{name}”."), 404
    try:
        session = remote_sessions.start(
            remote,
            askpass_url=_askpass_base(),
            auth_token=app.config.get("PLEXORA_AUTH_TOKEN") or None,
        )
    except remote_sessions.ConnectionRefused as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(_remote_view(remote, session)), 202


@app.route('/settings/remotes/<name>/disconnect', methods=['POST'])
def settings_remotes_disconnect(name):
    from plexora.server.models import remote_sessions, remotes as remote_store

    remote_sessions.stop(name)
    remote = remote_store.find(name)
    session = remote_sessions.get(name)
    if remote is None:
        return jsonify(ok=True)
    return jsonify(_remote_view(remote, session))


@app.route('/settings/remotes/<name>/status')
def settings_remotes_status(name):
    from plexora.server.models import remote_sessions, remotes as remote_store

    remote = remote_store.find(name)
    if remote is None:
        return jsonify(error=f"No saved server named “{name}”."), 404
    return jsonify(_remote_view(remote, remote_sessions.get(name)))


@app.route('/settings/remotes/<name>/answer', methods=['POST'])
def settings_remotes_answer(name):
    """Hand ssh the password, code or yes/no the user just typed.

    The value is passed straight to the waiting session and is not stored,
    echoed back, or written to the log the status route serves.
    """
    from plexora.server.models import remote_sessions

    session = remote_sessions.get(name)
    if session is None:
        return jsonify(error="That connection is no longer running."), 404
    payload = request.get_json(silent=True) or {}
    if not session.answer(payload.get("answer") or "", payload.get("id")):
        return jsonify(error="Nothing is waiting for an answer."), 409
    return jsonify(ok=True)


# The two routes the askpass helper itself talks to. Authenticated by the
# per-session nonce it was given in its environment -- loopback alone is not an
# authorisation on a shared machine, where every other account can reach
# 127.0.0.1 too. They are ordinary guarded routes otherwise: the helper carries
# the app's auth token when there is one, so neither needs an exemption from
# the rule that nothing is exempt.


@app.route('/settings/remotes/_askpass/prompt', methods=['POST'])
def settings_remotes_askpass_prompt():
    from plexora.server.models import remote_sessions

    payload = request.get_json(silent=True) or {}
    session = remote_sessions.find_by_nonce(payload.get("nonce"))
    if session is None:
        return jsonify(error="unknown connection"), 403
    prompt = session.open_prompt(payload.get("prompt") or "Password:")
    return jsonify(id=prompt.id)


@app.route('/settings/remotes/_askpass/answer')
def settings_remotes_askpass_answer():
    from plexora.server.models import remote_sessions

    session = remote_sessions.find_by_nonce(request.args.get("nonce"))
    if session is None:
        return jsonify(error="unknown connection"), 403
    answer = session.collect(request.args.get("id"))
    if answer is False:
        return jsonify(state="cancelled")
    if answer is None:
        return jsonify(state="pending")
    return jsonify(state="answered", answer=answer)


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
        # Which saved connection set this up, if any. A managed node's address
        # and token are rewritten every session, so editing them by hand is
        # not a repair -- reconnecting is.
        "managed_by": (node.extra or {}).get("managed_by"),
        # "client" for the node on the user's own computer -- see Node.role.
        # Listed so the settings page can say which one that is rather than
        # showing two indistinguishable loopback addresses.
        "role": node.role,
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
            # Set only by `plexora connect`, which POSTs here through its own
            # tunnel rather than writing the far side's registry directly. A
            # person filling in the form never sends either of these -- and for
            # `role` that is the right answer: somebody typing an address into
            # this form is describing a machine somewhere else, which is
            # precisely what "client" does not mean.
            managed_by=(payload.get("managed_by") or "").strip() or None,
            role=(payload.get("role") or "").strip() or None,
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(
            error=f"Could not reach a Plexora data node at {endpoint}: {exc}"), 400
    return jsonify(node=_node_view(node, []))


# --------------------------------------------------------------------------
# Telling a node about a file, from the browser
#
# The browser cannot talk to a node directly for this: it has no token (the
# one it gets for tiles is scoped to a URL it was handed, and this is a write),
# and on the layout that matters the node is on the user's own machine while
# the page came from a cluster. So the viewer relays -- which is also the only
# place that knows the node's address and token at all.
#
# Deliberately not under /settings: this is a data-import action a user takes
# from the upload and edit forms, not a setting they configure.
# --------------------------------------------------------------------------


def _relayed(call):
    """Run a node call, turning its failures into ones a form can show."""
    from plexora.server.providers.base import ResourceError, ResourceUnavailable

    try:
        return jsonify(ok=True, resource=call())
    except KeyError as exc:
        return jsonify(error=str(exc).strip("'\"")), 400
    except ResourceUnavailable as exc:
        return jsonify(error=str(exc)), 503
    except ResourceError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 502


@app.route('/nodes/<name>/resources', methods=['POST'])
def node_share_resource(name):
    """Have a node start serving one more file from its own machine.

    Answers with the `node://` locator the form field then holds, and with the
    resource's state -- a mask may still be converting, and the field shows a
    pill and polls rather than letting somebody attach a mask that cannot yet
    serve a tile.
    """
    from plexora import nodes as node_api

    payload = request.get_json(silent=True) or {}
    kind = (payload.get("kind") or "").strip()
    path = (payload.get("path") or "").strip()
    if not path:
        return jsonify(error="Choose a file on that machine."), 400
    return _relayed(lambda: node_api.share_path(name, kind, path))


@app.route('/nodes/<name>/resources/<resource_id>/status')
def node_resource_status(name, resource_id):
    from plexora import nodes as node_api

    return _relayed(lambda: node_api.resource_status(name, resource_id))


@app.route('/nodes/<name>/resources/<resource_id>', methods=['DELETE'])
def node_unshare_resource(name, resource_id):
    """Stop a node serving one resource. Nothing on its disk is touched.

    Sent when a field that had picked a file is pointed somewhere else, so a
    node does not accumulate every path a user browsed past on the way to the
    one they meant.
    """
    from plexora import nodes as node_api

    return _relayed(lambda: node_api.unshare_path(name, resource_id))


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
