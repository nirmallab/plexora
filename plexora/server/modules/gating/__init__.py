"""Marker-threshold gating: Plexora's first plugin.

Kept deliberately light. This module is imported whenever gating is activated,
and its only job is to describe the plugin -- the Blueprint is built by a
factory at install time so that reading the descriptor does not drag in
scipy/sklearn/anndata/h5py.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20260816"


def _blueprint():
    from plexora.server.modules.gating.routes import gating_bp

    return gating_bp


PLUGIN = Plugin(
    name="gating",
    label="Thresholding",
    version=VERSION,
    blueprint_factory=_blueprint,
    panels={
        "tool_panel_slot": "partials/gate_marker_section.html",
        "tool_panel_legacy_slot": "partials/csv_gating_legacy.html",
    },
    scripts=(
        "../client/src/js/views/csvGatingList.js",
        "../client/src/js/views/gatingSidebarController.js",
    ),
    styles=("../client/src/css/gating.css",),
    # Gating thresholds feature-table columns, so a project without one has
    # nothing to gate. Core hides the tool rather than offering an empty panel.
    requires=Requires(table=True),
    owns_cell_layer=True,
)
