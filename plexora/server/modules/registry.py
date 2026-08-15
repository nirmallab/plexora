"""Registry of optional feature modules (gating today; roi or others in
future). Each module is a package under plexora/server/modules/
exposing a register(app) function that attaches its own Flask Blueprint(s).

Exactly one module is active per running process, chosen via the
PLEXORA_ACTIVE_MODULE env var (see plexora/__init__.py's
create_app(), and jupyter.py/server_cli.py/proxy.py/run.py for how that
env var gets set at launch time).

Loaders are lazy (imported only when actually selected) so a build that
never activates a given module never pays its import cost -- e.g. a
gating-free process never imports h5py/anndata via the gating module's
anndata_gates submodule.
"""


def _load_gating():
    from plexora.server.modules.gating import register
    return register


MODULES = {
    "gating": _load_gating,
}

# User-facing label for the navbar Tools dropdown.
TOOL_LABELS = {
    "gating": "Thresholding",
}

# Partial template(s) rendered for a tool's sidebar panel, fetched by
# toolLoader.js the first time a tool is opened mid-session (see
# tool_routes.py's panel route) so opening/closing never needs a page
# navigation. Keyed by the DOM mount-point id each fragment gets injected
# into (see index.html's #tool_panel_slot / #tool_panel_legacy_slot).
TOOL_PANEL_TEMPLATES = {
    "gating": {
        "tool_panel_slot": "partials/gate_marker_section.html",
        "tool_panel_legacy_slot": "partials/csv_gating_legacy.html",
    },
}

# Client <script> files the tool's sidebar controller needs, loaded lazily
# (once) by toolLoader.js alongside the panel HTML above. Keep the `?v=`
# cache-busting suffixes in sync with base.html's own (eager-load) copies of
# these same <script> tags -- both paths must load identical script versions.
TOOL_SCRIPTS = {
    "gating": [
        "../client/src/js/views/csvGatingList.js?v=20260815_gating_client_load",
        "../client/src/js/views/gatingSidebarController.js?v=20260815_gating_client_load",
    ],
}


def get_available_tools(app):
    """Tools the navbar's Tools dropdown should list for this process: at
    most one entry, matching whichever module PLEXORA_ACTIVE_MODULE actually
    installed (mirrors the single-module-per-process constraint above)."""
    installed = app.config.get("PLEXORA_ACTIVE_MODULE", "")
    label = TOOL_LABELS.get(installed)
    if label is None:
        return []
    return [{"name": installed, "label": label}]


def register_active_module(app, name):
    """No-ops for an unknown/empty module name, so a core build with no
    modules installed (or an unrecognized PLEXORA_ACTIVE_MODULE value)
    still starts cleanly with just the core routes."""
    loader = MODULES.get(name)
    if loader is None:
        return
    register = loader()
    register(app)
