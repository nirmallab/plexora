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
    #
    # `job_defaults` for the same reason and by the same route: the Advanced
    # box's placeholder, and the line it fills in when somebody says this is a
    # cluster, are the recipes module's constants rather than a fourth copy of
    # them written into a template.
    from plexora.server.models import recipes as recipe_store

    return render_template('settings.html', data=template_data(),
                           sections=SECTIONS,
                           job_defaults=recipe_store.defaults())


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


def _session_kind():
    """Which of the two things "connect to this server" can mean.

    `viewer` -- the default and the historical meaning -- runs Plexora over
    there and tunnels the browser to it. `node` leaves the viewer here and
    starts a data node on the far side, which is what a data form's Remote
    option opens so that a field can name a file on that machine. One profile,
    one login, two entirely different arrangements, so they are asked for
    separately rather than inferred.
    """
    from plexora.server.models import remote_sessions

    asked = (request.args.get("kind") or "").strip().lower()
    return (remote_sessions.KIND_NODE if asked == remote_sessions.KIND_NODE
            else remote_sessions.KIND_VIEWER)


def _browser_origin():
    """The origin the browser will send to a node, spelled exactly.

    A node echoes one allowed origin and never `*`, so this has to be the
    string the browser actually sends or every direct tile fetch fails CORS and
    falls back to being proxied through here -- which works, and quietly costs
    a copy of every tile. The request's own `Origin` is the browser's own
    answer to the question; `host_url` is a reconstruction, used only when
    there is no header to read.
    """
    origin = (request.headers.get("Origin") or "").strip()
    return origin or request.host_url.rstrip("/")


def _record_node(name, endpoint, token, *, browser_endpoint=None,
                 managed_by=None, expires_at=None):
    """Put a node a session just started onto this machine's map."""
    from plexora import nodes as node_api

    return node_api.register_node(name, endpoint, token=token,
                                  browser_endpoint=browser_endpoint,
                                  managed_by=managed_by,
                                  expires_at=expires_at)


def _log_lines(default=25):
    """How much of the log this request asked for, clamped to what is kept.

    The list of every profile carries a short tail so a page can show the last
    thing that happened; a modal watching ONE connection asks for the whole
    buffer, because that is the surface where a stack of authentication
    failures is the thing the user needs to read. Clamped at both ends so a
    query string cannot ask for a megabyte or for nothing at all.
    """
    from plexora.server.models.remote_sessions import LOG_LINES

    asked = (request.args.get("log") or "").strip()
    if not asked:
        return default
    try:
        return max(1, min(int(asked), LOG_LINES))
    except ValueError:
        return default


def _remote_view(remote, session=None, log_lines=25):
    """A saved profile plus whatever its live connection is doing."""
    from plexora import connect
    from plexora.server.models import recipes as recipe_store

    view = {
        "name": remote.name,
        "target": remote.target,
        "remote_command": remote.remote_command,
        "install": remote.install,
        # Derived here rather than in the browser, and for the same reason the
        # srun line is split here: the environment the install writes to is
        # decided by `connect.install_command_line`, so the name shown to
        # somebody about to press Connect has to come from the same reading of
        # the same field. A second parser in JavaScript is how a step ends up
        # promising one environment while pip writes to another.
        "install_env": connect.environment_label(remote.remote_command),
        "datasource": remote.datasource,
        "data_dir": remote.data_dir,
        "srun": remote.srun,
        # The same line again, split into the boxes the Settings form offers
        # it in. Split here rather than in the browser so that the page which
        # SHOWS a walltime and the route which STORES one cannot disagree
        # about which flag carries it.
        "srun_parts": recipe_store.split_srun(remote.srun),
        "bind_node": remote.bind_node,
        "jump": remote.jump,
        "forwards": list(remote.forwards),
        "serve": list(remote.serve),
        "local_serve": list(remote.local_serve),
        "node_name": remote.node_name,
        "state": "idle",
        "kind": None,
        "node": None,
        "data_nodes": [],
        "node_errors": [],
        "phase": "",
        "error": None,
        "url": None,
        "prompt": None,
        "log": [],
    }
    if session is not None:
        view.update(session.status(log_lines))
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


