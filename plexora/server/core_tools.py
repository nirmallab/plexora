"""Tools core ships itself: Rotate and Flip.

They are ordinary `Plugin` descriptors with no blueprint, no assets and no
package, offered by the registry beside whatever plugins are installed (see
plugins.tools). Being descriptors is the whole point: toolLoader.js, the panel
route, `?tool=`, the sidebar card, its help and the one-tool-at-a-time rule
all work on them unchanged, and a core-only build (`PLEXORA_PLUGINS=""`) still
has them, because they are not plugins at all.

Their JavaScript and CSS are core's, always on the viewer page
(views/viewTransformTools.js, services/viewTransform.js, viewer.css), so
`scripts` and `styles` stay empty -- the panel route then hands the loader
nothing to fetch. `menu="view"` puts their rows in the View menu rather than
Tools: they change how the image is shown, not what is measured on it.

The orientation they edit is a property of the image and is stored with it,
in the per-datasource database (`/view_transform/<datasource>`), not with the
card: closing the card leaves the view as it is.
"""

from plexora.api.plugin import Plugin, Requires

VERSION = "20260925_view_transform"

#: The flat quick-view image has its own viewer with no view transform, and a
#: blank frame has no picture to turn.
_REQUIRES = Requires(excluded_image_kinds=("rgb", "blank"))

ROTATE = Plugin(
    name="rotate",
    label="Rotate",
    version=VERSION,
    icon="rotate",
    menu="view",
    # No shortcut: mod+R reloads the page.
    panels={"tool_panel_slot": "tools/rotate_panel.html"},
    requires=_REQUIRES,
)

FLIP = Plugin(
    name="flip",
    label="Flip",
    version=VERSION,
    icon="left-right",
    menu="view",
    panels={"tool_panel_slot": "tools/flip_panel.html"},
    requires=_REQUIRES,
)

CORE_TOOLS = (ROTATE, FLIP)
