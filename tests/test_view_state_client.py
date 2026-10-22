"""The client half of volumes: where the viewer is in a stack, and what the page loads.

`services/viewState.js` is run in node against fakes (tests/js/view_state_probe.mjs):
a planar image gets no view, a scrub coalesces, a following slab keeps its
thickness, the URL wins. The template checks pin that the three scripts are on
the viewer page and that the Depth card's panel exists where core's tool
machinery looks for it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "plexora" / "client"


def _run(probe_name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(REPO_ROOT / "tests" / "js" / probe_name)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_the_view_state_probe_passes_and_checks_something():
    out = _run("view_state_probe.mjs")
    assert "view state probe OK" in out
    checks = int(out.split("(")[1].split(" ")[0])
    assert checks >= 70


def test_the_plane_swap_probe_passes():
    """Every channel changes plane in one frame, behind one fade; a channel
    switched off mid-swap does not hold the others (tests/js/plane_swap_probe.mjs)."""
    out = _run("plane_swap_probe.mjs")
    assert "all checks passed" in out
    assert out.count("ok - ") >= 40


def test_the_viewer_motion_probe_passes():
    """Plane fades, the 3-D view's coast, section sizes, and a dragged rail
    read as travel that holds the scheduler to coarse standby planes, and its
    release ending the hold (tests/js/viewer_motion_probe.mjs)."""
    out = _run("viewer_motion_probe.mjs")
    assert "checks passed" in out
    assert out.count("ok - ") >= 111


def test_the_view_controls_probe_passes():
    """Which depth control is offered in which mode: the whole matrix, the
    registration rules, and resolving never touching the view state
    (tests/js/view_controls_probe.mjs)."""
    out = _run("view_controls_probe.mjs")
    assert "all " in out and "checks passed" in out


def test_the_depth_strip_probe_passes():
    """The strip follows the mode -- in 3-D the rail is the cut along Z and
    Play turns the stack -- and its cog popover follows it open; a drag
    released, a step key up or Play stopped ends the scrub
    (tests/js/depth_strip_probe.mjs)."""
    out = _run("depth_strip_probe.mjs")
    assert "all " in out and "checks passed" in out
    assert out.count("ok - ") >= 41


def test_the_inspect_geometry_probe_passes():
    """How the 3-D view is cut, in numbers: every mode over every axis on an
    anisotropic stack, the texture numbers relative to the area drawn, the
    repairs, and lengths in microns only where the image states them
    (tests/js/inspect_geometry_probe.mjs)."""
    out = _run("inspect_geometry_probe.mjs")
    assert "checks passed" in out
    assert out.count("ok - ") >= 69


def test_the_volume_dock_probe_passes():
    """The 3-D dock shows each spatial mode's rows and no other, previews a
    drag and commits it once, keeps a mode's settings, says lengths in the
    image's units, undoes (tests/js/volume_dock_probe.mjs)."""
    out = _run("volume_dock_probe.mjs")
    assert "all " in out and "checks passed" in out
    assert out.count("ok - ") >= 60


def test_the_volume_handles_probe_passes():
    """The cut's handles follow the hand along their axis, perspective and
    all, never turn the stack, preview and commit, and stay inside the area
    drawn; the area's own grips slide it (as Shift+drag) and resize it without
    moving the cut (tests/js/volume_handles_probe.mjs)."""
    out = _run("volume_handles_probe.mjs")
    assert "all " in out and "checks passed" in out
    assert out.count("ok - ") >= 35


def test_the_area_tool_probe_passes():
    """The Area tool's square -- a side on screen, so a zoom changes the
    tissue it takes (said under it), kept inside the image -- placed by a
    click or V in one change of the view state, V to 3-D and back (the square
    let go), yielding to the ROI tool, typing and dialogs
    (tests/js/area_tool_probe.mjs)."""
    out = _run("area_tool_probe.mjs")
    assert "all " in out and "checks passed" in out
    assert out.count("ok - ") >= 25


def test_the_view_history_probe_passes():
    """Back: an entry is spatial only, so a channel changed in the child view
    survives it; one change and one undo entry; the camera turns back only
    into 3-D, the 2-D view glides only into 2-D; the trail ends when 2-D comes
    back another way; B is Back's only while there is history, in the capture
    phase, so the dataset chip's B stands aside (tests/js/view_history_probe.mjs)."""
    out = _run("view_history_probe.mjs")
    assert "all " in out and "checks passed" in out
    assert out.count("ok - ") >= 27


def test_the_viewer_page_loads_the_cut_scripts_in_order():
    """The cut's geometry before the view state (which repairs `inspect` with
    it), its controller after; the dock and the handles after the renderer
    they draw over; all before main.js, which builds them."""
    page = (CLIENT / "templates" / "index.html").read_text(encoding="utf-8")
    order = [page.index(name) for name in (
        "js/services/inspectGeometry.js?v=", "js/services/viewState.js?v=",
        "js/services/inspectState.js?v=", "js/views/volumeRenderer.js?v=",
        "js/views/volumeDock.js?v=", "js/views/volumeHandles.js?v=", "js/views/areaTool.js?v=",
        "js/services/viewHistory.js?v=", "js/views/backButton.js?v=", "js/main.js?v=")]
    assert order == sorted(order)


