"""The tool core ships itself: Rotate & Flip.

An ordinary `Plugin` descriptor with no blueprint, no assets and no package,
offered by the registry beside whatever plugins are installed (see
plugins.tools). Being a descriptor is the whole point: toolLoader.js, the
panel route, `?tool=`, the sidebar card, its help and the one-tool-at-a-time
rule all work on it unchanged, and a core-only build (`PLEXORA_PLUGINS=""`)
still has it, because it is not a plugin at all.

ONE tool for both, because they are one question -- which way round is the
image shown -- and the state they edit is one state. Two tools meant two menu
rows and two cards that folded each other away, so turning an image and then
mirroring it was an open, a close and another open.

Its JavaScript and CSS are core's, always on the viewer page
(views/viewTransformTools.js, services/viewTransform.js, viewer.css), so
`scripts` and `styles` stay empty -- the panel route then hands the loader
nothing to fetch. `menu="view"` puts its row in the View menu rather than
Tools: it changes how the image is shown, not what is measured on it.

The name stays `rotate`, which is what `?tool=` links and a sample's
remembered arrangement already say.

The orientation it edits is a property of the image and is stored with it,
in the per-datasource database (`/view_transform/<datasource>`), not with the
card: closing the card leaves the view as it is.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20260926_one_orientation_card"

#: The flat quick-view image has its own viewer with no view transform, and a
#: blank frame has no picture to turn.
_REQUIRES = Requires(excluded_image_kinds=("rgb", "blank"))

ROTATE = Plugin(
    name="rotate",
    label="Rotate & Flip",
    version=VERSION,
    icon="rotate",
    menu="view",
    # No shortcut: mod+R reloads the page.
    panels={"tool_panel_slot": "tools/rotate_panel.html"},
    requires=_REQUIRES,
)

CORE_TOOLS = (ROTATE,)
