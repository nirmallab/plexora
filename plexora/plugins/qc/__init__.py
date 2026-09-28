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
- **The AI session (Paid, `ai:qc:session`)**: a deterministic pyramid scan
  finds where to look; an agent audits every channel at a glance and judges
  the candidates one typed packet at a time (`capabilities_session.py`).

Kept import-light, like every descriptor module.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20260928_qc1"


def _blueprint():
    from plexora.plugins.qc.server.routes import qc_bp

    return qc_bp


def _capabilities():
    from plexora.plugins.qc.capabilities import capabilities

    return capabilities()


def _mcp():
    from plexora.plugins.qc import mcp

    return mcp.contributions()


PLUGIN = Plugin(
    name="qc",
    label="Quality Control",
    version=VERSION,
    blueprint_factory=_blueprint,
    icon="clipboard-check",
    # Not Q: mod+q quits a Mac browser and ctrl+shift+q quits Chrome elsewhere.
    # X for "exclude", which is what a QC region mostly says.
    shortcut="mod+shift+x",
    panels={"tool_panel_slot": "qc/panel.html"},
    scripts=("qcApi.js", "qcSidebarController.js", "qcAgentBridge.js"),
    styles=("qc.css",),
    # An image is all image QC needs; a table (and a mask) add the cell half.
    # Offered, never demanded, so an image-only project can still be checked.
    requires=Requires(optional=("table", "segmentation", "role:cell_id", "role:x",
                                "role:y", "role:image_id")),
    intro=("None of these are needed to check an image. Adding the cell table (and "
           "the segmentation mask) lets QC flag cells as well as regions."),
    # QC never colours cells itself: the ROI plugin draws its regions.
    owns_cell_layer=False,
    capabilities_factory=_capabilities,
    # Resources under plexora://qc/ and the qc-image / review-qc prompts.
    mcp_factory=_mcp,
)
