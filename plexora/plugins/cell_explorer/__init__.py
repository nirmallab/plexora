"""Cell Explorer: colour every cell on the image by one metadata column.

The whole plugin follows from one boundary: **it owns the mapping from metadata
to colour, and core owns the geometry**. Cell Explorer never learns how a
segmentation tile is drawn, never renders a centroid, and never decides whether
cells appear as points, outlines or filled shapes -- it hands core a colour per
cell id and core draws it whichever way the Cells control is set to. That is
what keeps this plugin small, and what means the next plugin that wants to
colour cells gets all three representations for free.

Deliberately NOT here: marker expression. `.X` and its layers are what
Thresholding reads, and they are a different question with a different scale
problem (see TableHandle.log_transformed). This reads annotations -- phenotype,
cluster, neighbourhood, confidence, area -- and only through the format-agnostic
`table.metadata_values`, so a CSV column and an AnnData `.obs` column are the
same thing here.

It also never writes. Colours, hidden categories and ranges are display
preferences and live in the plugin store; the table is the source of truth and
is opened read-only.

Kept import-light, like every descriptor module: this is imported whenever the
plugin is activated, and building the Blueprint (which pulls in numpy-heavy
encoding and the repository) is left to the factory.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20260821_roi_composition_v2"


def _blueprint():
    from plexora.plugins.cell_explorer.server.routes import cell_explorer_bp

    return cell_explorer_bp


PLUGIN = Plugin(
    name="cell_explorer",
    label="Cell Explorer",
    version=VERSION,
    blueprint_factory=_blueprint,
    # Templates are namespaced by plugin name, so two plugins can both ship a
    # "panel.html" without colliding in Flask's shared template lookup.
    panels={"tool_panel_slot": "cell_explorer/panel.html"},
    # Listed in dependency order for reading, not because the browser needs it:
    # every cross-file reference is inside a method or a constructor, and
    # toolLoader awaits all seven before anything is activated, so the bindings
    # resolve whatever sequence they arrive in. What DOES matter is that all
    # seven are here -- one omitted is a plugin that loads and does nothing,
    # which is what tests/test_cell_explorer_boot.py exists to catch.
    scripts=(
        "cellExplorerColors.js",
        "cellExplorerApi.js",
        "cellExplorerState.js",
        "cellExplorerLegend.js",
        "cellExplorerContinuous.js",
        "cellExplorerRoiBridge.js",
        "cellExplorerSidebarController.js",
    ),
    styles=("cell_explorer.css",),
    # A table and a cell id are the floor: without them there is nothing to
    # colour and no way to say which cell a value belongs to.
    #
    # Segmentation and the coordinates are OPTIONAL, and that is the important
    # part. Either one is enough to draw cells -- a mask gives outlines and
    # filled shapes, x/y gives centroids -- so requiring the mask would rule out
    # every project that has coordinates and no segmentation, which is a large
    # share of them. `Requires` has no way to express "segmentation OR (x AND
    # y)", so all three are offered and the panel checks for itself that at
    # least one arrived (see `intro`, and the empty state in panel.html).
    #
    # `role:x`/`role:y` rather than `coordinates`: the coordinate question is a
    # CSV one. On AnnData and SpatialData x/y are answered at import, when the
    # adapter is told where to read them from, and asking again would put an
    # obsm picker in front of a user whose coordinates are already resolved.
    # Core translates between the two forms per format -- see plugin.py's
    # _coordinate_keys.
    requires=Requires(
        table=True,
        roles=("cell_id",),
        optional=("segmentation", "role:x", "role:y"),
    ),
    # Shown instead of core's generic subtitle, which says Plexora filled these
    # in from the data -- true of a form full of guesses to confirm, and wrong
    # for one that is asking for the thing without which this tool has nothing
    # to draw on.
    intro=("Colouring cells needs a way to draw them: a segmentation mask, or "
           "X/Y coordinates for centroids. Either one is enough."),
    # This is the plugin's whole purpose. It gets a LAYER of its own -- see
    # ImageViewer.registerCellLayer -- with its own colours, mode and opacity,
    # which survive another tool being opened over it. Opening Thresholding
    # switches this layer off; its card's eye turns it back on, and the two then
    # stack: gated cells over the phenotype map.
    owns_cell_layer=True,
)
