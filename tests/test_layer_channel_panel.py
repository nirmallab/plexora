"""A registered layer's channel controls are the reference image's controls.

A layer added with **+ Add Layer** used to get one `<select>`, one colour
swatch and two number boxes, and every change refetched the viewport. The
reference image, two cards below it in the same panel, had channel slots, a
colour each, a logarithmic contrast slider each, Auto and Add Channel -- all of
them free repaints. Two widgets for one job is two things to learn and two
things to fix, so the layer now mounts a second instance of the SAME widget.

What is written for that is three translations, and each fails silently:

  saved rows     `render.channels` holds raw 16-bit windows, the only domain
                 that survives a reload and an HD toggle. Read back in the
                 wrong one, a restored channel is drawn with its window
                 rescaled into a sliver of its real range.
  stored rows    written whole, because the PATCH merges `render` one key deep
                 -- a partial list IS the new list.
  drawn rows     in the fractional units the shader reads, which are not the
                 units anything is stored in.

They are checked in `tests/js/layer_channel_panel_probe.mjs`, run against the
shipped module. This drives it and names every check, so one quietly deleted
fails here rather than passing silently.

The instance's SCOPING is tests/test_sidebar_scoping.py; what it draws is
tests/test_layer_visibility.py; the routes behind it are
tests/test_layer_sources.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "layer_channel_panel_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"

CHECKS = [
    "the id prefix is derived from the layer id",
    "...and reduced to what an element id may hold",
    "...stably, so a rebuilt card finds the same markup",
    "every saved channel comes back as an active row",
    "in the order it was saved in",
    "the window comes back in raw 16-bit units, untouched",
    "the colour comes back as the three bytes a row carries",
    "a three-digit hex is expanded, not dropped",
    "a saved row naming only an index still finds its channel",
    "a saved row for a channel the layer no longer has is dropped",
    "a layer saved with one channel and one colour opens as it did",
    "a layer that was never styled restores nothing and auto-levels",
    "only an enabled, named slot is stored",
    "stored with the index the server knows it by",
    "the window is stored in raw units and rounded",
    "the colour is stored as the hex the picker gave",
    "only an enabled, named slot is drawn",
    "drawn in the fractional units the shader reads",
    "with the colour as the 0-255 triple toFloatColor takes",
    "and a copy of it, not the slot's own object",
    "what is stored and what is drawn are different domains",
    "the ceiling is the sidebar's own",
]


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=120)


def test_the_panel_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)


# -- the wiring ---------------------------------------------------------------


def test_the_panel_is_a_second_viewer_sidebar_and_not_a_second_widget():
    """The whole argument of the change. A channel widget written here would
    be a second implementation of colour, contrast and channel ordering that
    agrees with the first until the day one of them is fixed."""
    panel = source("src", "js", "views", "layerChannelPanel.js")
    assert "new ViewerSidebar(" in panel
    assert "idPrefix: prefix" in panel
    # Everything a slot IS stays the class's.
    for invented in ("createChannelSlot", "class ChannelSlot", "new PlexoraSlider"):
        assert invented not in panel, invented


def test_it_writes_the_layer_record_and_never_the_projects_channel_list():
    """A scoped instance must not overwrite the project's saved channels with
    a layer's -- which is what `persist: false` guards. The layer's own writes
    go through an override of the one method that guard sits on, so they get
    the debounce, the restore suppression and the first-run write for free."""
    panel = source("src", "js", "views", "layerChannelPanel.js")
    assert "persist: false" in panel
    assert "sidebar.persistChannelList = () =>" in panel
    assert "saveChannelList: () => Promise.resolve(null)" in panel


def test_the_sidebar_still_guards_the_projects_list_on_that_method():
    """The override above is only correct while `persistChannelList` is where
    the guard is and `scheduleSaveChannels` is not -- otherwise a layer's edits
    would be dropped instead of saved."""
    sidebar = source("src", "js", "views", "viewerSidebar.js")
    persist = sidebar.split("    persistChannelList() {", 1)[1].split("\n    }", 1)[0]
    assert "if (!this.persist) return" in persist
    schedule = sidebar.split("    scheduleSaveChannels() {", 1)[1].split("\n    }", 1)[0]
    # It calls `persistChannelList`, so the substring is there; what must not
    # be is a guard of its own on the flag.
    assert "if (!this.persist)" not in schedule
    assert "this.persist " not in schedule and "this.persist)" not in schedule


def test_the_panel_reaches_the_viewer_through_a_callback_not_a_held_handle():
    """A card can be built before `window.__plexora.seaDragonViewer` is
    assigned -- a stack change during the viewer's own construction is enough.
    A handle taken at mount would be undefined for the life of the panel, and
    every control on it would move nothing, silently."""
    panel = source("src", "js", "views", "layerChannelPanel.js")
    assert "viewerManager" not in panel
    assert "draw?.(slotsToDrawn(sidebar))" in panel
    manager = source("src", "js", "views", "layerManager.js")
    assert "draw: (list) => window.__plexora?.seaDragonViewer" in manager


def test_the_panel_is_mounted_once_and_unmounted_with_its_card():
    """It owns a ViewerSidebar -- its slots, its sliders, the restore it has
    already done -- and `refreshBody` wipes a raster card's body on every
    change to the layer. And a panel outliving its card keeps a window
    listener for the HD toggle."""
    manager = source("src", "js", "views", "layerManager.js")
    assert "const panels = new Map();" in manager
    assert "panels.get(layer.id)" in manager
    assert manager.count("dropChannelPanel(") >= 3


def test_every_channelled_image_layer_gets_one():
    """Deliberately broad: one channel or forty, a layer is not a lesser kind
    of image. The only image layer without a panel is an rgb one, whose bytes
    are already the picture."""
    manager = source("src", "js", "views", "layerManager.js")
    body = manager.split("function hasChannelPanel(layer) {", 1)[1].split("\n    }", 1)[0]
    assert "render || {}).rgb" in body
    assert "channels || []).some" in body
    assert "channels.length > 1" not in body


def test_the_panel_is_loaded_by_the_page():
    base = (CLIENT / "templates" / "base.html").read_text(encoding="utf-8")
    assert "views/layerChannelPanel.js?v=" in base
    assert base.index("views/viewerSidebar.js") < base.index("views/layerChannelPanel.js")
    assert base.index("views/layerChannelPanel.js") < base.index("views/layerManager.js")
