import multiprocessing

multiprocessing.freeze_support()

from flask import Flask
from pathlib import Path
from appdirs import user_data_dir

from numcodecs import compat_ext  # Needed for pyinstaller
from numcodecs import blosc  # Needed for pyinstaller
import xmlschema  # Needed for pyinstaller

import os
import json
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

# Only call freeze_support if we're in a frozen environment

## uncomment block if not on O2
env_data_path = os.environ.get("PLEXORA_DATA_PATH")
if env_data_path:
    data_path = Path(env_data_path).expanduser().resolve()
elif getattr(sys, "frozen", False):
    data_path = Path(Path(sys.executable).parent / "data")
else:
    data_path = Path("plexora/data").resolve()


## uncomment block if on O2
# appname = "plexora"
# appauthor = "lsp"
# data_path = Path(user_data_dir(appname, appauthor)+'/data').resolve()
# if getattr(sys, 'frozen', False):
#     multiprocessing.freeze_support()

# print('Data Path', str(data_path), str((data_path).resolve()))
# Make the Data Path
data_path.mkdir(parents=True, exist_ok=True)
config_json_path = data_path / "config.json"

app = None


def create_app(active_module=None):
    """Build the Flask app, then register the core routes plus whichever
    single feature module (gating today; roi or others in future) is
    active for this process.

    `global app` is assigned immediately after constructing Flask(...) --
    before the route-module imports below -- because those modules do
    `from plexora import app` themselves; if the module-level
    `app` attribute didn't exist yet at that point (e.g. only assigned via
    `app = create_app()` after this function returns), that import would
    fail on a partially-initialized package.

    In practice this is called exactly once, at import time, at the bottom
    of this file -- it's a factory (rather than a bare module-level
    Flask()) only so an active module can be chosen before route
    registration happens.
    """
    global app
    app = Flask(__name__, template_folder=Path("client/templates"), static_folder="data")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["CLIENT_PATH"] = app.root_path + "/client/"
    app.config["IS_DOCKER"] = False
    app.config["PLEXORA_BASE_URL"] = _clean_base_url(os.environ.get("PLEXORA_BASE_URL", ""))
    app.config["PLEXORA_NOTEBOOK_MODE"] = os.environ.get("PLEXORA_NOTEBOOK_MODE", "").lower() in ("1", "true", "yes")
    # `is None` (not a truthy check) so an explicit active_module="" -- "no
    # module, core only" -- is distinguishable from "not passed, use the
    # env var/default". A truthy-or here would silently treat an explicit
    # empty string the same as "not provided" and fall back to the default.
    if active_module is None:
        active_module = os.environ.get("PLEXORA_ACTIVE_MODULE", "")
    app.config["PLEXORA_ACTIVE_MODULE"] = active_module

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
    from plexora.server.routes import page_routes, data_routes, import_routes, datasource_config_routes, quick_view_routes, browse_routes, tool_routes
    from plexora.server.models import data_model, database_model
    from plexora.server.modules.registry import register_active_module

    register_active_module(app, app.config["PLEXORA_ACTIVE_MODULE"])

    return app


def get_config():
    if not Path.is_dir(data_path):
        Path.mkdir(data_path)

    if not Path.is_file(config_json_path):
        with open(config_json_path, "w") as f:
            json.dump({}, f)
            return []
    else:
        with open(config_json_path, "r+") as f:
            data = json.load(f)
    return data


def get_config_names():
    data = get_config()
    try:
        return [key for key in data.keys()]
    except AttributeError:
        return []


app = create_app()
