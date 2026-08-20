import multiprocessing

multiprocessing.freeze_support()

from flask import Flask
from pathlib import Path
from appdirs import user_data_dir

from numcodecs import compat_ext  # Needed for pyinstaller
from numcodecs import blosc  # Needed for pyinstaller
import xmlschema  # Needed for pyinstaller

import os
import sys

# Initialize sklearn global threadpool controller to avoid deadlock in threaded
# contexts.
from threadpoolctl import threadpool_limits

threadpool_limits()

# If you're running the pyinstaller version of the code, create a
# new directory for the data (this will be at ~/ on mac)

# centralizing path across app
cwd_path = Path.cwd()


def _clean_base_url(base_url):
    if not base_url:
        return ""
    base_url = str(base_url).strip()
    if base_url == "/":
        return ""
    return "/" + base_url.strip("/")

env_data_path = os.environ.get("PLEXORA_DATA_PATH")
if env_data_path:
    data_path = Path(env_data_path).expanduser().resolve()
elif getattr(sys, "frozen", False):
    data_path = Path(Path(sys.executable).parent / "data")
else:
    data_path = Path("plexora/data").resolve()

# Make the Data Path
data_path.mkdir(parents=True, exist_ok=True)
config_json_path = data_path / "config.json"

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
    app = Flask(__name__, template_folder=Path("client/templates"), static_folder="data")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["CLIENT_PATH"] = app.root_path + "/client/"
    app.config["IS_DOCKER"] = False
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
    # Imported here rather than at module scope: project.py is part of the
    # server package, which imports this module back.
    from plexora.server.models.project import read_config, write_config

    if not Path.is_dir(data_path):
        Path.mkdir(data_path)

    if not Path.is_file(config_json_path):
        write_config(config_json_path, {})
        return {}
    return read_config(config_json_path)


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
