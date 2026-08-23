"""Capture arithmetic, exercised rather than assumed.

Everything the probe checks is pure JavaScript that nothing else in this suite
runs: pytest renders HTML and stops, and `node --check` sees syntax only. Four
mistakes would therefore ship with a green suite, and all four produce a figure
that is wrong in a way nobody notices until export:

* storing SCREEN coordinates instead of full-resolution image pixels, which
  makes a panel un-re-renderable at any DPI other than the one it was captured
  at;
* squaring a Shift-drag on screen rather than in the image, which makes a row of
  "identical" panels quietly disagree about physical field of view;
* holding the VIEWFINDER in image pixels rather than screen pixels, which makes
  it travel with the image -- so the second capture of a set is of the first
  one's region instead of the same-sized field somewhere else;
* holding a CAPTURE BOX the other way round -- in screen pixels rather than
  image pixels -- which makes the record of where a capture came from slide
  around the slide as you pan, pointing at tissue nobody photographed;
* cropping the preview through a single devicePixelRatio rather than through
  each canvas's own backing scale, which puts a plugin's overlay in the wrong
  place on exactly the setups where an overlay matters.

The probe is a standalone script so it can also be run by hand while editing:

    node tests/js/figure_capture_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_capture_probe.mjs"
PLUGIN_ROOT = REPO_ROOT / "plexora" / "plugins" / "figure_builder"
TEMPLATES = PLUGIN_ROOT / "templates" / "figure_builder"
STATIC = PLUGIN_ROOT / "static"


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def test_the_capture_arithmetic_is_right(report):
    returncode, data = report
    assert not data["problems"], "\n".join(data["problems"])
    assert returncode == 0


def test_a_capture_is_stored_in_full_resolution_image_pixels(report):
    """The claim the whole format rests on. Screen pixels, viewport fractions
    and coordinates at whatever pyramid level was on screen are all
    indistinguishable from these once written down, and only these can be
    re-rendered at an arbitrary DPI years later."""
    _, data = report
    assert data["scene"]["viewport"] == {"x": 1000, "y": 750, "w": 2000, "h": 1500}


def test_channels_are_identified_by_a_key_the_user_cannot_rename(report):
    """A name is something people change on the way to making a figure. The key
    is the last segment of the tile URL, generated at import from the file and
    the channel's position, and it does not move."""
    _, data = report
    assert [c["key"] for c in data["scene"]["channels"]] == ["demo_0", "demo_1"]
    assert [c["fullname_at_capture"] for c in data["scene"]["channels"]] \
        == ["DNA_full", "CD8_full"]


def test_display_windows_are_stored_in_raw_units(report):
    """slot.range is byte-domain outside HD mode, so storing it directly would
    produce a window that means nothing the next time the viewer is in the other
    mode."""
    _, data = report
    assert [c["window"] for c in data["scene"]["channels"]] == [[0, 65535], [1028, 51400]]


def test_a_plugins_overlay_is_recorded_opaquely_with_its_legend(report):
    """Figure Builder never learns what a plugin's state means. It stores the
    name, the version, the blob and the legend the plugin computed at capture
    time -- which is what lets export draw that legend with no plugin
    JavaScript running at all."""
    _, data = report
    assert data["scene"]["plugins"]["roi"]["state"] == {"enabled": True}
    assert data["scene"]["plugins"]["roi"]["legend"] == [{"label": "Tumor"}]


def test_the_snapshot_records_nothing_about_the_user_interface(report):
    """A snapshot that stored which sidebar panel was open would make two
    identical captures compare unequal, and would restore a layout the user has
    since rearranged."""
    _, data = report
    assert sorted(data["scene"]) == [
        "captured_at", "channels", "core_overlays", "plugins",
        "snapshot_version", "source_id", "viewport",
    ]


