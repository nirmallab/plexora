"""Figure Builder: publication figures assembled from reproducible viewer scenes.

A captured panel looks like a screenshot and behaves like a saved viewer state.
It records the source image's identity, the region in FULL-RESOLUTION image
pixels, the channels and their display windows, the overlays, and whatever the
open plugins contributed -- so the same field can be reopened, re-edited, and
re-rendered at publication resolution long after the screen it was captured on
is gone. The raster on the canvas is a preview; it is never the master.

Two things make this plugin unlike the others in this tree.

**A figure is not datasource state.** `plexora.api.store` is scoped to one
datasource by construction, and a figure legitimately spans several -- a
composite from one slide, an H&E from another, a schematic from no project at
all. So figures live in their own single-file SQLite databases under
`data_path/.figures/`, one per figure, holding references and scene state and
never a pixel of image data. See server/repository.py.

**It has a life outside a project.** The library, and a figure opened from it,
work with no datasource loaded: `GET /plugins/figure_builder/figure/<id>` is a
page of its own. The in-viewer half is the ordinary plugin tool, and the two
halves talk through the same REST surface.

Kept import-light like every descriptor module: discovery imports this whenever
the plugin is activated, and building the Blueprint (which pulls in sqlite, the
schema and the operation vocabulary) is left to the factory.
"""

from plexora.api.plugin import NavItem, Plugin, Requires

VERSION = "20260827_transform_panel"


def _blueprint():
    from plexora.plugins.figure_builder.server.routes import figure_builder_bp

    return figure_builder_bp


PLUGIN = Plugin(
    name="figure_builder",
    label="Figure Builder",
    version=VERSION,
    blueprint_factory=_blueprint,
    # NO slots at all -- the only plugin in the tree with nothing in core's
    # markup.
    #
    # Figure Builder's controls belong on the image: capturing is something you
    # do while looking at it, and the sidebar is three hundred pixels away, next
    # to the channel controls people use without thinking. Splitting them --
    # capture on the image, "which figure?" in the panel -- put the two halves
    # of one decision in two places, which was worse than either. So the
    # in-viewer half is the dock over the image (figureCaptureDock.js, built in
    # JavaScript because core has no slot over the image), and the sidebar has
    # no Figure card at all.
    #
    # The canvas used to be rendered into a second slot beside the viewer. It is
    # not any more: composing a figure is a different activity from looking down
    # a microscope, and half a window was not enough room for either. The canvas
    # has the whole page and its own URL, and the dock's "Figure Canvas"
    # navigates to it.
    #
    # Declaring no tool_panel_slot means no card, and no card means no X to
    # close the tool with -- so the dock carries one and it removes the plugin
    # outright.
    panels={},
    # Order is for reading, not for the browser: every cross-file reference is
    # inside a method or a constructor, and both the tool loader and
    # DOMContentLoaded run after all of them have arrived. What matters is that
    # all of them are here -- one omitted is a tool that loads and does nothing,
    # which is what tests/test_figure_builder_boot.py exists to catch.
    #
    # The same set serves three pages: the sidebar panel inside the viewer, the
    # figure library, and one figure's own page. Each controller boots only when
    # its own root element is on the page, so loading all of them everywhere
    # costs a few kilobytes and buys one asset list instead of three.
    scripts=(
        "figureBuilderApi.js",
        "figureSchema.js",
        "figureRichText.js",
        "figureShapeGeometry.js",
        "figureShapeDefs.js",
        "figureStrokeGeometry.js",
        "figureLineDefs.js",
        "figureSceneSnapshot.js",
        "figureCaptureTool.js",
        "figureCaptureBoxes.js",
        "figureCaptureDock.js",
        "figureDocumentState.js",
        "figureConfirm.js",
        "figureColorField.js",
        "figureChoiceField.js",
        "figureActions.js",
        "figureCanvas.js",
        "figureTextEditor.js",
        "figureTextPanel.js",
        "figureShapePanel.js",
        "figureLinePanel.js",
        "figureImagePanel.js",
        "figureShapeDrawing.js",
        "figurePointEditor.js",
        "figureContextBar.js",
        "figureContextMenu.js",
        "figureViewOptions.js",
        "figurePanelCompositor.js",
        "figureQuickEdit.js",
        "figureLibrary.js",
        "figureExportUi.js",
        "figureWorkspace.js",
        "figureSidebarController.js",
    ),
    styles=("figure_builder.css",),
    # Nothing required, and for the same reason ROI requires nothing: capturing
    # a field needs the image and nothing else. A project that is one OME-TIFF
    # can build a figure from it, which a table requirement would have silently
    # ruled out.
    #
    # Nothing optional either. The inputs this plugin can make use of -- a
    # segmentation mask, a cell table -- are already asked for by whichever
    # tool actually draws with them; Figure Builder only records what those
    # tools put on screen, so asking for them again here would put a form in
    # front of a user who wants to take a picture.
    requires=Requires(),
    # The library is not about any one datasource, so it cannot be reached
    # through the Tools menu -- that menu is built per-project and is empty
    # when nothing is open, which is exactly the state a user is in when they
    # want to reopen a figure. These two entries are the way in, and they are
    # data: core renders them with its own classes.
    icon="images",
    # Opens the TOOL -- the capture dock over the image -- not the library page.
    # Declaring no panel does not mean there is nothing to open: activating this
    # plugin is what puts the dock on screen, and that is what a user asking for
    # "Figure Builder" means. The library is a different destination and has its
    # own way in, below.
    #
    # mod+X is free in the sense that nothing in Plexora binds it: the canvas
    # implements c/v/d/a/g and no cut. Worth knowing that it is the one shortcut
    # here whose key means something else everywhere ELSE -- Cut. Typing is
    # unaffected (keyboardShortcuts.js stands down while a field or the text
    # editor has focus, so cutting a caption still cuts), but a user on the
    # canvas with a panel selected may press it expecting the partner of the
    # Copy and Paste that canvas does have.
    shortcut="mod+x",
    nav_items=(
        # No shortcut. The library is a place you go once at the start of a
        # session, not something reached mid-gesture, and the keystroke people
        # actually want for "Figure Builder" is the one that opens the capture
        # dock -- which is the plugin's own, above. mod+K is free if this ever
        # earns one.
        NavItem(menu="file", label="Open Figures…", path="/figures",
                icon="images"),
        # The Open Project tab strip is a row of tabs, not a menu: no gutter to
        # draw an icon in and no room to print a key, and the File entry above
        # already carries the shortcut for anyone who wants one.
        NavItem(menu="open_project", label="Figures", path="/figures"),
    ),
    # Figure Builder never colours cells. It captures whatever layer another
    # plugin registered; claiming the layer would evict the plugin whose
    # colours are the thing being captured.
    owns_cell_layer=False,
)
