"""Transcripts: individual molecules, drawn where they were detected.

A spatial transcriptomics run puts tens to hundreds of millions of transcript
coordinates on a slide. The RENDERING of them is core's -- points and density are
spatial primitives, and the viewer owns every pixel of them (see
`server/models/transcript_tiles.py` and the layer stack). What is here is
everything that INTERPRETS the data: which genes exist, which ones the user
picked, what colour each is, and how to read a vendor's file.

That split is the whole reason this is a plugin at all. Two things follow from it
and both matter:

**The Xenium reader stays out of a core build.** Which vendor formats Plexora can
read is a question that grows -- Merscope, CosMx, whatever comes next -- and each
answer is a file in this plugin rather than a branch in core's importer.

(Not, as first written here, "pyarrow stays out of core". It does not: pandas
imports pyarrow and core imports pandas, so it is in every build already. The
boundary argument does not need it and the claim was wrong -- see
test_a_core_build_does_not_pay_for_the_transcript_reader, which asserts the part
that is true.)

**Zero core route changes.** The one route serving a layer's pixels
(`/generated/layer/...`) is core's, because it is the LAYER MODEL and not any one
modality -- it serves a second slide and an H&E just as happily. Everything
transcript-specific is under this blueprint's own prefix.

Deliberately NOT here: cell boundaries, spots and bins. Those are core rendering
primitives too, and a plugin that owned them would be a plugin every spatial
project had to install to see its own data.

Kept import-light, like every descriptor module: this is imported whenever the
plugin is activated, and building the Blueprint -- which pulls in the reader --
is left to the factory.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20260918_transcripts"


def _blueprint():
    from plexora.plugins.transcripts.server.routes import transcripts_bp

    return transcripts_bp


PLUGIN = Plugin(
    name="transcripts",
    label="Transcripts",
    version=VERSION,
    blueprint_factory=_blueprint,
    icon="dna",
    # G for Genes. mod+t is a new browser tab and mod+shift+t reopens a closed
    # one -- both refused by normalize_shortcut, which is right: a key the page
    # cannot intercept is a key that looks broken.
    shortcut="mod+g",
    panels={"tool_panel_slot": "transcripts/panel.html"},
    scripts=("transcriptLayer.js", "transcriptsSidebarController.js"),
    styles=("transcripts.css",),
    # Nothing required, for the same reason ROI requires nothing: a transcript
    # layer needs the image and its own points file, and neither a feature table
    # nor a segmentation is part of drawing a molecule where it was detected.
    # A project with no transcript layer simply has nothing for this to show,
    # which the panel says.
    requires=Requires(),
    intro=("Transcripts are drawn by the viewer itself. This panel chooses which "
           "genes are shown and what colour each one is."),
    # It colours POINTS of its own, not cells. Claiming the cell layer would
    # evict whichever plugin legitimately holds it -- the shader has one range
    # table -- in exchange for nothing.
    owns_cell_layer=False,
)