def test_each_layer_is_cropped_through_its_own_backing_scale(report):
    """The tile drawer is at device resolution and an overlay is at CSS
    resolution. One shared devicePixelRatio gets one of the two wrong, and which
    one depends on the machine."""
    _, data = report
    drawer, overlay = data["drawCalls"][0], data["drawCalls"][1]
    assert drawer[:4] == [200, 100, 400, 300]
    assert overlay[:4] == [100, 50, 200, 150]
    # Both land on the same destination, which is what makes them composite.
    assert drawer[4:] == overlay[4:] == [0, 0, 400, 300]


# --------------------------------------------------------------------------
# The viewfinder. One frame, several shots -- which is what makes a row of
# panels comparable, and what a per-panel hand-drawn rectangle never could.
# --------------------------------------------------------------------------

def test_the_frame_stays_the_same_size_over_a_panned_image(report):
    """The claim the frame exists for. It is held in SCREEN pixels, so panning
    the image underneath changes WHICH part is captured and not HOW MUCH: four
    panels from four places, all the same field. Held in image pixels it would
    travel with the image, and the second capture would be of the first one's
    region."""
    _, data = report
    assert data["framed"]["rect"]["w"] == 2000
    assert data["framed"]["rect"]["h"] == 1500


def test_a_frame_hanging_off_the_image_is_pulled_in(report):
    """The preview is a crop of what the viewer drew and the scene is a region
    of the image. A frame overlapping the edge would otherwise give a preview
    with a band of background in it and a scene that says there is none -- two
    records of one panel that disagree."""
    _, data = report
    assert data["framed"]["overhang"] == {"x": 0, "y": 0, "w": 2000, "h": 1500}


def test_the_frame_cannot_be_dragged_out_of_reach(report):
    """Fully inside the viewer, never merely overlapping it: the shutter and the
    corner handles live on the frame, so a frame half off the edge is one the
    user can see and cannot use."""
    _, data = report
    assert data["boxes"]["stopped"] == {"x": 600, "y": 450, "width": 200, "height": 150}
    assert data["boxes"]["pushedIn"] == {"x": 500, "y": 400, "width": 300, "height": 200}


def test_capture_mode_opens_with_a_frame_already_there(report):
    """What makes the mode legible in its first second: the user sees the region
    that will be taken and adjusts it, instead of reading a hint about a gesture
    they have not made yet."""
    _, data = report
    assert data["boxes"]["preset"] == {"x": 262, "y": 197, "width": 276, "height": 207}


def test_the_shortcut_is_a_bare_letter_and_nothing_else(report):
    """A single key is only usable if it knows when not to fire. Cmd-C is copy;
    and without the typing guard, naming a figure "cell cores" toggles capture
    mode four times."""
    _, data = report
    assert {key: data["shortcut"][key]
            for key in ("plain", "upper", "chord", "other", "typing")} == {
        "plain": True, "upper": True, "chord": False, "other": False, "typing": False,
    }


# --------------------------------------------------------------------------
# Where the controls are. Structural, because the whole point of the change is
# a location.
# --------------------------------------------------------------------------

def test_there_is_no_sidebar_panel_at_all():
    """Capturing is something you do while looking at the image, and the sidebar
    is three hundred pixels away from it -- next to the channel controls, which
    people use without thinking, which is a bad neighbour for a button that arms
    a modal gesture. Splitting the tool between the two (capture on the image,
    "which figure?" in the panel) put the two halves of one decision in two
    places, so the panel is gone: no template, and no `tool_panel_slot` in the
    descriptor to render one into."""
    from plexora.plugins.figure_builder import PLUGIN

    assert not (TEMPLATES / "panel.html").exists()
    assert "tool_panel_slot" not in PLUGIN.panels
    assert set(PLUGIN.panels) == {"workspace_split_slot"}


def test_the_tool_can_be_closed_from_the_two_places_it_appears():
    """With no card in the sidebar there is no X there either, so the dock and
    the canvas each carry one -- and both remove the plugin rather than folding
    it away, because a folded tool with no card is a tool with no way back."""
    dock = (STATIC / "figureCaptureDock.js").read_text(encoding="utf-8")
    controller = (STATIC / "figureSidebarController.js").read_text(encoding="utf-8")
    canvas = (TEMPLATES / "workspace_body.html").read_text(encoding="utf-8")

    assert 'data-role="close"' in dock
    assert "fb_close_split" in canvas
    assert "fb_close_split" in controller
    assert "removeTool" in controller