def _remote_payload(payload, name, existing=None):
    """One saved server, from what the form sent.

    `existing` is the record being edited, and it supplies every field the form
    has no box for. Three groups of them: `serve`, `local_serve` and
    `node_name`, which used to name the files each end would offer and are now
    chosen per field, when the data is added; `jump`, `ssh_opts`, `plugins`,
    `local_node` and any `extra` keys, which only `plexora connect --save` and a
    hand-edited remotes.json ever set; and `datasource`, which the form dropped
    when a saved server stopped meaning "a machine plus one project on it".
    Either way the rule is the same: a profile can carry them, and editing an
    address in Settings is not somebody asking for them to be dropped.
    **Everything the form does not send goes through `kept()`** -- reading such
    a key straight off the payload silently erases it on the first save.
    """
    from plexora.server.models import recipes as recipe_store
    from plexora.server.models.remotes import Remote

    def listed(key):
        value = payload.get(key) or []
        if isinstance(value, str):
            value = [part.strip() for part in value.splitlines()]
        return tuple(str(item).strip() for item in value if str(item).strip())

    def optional(key):
        value = (payload.get(key) or "").strip()
        return value or None

    def kept(key, fallback):
        """A field the form does not send: whatever was already recorded."""
        if key in payload:
            return listed(key) if isinstance(fallback, tuple) else optional(key)
        return fallback

    # The switch and the arguments are separate answers: "run it inside a
    # job" with no arguments is a real and common choice on a site whose
    # defaults are already right, and it has to be distinguishable from "do
    # not use a scheduler at all".
    #
    # The Settings form sends Cores, Memory and Time as three fields and the
    # rest of the line as `srun`; a recipe has already composed the whole line
    # by the time it gets here and sends none of the three. Membership decides
    # which of the two is talking -- splicing an absent box in as "" would be
    # harmless, but reading the payload for keys it never had is how a field
    # gets silently dropped, so the composed line is left exactly alone.
    srun = None
    if payload.get("use_srun"):
        srun = (payload.get("srun") or "").strip()
        if any(key in payload for key in ("walltime", "cores", "memory")):
            srun = recipe_store.join_srun(
                srun,
                walltime=(payload.get("walltime") or "").strip(),
                cores=(payload.get("cores") or "").strip(),
                memory=(payload.get("memory") or "").strip())

    return Remote(
        name=name,
        target=(payload.get("target") or "").strip(),
        remote_command=(payload.get("remote_command") or "").strip() or "plexora",
        # Membership, not truthiness: both forms send this switch, and an
        # absent key is a caller that never asked -- `plexora connect --save`,
        # or a hand-written body -- whose saved answer must survive the edit.
        install=(bool(payload["install"]) if "install" in payload
                 else (existing.install if existing else False)),
        datasource=kept("datasource", existing.datasource if existing else None),
        data_dir=optional("data_dir"),
        plugins=kept("plugins", existing.plugins if existing else None),
        srun=srun,
        bind_node=bool(payload.get("bind_node")),
        jump=kept("jump", existing.jump if existing else None),
        ssh_opts=kept("ssh_opts", existing.ssh_opts if existing else ()),
        forwards=listed("forwards"),
        serve=kept("serve", existing.serve if existing else ()),
        local_serve=kept("local_serve", existing.local_serve if existing else ()),
        node_name=kept("node_name", existing.node_name if existing else None),
        local_node=(bool(payload["local_node"]) if "local_node" in payload
                    else (existing.local_node if existing else True)),
        extra=dict(existing.extra) if existing else {},
    )


def _address_error(target):
    """Why this address cannot be connected to, or None.

    Its own function because two routes ask it. The second case is a typo
    with a bad failure mode rather than an obviously empty box: an address
    with nothing in front of the `@` reaches ssh with no username, and ssh
    answers that with "Permission denied" -- the one error message that sends
    people looking for a key problem they do not have.
    """
    if not target:
        return "Enter the address to connect to, e.g. you@login.cluster.edu."
    if target.startswith("@"):
        return f"Add your username in front of the \u201c@\u201d \u2014 e.g. you{target}."
    return None


