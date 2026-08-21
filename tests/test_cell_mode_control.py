"""The Cells control: one representation at a time, and only usable ones.

Two checkboxes became one choice of four. The reason is structural rather than
cosmetic: the pair could express "Outlines and Centroids", which the renderer
then had to arbitrate, and could not express "Filled" at all. Making it one
control makes the exclusivity a property of the widget instead of a rule spread
across two handlers that each undid the other.

The availability half matters just as much and is easier to get wrong. Filled
needs a mask whose labels are stored whole; a pyramid already reduced to
boundaries has no interior pixels, so the button would be inert. Offering it
there fails silently -- the click lands, nothing changes, and nothing on screen
says why.

Run in node against the real viewerControls.js, because nothing else can: the
Python suite renders the template and stops, and `node --check` sees syntax.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "cell_mode_control_probe.mjs"
TEMPLATE = REPO_ROOT / "plexora" / "client" / "templates" / "index.html"
NAVBAR = REPO_ROOT / "plexora" / "client" / "templates" / "base.html"


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def test_the_viewer_still_opens_drawing_nothing(probe):
    """A cell layer costs a manifest fetch, a mask pyramid read and a full
    repaint. Someone who opened a project to look at the image wanted the
    image."""
    assert "the viewer opens drawing no cells" in probe, probe
    assert "no mask is fetched before something asks for one" in probe, probe


def test_only_one_representation_is_active(probe):
    assert "choosing outlines makes it the only active option" in probe, probe
    assert "choosing centroids turns the label layer off" in probe, probe


def test_options_this_project_cannot_draw_are_refused(probe):
    """Disabled in the markup AND refused by selectMode: the menu and the
    keyboard reach the same code, so a check that lived only in the click
    handler would be two ways around the control."""
    assert "a pre-reduced mask offers outlines but not filled" in probe, probe
    assert "a disabled option cannot be selected through the API either" in probe, probe
    assert "a project with neither offers nothing but None" in probe, probe


def test_a_mask_that_will_not_load_falls_back_visibly(probe):
    assert "a mask that fails to load falls back to centroids" in probe, probe
    assert "with nothing to fall back to, the control returns to where it was" in probe, probe


def test_a_mask_arriving_late_unlocks_its_options(probe):
    """Segmentation conversion runs in the background and can finish minutes
    into a session. It used to trigger a page reload; now the control gains the
    options the mask enables, in place."""
    assert "a mask finishing conversion enables the options it unlocks" in probe, probe


def test_the_events_other_views_listen_for_still_fire(probe):
    """navbarControls.js and any plugin that hooked the old pair read these.
    Firing the new mode event instead of them would break those quietly."""
    assert "the legacy outline event still fires" in probe, probe
    assert "filled counts as outlines for anything still listening for that" in probe, probe


def test_a_user_choice_still_outranks_an_automatic_one(probe):
    assert "a click is a decision, and outranks the automatic fallback" in probe, probe
    assert "a second tool activating does not undo what is already showing" in probe, probe


def test_how_the_mask_is_drawn_is_the_plugins_and_which_layer_is_the_projects(probe):
    """Two separate questions that a single "preference" used to conflate.
    Which layer -- mask or points -- is recorded on the project, so opening a
    different tool first cannot change the answer. How to draw the mask --
    filled or outlines -- depends on what the tool is showing, so it is the
    plugin's to ask for, and core still refuses what the mask cannot do."""
    assert "a plugin that colours every cell gets a filled mask" in probe, probe
    assert "asking for filled where the mask cannot fill lands on outlines" in probe, probe
    assert "the project's recorded layer still wins over a plugin's preference" in probe, probe


def test_a_mask_arriving_late_is_drawn_the_way_the_open_tool_wants(probe):
    """The other half of the seam above, and the one that was missing. A
    pyramid finishing conversion turns the mask on with no plugin activating,
    so it cannot be handed a preference -- it has to ask whoever holds the
    layer. Hardcoding outlines there is why a project that gained its mask from
    the edit page drew outlines for the whole of that session, while every
    later page load drew the same project filled."""
    assert "with nothing holding the cell layer there is no preference to read" in probe, probe
    assert "the preference is read off whoever holds the layer" in probe, probe
    assert "a late mask is drawn the way the holder asked" in probe, probe
    assert "and still not in a way this mask cannot manage" in probe, probe


