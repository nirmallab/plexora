"""Video Studio: scientific videos made from the viewer, frame-exact and reproducible.

A rotating z-stack, a sweep through its planes, a time-lapse, a zoom into a
region, channels revealed one by one -- each in a couple of clicks from the
Quick Capture bar, then refinable as camera paths and a timeline. Every frame
is rendered by the live viewer itself, driven by a fixed clock and captured
only once everything on screen has finished loading, so an export never shows
a half-loaded tile, a provisional contrast, or a plane fading into the next.

Three choices shape the plugin:

**No second renderer.** Frames come from the viewer the user is looking at,
placed in a "stage" sized to the output and composited with the scientific
overlays at output resolution (static/videoRenderCoordinator.js). Overlays,
masks, cell layers and the 3-D renderer are bound to the live viewer; a copy of
any of them would be a second implementation to keep in step.

**A video references its image.** One SQLite file per video under
`data_path/.videos/`, Figure Builder's pattern (server/repository.py). The
document is the choreography -- the starting scene and the tracks that change
it -- never a pixel.

**Acquired data is never interpolated.** Timepoints and planes change only in
whole steps; what may change smoothly is how they are shown. The schema
refuses anything else, and every export carries a manifest that says what each
frame shows (server/schema.py, server/manifest.py).

Card-less, like Figure Builder: its controls dock over the image, and nothing is
added to the sidebar. Paid: the whole plugin needs the `plugin:video_studio`
entitlement, so the Tools menu badges it and its routes answer 403 on a licence
without it.

Kept import-light, like every descriptor module.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20261012_merge_main"


def _blueprint():
    from plexora.plugins.video_studio.server.routes import video_studio_bp

    return video_studio_bp


def _capabilities():
    from plexora.plugins.video_studio.capabilities import capabilities

    return capabilities()


PLUGIN = Plugin(
    name="video_studio",
    label="Video Studio",
    version=VERSION,
    blueprint_factory=_blueprint,
    # No slots: the Quick Capture bar is docked over the image in JavaScript
    # (static/videoStudioBar.js), and carries its own close button.
    panels={},
    # Load order is for reading; nothing runs until the tool loader has every
    # file. Pure modules first, then the viewer adapters, the render path, the
    # UI, and the controller that wires them.
    scripts=(
        "videoEasing.js",
        "videoInterp.js",
        "videoArcLength.js",
        "videoSchema.js",
        "videoCamera3d.js",
        "videoCamera2d.js",
        "videoAnimation.js",
        "videoHistory.js",
        "videoProjectApi.js",
        "videoViewerState.js",
        "videoSceneState.js",
        "videoTracking.js",
        "videoStage.js",
        "videoReadiness.js",
        "videoOverlays.js",
        "videoComposite.js",
        "videoPrefetch.js",
        "videoRenderCoordinator.js",
        "videoEncoders.js",
        "videoExportLoop.js",
        "videoFigureBridge.js",
        "videoAgentBridge.js",
        "videoRecorder.js",
        "videoParamFields.js",
        "videoPreview.js",
        "videoRenderCover.js",
        "videoExportUi.js",
        "videoPathEditor.js",
        "videoTimeline.js",
        "videoStudioBar.js",
        "videoStudioSidebarController.js",
    ),
    styles=("video_studio.css",),
    # A flat image can zoom, pan and reveal channels; a stack can do the rest.
    # Every representation the viewer has, so the bar is there whichever way
    # the stack is being shown.
    # RGB images too (`excluded_image_kinds=()`): an H&E has no channel slots
    # to reveal, but a zoom or a pan across it is as useful as across any.
    requires=Requires(dims=("2d", "3d"), representations=("plane", "slab", "section"),
                      excluded_image_kinds=()),
    icon="video",
    # mod+J: free everywhere Plexora runs (see figure_builder for the map of
    # what is taken). J is also the transport key in most video editors.
    shortcut="mod+j",
    capabilities_factory=_capabilities,
    owns_cell_layer=False,
    launcher=True,
    entitlement="plugin:video_studio",
)