@app.route('/settings/remotes')
def settings_remotes():
    from plexora.server.models import remote_sessions, remotes as remote_store

    listed = []
    for remote in remote_store.load_all().values():
        listed.append(_remote_view(remote, remote_sessions.get(remote.name)))
    return jsonify(remotes=sorted(listed, key=lambda item: item["name"]))


@app.route('/settings/recipes')
def settings_recipes():
    """Starting points for adding a server.

    Static, and served rather than shipped in the page, so that the connection
    modal -- which is loaded on every page, including the viewer -- does not
    carry a catalogue of cluster documentation it will use on one page in a
    hundred. One request, when somebody presses "Add a new server".
    """
    from plexora.server.models import recipes as recipe_store

    # `defaults` rides along so the form's boxes and the srun line a preset
    # composes cannot drift apart: the walltime, cores and memory somebody
    # reads on screen are the same three constants the server splices.
    return jsonify(recipes=[recipe.to_dict()
                            for recipe in recipe_store.all_recipes()],
                   defaults=recipe_store.defaults())


@app.route('/settings/recipes/<recipe_id>', methods=['POST'])
def settings_recipes_save(recipe_id):
    """Save the profile a recipe and these answers describe.

    Composing happens here rather than in the browser so that there is one
    implementation of it. The result goes through exactly the same save as the
    Settings form -- a recipe is a filled-in form, not a second way to write a
    profile, and it must not be able to produce one the form could not.
    """
    from plexora.server.models import recipes as recipe_store
    from plexora.server.models import remotes as remote_store

    payload = request.get_json(silent=True) or {}
    try:
        body = recipe_store.compose(recipe_id, payload)
    except KeyError:
        return jsonify(error=f"No preset called “{recipe_id}”."), 404
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    name = (body.get("name") or "").strip()
    if not name:
        return jsonify(error="Give this server a short name, e.g. “hpc”."), 400
    if "/" in name or name.startswith("_"):
        return jsonify(error="Use a plain name -- letters, digits and dashes."), 400
    remote = _remote_payload(body, name, remote_store.find(name))
    problem = _address_error(remote.target)
    if problem:
        return jsonify(error=problem), 400
    remote_store.save(remote)
    return jsonify(remote=_remote_view(remote))


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
    remote = _remote_payload(payload, name, remote_store.find(name))
    problem = _address_error(remote.target)
    if problem:
        return jsonify(error=problem), 400
    remote_store.save(remote)
    return jsonify(remote=_remote_view(remote))


@app.route('/settings/remotes/<name>', methods=['DELETE'])
def settings_remotes_remove(name):
    """Forget a saved server, disconnecting it first if it is up."""
    from plexora.server.models import remote_sessions, remotes as remote_store

    remote_sessions.forget(name)
    remote_sessions.forget(name, remote_sessions.KIND_NODE)
    _forget_node(name)
    remote_store.remove(name)
    return jsonify(ok=True)


