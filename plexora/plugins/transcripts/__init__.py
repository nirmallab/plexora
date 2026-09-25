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

VERSION = "20260928_gene_taken"


def _blueprint():
    from plexora.plugins.transcripts.server.routes import (build_layer,
                                                           transcripts_bp,
                                                           TRANSCRIPT_STAGES)
    from plexora.server.models import layer_jobs

    # How core prepares a `transcripts` layer, said once, here -- the factory
    # is the one place that runs exactly when this plugin is activated. Core
    # holds no branch for the modality and imports nothing from here; it looks
    # the builder up by name and calls it, which is the same shape the plugin
    # registry itself uses and is what keeps the Xenium reader out of a core
    # build (tests/test_plugin_boundary.py).
    layer_jobs.register_builder("transcripts", build_layer,
                                stages=TRANSCRIPT_STAGES)
    return transcripts_bp


PLUGIN = Plugin(
    name="transcripts",
    label="Transcripts",
    version=VERSION,
    blueprint_factory=_blueprint,
    icon="dna",
    # A LAYER SECTION, not a tool. It mounts in the sidebar under Image
    # Channels for any sample that has transcripts in it, it is on from the
    # moment the page loads, and it is not opened from the Tools menu or
    # closed with an X.
    #
    # Nor switched here. The transcript layer takes an ordinary card in the
    # Layers list -- eye, opacity, drag to restack -- because something does
    # draw it and `ctx.layers.claim` is how core is told so. This section is
    # what a card cannot be: which genes, in what colour, drawn how.
    #
    # That is not a cosmetic reclassification. A tool competes for the panel
    # and toolLoader stands the previous one down when the next opens -- which
    # is right for two analyses of one slide, and wrong for something that is
    # part of what the viewer IS for this sample. Opening Gating should not
    # turn the transcripts off. See `Plugin.LAYER_SECTION_SLOT`.
    #
    # No shortcut, for the same reason: there is nothing to open.
    panels={Plugin.LAYER_SECTION_SLOT: "transcripts/panel.html"},
    scripts=("transcriptsApi.js", "transcriptPoints.js", "transcriptLayer.js",
             "transcriptsSidebarController.js"),
    styles=("transcripts.css",),
    # The one thing it cannot do without. A gene selector on a sample with no
    # transcripts is a section that can only say "nothing here", and an empty
    # section in the sidebar of every ordinary project is exactly the shallow
    # layer this design exists to avoid. Declared by MODALITY, so a still-
    # building layer still mounts and the section reports the build.
    requires=Requires(layers=("transcripts",)),
    intro=("Transcripts are drawn by the viewer itself. This section chooses "
           "which genes are shown, what colour and shape each one is, and "
           "whether they are drawn as molecules or as density."),
    # It colours POINTS of its own, not cells. Claiming the cell layer would
    # evict whichever plugin legitimately holds it -- the shader has one range
    # table -- in exchange for nothing.
    owns_cell_layer=False,
)