def test_the_dock_the_frame_and_the_boxes_hang_beside_the_viewer_not_inside_it():
    """OpenSeadragon binds its mouse tracker to #openseadragon, so chrome
    mounted as a CHILD of it swallows or duplicates every drag that starts on
    top of it. As a sibling inside #openseadragon_wrapper -- where miniMap.js
    puts the overview lens -- clicks on the orb, drags on the frame and clicks
    on a capture's outline never reach the viewer, and nothing needs
    stopPropagation."""
    for name in ("figureCaptureDock.js", "figureCaptureTool.js",
                 "figureCaptureBoxes.js"):
        source = (STATIC / name).read_text(encoding="utf-8")
        assert 'getElementById("openseadragon_wrapper")' in source, name


def test_the_frame_is_not_a_canvas():
    """The preview is a crop of every canvas inside the viewer, in stacking
    order. The old marquee was one of them, which is why it had to be cleared
    synchronously before each grab -- one forgotten clear and the marquee is
    baked into the panel. A <div> cannot be."""
    source = (STATIC / "figureCaptureTool.js").read_text(encoding="utf-8")
    assert "CanvasOverlayHd" not in source
    assert 'createElement("div")' in source


def test_the_dock_ships_with_the_plugin():
    """A file that exists and is not declared is a tool that renders and does
    nothing."""
    from plexora.plugins.figure_builder import PLUGIN

    for name in ("figureCaptureDock.js", "figureCaptureBoxes.js"):
        assert name in PLUGIN.scripts, name
        assert (STATIC / name).is_file(), name


# --------------------------------------------------------------------------
# The capture boxes. The mirror image of the viewfinder, and the pair of them
# is where this gets subtle: one is anchored to the screen and one to the
# image, they sit in the same corner of the same window, and either one held
# the wrong way round looks completely fine until it matters.
# --------------------------------------------------------------------------

def test_a_box_that_is_nowhere_is_drawn_nowhere(report):
    """#openseadragon_wrapper does not clip -- it cannot, because a plugin may
    not style a core id and the wrapper is core's. A box the user has panned a
    mile away from would otherwise be a div a mile wide, laid over the sidebar
    and over whatever else is beside the viewer."""
    _, data = report
    assert data["marks"]["offLeft"] is None
    assert data["marks"]["offRight"] is None


def test_a_box_half_off_the_screen_keeps_its_shape(report):
    """Trimmed to the viewer's edge it would no longer be the region it marks:
    the outline would sit inside the capture rather than on it. It is left whole
    and the container's overflow does the cutting."""
    _, data = report
    assert data["marks"]["straddling"] == {
        "left": -100, "top": -50, "width": 400, "height": 300}


def test_a_capture_seen_from_far_out_is_still_a_mark(report):
    """Zoomed out to the whole slide, a captured field is a pixel or two. It
    stays visible rather than rounding away to nothing -- a session's captures
    are a map of where you have been, and it has to survive zooming out to
    read."""
    _, data = report
    assert data["marks"]["pinprick"] == {"left": 10, "top": 10, "width": 1, "height": 1}


def test_going_back_to_a_capture_puts_the_frame_exactly_on_it(report):
    """The claim behind "capture the same field again under different channels".
    The frame is normally screen-anchored, so aiming it at a region is the one
    place the two coordinate systems are joined on purpose -- and it has to be
    exact, or the second panel of a pair is a few pixels off the first and
    nothing on screen says so."""
    _, data = report
    assert data["aimed"]["landed"] is True
    assert data["aimed"]["back"] == {"x": 1000, "y": 750, "w": 2000, "h": 1500}


# --------------------------------------------------------------------------
# The pinned frame. Selecting a capture stops the shutter reading the screen
# and makes it read the capture, which is the difference between "the same
# field again" being true and being nearly true.
# --------------------------------------------------------------------------

