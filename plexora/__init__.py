import multiprocessing

multiprocessing.freeze_support()

from flask import Flask, jsonify, request
from pathlib import Path

import hmac
import os
import threading

# Cap every numeric thread pool at this process's REAL CPU allocation, and do
# it here -- before anything numeric is imported -- so the environment
# variables OpenBLAS/MKL/OpenMP read at load time are already in place.
#
# This replaces a bare `threadpool_limits()`, which capped nothing: threadpoolctl
# only applies a limit when one is passed, so that call's only effect was to
# force the BLAS libraries to load. That effect mattered (it is what stops two
# request threads racing to load them and deadlocking) and is preserved --
# configure_thread_pools() calls threadpool_limits with a real limit, which
# loads them just the same.
#
# See plexora._resources for why os.cpu_count() is the wrong question here: on
# a 2-core SLURM allocation carved out of a 64-core node it reports 64, and the
# resulting oversubscription is what buried the server on HMS O2.
from plexora._resources import configure_thread_pools

configure_thread_pools()

# Where data lives is `plexora.paths`' business, and it is resolved on demand
# rather than here. This module used to compute `data_path` at import, whose
# last fallback was relative to the current working directory -- see that
# module's docstring for why that had to go, and why nothing may snapshot the
# answer into a module constant again.


# Re-exported under its historical name because server_cli.py imports it from
# here. `plexora._url` is a leaf module -- it imports nothing from this package
# -- so pulling it in mid-initialisation is safe.
from plexora._url import clean_prefix as _clean_base_url

#: Where the entry URL's `?token=` is remembered for the rest of the session.
AUTH_COOKIE = "plexora_auth"

#: What the unavailable-resource handler has already said, and the load it said
#: it for. One screenful of the viewer is dozens of tile requests and every one
#: of them fails the same way against the same absent machine, so the sentence
#: is printed once and the rest are quiet.
_said_unavailable = set()
_said_for_generation = None
_said_lock = threading.Lock()


def _say_unavailable_once(message, context=""):
    """Print `message` unless this load of a project has already printed it.

    Keyed on `data_model.load_generation` rather than cleared on a timer:
    reloading is the only thing that re-reads a project's bindings, so it is
    exactly when "is that node here" can have a different answer -- which is
    the same reason the tile cache keys on it.

    `context` rides along on the line without joining the key, so the line
    names one of the requests that hit this rather than standing for none of
    them.
    """
    global _said_for_generation

    # Local, like every other import of the server package in this module: it
    # imports this one back, and this runs long after both are built anyway.
    from plexora.server.models import data_model

    with _said_lock:
        if data_model.load_generation != _said_for_generation:
            _said_for_generation = data_model.load_generation
            _said_unavailable.clear()
        if message in _said_unavailable:
            return
        _said_unavailable.add(message)
    print(f"{context} -- {message}" if context else message)


app = None