@app.route('/data_places')
def data_places():
    """Every machine a data field can name a file on, and its state.

    The list behind the Remote option on every data form. It answers one
    question -- "where could this file be?" -- and it has to answer it the same
    way on a laptop running Plexora by itself and on a laptop looking at a
    Plexora running on a cluster, because the person filling in the form is in
    the same position either way.

    Three kinds of answer:

    - `local`: the machine the browser is on. Reachable as plain paths when
      Plexora is running on it too, and through the node `plexora connect`
      started otherwise. Never listed here as a "place" -- it is the other
      half of the switch -- but its situation is reported so the field knows
      which of those two it is.
    - `server`: the machine Plexora is running on, when that is NOT the
      browser's. Plain paths, and the historical meaning of every path box.
    - one entry per saved connection: reachable once a data node has been
      opened on it, which this list says whether it has.
    """
    from plexora.server.models import nodes as node_registry
    from plexora.server.models import remote_sessions, remotes as remote_store

    # One read for the whole listing. This route is what the surfaces poll, so
    # a registry read per profile would be a file read per profile per second
    # while anybody has the Settings page or a dialog open.
    registry = node_registry.load_all()
    places = []
    if _server_is_remote():
        places.append({
            "id": "server",
            "kind": "server",
            "label": "This Plexora server",
            "detail": "The machine Plexora itself is running on.",
            "node": None,
            "registered_node": None,
            "time_left": None,
            "time_limit": None,
            "state": "connected",
        })
    for remote in sorted(remote_store.load_all().values(),
                         key=lambda item: item.name):
        session = remote_sessions.get(remote.name, remote_sessions.KIND_NODE)
        status = session.status(log_lines=8) if session is not None else {}
        # A viewer connection to the same profile is a separate thing serving a
        # separate purpose, and opening a data node does not disturb it. It is
        # reported because it is not free: on a profile that runs Plexora
        # inside a job, these are two allocations, and somebody should not
        # discover that from `squeue`.
        viewer = remote_sessions.get(remote.name, remote_sessions.KIND_VIEWER)
        registered = _registered_node_for(remote, registry)
        # How long this connection has left, when it is running inside a job.
        # The live session first, the registry entry behind it: a data node
        # outlives the process that started it, so after a restart the session
        # is gone and the tunnel is not -- and that is exactly the moment
        # somebody most needs to know, because nothing else would say.
        time_left = status.get("time_left")
        if time_left is None and registered:
            entry = registry.get(registered)
            time_left = entry.time_left if entry is not None else None
        places.append({
            "id": remote.name,
            "kind": "remote",
            "label": remote.name,
            "detail": remote.target,
            "node": status.get("node"),
            # The name this profile is on the MAP under, whoever put it there.
            # `node` above is only ever set by a session this process owns, so
            # it is empty for a node that outlived the Plexora that started it
            # -- and a surface matching a project's routing against `node`
            # alone was comparing two empties and calling that a match. See
            # `_registered_node_for`.
            "registered_node": registered,
            # Sent as a DURATION rather than a deadline, so a browser whose
            # clock disagrees with this machine's still counts down correctly.
            "time_left": time_left,
            "time_limit": status.get("time_limit"),
            "state": status.get("state") or "idle",
            "phase": status.get("phase") or "",
            "error": status.get("error"),
            "prompt": status.get("prompt"),
            # A short tail, so a surface showing this connection has something
            # to draw before anyone asks for the deep log. Eight lines is the
            # last thing ssh said, which is what "why is it stuck" needs; the
            # 200-line pull is `…/status?log=` and is asked for per connection,
            # not per list.
            "log": status.get("log") or [],
            "viewer_state": viewer.state if viewer is not None else None,
            # Whether choosing this place will ask a scheduler for a node --
            # which turns Connect from seconds into a wait in a queue, and is
            # the profile's own setting rather than anything decided here.
            "queued": remote.srun is not None,
        })
    return jsonify(places=places, client_node=_client_node_name(),
                   server_is_remote=_server_is_remote())


def _server_is_remote():
    from plexora.server.routes.page_routes import server_is_remote

    return server_is_remote()


def _client_node_name():
    from plexora.server.routes.page_routes import _client_node_name as named

    return named()


#: How long a health probe may take before it is a "no".
#:
#: Short on purpose. This answers "is that machine answering *right now*", for
#: somebody who has just opened a menu -- a probe that took ten seconds to say
#: "unreachable" would have been replaced by the user's own conclusion long
#: before it arrived.
HEALTH_TIMEOUT = 4.0


