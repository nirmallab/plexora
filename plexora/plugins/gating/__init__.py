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

VERSION = "20260824_icon_names"


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
    # nothing to gate; it needs the marker/metadata split to know WHICH columns
    # to offer, and the three roles it resolves through dataset.schema (cell id
    # to report selections, x/y to keep coordinates out of the marker list).
    #
    # How cells are drawn is deliberately NOT declared here. Gating paints
    # per-cell results over the image, but the project answers that itself --
    # the mask if there is one, centroids otherwise (see project.cell_layer).
    #
    # Which matrix holds those values IS declared, because a threshold is a
    # number on an axis: set against raw counts it means nothing on a
    # log-transformed copy of the same data, and nothing about the values says
    # which of the two the file handed over.
    #
    # Segmentation is offered, not demanded: gating still works with centroids
    # alone. Listing it as optional is what lets core ask at the right moment
    # instead of this plugin growing its own file-path box.
    #
    # image_id is demanded, and used to be optional. It is only safely optional
    # when the table covers a single image, and nothing establishes that:
    # deciding it needs to know which column is the image id, which is the
    # question itself. The import-time guard only fires when a column-name
    # heuristic recognises the name, so a table keyed on "roi" or "core" gets
    # imported whole and draws several images' cells over one image. Asking is
    # the only honest form -- and "this data has only one image" is one of the
    # answers, so a genuinely single-image project is not made to invent a
    # column it does not have.
    requires=Requires(
        table=True,
        markers=True,
        features=True,
        roles=("cell_id", "x", "y", "image_id"),
        optional=("segmentation",),
    ),
    owns_cell_layer=True,
)