def create_app(plugins=None):
    """Build the Flask app, then register the core routes and whichever
    plugins this process activates.

    `global app` is assigned immediately after constructing Flask(...) --
    before the route-module imports below -- because those modules do
    `from plexora import app` themselves; if the module-level
    `app` attribute didn't exist yet at that point (e.g. only assigned via
    `app = create_app()` after this function returns), that import would
    fail on a partially-initialized package.

    In practice this is called exactly once, at import time, at the bottom
    of this file -- it's a factory (rather than a bare module-level
    Flask()) only so plugins can be chosen before route registration
    happens. Calling it a second time in one interpreter does NOT rebuild
    the app: the route modules below are already in sys.modules, so their
    @app.route decorators never run again and the second app comes back
    with no core routes at all.
    """
    global app
    # static_folder is None on purpose. It used to be "data", which Flask
    # resolves against the PACKAGE directory -- so it pointed at
    # site-packages/plexora/data whatever the data root actually was, and in
    # the old dev layout (where those happened to be the same directory) it
    # served the entire data tree from `/data/<path:filename>`. Nothing ever
    # used it: no template calls url_for('static'), no client code requests
    # /data/, and tiles go through /generated/data/... instead.
    app = Flask(__name__, template_folder=Path("client/templates"), static_folder=None)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["CLIENT_PATH"] = app.root_path + "/client/"
    # Read from the environment as well as being settable afterwards. It used
    # to be set only by run.py, from its second positional argv -- so the flag
    # (which drives the import page's path hints) was silently False for every
    # other way of starting the server, including the console script the
    # container could otherwise use.
    app.config["IS_DOCKER"] = os.environ.get("PLEXORA_DOCKER", "").lower() in ("1", "true", "yes")
    app.config["PLEXORA_BASE_URL"] = _clean_base_url(os.environ.get("PLEXORA_BASE_URL", ""))
    app.config["PLEXORA_NOTEBOOK_MODE"] = os.environ.get("PLEXORA_NOTEBOOK_MODE", "").lower() in ("1", "true", "yes")
    # Set only by the paths that bind an address other than loopback for a
    # single user -- the Open OnDemand routes, which need the portal's web host
    # to be able to reach the node. Everything else leaves it empty and the
    # guard below is inert, which is what keeps the Docker image (deliberately
    # 0.0.0.0, deliberately shared) unauthenticated as it has always been.
    app.config["PLEXORA_AUTH_TOKEN"] = os.environ.get("PLEXORA_AUTH_TOKEN", "")

    # Registered unconditionally and consulted per request, NOT registered only
    # when a token exists: create_app() runs once per interpreter (see the
    # docstring), so a second call cannot add it later -- a test, or any caller
    # that sets the token after import, would silently get an unguarded app.
    @app.before_request
    def require_auth_token():
        expected = app.config.get("PLEXORA_AUTH_TOKEN") or ""
        if not expected:
            return None
        supplied = request.args.get("token") or request.cookies.get(AUTH_COOKIE) or ""
        if hmac.compare_digest(str(supplied), str(expected)):
            return None
        # Nothing is exempt, health checks included: whoever started this
        # server knows the token and passes it, and a same-node neighbour is
        # precisely who the token is keeping out.
        return (
            "This viewer requires a token. Use the exact link printed in your "
            "notebook or terminal.\n",
            403,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    @app.after_request
    def remember_auth_token(response):
        """Trade the URL's token for a cookie, so only the entry URL carries it.

        Every asset, tile and API call the page then makes rides the cookie
        instead, which keeps the token out of the query string of hundreds of
        requests -- and means the viewer survives navigation inside the app.

        Scoped to this server's own mount path, not "/": under Open OnDemand
        every job on the cluster is proxied through one portal origin, so a
        cookie at "/" would be sent to -- and overwritten by -- every other
        Plexora and every other OnDemand app the user opens.
        """
        expected = app.config.get("PLEXORA_AUTH_TOKEN") or ""
        supplied = request.args.get("token") or ""
        if expected and supplied and hmac.compare_digest(str(supplied), str(expected)):
            response.set_cookie(
                AUTH_COOKIE,
                expected,
                httponly=True,
                samesite="Lax",
                path=app.config.get("PLEXORA_BASE_URL") or "/",
            )
        return response

    @app.after_request
    def add_notebook_headers(response):
        # X-Frame-Options: SAMEORIGIN would block the direct (non-proxy) notebook
        # iframe flow, since the sidecar server (127.0.0.1:<port>) is always a
        # different origin than the Jupyter page embedding it. A loopback
        # sidecar is not on a network at all; the one case that is -- the Open
        # OnDemand bind -- is behind the token above, which a framing page
        # cannot read out of an HttpOnly cookie.
        return response

    # The one failure the provider layer is built to survive, answered as one.
    #
    # `ResourceUnavailable` means a machine did not answer -- a laptop asleep, a
    # tunnel dropped, a compute job ended, a node disconnected -- and it arrives
    # already carrying the sentence its owner should read. Unhandled, Flask
    # called that a 500 and logged the whole traceback, and since the viewer
    # asks for one tile at a time, a single screenful of a project on a vanished
    # node buried the terminal in dozens of identical stacks. None of it was
    # news: the node is named in the first line, and `/resource_status` has
    # already put the same sentence on screen with the button that fixes it.
    #
    # 503 rather than 500 because the condition is temporary by definition, and
    # rather than 404 because the resource has not gone -- the road to it has.
    # The answer is JSON because only routes that read data can arrive here;
    # rendering a page touches no resource.
    from plexora.server.providers.base import ResourceUnavailable

    @app.errorhandler(ResourceUnavailable)
    def resource_unavailable(exc):
        # The path goes in the line but not in the key: one request stands for
        # the screenful, and keying on it would print every one of them.
        _say_unavailable_once(str(exc), f"{request.method} {request.path}")
        return jsonify(success=False, error=str(exc), node=exc.node,
                       unavailable=True), 503

    # Imported here (not at module top) purely for their route-registration
    # side effects -- see the docstring above for why `app` must already be
    # assigned by this point.
    from plexora.server.routes import page_routes, data_routes, import_routes, quick_view_routes, browse_routes, transfer_routes, tool_routes, system_routes, project_routes, settings_routes, gcloud_routes
    from plexora.server.models import data_model, database_model
    from plexora.server import plugins as plugin_registry

    # `plugins is None` means "not passed, consult PLEXORA_PLUGINS", which in
    # turn distinguishes unset (activate everything installed) from "" (a
    # deliberate core-only build). A truthy check here would collapse those.
    plugin_registry.install(app, plugins)

    return app


def get_config():
    """Every project this user can open, merged across the roots.

    Thin wrapper over the project registry, kept because a great deal of the
    route layer already calls it. The merge -- and the rule that the user's own
    root wins a name collision with a shared one -- lives in project.py, which
    is the module that knows how to read a config.json safely.
    """
    # Imported here rather than at module scope: project.py is part of the
    # server package, which imports this module back.
    from plexora.server.models.project import Project

    return Project.load_all()


def get_config_names():
    data = get_config()
    try:
        return [key for key in data.keys()]
    except AttributeError:
        return []


def view(datasource, **kwargs):
    """Return a notebook-displayable Plexora viewer for `datasource`.

    This is a small public convenience wrapper around PlexoraViewer. In a
    Jupyter cell, making it the last expression starts the sidecar server and
    displays the viewer iframe.
    """
    from plexora.jupyter import PlexoraViewer

    return PlexoraViewer(datasource=datasource, **kwargs)


app = create_app()
