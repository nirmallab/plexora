"""Regions of interest: shapes the user draws on the image.

An ROI is an annotation in image space, not a property of the cell table. That
one decision is what the rest of this plugin follows from: drawing works on a
project that is nothing but an image, the geometry is stored in full-resolution
image pixels, and CSV/AnnData/SpatialData are destinations you can export to
rather than the thing being edited.

Deliberately NOT here: what the cells inside a region express, or anything else
that needs the marker matrix. Building that into the annotation engine is what
would make drawing a box conditional on having imported one -- see Requires()
below, which requires nothing.

WHICH cells fall inside a region is the one exception, and it stays on the right
side of the line because it is opt-in in both directions: the table is offered
rather than demanded, and "Map to cells" only exists once one is there.

Kept import-light, like every descriptor module: this is imported whenever the
plugin is activated, and building the Blueprint (which pulls in the geometry and
adapter code) is left to the factory.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20260821_plugin_layers"


def _blueprint():
    from plexora.plugins.roi.server.routes import roi_bp

    return roi_bp


PLUGIN = Plugin(
    name="roi",
    label="ROI",
    version=VERSION,
    blueprint_factory=_blueprint,
    # Templates are namespaced by plugin name, so two plugins can both ship a
    # "panel.html" without colliding in Flask's shared template lookup.
    panels={"tool_panel_slot": "roi/panel.html"},
    # Listed in dependency order for reading, not because the browser needs it:
    # every cross-file reference is inside a method or a constructor, and
    # toolLoader awaits all six before anything is activated, so the bindings
    # resolve whatever sequence they arrive in. What DOES matter is that all six
    # are here -- one omitted is a plugin that loads and does nothing, which is
    # what plexora/plugins/roi/tests/test_roi_boot.py exists to catch.
    scripts=(
        "roiApi.js",
        "roiGeometry.js",
        "roiState.js",
        "roiRenderer.js",
        "roiTools.js",
        "roiSidebarController.js",
    ),
    styles=("roi.css",),
    # Nothing REQUIRED, and that is still the whole point. Drawing a region
    # needs the image and nothing else -- no table, no segmentation, no marker
    # split, no column roles. A project that is one OME-TIFF and nothing more
    # can annotate, which is the case a table requirement would have silently
    # ruled out.
    #
    # What is new is the optional tier. Requiring nothing also meant SAYING
    # nothing: a user never found out that attaching their cells is what lets
    # ROI annotations be written back onto them, because the tool opened
    # straight into the panel and the connection was invisible. These four are
    # offered exactly once (Requires.optional_missing_from), and skipping them
    # is a real answer that is never re-asked.
    #
    # Ordering is the framework's, not ours: the two files come first, and the
    # column questions are held back until a table exists to ask them about --
    # "which column holds the cell id" has no answers before then.
    #
    # `excluded_image_kinds` keeps its default of ('rgb',): the flat quick-view
    # path returns from main.js's init() before any plugin is activated, so a
    # tool offered there could never come up.
    requires=Requires(
        optional=("table", "segmentation", "role:image_id", "role:cell_id"),
    ),
    # Shown instead of core's generic subtitle, because the generic one says
    # Plexora filled these in from the data -- true of a form full of guesses to
    # confirm, false twice over for a form where nothing was guessed and nothing
    # is required. Core cannot word this; only the plugin knows what the offer
    # buys.
    intro=("None of these are needed to draw ROIs. Adding cell-level data is "
           "what lets Plexora write ROI annotations back onto your cells."),
    # ROI draws its own overlay above the image and never colours cells. Claiming
    # the cell layer would evict whichever plugin legitimately holds it (the
    # shader has one range table) in exchange for nothing.
    owns_cell_layer=False,
)
