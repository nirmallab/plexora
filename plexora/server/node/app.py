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
from pathlib import Path

from flask import Flask, jsonify

from plexora.server.node import resources as node_resources
from plexora.server.node.api import API_VERSION, node_bp


class NodeStartupError(RuntimeError):
    """The node cannot start, with a sentence worth printing."""


def create_node_app(serve, token, *, node_id=None, allow_origins=(), plugins=None):
    """One node app serving the resources named in `serve`.

    `serve` is a list of `kind:id=path` strings -- see
    `node_resources.parse_serve`. Every one is resolved and checked here rather
    than on first use: a typo in a path is a thing to find out about while the
    operator is still looking at the terminal, not three hours later when
    somebody opens the project.
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

    registry = node_resources.Registry()
    for argument in serve or ():
        kind, resource_id, path = node_resources.parse_serve(argument)
        resource = registry.add(kind, resource_id, path)
        if kind == "segmentation":
            _refuse_unservable_mask(resource)
    if not len(registry):
        raise NodeStartupError(
            "a data node with nothing to serve would answer every request with "
            "404. Pass at least one --serve kind:id=path."
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


def _refuse_unservable_mask(resource):
    """Stop at startup for a mask the tile route could not serve.

    A segmentation file has to be a TILED, PYRAMIDAL label image before
    anything can hand out tiles of it, and the masks that come out of a
    segmentation pipeline usually are neither -- one full-resolution strip-
    based plane is the norm. On the viewer's own machine that conversion
    happens at import and the user watches a progress bar; a node is pointed at
    a file and told to serve it, so the same discovery has to happen here.

    At startup rather than on the first tile, and with the command that fixes
    it, because the alternative is a viewer that opens with an empty cell layer
    hours later and nothing anywhere saying why. A node does not convert on its
    own: the conversion writes a derived file that is often larger than the
    original, and where that lands is a question about somebody's disk quota
    rather than about Plexora.
    """
    from plexora.server.utils import segmentation_pyramid

    # A mask Plexora produced is servable whatever its level count -- an image
    # small enough to fit in one tile converts to a single tiled level, and
    # there is nothing further to downsample to. This is the same rule
    # `refresh_segmentation_mapping` applies before it adopts a derived file,
    # and the two must agree or a mask that opens locally is refused here.
    if segmentation_pyramid.generated_mask_kind(resource.path) is not None:
        return
    if segmentation_pyramid.looks_like_outline_mask(resource.path):
        return

    gaps = segmentation_pyramid.label_pyramid_gaps(resource.path)
    if gaps == []:
        return
    if gaps is None:
        raise NodeStartupError(
            f"{resource.path} could not be read as a label mask.")
    suggested = str(Path(resource.path).with_suffix("")) + "_pyramid.ome.tif"
    raise NodeStartupError(
        f"{resource.path} cannot be served as a cell layer as it is, because "
        f"{' and '.join(gaps)}.\n\n"
        f"Convert it once, on this machine, then serve the result:\n"
        f"  plexora node prepare {resource.path} {suggested}\n"
        f"  plexora node serve --serve segmentation:{resource.id}={suggested} ..."
    )


def prepare_mask(source, output=None, *, outline=False, log=print):
    """Turn a label mask into something a node can serve tiles of.

    The same conversion an import runs, reachable on a machine that has no
    viewer and no projects. Prints progress, because on a whole-slide mask this
    is tens of seconds to minutes and a silent terminal is indistinguishable
    from a hang.
    """
    from plexora.server.utils import segmentation_pyramid

    source = Path(source).expanduser()
    if not source.exists():
        raise NodeStartupError(f"there is nothing at {source}")
    output = Path(output).expanduser() if output else Path(
        str(source.with_suffix("")) + "_pyramid.ome.tif")

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

    written = segmentation_pyramid.pyramidize_segmentation_mask(
        source, output, overwrite=True, outline=outline,
        progress_callback=report,
    )
    log("  100%      ")
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


def serve_node(serve, token=None, host="127.0.0.1", port=8642, *, node_id=None,
               allow_origins=(), plugins=None, log=print):
    """Start a node and block, printing what a primary needs to register it."""
    from waitress import serve as waitress_serve

    from plexora._resources import worker_threads

    # Hex rather than token_urlsafe: the printed registration line is meant to
    # be copied and pasted, and a token that begins with '-' is read as a flag
    # by the very command it is being pasted into.
    token = token or secrets.token_hex(16)
    app = create_node_app(serve, token, node_id=node_id,
                          allow_origins=allow_origins, plugins=plugins)
    registry = app.config["PLEXORA_NODE_RESOURCES"]

    log(f"Plexora data node {app.config['PLEXORA_NODE_ID']} on {host}:{port}")
    for resource in registry.all():
        log(f"  {resource.kind:13} {resource.id:20} {resource.path}")
    log("")
    log("Register it on the machine running the viewer:")
    log(f"  plexora.register_node(\"<name>\", \"http://{_advertised(host)}:{port}\", "
        f"token=\"{token}\")")
    log("")

    waitress_serve(
        app,
        host=host,
        port=port,
        max_request_body_size=1073741824000000,
        max_request_header_size=85899345920000,
        threads=worker_threads(),
    )


def _advertised(host):
    """The address to print in the registration line.

    A node bound to 0.0.0.0 is reachable at this machine's name, and printing
    "0.0.0.0" would have the operator paste an address that means "every
    interface" into a field that means "this one".
    """
    if host in ("0.0.0.0", "::", ""):
        return socket.gethostname()
    return host