def test_going_back_leaves_room_around_the_capture(report):
    """Half the window rather than all of it. Filling the viewer with the region
    hides everything that makes the region worth returning to -- what surrounds
    it, and its own outline, which is the only thing on screen saying the user
    has been here before."""
    _, data = report
    assert data["context"]["factor"] == 2
    assert data["context"]["framing"] == {"x": 0, "y": 0, "w": 4000, "h": 3000}


def test_the_frame_locks_onto_a_region_it_is_already_on(report):
    """And the lock is held in image pixels, like the capture it came from --
    not as a screen rectangle that would mean somewhere else after a pan."""
    _, data = report
    assert data["pinned"]["took"] is True
    assert data["pinned"]["region"] == {"x": 1000, "y": 750, "w": 2000, "h": 1500}


def test_a_locked_shutter_takes_the_stored_region(report):
    """Read back off the frame instead, the region round-trips through screen
    pixels and integer clamping, and the second capture of a field lands a pixel
    or two from the first. Nothing on screen shows a discrepancy that small; it
    turns up when the two panels are side by side in the figure."""
    _, data = report
    assert data["pinned"]["pinnedShot"] == data["pinned"]["shot"]


def test_a_frame_the_user_set_up_is_never_moved_to_lock_it(report):
    """pinTo locks what the frame is already on and aimAt is what puts it there,
    kept apart on purpose: a capture that has just been taken can then lock for
    free, while a frame that was hanging over the edge of the slide is not
    quietly resized to the smaller region that actually got saved -- which would
    cost the user the frame their next three panels were going to use."""
    _, data = report
    assert data["pinned"]["refused"] is False
    assert data["pinned"]["frameKept"] == {"x": 40, "y": 40, "width": 120, "height": 90}


def test_aiming_the_frame_somewhere_else_lets_go_of_the_capture(report):
    """The user drawing a new region is the one gesture that means "not that one
    any more", and it has to reach the strip and the boxes as well: all three are
    saying the shutter will take this capture, so they cannot disagree."""
    _, data = report
    assert data["pinned"]["stillPinned"] is None
    assert data["pinned"]["released"] == 1


# --------------------------------------------------------------------------
# The two one-key shortcuts. C opens capture mode, S takes the shot.
# --------------------------------------------------------------------------

def test_the_shutter_answers_to_a_bare_letter(report):
    """Not to Enter, which is what it used to be. Enter activates whatever
    button has focus, and by the time anybody presses it the focus is on the
    last thing they clicked -- the mode toggle, a thumbnail, "Figure Canvas".
    So one keystroke fired the shutter, or that button, or both, in an order
    that depended on where the user's hand had last been. A letter is only ever
    the document handler's, which is why the mode toggle is one too."""
    _, data = report
    assert data["shortcut"]["shoot"] is True
    assert data["shortcut"]["shootUpper"] is True
    assert data["shortcut"]["shootEnter"] is False


def test_the_shutter_stands_down_for_chords_and_for_typing(report):
    """Cmd-S is save. And a bare letter bound with no guard turns naming a
    figure "cross sections" into three photographs."""
    _, data = report
    assert data["shortcut"]["shootChord"] is False
    assert data["shortcut"]["shootTyping"] is False


def test_the_two_shortcuts_are_different_keys(report):
    """One key, one job. A letter that did both would leave capture mode and
    take a shot on its way out, and nothing on screen would say in which
    order."""
    _, data = report
    assert data["shortcut"]["distinct"] is True
    assert data["shortcut"]["crossed"] is False


# --------------------------------------------------------------------------
# One shot per arming, and a dock that stops above the legend.
# --------------------------------------------------------------------------

def test_taking_a_capture_ends_capture_mode(report):
    """Capture mode changes what a drag on the image does, so it lasts exactly
    as long as the thing it is for. Left on afterwards, the user went back to
    adjusting the picture -- which is the whole reason to take a second capture
    -- with the gesture for that still redrawing a viewfinder."""
    _, data = report
    assert data["spent"]["shot"] == {"x": 1000, "y": 750, "w": 2000, "h": 1500}
    assert data["spent"]["count"] == 1
    assert data["spent"]["armed"] is False


