"""The node process: a Plexora with everything but the data switched off.

`plexora node serve` builds a Flask app that mounts the node blueprint and
nothing else -- no viewer, no project pages, no plugin UIs, no database. What
it does load is the plugin *server* code, because a node runs the same table
operations the primary would have run: the ROI writer, gating's `uns` writer.
One implementation, two machines.

Why a separate app rather than a flag on the main one: `plexora/__init__.py`
builds its app at import and registers every route the viewer has. A node that
shared it would be serving the project picker, the import screens and the
figure library on a port whose whole purpose is to hand out bytes from one
directory -- and every one of those pages assumes a config.json this process
deliberately does not have.

**Bind and token are independent, and the token is not optional.** Plexora's
main server guards on the token being set, not on the address it bound (see
`plexora/__init__.py`), because a loopback port on a shared machine is not
private. A node inherits that reasoning and goes further: it refuses to start
without one, since unlike the viewer there is no local-desktop case where a
node is obviously the user's own.
"""

from __future__ import annotations

import secrets
import socket
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify

from plexora.server.node import resources as node_resources
from plexora.server.node.api import API_VERSION, node_bp

#: Printed on its own line at startup so that whoever launched this node over
#: ssh can read back where it landed and what its token is. `plexora connect`
#: parses it (connect.parse_node_announce); nothing else depends on it, and the
#: human-readable banner below is unchanged.
NODE_ANNOUNCE_PREFIX = "[plexora-node]"


class NodeStartupError(RuntimeError):
    """The node cannot start, with a sentence worth printing."""


def create_node_app(serve, token, *, node_id=None, allow_origins=(), plugins=None,
                    dynamic=False, manifest=None, log=print):
    """One node app serving the resources named in `serve`.

    `serve` is a list of `kind:id=path` strings -- see
    `node_resources.parse_serve`. Every one is resolved and checked here rather
    than on first use: a typo in a path is a thing to find out about while the
    operator is still looking at the terminal, not three hours later when
    somebody opens the project.

    `dynamic` lets the token holder add and remove resources while the node
    runs, which is what makes a Local/Remote toggle possible at all -- the user
    picks a file long after the node started. It is opt-in because it hands
    whoever holds the token arbitrary file reads on this account: fine for the
    node `plexora connect` starts on the user's OWN laptop, bound to loopback,
    with a token that never leaves an ssh channel; not fine for one an operator
    started to share a scratch directory.

    `manifest` is a file recording what this node ends up serving, re-read at
    startup. It is what lets a project opened in a later session find its
    laptop-side files again without the user pointing at anything.
    """
    if not token:
        raise NodeStartupError(
            "a data node needs a token: pass --token, or omit it to have one "
            "generated and printed."
        )

    app = Flask(__name__)
    app.config["PLEXORA_NODE_TOKEN"] = str(token)
    app.config["PLEXORA_NODE_ID"] = node_id or f"{socket.gethostname()}-{secrets.token_hex(4)}"
    app.config["PLEXORA_NODE_ORIGINS"] = [
        origin.rstrip("/") for origin in (allow_origins or ()) if origin
    ]
    app.config["PLEXORA_NODE_DYNAMIC"] = bool(dynamic)
    app.config["PLEXORA_NODE_MANIFEST"] = str(manifest) if manifest else None

    registry = node_resources.Registry()
    # The command line first and strictly: what an operator typed is
    # authoritative, and a typo in it is worth refusing to start over. The
    # manifest second and tolerantly -- see below.
    for argument in serve or ():
        kind, resource_id, path = node_resources.parse_serve(argument)
        resource = registry.add(kind, resource_id, path)
        if kind == "segmentation":
            _make_mask_servable(resource, log=log)
    _restore_manifest(registry, manifest, log=log)

    if not len(registry) and not dynamic:
        raise NodeStartupError(
            "a data node with nothing to serve would answer every request with "
            "404. Pass at least one --serve kind:id=path, or --dynamic to let "
            "the viewer add them while this node runs."
        )
    app.config["PLEXORA_NODE_RESOURCES"] = registry

    _load_plugin_operations(plugins)
    app.register_blueprint(node_bp)

    @app.route("/")
    def _root():
        """Deliberately unauthenticated and deliberately uninformative.

        Somebody who lands here has pointed a browser at a node, which is an
        ordinary mistake worth a sentence -- and one that must not list what
        this node holds, since that answer is behind the token.
        """
        return jsonify(
            plexora_node=True,
            api_version=API_VERSION,
            message="This is a Plexora data node, not a viewer. "
                    "Register it with a Plexora server to use it.",
        )

    return app


