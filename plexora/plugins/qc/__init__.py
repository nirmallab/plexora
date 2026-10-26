"""Quality control: artifact regions on the image, pass/fail on the cells.

QC is an annotation layer, never a removal. An artifact -- a fold, a blurred
field, an antibody aggregate, a failed channel -- becomes an ROI in the ROI
plugin (the canonical geometry, editable by the user), with its class, scope,
severity and action kept beside it in this plugin's store, keyed by ROI id.
When the project has a cell table, every cell gets a pass/fail call with its
reasons; nothing is deleted from anything.

Two halves with one store:

- **Manual and read paths (Free)**: drawing QC regions in the ROI panel,
  reading results, changing strictness, exports, source writes, reports.
- **The AI session (Pro, `ai:qc:session`)**: a deterministic pyramid scan
  finds where to look; an agent audits every channel at a glance and judges
  the candidates one typed packet at a time (`capabilities_session.py`).

Kept import-light, like every descriptor module.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20261025_qc_cells"


def _blueprint():
    from plexora.plugins.qc.server.routes import qc_bp

    return qc_bp


def _capabilities():
    from plexora.plugins.qc.capabilities import capabilities

    return capabilities()


def _mcp():
    from plexora.plugins.qc import mcp

    return mcp.contributions()


def _cell_exclusions():
    from plexora.plugins.qc.server.exclusions import factory

    return factory()


def _cell_annotations():
    from plexora.plugins.qc.server.exclusions import annotations_factory

    return annotations_factory()


PLUGIN = Plugin(
    name="qc",
    label="Quality Control",
    version=VERSION,
    blueprint_factory=_blueprint,
    # Stack QC is Plexora 3D (capabilities_stack.py); reading the last stack
    # check (GET /stack) and every 2-D check stay Free.
    endpoint_entitlements={"stack_run": "volume:analysis",
                           "stack_write": "volume:analysis",
                           "stack_plane": "volume:analysis"},
    icon="clipboard-check",
    # First in the Tools menu: QC, Thresholding, ROI, Cell Explorer, Figure Builder.
    order=10,
    # Not Q: mod+q quits a Mac browser and ctrl+shift+q quits Chrome elsewhere.
    # X for "exclude", which is what a QC region mostly says.
    shortcut="mod+shift+x",
    panels={"tool_panel_slot": "qc/panel.html"},
    # qcTree (the panel's tree), qcLayers (what is drawn on the tissue),
    # qcDraw (a region drawn by hand) and qcHover (the card under the pointer)
    # before the controller that builds them.
    # qcRegistration / qcBlur / qcArtifacts / qcSegmentation: the free image
    # checks' rows.
    scripts=("qcApi.js", "qcTree.js", "qcLayers.js", "qcDraw.js", "qcHover.js",
             "qcRegistration.js", "qcBlur.js", "qcArtifacts.js", "qcSegmentation.js",
             "qcStack.js",
             "qcSidebarController.js", "qcAgentBridge.js"),
    styles=("qc.css",),
    # An image is all image QC needs; a table (and a mask) add the cell half.
    # Offered, never demanded, so an image-only project can still be checked.
    requires=Requires(optional=("table", "segmentation", "role:cell_id", "role:x",
                                "role:y", "role:image_id")),
    intro=("None of these are needed to check an image. Adding the cell table (and "
           "the segmentation mask) lets QC flag cells as well as regions."),
    # QC colours the cells it flagged, by reason, in a cell layer of its own
    # (qcLayers.js). Its regions are ROIs, drawn by QC's own overlay while the
    # ROI tool is not on screen.
    owns_cell_layer=True,
    capabilities_factory=_capabilities,
    # Resources under plexora://qc/ and the qc-image / review-qc prompts.
    mcp_factory=_mcp,
    # The cells automatic gating (and anything else that estimates from the
    # table) must leave out: plexora/agent/cell_exclusions.py.
    cell_exclusions_factory=_cell_exclusions,
    # QC's status and category per cell, for Cell Explorer and explain_cell
    # to read without a source write: plexora/agent/cell_exclusions.py.
    cell_annotations_factory=_cell_annotations,
)