@app.route('/remote_health')
def remote_health():
    """Whether each open data node is actually answering, and how fast.

    Session state says what Plexora *did* -- it started an ssh, the node
    announced, the tunnel is up. That is not the same claim as "the node
    answers now", and the gap between them is exactly the failure people hit:
    a laptop sleeps, a job hits its walltime, a VPN drops, and the session
    still reads `connected` because nothing has told it otherwise.

    So this is a live probe, and it is deliberately **not** polled. It is asked
    for when somebody opens the connections menu, because that is when the
    question is being asked; a background health poll would be a second opinion
    running against every connection forever, and the first thing it would do
    is disagree with the session state at some point that nobody was watching.

    One authenticated GET per node (`/node/v1/hello`, which is also the version
    handshake), timed. Nothing is contacted for a profile that has no node up
    -- there is nothing there to ask.

    **And it checks that the probe is about the same node the project is
    reading from.** This route resolves the node freshly out of the registry on
    every call, while a loaded project holds the one its providers resolved
    when it opened. Those are usually the same entry and the distinction never
    comes up -- but a reconnect lands the tunnel on whatever local port was
    free, and then they are two different addresses, only one of which anything
    is listening on. Probing the registry's copy in that state answers a
    question nobody asked: it reports a machine that is genuinely up and well
    while every tile, stat and GMM in the open project is refused against the
    port that has gone. So the held address is compared first, and a mismatch
    is its own state rather than a fast green tick.
    """
    import time

    from plexora.server.models import data_model
    from plexora.server.models import nodes as node_registry
    from plexora.server.models import remote_sessions, remotes as remote_store
    from plexora.server.providers import http

    held = data_model.held_node_addresses()
    health = {}
    for remote in sorted(remote_store.load_all().values(),
                         key=lambda item: item.name):
        session = remote_sessions.get(remote.name, remote_sessions.KIND_NODE)
        node_name = session.status(log_lines=0).get("node") if session else None
        if not node_name:
            node_name = _registered_node_for(remote)
        if not node_name:
            continue
        entry = node_registry.find(node_name)
        if entry is None:
            health[remote.name] = {"state": "unknown", "ms": None,
                                   "detail": "That node is not on the map."}
            continue
        holding = held.get(node_name)
        if holding is not None and holding != entry.endpoint:
            # Said as the thing to do about it, not as a diagnosis. The node is
            # up, the tunnel is up, and nothing the user can see is wrong --
            # the open project is simply still addressed to where that node was
            # before it was reconnected.
            health[remote.name] = {
                "state": "stale", "ms": None,
                "detail": "That machine is answering, but this project is "
                          "still pointed at the address it had before it was "
                          "reconnected. Reload the project to read from it "
                          "again.",
            }
            continue
        started = time.perf_counter()
        try:
            http.hello(entry, timeout=HEALTH_TIMEOUT)
        except Exception as exc:
            health[remote.name] = {
                "state": "unreachable", "ms": None,
                # The reason, not a category: "refused this server's token" and
                # "connection timed out" want different things done about them.
                "detail": str(exc) or exc.__class__.__name__,
            }
            continue
        health[remote.name] = {
            "state": "healthy",
            "ms": int(round((time.perf_counter() - started) * 1000)),
            "detail": "",
        }
    return jsonify(health=health)


def _registered_node_for(remote, registry=None):
    """The node this profile left on the map, when no session owns it.

    A data node outlives the process that started it. `nodes.json` is written
    when the node announces and is still there after Plexora restarts -- which
    is exactly when `remote_sessions` is empty, because a session is a child
    process this server started and nothing rebuilds one on boot.

    Routing already reads the registry, and that is the whole point: it is
    what makes a reopened project try the node at all. So a panel that
    reported only on sessions called the machine "Unknown" and offered no
    latency, while the rest of the app was busy failing to reach that very
    node and logging the refusal. Two answers to one question, and the one on
    screen was the less informed of them.

    `managed_by` is the proof of ownership, the same test `_forget_node`
    makes: a node somebody registered by hand under this name is theirs, it
    points at an address they maintain, and this profile does not get to
    speak for it.
    """
    from plexora.server.models import nodes as node_registry

    node_name = remote.node_name or remote.name
    entries = node_registry.load_all() if registry is None else registry
    entry = entries.get(node_name)
    if entry is not None and entry.managed_by == f"connect:{node_name}":
        return node_name
    return None


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
    kind = _session_kind()
    extra = {}
    if kind == remote_sessions.KIND_NODE:
        # Read here, in the request, because both are facts about the browser
        # that made it -- and the thread that uses them has no request to ask.
        # `unregister` is the other half of `register`: when the session ends
        # on its own -- a walltime, a dropped network -- the node it put on
        # the map is dead, and the session's own teardown is the only thing
        # left that knows to take it off.
        extra = {"allow_origin": _browser_origin(), "register": _record_node,
                 "unregister": _forget_node_entry}
    try:
        session = remote_sessions.start(
            remote,
            askpass_url=_askpass_base(),
            auth_token=app.config.get("PLEXORA_AUTH_TOKEN") or None,
            kind=kind,
            **extra,
        )
    except remote_sessions.ConnectionRefused as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(_remote_view(remote, session)), 202