def _restore_manifest(registry, manifest, log=print):
    """Serve again whatever this node was serving when it last stopped.

    Every failure here is tolerated and reported, which is the opposite of how
    a `--serve` argument is treated, and the difference is who wrote it. A
    `--serve` is somebody typing a path just now; a manifest entry is a record
    of a file shared in some previous session, and by the time the node starts
    again the user may perfectly reasonably have moved it, renamed it, or
    finished with it. A node that refused to start over one of those would
    strand every OTHER file it was asked to serve -- and it would do so at the
    exact moment the user is trying to reopen their work.
    """
    if not manifest:
        return
    for kind, resource_id, path in node_resources.load_manifest(manifest):
        try:
            resource = registry.add(kind, resource_id, path)
            if kind == "segmentation":
                _make_mask_servable(resource, log=log)
        except Exception as exc:
            log(f"  not serving {resource_id!r} again: {exc}")


def _make_mask_servable(resource, log=print):
    """`_convert_mask_if_needed`, remembering that it ran.

    The flag is what stops a mask being put back into `preparing` every time
    somebody shares the same file again: the commonest good outcome of the
    check below is that nothing needs doing, which leaves no other trace.
    """
    _convert_mask_if_needed(resource, log=log)
    resource.prepared = True


def _convert_mask_if_needed(resource, log=print):
    """Give this node a mask the tile route can actually serve.

    A segmentation file has to be a TILED, PYRAMIDAL label image before
    anything can hand out tiles of it, and the masks that come out of a
    segmentation pipeline usually are neither -- one full-resolution strip-
    based plane is the norm. On the viewer's own machine that conversion
    happens at import while the user watches a progress bar, and this is the
    same conversion in the same place in the sequence: before anything is
    served, rather than on the first tile request hours later, where the
    symptom is an empty cell layer and nothing anywhere saying why.

    Three outcomes, in order of what they cost. A mask that is already servable
    is served. A pyramid somebody already derived from it -- an earlier run of
    this node, another server on the same mount, `plexora node prepare` -- is
    adopted, which is what makes restarting a node cheap. Otherwise it is
    converted here and now, and the node starts when that finishes.

    The one thing it will not do is convert with nowhere to put the result. The
    derived pyramid is frequently larger than the mask it came from, so when
    neither the mask's own directory nor anywhere else will take a write, that
    is a question about somebody's disk quota rather than about Plexora, and
    the answer is a sentence naming the command and a destination.
    """
    from plexora.server.utils import segmentation_pyramid as sp

    source = Path(resource.path)
    # A mask Plexora produced is servable whatever its level count -- an image
    # small enough to fit in one tile converts to a single tiled level, and
    # there is nothing further to downsample to. This is the same rule
    # `refresh_segmentation_mapping` applies before it adopts a derived file,
    # and the two must agree or a mask that opens locally is refused here.
    if sp.generated_mask_kind(source) is not None:
        return
    if sp.looks_like_outline_mask(source):
        return

    gaps = sp.label_pyramid_gaps(source)
    if gaps == []:
        return
    if gaps is None:
        raise NodeStartupError(f"{source} could not be read as a label mask.")

    # Both modes, filled first. A node has no project to tell it which one this
    # mask is meant to be read as, so what settles it is what is actually on
    # disk: an operator who ran `prepare --outlines` gets their outline pyramid
    # served and reported as outlines, rather than a second conversion to
    # filled sitting next to it.
    for mode in (sp.MODE_FILLED, sp.MODE_OUTLINES):
        found = sp.resolve_derived_mask(source, mode=mode)
        if found.existing is not None:
            log(f"  {source.name} {' and '.join(gaps)}; serving the prepared "
                f"pyramid beside it")
            log(f"    {found.existing}")
            resource.repoint(found.existing)
            return

    location = sp.resolve_derived_mask(source, mode=sp.DEFAULT_MODE)
    if not location.writable:
        raise NodeStartupError(
            f"{source} cannot be served as a cell layer as it is, because "
            f"{' and '.join(gaps)} -- and {source.parent} cannot be written "
            f"to, so it cannot be converted where it lies.\n\n"
            f"Convert it once, into a directory you can write:\n"
            f"  plexora node prepare {source} <somewhere-writable>/{location.target.name}\n"
            f"  plexora node serve --serve segmentation:{resource.id}="
            f"<somewhere-writable>/{location.target.name} ..."
        )

    log(f"  {source.name} {' and '.join(gaps)}, so it cannot be tiled as it is.")
    log(f"  Converting -> {location.target}")
    resource.repoint(prepare_mask(source, location.target, log=log, banner=False))


