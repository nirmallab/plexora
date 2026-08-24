import multiprocessing

multiprocessing.freeze_support()

from flask import Flask
from pathlib import Path

import os

# Initialize sklearn global threadpool controller to avoid deadlock in threaded
# contexts.
from threadpoolctl import threadpool_limits

threadpool_limits()

# Where data lives is `plexora.paths`' business, and it is resolved on demand
# rather than here. This module used to compute `data_path` at import, whose
# last fallback was relative to the current working directory -- see that
# module's docstring for why that had to go, and why nothing may snapshot the
# answer into a module constant again.


# Re-exported under its historical name because server_cli.py imports it from
# here. `plexora._url` is a leaf module -- it imports nothing from this package
# -- so pulling it in mid-initialisation is safe.
from plexora._url import clean_prefix as _clean_base_url

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

    @app.after_request
    def add_notebook_headers(response):
        # X-Frame-Options: SAMEORIGIN would block the direct (non-proxy) notebook
        # iframe flow, since the sidecar server (127.0.0.1:<port>) is always a
        # different origin than the Jupyter page embedding it. The sidecar only
        # binds to 127.0.0.1, so omitting the header does not expose it to the
        # network.
        return response

    # Imported here (not at module top) purely for their route-registration
    # side effects -- see the docstring above for why `app` must already be
    # assigned by this point.
    from plexora.server.routes import page_routes, data_routes, import_routes, quick_view_routes, browse_routes, tool_routes, system_routes, project_routes
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