@app.route('/settings/remotes/<name>/disconnect', methods=['POST'])
def settings_remotes_disconnect(name):
    from plexora.server.models import remote_sessions, remotes as remote_store

    kind = _session_kind()
    remote_sessions.stop(name, kind)
    if kind == remote_sessions.KIND_NODE:
        # The node entry is the tunnel, and the tunnel has just gone. Leaving
        # it on the map would offer a machine that cannot answer -- and worse,
        # would offer it under a port number the next session gives to
        # something else.
        _forget_node(name)
    remote = remote_store.find(name)
    session = remote_sessions.get(name, kind)
    if remote is None:
        return jsonify(ok=True)
    return jsonify(_remote_view(remote, session))


def _forget_node(name):
    """Drop the node record a node session created. Never fatal.

    `name` is the PROFILE's name, and the node it registered may be called
    something else -- a profile with a `node_name` sets both the map entry and
    the `managed_by` marker from that, not from the profile name. Resolving the
    profile first is what makes disconnect actually clean up: matching on the
    profile name left the entry behind, offering a machine whose tunnel had
    gone under a port the next session would give to something else.

    Only a node this session is responsible for -- `managed_by` is the proof.
    A node somebody registered by hand under the same name is theirs, points at
    an address they can fix, and is none of this route's business.
    """
    try:
        from plexora.server.models import remotes as remote_store

        remote = remote_store.find(name)
        node_name = (remote.node_name or name) if remote is not None else name
        _forget_node_entry(node_name)
    except Exception:
        pass


def _forget_node_entry(node_name):
    """Drop one registry entry, if a saved connection is what wrote it.

    The half of `_forget_node` that acts on a NODE name rather than a profile
    name. Shared with the session's own teardown (`remote_sessions.start`'s
    `unregister`), which already knows the node's name and has no request to
    resolve a profile from. Never fatal, and guarded by the same `managed_by`
    proof: an entry somebody registered by hand is theirs.
    """
    try:
        from plexora import nodes as node_api
        from plexora.server.models import nodes as node_registry

        entry = node_registry.load_all().get(node_name)
        if entry is not None and entry.managed_by == f"connect:{node_name}":
            node_api.forget_node(node_name)
    except Exception:
        pass


@app.route('/settings/remotes/<name>/status')
def settings_remotes_status(name):
    from plexora.server.models import remote_sessions, remotes as remote_store

    remote = remote_store.find(name)
    if remote is None:
        return jsonify(error=f"No saved server named “{name}”."), 404
    return jsonify(_remote_view(remote,
                                remote_sessions.get(name, _session_kind()),
                                log_lines=_log_lines()))


@app.route('/settings/remotes/<name>/answer', methods=['POST'])
def settings_remotes_answer(name):
    """Hand ssh the password, code or yes/no the user just typed.

    The value is passed straight to the waiting session and is not stored,
    echoed back, or written to the log the status route serves.
    """
    from plexora.server.models import remote_sessions

    session = remote_sessions.get(name, _session_kind())
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
    # `asker` says WHICH ssh is asking, which is the only thing that
    # distinguishes the second hop's identical question from a first hop
    # re-asking because the answer was refused. Absent on Windows; see
    # askpass.asking_process.
    prompt = session.open_prompt(payload.get("prompt") or "Password:",
                                 asker=payload.get("asker"))
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