def prepare_mask(source, output=None, *, outline=False, log=print, banner=True):
    """Turn a label mask into something a node can serve tiles of.

    The same conversion an import runs, reachable on a machine that has no
    viewer and no projects. Prints progress, because on a whole-slide mask this
    is tens of seconds to minutes and a silent terminal is indistinguishable
    from a hang.

    The default destination is the one `resolve_derived_mask` would pick, which
    is what lets `prepare` and `serve` be run without arguments in between: the
    node looks beside the mask, finds what this wrote, and adopts it. A named
    `output` overrides that -- necessary when the mask's own directory is
    read-only, which is where an operator has to make the choice themselves.
    """
    from plexora.server.utils import segmentation_pyramid as sp

    source = Path(source).expanduser()
    if not source.exists():
        raise NodeStartupError(f"there is nothing at {source}")
    mode = sp.MODE_OUTLINES if outline else sp.MODE_FILLED
    if output:
        output = Path(output).expanduser()
    else:
        location = sp.resolve_derived_mask(source, mode=mode)
        if not location.writable:
            raise NodeStartupError(
                f"{source.parent} cannot be written to, so there is nowhere to "
                f"put the converted mask. Name a destination:\n"
                f"  plexora node prepare {source} <somewhere-writable>/"
                f"{location.target.name}")
        output = location.target

    if banner:
        log(f"{source}")
        log(f"  -> {output}")
    last = [-1]

    def report(done, total):
        percent = int(done * 100 / total) if total else 0
        if percent != last[0]:
            last[0] = percent
            # A carriage return rather than a new line: this fires once per
            # written tile, which is thousands of times on a real mask.
            try:
                log(f"  {percent}%", end=chr(13), flush=True)
            except TypeError:
                log(f"  {percent}%")

    written = sp.pyramidize_segmentation_mask(
        source, output, overwrite=True, outline=outline,
        progress_callback=report,
    )
    log("  100%      ")
    if banner:
        # Skipped when this ran from startup: the node is about to list what it
        # serves, and telling an operator to run the command they are already
        # running reads as though something went wrong.
        log(f"Ready. Serve it with:\n"
            f"  plexora node serve --serve segmentation:mask={written} ...")
    return written


