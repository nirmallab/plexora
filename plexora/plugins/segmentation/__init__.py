"""Segmentation in 3-D: Cellpose on a z-stack, the result made the project's mask.

A z-stack segmented plane by plane gives every plane its own cells; segmented
in 3-D it gives each cell one id through every plane it reaches -- the label
volume the viewer outlines, `measure_objects` measures whole, and gating, QC
and Cell Explorer read like any mask. This plugin runs Cellpose (Cellpose-SAM)
on the project's own stack, where Plexora runs, block by block over the plane
(server/cellpose3d.py), writes the label volume beside the project's derived
files and points the project at it (server/run.py).

Cellpose and PyTorch come with the optional extra `plexora[segment3d]` and are
imported only when a stack is segmented; without them the card says how to
install them and the agent's `segment_3d` answers `precondition_missing`.
Offered only on a z-stack (`Requires(dims=("3d",))`), and only on a licence
with Plexora 3D (`volume:segmentation`): without it the tool is not shown and
its routes and `segment_3d` refuse.

Kept import-light, like every descriptor module.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20261027_sidebar_radius"


def _blueprint():
    from plexora.plugins.segmentation.server.routes import segmentation_bp

    return segmentation_bp


def _capabilities():
    from plexora.plugins.segmentation.capabilities import capabilities

    return capabilities()


PLUGIN = Plugin(
    name="segmentation",
    label="Segment in 3-D",
    version=VERSION,
    blueprint_factory=_blueprint,
    entitlement="volume:segmentation",
    icon="cubes",
    panels={"tool_panel_slot": "segmentation/panel.html"},
    scripts=("segmentation3d.js",),
    styles=("segmentation3d.css",),
    requires=Requires(dims=("3d",)),
    intro=("Finds every cell of a z-stack in 3-D with Cellpose and makes the result this "
           "project's mask, one id per cell through every plane, measured into the cell table."),
    capabilities_factory=_capabilities,
    owns_cell_layer=False,
)