def test_the_shutter_does_nothing_once_the_mode_is_off(report):
    """A key held a moment too long must not produce a second identical panel."""
    _, data = report
    assert data["spent"]["after"] == 1


def test_arming_again_comes_back_to_the_same_region(report):
    """disarm() keeps the frame's geometry and its pin on purpose. Without that,
    every capture after the first would start from a default box in the middle
    of the viewer, and capture-adjust-capture would stop landing on one region
    -- which is the entire promise of the pin."""
    _, data = report
    assert data["spent"]["again"] == {
        "armed": True,
        "box": {"x": 200, "y": 150, "width": 400, "height": 300},
        "pinned": {"x": 1000, "y": 750, "w": 2000, "h": 1500},
    }


def test_the_dock_stops_above_the_channel_legend(report):
    """The strip grows with every capture and the legend sits in the same corner
    below it. The legend is `pointer-events: none`, so the captures underneath
    stayed clickable and the two simply overlapped illegibly. The room is
    measured rather than fixed: the legend's height is however many channels are
    on, which no stylesheet can know."""
    _, data = report
    assert data["room"]["clear"] == 876
    assert data["room"]["below"] == 710
    assert data["room"]["taller"] == 578
    # Real clearance, not a rounding coincidence: 710 + 10 gap + 12 top inset
    # puts the bottom of the dock 10px clear of a legend whose top is at 732.
    assert data["room"]["below"] + data["room"]["gap"] + data["room"]["margin"] == 732


def test_the_dock_is_never_squeezed_below_a_usable_height(report):
    """A short window under a tall legend has room for one of them, and it is
    not neither. A dock squeezed below the orb and the strip's header is one
    nobody can capture with -- worse than overlapping a legend in the one case
    where nothing else fits."""
    _, data = report
    assert data["room"]["squeezed"] == data["room"]["floor"]


# --------------------------------------------------------------------------
# Locking onto a region, and following it.
# --------------------------------------------------------------------------

def test_one_call_aims_the_frame_and_locks_it(report):
    """Going back to a capture wants both, atomically. Done as aimAt-then-pinTo
    the pin was judged against a second reading of where the region is, taken a
    moment after the aim -- and OpenSeadragon is still settling for a while
    after it says it has finished, so the two readings differed by a few pixels
    and the lock was refused on the capture the user had just clicked."""
    _, data = report
    assert data["following"]["locked"] is True
    assert data["following"]["box"] == {"x": 200, "y": 150, "width": 400, "height": 300}
    assert data["following"]["pinned"] == {"x": 1000, "y": 750, "w": 2000, "h": 1500}


def test_a_locked_frame_follows_its_region(report):
    """Three pixels of settling is not the user going somewhere else. The lock
    is on the region and the frame is only how that region is shown, so a frame
    left sitting elsewhere would be the tool lying about what the next shot
    takes."""
    _, data = report
    assert data["following"]["nudged"]["pinned"] == {
        "x": 1000, "y": 750, "w": 2000, "h": 1500}
    assert data["following"]["nudged"]["released"] == 0
    assert data["following"]["nudged"]["box"]["x"] == 203


def test_it_lets_go_when_the_region_can_no_longer_be_framed(report):
    """Off screen, larger than the viewer, or a speck -- clampBox moving the
    rectangle is the one test for all three. This is what keeps a following
    frame inside the viewer: #openseadragon_wrapper does not clip, so a frame
    that chased the tissue without this would end up drawn over the sidebar."""
    _, data = report
    assert data["following"]["gone"]["pinned"] is None
    assert data["following"]["gone"]["released"] == 1
    # Left where it last sat rather than dragged to the edge: unlocked, it is a
    # screen-anchored viewfinder again.
    assert data["following"]["gone"]["box"]["x"] == 203
