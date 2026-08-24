"""Subprocess probe that reports one process's plugin boundary as JSON.

Run as `python -m tests._plugin_boundary_probe` with PLEXORA_PLUGINS set.
It must be a subprocess, not an in-process call: `plexora.create_app()` is
single-shot per interpreter. The core route modules are imported for their
`@app.route` side effects (plexora/__init__.py's create_app), so once they are
in sys.modules a second create_app() call re-imports nothing and returns an app
carrying only Flask's own /static rule -- 1 route instead of 60. Blueprints do
re-register, which makes the failure mode especially deceptive: gating route
counts still look right on that hollow app while every core route is missing.

Import isolation (does a gating-free build pull in anndata/h5py?) is likewise a
process-global property, so it can only be observed from a fresh interpreter.

Pages are reported as a STRUCTURAL DIGEST -- which scripts and stylesheets load,
which element ids exist, and what the server told the frontend -- rather than as
raw HTML. That is the part of template output the plugin boundary is actually
about, and it stays readable in a golden diff.
"""

import json
import os
import sys
from html.parser import HTMLParser

# Route registration must not depend on, or write to, the user's real data
# directory. Left unset, plexora.paths would resolve the platform default --
# this probe's own projects would land in the developer's real one.
os.environ.setdefault("PLEXORA_DATA_PATH", os.environ["PLEXORA_PROBE_DATA_PATH"])

import plexora  # noqa: E402  (must follow the env pin above)

# Modules whose absence is the actual contract: a core build must not pay the
# import cost of an addon's heavy dependencies.
WATCHED = (
    "anndata",
    "h5py",
    "sklearn.mixture",
    "scipy.stats",
    "plexora.plugins.cell_explorer",
    "plexora.plugins.figure_builder",
    "plexora.plugins.gating",
    "plexora.plugins.roi",
)

# Which tool the ?tool= page is rendered with. Parameterized because each build
# has to be probed with a tool it actually installs: asking a roi-only build for
# ?tool=gating renders the plain viewer, and the digest would then assert
# nothing about the plugin under test.
PROBE_TOOL = os.environ.get("PLEXORA_PROBE_TOOL", "gating")

# Minimal datasource that makes the viewer page renderable. image_viewer() only
# needs the name present in config plus image_kind; everything else the template
# touches is defaulted by template_data().
#
# Written as a literal rather than through tests/helpers.py on purpose: this
# probe runs in a subprocess to watch which modules get imported, so it imports
# as little as it can get away with.
#
# The entry has to satisfy everything gating declares in its Requires -- a
# table, classified columns and the cell_id/x/y/image_id roles -- AND have
# those answers marked confirmed. Either gap resolves ?tool=gating to "ask the
# user first", and the page under test never renders the plugin's panels at
# all. `confirmed` is what says a human has seen these values rather than the
# column predictor having guessed them.
#
# `singleImage` answers the image-id question the only way a table with no such
# column can: it covers one image. Confirming that key is not enough on its own
# -- a blocking requirement is satisfied by an answer, never by having been
# shown -- which is the whole point of storing this as its own state.
PROBE_DATASOURCE = "probe_ds"
PROBE_CONFIG = {
    PROBE_DATASOURCE: {
        "image_kind": "ome_tiff",
        "imageData": [{"name": "DNA", "fullname": "DNA", "src": "/generated/x/"}],
        "dataset": {
            "type": "csv",
            "src": "/probe/cells.csv",
            "roles": {"cell_id": "CellID", "x": "X", "y": "Y"},
            "columns": {"markers": ["DNA"], "metadata": ["CellID", "X", "Y"]},
            "singleImage": True,
        },
        "confirmed": ["markers", "role:cell_id", "role:x", "role:y",
                      "role:image_id"],
    }
}


class _Digest(HTMLParser):
    """Collects the structure a plugin can influence: asset URLs and mount ids."""

    def __init__(self):
        super().__init__()
        self.scripts = []
        self.styles = []
        self.ids = []
        self.tool_mounts = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])
        elif tag == "link" and a.get("rel") == "stylesheet" and a.get("href"):
            self.styles.append(a["href"])
        if a.get("id"):
            self.ids.append(a["id"])
        if a.get("data-tool-mount") is not None:
            self.tool_mounts.append((a.get("id", ""), a["data-tool-mount"]))


def _flask_variables(html):
    """The JSON blob passVariablesToFrontend() receives -- the server's own
    statement of what module/tool/tools this page believes are active."""
    marker = "window.flaskVariables = passVariablesToFrontend("
    start = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    depth, i = 0, start
    while i < len(html):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except ValueError:
                    return None
        i += 1
    return None


def page_digest(client, path):
    response = client.get(path)
    html = response.get_data(as_text=True)
    parser = _Digest()
    parser.feed(html)
    return {
        "status": response.status_code,
        "scripts": parser.scripts,
        "styles": parser.styles,
        "tool_mounts": parser.tool_mounts,
        "ids": sorted(set(parser.ids)),
        "flask_variables": _flask_variables(html),
    }


def describe(app):
    routes = sorted(
        "{} {}".format(",".join(sorted(rule.methods - {"HEAD", "OPTIONS"})), rule.rule)
        for rule in app.url_map.iter_rules()
    )
    plexora.paths.config_path().write_text(json.dumps(PROBE_CONFIG), encoding="utf-8")
    client = app.test_client()
    from plexora.server import plugins as plugin_registry

    return {
        "installed_plugins": [p.name for p in plugin_registry.installed(app)],
        "route_count": len(routes),
        "routes": routes,
        "imported": {name: name in sys.modules for name in WATCHED},
        "pages": {
            "viewer": page_digest(client, f"/{PROBE_DATASOURCE}"),
            "viewer_tool": page_digest(client, f"/{PROBE_DATASOURCE}?tool={PROBE_TOOL}"),
            "upload": page_digest(client, "/upload_page"),
            # Not a viewer page and not about a datasource, which is exactly why
            # it is here: a plugin may contribute an entry to core's menus
            # (Plugin.nav_items), and this is where such an entry shows up. The
            # digest pins that a core-only build renders no tab strip and no
            # extra File-menu item.
            "open_project": page_digest(client, "/open_project"),
        },
    }


if __name__ == "__main__":
    json.dump(describe(plexora.app), sys.stdout)