def test_the_late_mask_path_asks_rather_than_assuming():
    """main.js is not run by the probe, so the call itself is pinned here: it
    is the one line that has to change from a hardcoded mode to a question."""
    main = (REPO_ROOT / "plexora" / "client" / "src" / "js"
            / "main.js").read_text(encoding="utf-8")
    adopt = main.split("function adoptSegmentation", 1)[1].split("\n    }", 1)[0]
    assert "ownerMaskPreference()" in adopt
    assert "selectMode('outlines')" not in adopt
    # And the preference has to get onto the provider in the first place.
    assert "preferredCellMode = definition.preferredCellMode" in main


def test_centroids_can_be_resized_and_nothing_else_can(probe):
    """Point size is core's because the geometry is: every plugin that colours
    cells gets it without shipping a centroid renderer. It is shown only in
    Centroids -- sizing a mask means nothing, and a control that is present but
    inert reads as broken rather than as not applicable."""
    assert "the size slider is hidden while nothing is drawn" in probe, probe
    assert "and appears when points are what is on screen" in probe, probe
    assert "and goes again for a mask, which it cannot size" in probe, probe
    assert "dragging it resizes the points" in probe, probe


def test_the_control_is_reachable_without_a_pointer(probe):
    assert "arrow keys move through the enabled options" in probe, probe
    assert "the keyboard skips options this project cannot draw" in probe, probe


def test_the_old_checkboxes_are_gone_from_every_template():
    """Both templates and every reader of them moved at once. A leftover id
    renders a control that looks live and is wired to nothing."""
    for template in (TEMPLATE, NAVBAR):
        markup = template.read_text(encoding="utf-8")
        assert "seg_controls_outlines" not in markup, template.name
        assert "seg_controls_centroids" not in markup, template.name
        assert "nav_toggle_outlines" not in markup, template.name
        assert "nav_toggle_centroids" not in markup, template.name


def test_the_size_slider_offers_exactly_what_the_renderer_accepts():
    """The slider's bounds live in the template and the clamp lives in
    ImageViewer. Two numbers in two files: if the slider offers more than the
    renderer accepts, its top end silently does nothing, and if it offers less,
    part of the range is unreachable."""
    viewer = (REPO_ROOT / "plexora" / "client" / "src" / "js" / "views"
              / "imageViewer.js").read_text(encoding="utf-8")
    bounds = {
        name: float(re.search(rf"static {name}_CENTROID_SCALE = ([\d.]+)", viewer).group(1))
        for name in ("MIN", "MAX", "DEFAULT")
    }
    markup = TEMPLATE.read_text(encoding="utf-8")
    slider = re.search(r'id="cell_point_size"[^>]*?'
                       r'min="([\d.]+)" max="([\d.]+)" step="[\d.]+" value="([\d.]+)"',
                       markup, re.S)
    assert slider, "the point size slider is not in the template"
    assert float(slider.group(1)) == bounds["MIN"]
    assert float(slider.group(2)) == bounds["MAX"]
    assert float(slider.group(3)) == bounds["DEFAULT"]


def test_every_mode_the_control_offers_has_a_button():
    """The template and ViewerControls.MODES are two lists that have to agree.
    A mode in one and not the other is a button that does nothing, or a mode
    nothing can reach."""
    source = (REPO_ROOT / "plexora" / "client" / "src" / "js" / "views"
              / "viewerControls.js").read_text(encoding="utf-8")
    declared = re.search(r"static MODES = \[(.*?)\]", source, re.S).group(1)
    modes = set(re.findall(r'"([a-z]+)"', declared))
    markup = TEMPLATE.read_text(encoding="utf-8")
    assert modes == set(re.findall(r'data-cell-mode="([a-z]+)"', markup))
    assert modes == set(re.findall(r'name="nav_cell_mode" id="nav_cell_mode_([a-z]+)"',
                                   NAVBAR.read_text(encoding="utf-8")))
