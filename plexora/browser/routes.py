"""Which part of the website answers each of the app's routes.

On the website the page's fetch facade (`services/backend.js`) decides, per
request, who answers it, from the table this module generates
(`client/src/js/services/backendRoutes.js`):

- ``js``: the page itself. The viewer must never wait on Python, which starts
  seconds after the page does, so the routes a first screenful of tiles and
  cells needs are answered by the JavaScript primary and the browser node --
  a short, enumerated list (`JS`), held to Python's answers by conformance
  vectors.
- ``python``: Plexora's own Flask app, running in Pyodide in a worker, through
  the request bridge. Everything not named otherwise: every plugin, every
  panel, the AI layer.
- ``static``: a page rendered when the site is built (`client/static_build.py`)
  and served as a file, so the shell appears before Python has started.
- ``network``: a file the site serves as it is -- the client's scripts and
  styles, a plugin's static assets.
- ``handoff``: something a web page cannot do -- open an SSH connection, update
  or stop an installed program, change a data folder on a disk. It answers
  with a sentence saying so and what to do instead (`HANDOFF`), shown in the
  locked-panel style; never a blank panel.

`tests/test_browser_routes.py` fails when an endpoint of the full app has no
class, when an entry names an endpoint that no longer exists, when a
``handoff`` has no sentence, and when the generated table is stale:
`python -m plexora.browser.routes --write` regenerates it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

CLASSES = ("js", "python", "static", "network", "handoff")

#: Answered by the page: the viewer's first screenful (tiles, channel windows,
#: the cell table's columns) and the file picker, which only the page can
#: reach. Growing this list is a decision, not a convenience: every entry is a
#: second implementation of a Python route.
JS = (
    # The project as the viewer opens it.
    "serve_config", "get_channel_names", "get_load_status", "get_table_status",
    "get_segmentation_status", "init_database", "resource_routing", "resource_status",
    "image_status", "health",
    # Tiles and the windows that colour them.
    "generate_png", "generate_layer_tile", "generate_rgb_image", "generate_blank_tile",
    "generate_overview", "generate_batch", "get_image_channel_stats", "get_channel_gmm",
    "generate_layer_channel_stats", "generate_layer_channel_gmm",
    # Cells as the viewer draws them.
    "get_all_cells", "get_nearest_cell", "get_database_description", "get_centroid_manifest",
    "get_centroid_tiles", "get_channel_cell_ids",
    # The file picker: the user's own disk, which only the page can reach.
    "browse_capability", "browse_path", "list_dir", "search_dir", "stat_entries",
    "fs_places_route", "data_places", "check_file_existence", "check_path_existence",
    "picker_prefs", "save_picker_prefs",
    # The licence: the page holds the certificate and runs the device flow
    # against the platform (services/licenseClient.js); Python reads what the
    # page writes (browser/licence.py). The account is the page's to ask.
    "license.license_account", "license.license_activate", "license.license_connect",
    "license.license_connect_poll", "license.license_deactivate", "license.license_forget",
    "license.license_install", "license.license_refresh", "license.license_remove",
)

#: Rendered when the site is built; the shell is on screen before Python is.
STATIC = ("my_index", "image_viewer", "open_project_page", "settings_page")

#: Files served as they are.
NETWORK = ("serveClient", "favicon")

class Handoff(NamedTuple):
    """What the website says instead of answering a route it cannot serve.

    `to`: where the work goes -- "desktop" (the desktop app, or a node it or
    the cluster's portal starts) or "none" (the website does not need it).
    `title`: the notice's few words. `why`: its one line. `actions`: the
    buttons, from `ACTIONS`, in order (services/handoff.js draws them).
    """

    to: str
    title: str
    why: str
    actions: tuple


#: The buttons a notice can offer (services/handoff.js): the desktop app
#: opened (on this project, when there is one), its download page, the node
#: pairing flow (a node started on the server, paired with this browser), and
#: Google Cloud, which the website reaches without SSH (plan L9: a VM that
#: runs its own node, opened at its own address).
ACTIONS = ("open_desktop", "get_desktop", "pair_node", "google_cloud")

# A web page cannot open a TCP socket, so it cannot run ssh. Remote data keeps
# its zero-cost routes: the desktop app (or the small connector), a node the
# cluster's portal starts, or a tunnel the user opens (plan decision 1).
_SSH = Handoff("desktop", "Connect from the desktop app",
               "A web page cannot open an SSH connection: use the desktop app, "
               "or pair a node started on the server.",
               ("open_desktop", "pair_node", "google_cloud"))
# An installed program's own business: the website is always the current
# version, and there is nothing to stop.
_UPDATES = Handoff("none", "Always up to date",
                   "The website is always the current version of Plexora.", ())
# A data folder on a disk: the website keeps projects in this browser, or in
# Documents/Plexora when the user chose it (services/browserStore.js).
_DATA_FOLDER = Handoff("desktop", "A desktop app setting",
                       "The website keeps projects in this browser or in Documents › Plexora.",
                       ("open_desktop",))

#: What a web page cannot do: the notice that says so, by endpoint. The set is
#: exactly the ssh- and desktop-bound routes (tests/test_handoff_routes.py
#: holds it), so a new entry is a decision, not a default.
HANDOFF = {
    "settings_remotes_connect": _SSH,
    "settings_remotes_answer": _SSH,
    "settings_remotes_disconnect": _SSH,
    "settings_remotes_status": _SSH,
    "settings_remotes_askpass_prompt": _SSH,
    "settings_remotes_askpass_answer": _SSH,
    "update_check": _UPDATES,
    "update_install": _UPDATES,
    "update_install_status": _UPDATES,
    "update_prefs": _UPDATES,
    "update_restart": _UPDATES,
    "shutdown": Handoff("none", "Nothing to quit",
                        "The website has no process to stop: close its tab.", ()),
    "settings_data": _DATA_FOLDER,
    "settings_data_set": _DATA_FOLDER,
    "settings_data_check": _DATA_FOLDER,
    "settings_data_migration": Handoff("desktop", "A desktop app setting",
                                       "Moving a data folder is done by the desktop app.",
                                       ("open_desktop",)),
    # The desktop app's own window.
    "desktop_info": Handoff("none", "This is the website",
                            "Details of an installed Plexora come from the desktop app.",
                            ("get_desktop",)),
    "desktop_open": Handoff("desktop", "Open in the desktop app",
                            "File › Open in Desktop App hands this project over.",
                            ("open_desktop", "get_desktop")),
    # A benchmark's cache flush, for the bench harness against a real server.
    "bench_flush": Handoff("none", "Not on the website",
                           "A benchmark route of the server; the website has no caches to flush.",
                           ()),
}

#: What the website cannot do inside a route it otherwise answers, by name:
#: the same notice, carried in that route's answer (`feature(name)`) for the
#: panel to draw with services/handoff.js.
FEATURES = {
    # Video Studio writes ProRes with FFmpeg's prores_ks, a program the desktop
    # runs as a separate process. No LGPL build of FFmpeg for the web with that
    # encoder exists to ship (docs/internal/bench/browser_node/L10.md).
    "video_studio.prores": Handoff("desktop", "ProRes is in the desktop app",
                                   "A web page cannot run FFmpeg, ProRes's encoder.",
                                   ("open_desktop", "get_desktop")),
}


def feature(name: str) -> dict:
    """`FEATURES[name]` in the shape backendRoutes.js gives a handoff route."""
    notice = FEATURES[name]
    return {"to": notice.to, "title": notice.title, "why": notice.why, "actions": list(notice.actions)}


#: Whole blueprints, by name: every route of a plugin is its Python's, and its
#: static files are files (`<blueprint>.static` is ``network``).
BLUEPRINTS = {
    "agent_v1": "python", "ai_v1": "python", "ai_chat_v1": "python", "segment_v1": "python",
    "license": "python",
    "cell_explorer": "python", "figure_builder": "python", "gating": "python", "qc": "python",
    "roi": "python", "transcripts": "python",
    "video_studio": "python", "visium_hd": "python",
}

#: Core routes answered by Python. Listed rather than defaulted, so a new core
#: route is a decision about who answers it.
PYTHON = (
    "project_requirements", "satisfy_requirements", "open_tool", "tool_panel", "tool_requirements",
    "check_dataset_exists", "create_dataset", "list_datasets", "update_dataset", "delete_dataset",
    "dataset_scan_start", "dataset_scan_get", "dataset_scan_cancel", "dataset_scan_create",
    "dataset_scan_create_status", "dataset_scan_create_cancel", "dataset_scan_plan",
    "dataset_scan_spec", "detect_image_type", "get_display_contrast", "compute_display_contrast",
    "drop_inspect", "edit_project_page", "fetch_file", "fix_table", "undo_table_fix",
    "get_consistency_report", "get_not_called", "get_ome_metadata", "get_saved_channel_list",
    "save_channel_list", "import_inspect", "import_layers_route", "import_reclassify_route",
    "import_release_route", "import_sample_route", "import_status", "inspect_data",
    "list_spatialdata_tables_route", "node_share_resource", "node_unshare_resource",
    "node_resource_status", "save_project", "delete_project", "project_remove_layer",
    "project_update_layer", "project_order_layers", "project_manifest", "project_resources",
    "project_files_here", "project_files_found", "project_open_elsewhere",
    "project_bundle", "projects_import_bundle", "project_size",
    "project_attach_resource", "project_thumbnail", "list_projects", "assign_projects", "put_file",
    "reload_datasource", "remote_health", "rename_channels_json", "set_pixel_size",
    # File > Open on the Website (plexora/website_link.py): `shown: false` here,
    # as off any desktop app's server; the row is the desktop window's.
    "desktop_website",
    # Google Cloud over its JSON APIs with the page's sign-in (plan L9:
    # plexora/gcloud_rest.py, services/googleAuth.js); the website's VM runs a
    # node the page hands the user to, so no SSH is involved.
    "gcloud_auth", "gcloud_status", "gcloud_projects", "gcloud_buckets", "gcloud_bucket",
    "gcloud_objects", "gcloud_zones", "gcloud_instances", "gcloud_vm_status",
    "gcloud_vm_start", "gcloud_vm_stop", "gcloud_vm_delete", "gcloud_vm_standard",
    "gcloud_vm_resize", "gcloud_vm_node",
    "settings_nodes", "settings_nodes_add", "settings_nodes_remove", "settings_recipes",
    "settings_recipes_save", "settings_remotes", "settings_remotes_save", "settings_remotes_remove",
    "settings_webdata", "settings_webdata_budget", "settings_webdata_clear", "settings_webdata_jobs",
    "settings_webdata_options", "settings_webdata_options_delete", "settings_webdata_options_save",
    "settings_webdata_pin", "settings_webdata_remove", "settings_webdata_unpin",
    "supported_data_formats", "telemetry_ingest", "telemetry_mode", "telemetry_notice_seen",
    "telemetry_preview", "telemetry_reset", "telemetry_send", "telemetry_status",
    "upload_channels", "upload_data_file", "get_view_state_route", "put_view_state",
    "get_view_transform", "put_view_transform", "get_viewer_prefs", "put_viewer_prefs",
    # Volumes and surfaces: Python's, over the same chunk reads as the desktop.
    "volume_bricks", "bricks_manifest", "volume_info", "volume_level", "get_objects",
    "measure_objects_route", "volume_perf", "pin_projection", "plane_cells", "volume_prepare",
    "section_geometry", "section_cells", "create_surface", "list_surfaces", "cancel_surface",
    "export_surface", "surface_manifest", "surface_mesh", "surface_neighbours", "surface_objects",
    "refine_surface", "surface_sections", "estimate_surface", "import_surface", "preview_surface",
    "suggest_threshold", "track_objects_route", "volume_window",
    # The desktop app's Advanced settings (plexora/browser_rendering.py): off
    # the desktop app Python says there is nothing to set.
    "settings_advanced", "settings_advanced_set",
)


def classify(endpoint: str) -> str | None:
    """The class of one endpoint, or None when nothing names it."""
    if endpoint in JS:
        return "js"
    if endpoint in STATIC:
        return "static"
    if endpoint in NETWORK:
        return "network"
    if endpoint in HANDOFF:
        return "handoff"
    if endpoint in PYTHON:
        return "python"
    blueprint, dot, name = endpoint.partition(".")
    if dot:
        if name == "static":
            return "network"
        return BLUEPRINTS.get(blueprint)
    return None


def named() -> set:
    """Every endpoint this module names one by one (stale names are an error)."""
    return set(JS) | set(STATIC) | set(NETWORK) | set(HANDOFF) | set(PYTHON)


_CONVERTER = re.compile(r"<(?:(\w+)(?:\([^)]*\))?:)?(\w+)>")
_PATTERN = {"path": ".+", "int": r"\d+", "float": r"\d+(?:\.\d+)?"}


def pattern(rule: str) -> str:
    """A Werkzeug rule as a JavaScript regular expression's source, anchored."""
    out, at = [], 0
    for match in _CONVERTER.finditer(rule):
        out.append(re.escape(rule[at:match.start()]).replace("/", "\\/"))
        out.append(f"({_PATTERN.get(match.group(1) or 'string', '[^/]+')})")
        at = match.end()
    out.append(re.escape(rule[at:]).replace("/", "\\/"))
    return "^" + "".join(out).replace("\\-", "-") + "$"


def table(app) -> list[dict]:
    """One row per (rule, methods): what the facade matches a request against,
    most specific first (fewer variables, then more fixed text), the order in
    which `/config` must win over `/<datasource>`."""
    rows = []
    for rule in app.url_map.iter_rules():
        methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
        variables = len(list(_CONVERTER.finditer(rule.rule)))
        fixed = len(_CONVERTER.sub("", rule.rule))
        row = {"pattern": pattern(rule.rule), "methods": methods,
               "class": classify(rule.endpoint) or "python", "endpoint": rule.endpoint}
        if row["class"] == "handoff":
            notice = HANDOFF[rule.endpoint]
            row["handoff"] = {"to": notice.to, "title": notice.title, "why": notice.why,
                              "actions": list(notice.actions)}
        rows.append((variables, -fixed, rule.rule, row))
    rows.sort(key=lambda each: each[:3])
    return [row for *_key, row in rows]


GENERATED = Path(__file__).resolve().parents[1] / "client" / "src" / "js" / "services" / "backendRoutes.js"


def generate_js(app) -> str:
    """`backendRoutes.js`: the table above as a classic script."""
    rows = table(app)
    body = ",\n".join("  " + json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
    return ("// Generated by `python -m plexora.browser.routes --write` from\n"
            "// plexora/browser/routes.py -- do not edit. Which part of the website answers\n"
            "// each route (services/backend.js): js | python | static | network | handoff.\n"
            "(function () {\n"
            "  \"use strict\";\n"
            "  // globalThis: the page reads it, and so does the site's service worker.\n"
            "  globalThis.PlexoraBackendRoutes = Object.freeze([\n"
            f"{body}\n"
            "  ]);\n"
            "})();\n")


def main(argv=None):
    import argparse
    import os
    import tempfile

    parser = argparse.ArgumentParser(description="Regenerate backendRoutes.js from the full app.")
    parser.add_argument("--write", action="store_true", help="write the file (default: check it)")
    args = parser.parse_args(argv)
    os.environ.setdefault("PLEXORA_DATA_PATH", tempfile.mkdtemp())
    os.environ.pop("PLEXORA_PLUGINS", None)
    import plexora

    text = generate_js(plexora.app)
    if args.write:
        GENERATED.write_bytes(text.encode("utf-8"))
        print(f"wrote {GENERATED}")
        return 0
    current = GENERATED.read_bytes().decode("utf-8") if GENERATED.exists() else ""
    if current != text:
        print(f"{GENERATED} is stale: run python -m plexora.browser.routes --write")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