def _load_plugin_operations(plugins=None):
    """Import the server half of each active plugin, for its table operations.

    Best-effort per plugin: a node whose build lacks one plugin should serve
    everything else rather than refusing to start, and the primary finds out
    which operations exist from `/hello`'s capability list rather than by
    assuming. A plugin that cannot be imported is reported and skipped -- the
    failure the operator needs to see is at attach time, where the button that
    needs it is.
    """
    from plexora.server import plugins as plugin_registry

    available = plugin_registry.available_names()
    wanted = plugin_registry.requested() if plugins is None else list(plugins)
    names = available if wanted is None else [n for n in wanted if n in available]

    for name in names:
        module = f"plexora.plugins.{name}.server.tableops"
        try:
            __import__(module)
        except ModuleNotFoundError:
            # Not every plugin has file-side work. Figure Builder's is on the
            # image, and Cell Explorer reads columns through the provider.
            continue
        except Exception as exc:  # pragma: no cover - a broken third-party plugin
            print(f"Plugin {name!r} has table operations that would not load, "
                  f"so this node cannot run them: {exc}")


def warm_resources(registry, log=print):
    """Open what this node serves and precompute what a first zoom needs.

    The primary already does this for a project on its own disk -- see
    `data_model._warm_datasource_caches`, which runs on load so that the first
    paint is not also the first read. A node had no equivalent, and the gap
    only shows when a node comes back underneath a project that is already
    open: the primary's caches survive the restart, so its warm-up finds
    everything cached and asks this node for nothing, and the node stays cold
    until a user zooms. That user then waits for a multi-gigabyte pyramid to
    open and for a full-resolution read per channel, inside their own request.
    Measured on a 31 GB slide over an ssh tunnel, that was 7.3 s for twelve
    tiles against 0.7 s once warm.

    Deliberately NOT the mixture fits. What a tile needs is the open pyramid
    and the channel's quantization window; a GMM only refines contrast
    afterwards, costs about a second each, and the primary keeps its own copy
    across a node restart -- so fitting them here would be twenty seconds of
    work for an answer nobody is going to ask this node for.

    Sequential, and on one background thread. The reads contend for the same
    file, so racing them wins nothing, and the readers-writer lock means a real
    request can interleave rather than queue behind the whole warm.
    """
    def run():
        from plexora.server.node import api as node_api

        for resource in registry.all():
            if resource.kind not in ("image", "segmentation"):
                continue
            if resource.state != node_resources.READY:
                continue
            try:
                with node_api._reading(resource):
                    pass
                if resource.kind != "image":
                    continue
                overview = resource.opened_overview
                for index in range(len(overview) if overview is not None else 0):
                    with node_api._reading(resource):
                        node_api._quantization(resource, index)
            except Exception as exc:  # pragma: no cover - unreadable at warm time
                # Never fatal. A resource that cannot be warmed is one that
                # will report its own failure when something asks for it, and
                # taking the node down over it would lose every other resource.
                log(f"  could not warm {resource.id}: {exc}")
        log("Warm-up finished; tiles will not have to open anything.")

    thread = threading.Thread(target=run, name="plexora-node-warm", daemon=True)
    thread.start()
    return thread


