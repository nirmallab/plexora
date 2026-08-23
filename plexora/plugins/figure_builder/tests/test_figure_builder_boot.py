"""The plugin's client comes up when loaded the way the server loads it.

Nothing else here runs this plugin's JavaScript. The Python suite renders HTML
and stops; `node --check` sees syntax only. So the entire client can be broken --
a file missing from `PLUGIN.scripts`, a constructor that throws the moment it
runs, a registration that never happens -- while every server-side test passes,
and the only symptom is a panel that appears and does nothing.

The file list is read off the descriptor rather than restated here, so what gets
exercised is what the server will actually send.

This plugin's scripts drive three different pages, which is the wrinkle none of
the others have: the same six files load in the viewer's sidebar, on the figure
library and on one figure's own page, and each controller has to stand down
politely on the two pages that are not its own. The probe runs them against a
DOM where no root element exists at all, which is that case at its most extreme.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.figure_builder import PLUGIN

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_builder_boot_probe.mjs"


def _run(scripts):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE), *scripts],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


@pytest.fixture(scope="module")
def report():
    return _run(list(PLUGIN.scripts))


def test_the_declared_order_loads(report):
    returncode, data = report
    assert not data["problems"], "\n".join(data["problems"])
    assert returncode == 0
    assert data["loaded"] == list(PLUGIN.scripts)


def test_the_plugin_registers_itself(report):
    """Core never names a plugin -- it activates whatever registered. A client
    that loads without registering is a tool whose panel appears and does
    nothing."""
    _, data = report
    assert len(data["registered"]) == 1
    assert data["registered"][0]["name"] == "figure_builder"
    assert data["registered"][0]["ownsCellLayer"] is False


def test_a_controller_can_be_built_from_a_plugin_context(report):
    """Loading without throwing is not the same as working: a class can define
    fine and still name something that does not exist when it is used."""
    _, data = report
    assert data["controller"] == {"datasource": "demo", "figureId": None, "hasApi": True}


def test_a_page_controller_stands_down_on_a_page_that_is_not_its_own(report):
    """Every file loads on all three pages. A controller that assumed its DOM
    would throw on two of them -- and since these two self-boot on
    DOMContentLoaded, that throw would land before anything else could report
    it."""
    returncode, data = report
    assert returncode == 0
    assert not [p for p in data["problems"] if "page controller" in p]


def test_the_client_issues_no_requests_merely_by_loading(report):
    """Loading the library page's script must not fetch the library. The viewer
    loads all six files whenever this tool is opened, and a request fired at load
    time would hit the server once per page for a page nobody is on."""
    _, data = report
    assert data["requests"] == []


def test_panel_labels_run_A_to_Z_then_AA(report):
    """Base-26 with no zero digit -- the spreadsheet-column sequence. Plain base
    conversion gives A..Z then BA, which is wrong in a way no server-side test
    can see and that a reader would notice only in a finished figure."""
    _, data = report
    assert data["math"]["labels"] == ["A", "B", "Z", "AA", "AB"]


def test_an_uncalibrated_source_produces_no_physical_width(report):
    """Never a default. A scale bar drawn from an assumed pixel size is wrong and
    looks exactly like one that is right."""
    _, data = report
    assert data["math"]["noScale"] is None
    assert data["math"]["width"] == 1300


def test_every_declared_asset_exists(report):
    static = REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"
    for name in PLUGIN.scripts:
        assert (static / name).is_file(), f"{name} is declared but not shipped"
    for name in PLUGIN.styles:
        assert (static / name).is_file(), f"{name} is declared but not shipped"


def test_the_order_of_the_declared_scripts_does_not_matter(report):
    """Stated as a test rather than assumed. Every cross-file reference here is
    inside a method or a constructor, and both the tool loader and
    DOMContentLoaded run after every file has arrived -- so a reordering is
    harmless, and knowing that is what makes the omission case below the failure
    worth guarding."""
    returncode, data = _run(list(reversed(PLUGIN.scripts)))
    assert returncode == 0, "\n".join(data["problems"])
    assert data["registered"][0]["name"] == "figure_builder"


def test_the_probe_catches_a_file_dropped_from_the_descriptor(report):
    """The real failure, produced on purpose: a file the others depend on left
    out of PLUGIN.scripts, so the browser never fetches it. Everything
    server-side is unaffected -- the panel still renders."""
    scripts = [name for name in PLUGIN.scripts if name != "figureSchema.js"]

    returncode, data = _run(scripts)
    assert returncode == 1
    assert any("FigureSchema" in problem for problem in data["problems"])


def test_capture_mode_does_not_need_a_figure(report):
    """The point of this batch. Choosing a figure is a management decision, and
    demanding it before the user has decided which regions are worth keeping
    demands it at the worst possible moment -- so capture arms with no figure
    open, and where the captures go is asked once, later, when they move to the
    canvas.

    Asked as a behaviour rather than read off the source: "the button is
    enabled" and "the tool actually arms" are different claims, and only the
    second one is the feature.
    """
    _, data = report
    assert data["armed"]["withNoFigure"] is True
    assert data["armed"]["offAgain"] is True


def test_capture_mode_stays_down_while_a_panels_view_is_borrowed(report):
    """An edit session puts a panel's captured scene into the live viewer. A
    capture taken then would be a panel of somebody else's view, and nothing on
    screen would say so -- so the toggle, the shortcut and the orb all refuse
    while one is running."""
    _, data = report
    assert data["armed"]["whileEditing"] is False


def test_a_strip_of_captures_becomes_one_batch_of_panels(report):
    """Each capture gets its own id, none of them places itself on a page, and
    every scene is joined to the figure's source. The ids matter most: a batch
    whose panels share one id is a batch the server rejects, and it would only
    ever happen to somebody capturing quickly."""
    _, data = report
    panels = data["panels"]
    assert len(set(panels["ids"])) == 3
    assert panels["sources"] == ["src_1", "src_1", "src_1"]
    assert panels["placed"] == 0


def test_a_capture_from_an_uncalibrated_source_gets_no_scale_bar(report):
    """A bar drawn from an assumed pixel size looks exactly like one that is
    right, which is the single worst failure mode a figure tool has."""
    _, data = report
    assert data["panels"]["scalebars"] is True
    assert data["panels"]["uncalibrated"] is False


# --------------------------------------------------------------------------
# Going back to a capture. Two claims, and the second is the reason the first
# is worth anything.
# --------------------------------------------------------------------------

def test_selecting_a_capture_puts_the_viewer_back_over_its_region(report):
    """A capture leaves an outline on the image and a thumbnail in the strip,
    and either one is a way back to the field it came from. Without the move
    they would be a label on a region the user still has to find by eye, which
    is the state this replaced."""
    _, data = report
    assert data["selection"]["selected"] == "cap_1"
    assert data["selection"]["fitted"] == 1


def test_going_back_to_a_capture_does_not_restore_how_it_looked(report):
    """The load-bearing one.

    Returning to a region must NOT put back the channels, windows, colours or
    overlays it was captured under, because the obvious next move is to change
    those and capture the same field again -- two panels of one region under two
    renderings, in pixel-level concordance. Restoring the scene here would make
    that unreachable by the obvious route and would rewrite the user's colours
    without being asked. Reopening a panel's whole scene is a different action,
    is reached from the canvas, and says on screen that it is running.
    """
    _, data = report
    assert data["selection"]["restores"] == 0


def test_going_back_lands_the_frame_on_the_region(report):
    """What makes "capture it again" mean the same pixels rather than roughly
    the same place. The frame is screen-anchored the rest of the time, so this
    is the one moment the two coordinate systems are deliberately joined."""
    _, data = report
    assert data["selection"]["frame"] == {
        "x": 200, "y": 150, "width": 400, "height": 300}


def test_an_unknown_capture_id_selects_nothing(report):
    """A box and a strip item are two renderings of one list. An id that is in
    neither must leave the selection where it was rather than clearing it --
    a selection pointing at nothing is a highlighted box the user cannot find."""
    _, data = report
    assert data["selection"]["stillSelected"] == "cap_1"


def test_going_back_does_not_fill_the_window_with_the_capture(report):
    """Arriving edge to edge answers "what was in this capture" and nothing
    else: where it sits, what surrounds it and whether the next panel should be
    a little to the left are all off screen, and it is a lurch out of whatever
    the user was looking at. The region gets about half the window instead --
    which also keeps its own outline visible, and that outline is how the user
    knows they have arrived somewhere they have already been."""
    _, data = report
    assert data["selection"]["framing"] == {"x": 0, "y": 0, "w": 4000, "h": 3000}


def test_going_back_locks_the_shutter_onto_that_region(report):
    """The frame is normally whatever rectangle the user last left lying about,
    and the shutter takes what is under it. That is wrong the moment a capture
    has been selected: pressing Capture then has to give back THAT region, from
    the stored numbers, or the second panel of a pair lands a pixel or two from
    the first -- close enough to look right on screen and wrong in the file."""
    _, data = report
    assert data["selection"]["pinned"] == {"x": 1000, "y": 750, "w": 2000, "h": 1500}


def test_a_nudge_does_not_cost_the_user_the_capture(report):
    """The lock is on a REGION and the frame is only how that region is shown,
    so a viewer that moves takes the frame with it. This is what makes going
    back to a capture survive the movement nobody asked for: OpenSeadragon
    carries on settling for a while after it says it has stopped, and treating
    those few pixels as "the user has left" is what made a clicked capture come
    back unselected."""
    _, data = report
    nudged = data["selection"]["afterNudge"]
    assert nudged["selected"] == "cap_1"
    assert nudged["pinned"] == {"x": 1000, "y": 750, "w": 2000, "h": 1500}
    # Followed, not left behind: the frame is 120px further along, exactly where
    # the region now projects.
    assert nudged["frame"]["x"] == 320


def test_navigating_away_lets_go_of_the_capture(report):
    """The lock survives every rendering change -- channels, windows, colours,
    overlays -- because that is the entire reason to go back to a region. It
    does not survive the region leaving the viewer: a shutter still aimed at
    tissue that has scrolled off the screen is the one outcome worse than no
    lock, and releasing there is also what stops a following frame being drawn
    outside the viewer, which does not clip. The highlight on the image and the
    active item in the strip say the same thing the lock does, so all three go
    together."""
    _, data = report
    assert data["selection"]["afterMoving"] == {"pinned": None, "selected": None}


def test_clicking_a_capture_turns_capture_mode_on(report):
    """Everything going back to a capture does -- the frame landing on the
    region, the shutter locking onto it, the outline saying "you have been
    here" -- is invisible while the mode is off. Left to the user to arm
    afterwards, the click reads as a viewer that jumped for no reason."""
    _, data = report
    assert data["selection"]["armedBefore"] is False
    assert data["selection"]["armedAfter"] is True


def test_the_canvas_button_leaves_for_the_figures_own_page(report):
    """A page, not a pane beside the image. The canvas used to open in a split
    that gave the figure half a window and the slide the other half, and neither
    job enough room to do."""
    _, data = report
    assert data["canvas"]["order"][1] == "href:/plugins/figure_builder/figure/fig_abc"


def test_waiting_captures_are_saved_before_the_page_changes(report):
    """An unattached capture is memory, and this navigation ends the memory. So
    the write happens first, and a write that fails stops the navigation rather
    than carrying the captures off the page -- the strip keeps them and the dock
    says why. A pane could afford to open anyway; a navigation cannot."""
    _, data = report
    assert data["canvas"]["order"][0] == "attached"
    assert data["canvas"]["blockedHref"] == ""
    assert data["canvas"]["said"]


def test_clicking_a_capture_selects_it_at_once(report):
    """Before the viewer has gone anywhere. The flight takes half a second, and
    a click whose only visible effect arrives at the end of it reads as a click
    that missed -- which is why the mode and the highlight are both set on the
    click rather than on the landing."""
    _, data = report
    assert data["selection"]["atOnce"] == {"armed": True, "selected": "cap_1"}


def test_the_capture_is_still_selected_once_the_viewer_arrives(report):
    """`animation-finish` is not the end of the movement: OpenSeadragon raises
    it when the springs reach their target and then pulls the viewport back
    inside its constraints, which is a second, smaller arrival. Landing the
    frame on the first of the two put it three pixels off the region, so the
    lock -- which IS the selection -- broke immediately, and the capture went
    unselected before the user could see it had been selected. Going back now
    waits for the viewer to be quiet rather than for it to say it has
    finished."""
    _, data = report
    assert data["selection"]["selected"] == "cap_1"
    assert data["selection"]["frame"] == {
        "x": 200, "y": 150, "width": 400, "height": 300}
    assert data["selection"]["pinned"] == {"x": 1000, "y": 750, "w": 2000, "h": 1500}
