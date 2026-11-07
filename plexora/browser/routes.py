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
    # against the platform; Python reads what the page writes.
    "license.license_activate", "license.license_connect", "license.license_connect_poll",
    "license.license_deactivate", "license.license_install", "license.license_refresh",
    "license.license_remove",
)

#: Rendered when the site is built; the shell is on screen before Python is.
STATIC = ("my_index", "image_viewer", "open_project_page", "settings_page")

#: Files served as they are.
NETWORK = ("serveClient", "favicon")

#: What a web page cannot do, the sentence that says so, where to go instead
#: (`to`: the desktop app, a node beside the data, or nowhere because the
#: website does not need it) and the actions the notice offers.
HANDOFF = {
    # A web page cannot open a TCP socket, so it cannot run ssh. Remote data
    # keeps its zero-cost routes: the desktop app (or the small connector),
    # a node the cluster's portal starts, or a tunnel the user opens.
    "settings_remotes_connect": ("desktop", "Connecting to a server runs ssh, which a web page cannot do. Connect from the Plexora desktop app, or start a node on the server and pair it here.", ("open_desktop", "pair_node")),
    "settings_remotes_answer": ("desktop", "Answering a server's password or Duo prompt is part of an ssh connection, which a web page cannot make.", ("open_desktop",)),
    "settings_remotes_disconnect": ("desktop", "Connections to servers are made and closed by the Plexora desktop app.", ("open_desktop",)),
    "settings_remotes_status": ("desktop", "Connections to servers are made by the Plexora desktop app; a node paired here shows its own status.", ("open_desktop", "pair_node")),
    "settings_remotes_askpass_prompt": ("desktop", "Password prompts belong to an ssh connection, which a web page cannot make.", ("open_desktop",)),
    "settings_remotes_askpass_answer": ("desktop", "Password prompts belong to an ssh connection, which a web page cannot make.", ("open_desktop",)),
    # Google Cloud through its command-line tool, until the website reaches
    # Google's own APIs with the user's sign-in (plan L9).
    "gcloud_auth": ("desktop", "Google Cloud is reached through the Google Cloud CLI, which runs on your computer. Use the Plexora desktop app for cloud machines.", ("open_desktop",)),
    "gcloud_status": ("desktop", "Google Cloud is reached through the Google Cloud CLI, which runs on your computer.", ("open_desktop",)),
    "gcloud_projects": ("desktop", "Google Cloud is reached through the Google Cloud CLI, which runs on your computer.", ("open_desktop",)),
    "gcloud_buckets": ("desktop", "Google Cloud is reached through the Google Cloud CLI, which runs on your computer.", ("open_desktop",)),
    "gcloud_bucket": ("desktop", "Google Cloud is reached through the Google Cloud CLI, which runs on your computer.", ("open_desktop",)),
    "gcloud_zones": ("desktop", "Google Cloud is reached through the Google Cloud CLI, which runs on your computer.", ("open_desktop",)),
    "gcloud_instances": ("desktop", "Google Cloud is reached through the Google Cloud CLI, which runs on your computer.", ("open_desktop",)),
    "gcloud_vm_status": ("desktop", "Cloud machines are started and stopped from the Plexora desktop app.", ("open_desktop",)),
    "gcloud_vm_start": ("desktop", "Cloud machines are started and stopped from the Plexora desktop app.", ("open_desktop",)),
    "gcloud_vm_stop": ("desktop", "Cloud machines are started and stopped from the Plexora desktop app.", ("open_desktop",)),
    "gcloud_vm_delete": ("desktop", "Cloud machines are deleted from the Plexora desktop app.", ("open_desktop",)),
    "gcloud_vm_standard": ("desktop", "Cloud machines are changed from the Plexora desktop app.", ("open_desktop",)),
    # An installed program's own business: the website is always the current
    # version, and there is nothing to stop.
    "update_check": ("none", "The website is always the current version of Plexora.", ()),
    "update_install": ("none", "The website is always the current version of Plexora.", ()),
    "update_install_status": ("none", "The website is always the current version of Plexora.", ()),
    "update_prefs": ("none", "The website is always the current version of Plexora.", ()),
    "update_restart": ("none", "The website is always the current version of Plexora.", ()),
    "shutdown": ("none", "There is no Plexora process to stop: close the tab.", ()),
    # A data folder on a disk: the website keeps projects in this browser.
    "settings_data": ("none", "On the website, projects are kept in this browser's storage; a data folder is a setting of the desktop app.", ("open_desktop",)),
    "settings_data_set": ("desktop", "A data folder is a setting of the desktop app; the website keeps projects in this browser.", ("open_desktop",)),
    "settings_data_check": ("desktop", "A data folder is a setting of the desktop app; the website keeps projects in this browser.", ("open_desktop",)),
    "settings_data_migration": ("desktop", "Moving a data folder is done by the desktop app.", ("open_desktop",)),
    # The desktop app's own window.
    "desktop_info": ("none", "This is the website, not the desktop app.", ("get_desktop",)),
    "desktop_open": ("desktop", "Opening a project in the desktop app is the notice's \"Open in Plexora desktop\".", ("open_desktop", "get_desktop")),
    # A benchmark's cache flush, for the bench harness against a real server.
    "bench_flush": ("none", "A benchmark route of the server; the website has no server caches to flush.", ()),
}

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
    "project_files_here", "project_files_found",
    "project_attach_resource", "project_thumbnail", "list_projects", "assign_projects", "put_file",
    "reload_datasource", "remote_health", "rename_channels_json", "set_pixel_size",
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
            to, why, actions = HANDOFF[rule.endpoint]
            row["handoff"] = {"to": to, "why": why, "actions": list(actions)}
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