def test_the_cut_scripts_parse():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for path in ("services/inspectGeometry.js", "services/inspectState.js", "views/volumeDock.js",
                 "views/volumeHandles.js", "views/areaTool.js",
                 "services/viewHistory.js", "views/backButton.js"):
        proc = subprocess.run([node, "--check", str(CLIENT / "src" / "js" / path)],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, path + proc.stderr


def test_the_canvas_layout_probe_passes():
    """Bands add up along their edge, docks keep order, and a plugin's element
    asking to be docked is docked (tests/js/canvas_layout_probe.mjs)."""
    out = _run("canvas_layout_probe.mjs")
    assert "checks passed" in out
    assert out.count("ok - ") >= 10


def test_a_moved_control_is_placed_by_fractions_of_its_room():
    """The CSS half of a movable control (canvasLayout.js Movable): `left`
    at the fraction of the free layer and `translate` back by the same
    fraction of the control's own size, so it can never cross an edge however
    the canvas is resized; the free layer is the safe area's content box."""
    css = (CLIENT / "src" / "css" / "viewer.css").read_text(encoding="utf-8")
    rule = css[css.index("#openseadragon_wrapper .plx-canvas-free > .plx-floating {"):]
    rule = rule[:rule.index("}")]
    assert "left: calc(var(--plx-x, 0) * 100%);" in rule
    assert "top: calc(var(--plx-y, 0) * 100%);" in rule
    assert "translate: calc(var(--plx-x, 0) * -100%) calc(var(--plx-y, 0) * -100%);" in rule
    free = css[css.index(".plx-canvas-free {"):]
    free = free[:free.index("}")]
    assert "pointer-events: none;" in free
    # The x is hidden with `hidden` -- a display rule alone would beat it.
    assert ".plx-movable-reset[hidden] {" in css


def test_the_canvas_layout_loads_before_anything_it_places():
    """base.html loads the layout before imageViewer.js, which docks the
    caption, the legend and the mini-map as it builds them."""
    base = (CLIENT / "templates" / "base.html").read_text(encoding="utf-8")
    assert base.index("js/services/canvasLayout.js?v=") < base.index("js/views/imageViewer.js?v=")
    assert base.index("js/services/canvasLayout.js?v=") < base.index("js/views/miniMap.js?v=")


def test_no_core_rule_still_nudges_docked_controls_by_hand():
    """The legend and mini-map used to step clear of the strip and the panes by
    `.has-*` rules; the layout places them now, and a leftover rule would
    out-rank it (an id and two classes beat the dock's id and class)."""
    css = (CLIENT / "src" / "css" / "viewer.css").read_text(encoding="utf-8")
    for stale in (".has-depth-strip .viewer-channel-legend", ".has-sections .viewer-channel-legend",
                  ".has-sections .viewer-mini-map"):
        assert stale not in css, stale


def test_the_viewer_page_loads_the_depth_scripts_before_main():
    page = (CLIENT / "templates" / "index.html").read_text(encoding="utf-8")
    order = [page.index(name) for name in (
        "js/services/viewState.js?v=", "js/services/perfProbe.js?v=",
        "js/services/volumeWindows.js?v=",
        "js/services/viewerPrefs.js?v=", "js/services/viewControls.js?v=",
        "js/views/depthStrip.js?v=", "js/views/depthSettings.js?v=", "js/views/depthTools.js?v=",
        "js/views/volumeCamera.js?v=", "js/views/orientationGizmo.js?v=",
        "js/views/volumeRenderer.js?v=",
        "js/views/sectionPanes.js?v=", "js/views/planeMotion.js?v=", "js/main.js?v=")]
    assert order == sorted(order)


def test_the_depth_panel_has_every_control_the_controller_binds():
    panel = (CLIENT / "templates" / "tools" / "depth_panel.html").read_text(encoding="utf-8")
    controller = (CLIENT / "src" / "js" / "views" / "depthTools.js").read_text(encoding="utf-8")
    for element_id in ("depth_panel_section", "depth_plane_input", "depth_frame_input",
                       "depth_spacing_input", "depth_spacing_save", "depth_spacing_button",
                       "depth_sections_reset"):
        assert f'id="{element_id}"' in panel, element_id
        assert element_id in controller, element_id
    # The projection and Motion live under the strip's gear
    # (views/depthSettings.js), and only there: one home for each setting.
    for gone in ("depth_op_select", "depth_z0_input", "depth_follow_input", "depth_motion_input",
                 "data-depth-mode"):
        assert gone not in panel and gone not in controller, gone


def test_the_depth_ui_uses_icons_font_awesome_ships():
    """A dead icon name draws nothing and says nothing (FA 7 dropped the FA 5
    aliases). The strip names its glyphs through `icon("...")`, which the
    tree-wide scan in test_icon_names.py does not see, so they are checked
    here against the same shipped set."""
    import re

    from tests import test_icon_names

    if not test_icon_names.FONTAWESOME.is_dir():
        pytest.skip("font awesome is not installed")
    shipped = test_icon_names._shipped()
    strip = (CLIENT / "src" / "js" / "views" / "depthStrip.js").read_text(encoding="utf-8")
    settings = (CLIENT / "src" / "js" / "views" / "depthSettings.js").read_text(encoding="utf-8")
    used = set(re.findall(r'icon\("([a-z-]+)"\)', strip))
    # The popover's glyph buttons name theirs as `glyph: "..."` or in a
    # choices table: [value, text, glyph, title].
    used |= set(re.findall(r'glyph: "([a-z-]+)"', settings))
    used |= set(re.findall(r'\[(?:-?1|"[a-z]+"), "[^"]*", "([a-z-]+)", "', settings))
    # The mode buttons name theirs in a table: [mode, glyph, title].
    used |= set(re.findall(r'\["(?:plane|slab|sections|volume)", "([a-z-]+)"', strip))
    assert {"square", "layer-group", "play", "pause", "gear", "rotate-left", "arrows-up-down",
            "ruler-combined", "cubes", "file-arrow-up"} <= used
    assert used <= shipped, used - shipped
