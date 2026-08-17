"""Marker-threshold gating: Plexora's first plugin.

Everything gating owns lives under this directory -- server code, client
assets, templates and tests -- so the whole feature can be read, moved or
removed as one unit.

Kept deliberately light: this module is imported whenever gating is activated,
and its only job is to describe the plugin. The Blueprint is built by a factory
at install time so that reading the descriptor does not drag in
scipy/sklearn/anndata/h5py.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20260816_css_boundary"


def _blueprint():
    from plexora.plugins.gating.server.routes import gating_bp

    return gating_bp


PLUGIN = Plugin(
    name="gating",
    label="Thresholding",
    version=VERSION,
    blueprint_factory=_blueprint,
    # Templates are namespaced by plugin name, so two plugins can both ship a
    # "panel.html" without colliding in Flask's shared template lookup.
    panels={
        "tool_panel_slot": "gating/panel.html",
        "tool_panel_legacy_slot": "gating/legacy.html",
    },
    # Filenames within this plugin's own static/ directory. Core turns them
    # into base-URL-safe, version-stamped URLs -- a plugin never writes a path
    # that assumes where the app is mounted.
    # gatingApi.js first: the other two construct GatingApi at init.
    scripts=("gatingApi.js", "csvGatingList.js", "gatingSidebarController.js"),
    styles=("gating.css",),
    # Gating thresholds feature-table columns, so a project without one has
    # nothing to gate. Core hides the tool rather than offering an empty panel.
    requires=Requires(table=True),
    owns_cell_layer=True,
)
