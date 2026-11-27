"""SCIMAP: a notebook of single-cell and spatial analyses, run by Scimap Pro.

A full page of its own, like Figure Builder's figure page: a sidebar of tools,
a column of steps ("cells"), and each step's result drawn under it. A step
runs one Scimap Pro function on the project's (or the dataset's) cell table
and writes its result into a working AnnData file in the results folder the
user picked. Every step can be looked at in the image viewer: its labels
colour the cells there, and the viewer offers the way back.

Three things shape this plugin.

**Scimap Pro does the analysis; this plugin never re-implements it.** Every
number on the page comes out of a `scimappro` call whose arguments are stored
with the step, so a notebook exports as a script that reruns without Plexora.

**Scimap Pro runs one analysis at a time.** Its kernels are numba
`parallel=True`, and numba's thread pool is not re-entrant: two runs in one
process corrupt each other. So every run, from a button or from an agent, goes
through one queue (server/runner.py) and the page says what it is waiting for.

**Importing `scimappro` is expensive and optional.** This module and the page
routes never import it; only a run, the data preparation and the licence
activation do, inside a function. Whether it is installed is answered with the
import system's finder, and the licence with `biocognia` reading the same file
Scimap Pro reads -- so the page opens in milliseconds, and opens to an honest
"not installed" state where it is missing.

Desktop only: on the website the page renders the desktop-app notice and runs
nothing (`browser/routes.py` FEATURES["scimap.workspace"]).
"""

from plexora.api.plugin import NavItem, Plugin, Requires

VERSION = "20261127_r22"


def _blueprint():
    from plexora.plugins.scimap.server.routes import scimap_bp

    return scimap_bp


def _capabilities():
    from plexora.plugins.scimap.server.capabilities import capabilities

    return capabilities()


def _annotations():
    from plexora.plugins.scimap.server.overlay import provider

    return provider


PLUGIN = Plugin(
    name="scimap",
    label="Analyze",
    version=VERSION,
    blueprint_factory=_blueprint,
    # No slots: the notebook is a page, not a sidebar card. Analysis is a
    # different activity from looking at one image -- it spans projects, and it
    # needs the width -- so it has its own URL, the way Figure Builder's canvas
    # does.
    panels={},
    # The whole client, in load order for reading only: every cross-file
    # reference is inside a function that runs after all of them have arrived.
    scripts=(
        "scimapIcons.js",
        "scimapApi.js",
        "scimapPlots.js",
        "scimapFigures.js",
        "scimapAdvanced.js",
        "scimapHistory.js",
        "scimapPrepare.js",
        "scimapPicker.js",
        "scimapScatterGL.js",
        "scimapEmbed.js",
        "scimapUi.js",
        "scimapViews.js",
        "scimapInspector.js",
        "scimapCells.js",
        "scimapWorkspace.js",
    ),
    styles=("scimap.css", "scimapEmbed.css"),
    # On every viewer page: re-applies the overlay a step put on the image
    # (it persists across the projects of a dataset) and offers the way back
    # to the notebook. Asks the server nothing until a step has been viewed.
    # And the floating embedding (scimapEmbedWindow.js): a notebook's UMAP in
    # a window over the tissue, opened from Cell Explorer or a UMAP step.
    viewer_scripts=("scimapViewerReturn.js", "scimapPicker.js", "scimapScatterGL.js", "scimapEmbed.js",
                    "scimapEmbedWindow.js"),
    requires=Requires(),
    icon="chart-simple",
    order=60,
    # The Tools row opens the notebook page for the open project, not a panel.
    page="/notebook",
    # A for Analyze. mod+a is select-all and alt changes the key on a Mac.
    shortcut="mod+shift+a",
    nav_items=(
        NavItem(menu="file", label="Analyze…", path="/notebook", icon="chart-simple"),
        NavItem(menu="open_project", label="Analyze", path="/notebook"),
    ),
    capabilities_factory=_capabilities,
    # The step the user is viewing in the image viewer, as a categorical
    # annotation of the loaded project's cells (server/overlay.py). Cell
    # Explorer colours by it; it never writes the user's table.
    cell_annotations_factory=_annotations,
    owns_cell_layer=False,
)
