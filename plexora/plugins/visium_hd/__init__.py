"""Visium HD: gene expression on a 2 micron grid, drawn over its H&E.

A Space Ranger run of a Visium HD slide is thirty million squares with a count
of every gene in each. The RENDERING of them is core's -- a counted grid is a
spatial primitive, pooled and coloured by `server/models/bin_tiles.py` and
served through the one layer route -- and so is the TABLE, which the importer
converts into an ordinary AnnData (`server/utils/tenx_matrix.py`) so gating,
ROIs, the cell explorer and SCIMAP work on bins the way they work on cells.
What is here interprets: which genes exist, which ones the user picked, what
colour each is, how coarse the squares are, and how to read Space Ranger's
matrix into the store.

The split follows the transcripts plugin's exactly, and for its reason: which
vendor files Plexora can read is a question that grows, and each answer is a
plugin rather than a branch in core's importer.

Kept import-light, like every descriptor module: building the Blueprint --
which pulls in the reader -- is left to the factory.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20260928_gene_hover"


def _blueprint():
    from plexora.plugins.visium_hd.server.routes import (BIN_STAGES,
                                                         build_layer,
                                                         visium_hd_bp)
    from plexora.server.models import layer_jobs

    # How core prepares a `visium_bins` layer, said once, here. Core holds no
    # branch for the modality and imports nothing from this package; it looks
    # the builder up by name (tests/test_plugin_boundary.py).
    layer_jobs.register_builder("visium_bins", build_layer, stages=BIN_STAGES)
    return visium_hd_bp


PLUGIN = Plugin(
    name="visium_hd",
    label="Visium HD bins",
    version=VERSION,
    blueprint_factory=_blueprint,
    icon="table-cells",
    # A LAYER SECTION, like transcripts: part of what the viewer is for this
    # sample, on from page load, not a tool that competes for the panel.
    panels={Plugin.LAYER_SECTION_SLOT: "visium_hd/panel.html"},
    scripts=("visiumHdApi.js", "binLayer.js", "spotLayer.js",
             "visiumHdSidebarController.js"),
    styles=("visium_hd.css",),
    # HD bins or a standard run's 55 micron spots: the same panel, the same
    # gene list, a different drawing (binLayer.js / spotLayer.js).
    requires=Requires(layers=("visium_bins|visium_spots",)),
    intro=("Visium bins and spots are drawn by the viewer itself. This "
           "section chooses which genes are shown, in what colour, and how "
           "coarse the squares are."),
    # It colours SQUARES and SPOTS of its own, not cells.
    owns_cell_layer=False,
)