def serve_node(serve, token=None, host="127.0.0.1", port=8642, *, node_id=None,
               allow_origins=(), plugins=None, dynamic=False, manifest=None,
               log=print):
    """Start a node and block, printing what a primary needs to register it."""
    from waitress import serve as waitress_serve

    from plexora._resources import worker_threads

    # Hex rather than token_urlsafe: the printed registration line is meant to
    # be copied and pasted, and a token that begins with '-' is read as a flag
    # by the very command it is being pasted into.
    token = token or secrets.token_hex(16)
    # A dynamic node with a stable identity remembers what it was given, unless
    # told where to keep that memory. Defaulted HERE rather than by whoever
    # launched it, because the path is on THIS machine and the launcher is
    # usually on another one -- an ssh command line cannot name a data root it
    # has never seen, and `~` in a quoted argument is not expanded by anything.
    if dynamic and manifest is None and node_id:
        manifest = _default_manifest(node_id, log=log)
    # `log` reaches the app builder because preparing a mask happens in there
    # and can take minutes. A terminal that sits silent for that long, before
    # the startup banner has even appeared, is one an operator kills.
    app = create_node_app(serve, token, node_id=node_id,
                          allow_origins=allow_origins, plugins=plugins,
                          dynamic=dynamic, manifest=manifest, log=log)
    registry = app.config["PLEXORA_NODE_RESOURCES"]

    # Emitted before anything else and on one line, so that a `plexora connect`
    # reading this node's stdout over ssh can register it without the user
    # copying anything. The token is on it deliberately: the only reader is the
    # process on the other end of an ssh channel, which is encrypted, and the
    # alternative -- putting it in the remote command line -- would expose it
    # in `ps` output to every other account on the cluster. It is redacted
    # again before any of this reaches a page (see remote_sessions.redact).
    # `hostname` is not `host`. `host` is where this process bound, which for
    # the ordinary loopback case is "127.0.0.1" and says nothing about which
    # machine that loopback belongs to. Under a scheduler that is the one thing
    # the other end needs: srun decides which compute node this lands on, and
    # nothing on the launching side can know it until this line says so. Sent
    # always, because a field that appears only sometimes is one every reader
    # has to special-case.
    log(f"{NODE_ANNOUNCE_PREFIX} host={_advertised(host)} port={port} "
        f"node_id={app.config['PLEXORA_NODE_ID']} token={token} "
        f"hostname={socket.gethostname()}")
    # The announce's entire job is to cross a pipe promptly. When stdout IS a
    # pipe, Python block-buffers it, and an unflushed announce sits invisible
    # while the parent waits its full deadline for a node that is in fact up
    # and serving. The ssh-launched node dodges this only because its command
    # line happens to wrap it in `env PYTHONUNBUFFERED=1`; a locally spawned
    # one has no such cover. Flushing here, at the source, protects every
    # consumer instead of relying on each launcher to remember the env var.
    sys.stdout.flush()

    log(f"Plexora data node {app.config['PLEXORA_NODE_ID']} on {host}:{port}")
    for resource in registry.all():
        log(f"  {resource.kind:13} {resource.id:20} {resource.path}")
    if dynamic:
        log("  (accepting more from the viewer while this runs -- --dynamic)")
    log("")
    log("Register it on the machine running the viewer:")
    log(f"  plexora.register_node(\"<name>\", \"http://{_advertised(host)}:{port}\", "
        f"token=\"{token}\")")
    log("")

    # After the announce, never before it: the parent is waiting on that line
    # to know this node is up, and warming reads gigabytes. Started here rather
    # than in `create_node_app` so that building an app -- which every test
    # does -- never touches the files it was pointed at.
    warm_resources(registry, log=log)

    waitress_serve(
        app,
        host=host,
        port=port,
        max_request_body_size=1073741824000000,
        max_request_header_size=85899345920000,
        threads=worker_threads(),
    )


def _default_manifest(node_id, log=print):
    """Where a node with a name of its own keeps its list of served files.

    Beside the other private registries in this machine's Plexora data root,
    under the node's id -- so two nodes on one host do not overwrite each
    other's memory, and the same node coming back next week finds its own.

    Never fatal: a node that cannot work out where to keep a manifest is a node
    that forgets between sessions, which is exactly what it did before this
    existed.
    """
    try:
        from plexora import paths

        directory = Path(paths.data_root()) / "node-manifests"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{node_id}.json"
    except Exception as exc:  # noqa: BLE001 - a convenience, not a requirement
        log(f"  (no manifest: {exc})")
        return None


def _advertised(host):
    """The address to print in the registration line.

    A node bound to 0.0.0.0 is reachable at this machine's name, and printing
    "0.0.0.0" would have the operator paste an address that means "every
    interface" into a field that means "this one".
    """
    if host in ("0.0.0.0", "::", ""):
        return socket.gethostname()
    return host
