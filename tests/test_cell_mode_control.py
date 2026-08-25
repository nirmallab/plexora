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


def test_the_control_shows_what_this_project_actually_has(probe):
    """Four project states, four different rows.

    A mode the project has no resource for is no longer shown greyed out. That
    version stated a problem and withheld the fix: "Needs a segmentation mask"
    describes a file the user could attach in about four clicks, gives them no
    way to reach the page that attaches it, and spends a quarter of a control
    that has to fit on one line saying so. The modes go, and a link naming what
    to add takes the space -- the whole row when there is nothing left to
    choose between."""
    assert "with neither a mask nor data the mode buttons go entirely" in probe, probe
    assert "...and the row offers the way to fix that instead" in probe, probe
    assert "a mask without data offers the two it can draw, and not centroids" in probe, probe
    assert "data without a mask offers centroids, and not outlines or filled" in probe, probe
    assert "with both, all four are on the row and nothing is asked for" in probe, probe


def test_a_resource_that_is_present_but_unusable_is_explained_not_replaced(probe):
    """The other side of the line above, and the one that is easy to lose.

    "Missing" and "there but not usable for this" want opposite treatments. A
    table whose coordinate columns nobody has answered, and a mask still being
    converted, are both PRESENT -- telling either to go and add the thing it
    already has is the wrong instruction, and for the converting mask it is
    wrong about a state that resolves itself in minutes."""
    assert "a table with no coordinates keeps centroids, disabled, with a reason" in probe, probe
    assert "...and is not told to add data it already has" in probe, probe
    assert "a converting mask keeps its options on the row" in probe, probe
    assert "...and says so rather than asking for a mask that is already there" in probe, probe


def test_the_link_goes_to_the_page_that_can_actually_take_the_file():
    """/upload_page has no "attach to an existing project" mode -- adding data
    to a project that exists is an edit (see page_routes.upload_page). A link to
    the import wizard would land the user on a form that makes a SECOND project
    from the same image."""
    markup = TEMPLATE.read_text(encoding="utf-8")
    cta = re.search(r'id="cell_data_cta"[^>]*href="([^"]+)"', markup)
    assert cta, "the Cells row has no link to add what the project is missing"
    assert "edit_config" in cta.group(1), cta.group(1)
    assert "upload_page" not in cta.group(1), cta.group(1)
    # A plain <a>, so appRouter swaps the page in rather than tearing the
    # viewer down -- and so a middle-click still opens it in a tab.
    assert "<a id=\"cell_data_cta\"" in markup


def test_a_mask_that_will_not_load_falls_back_visibly(probe):
    assert "a mask that fails to load falls back to centroids" in probe, probe
    assert "with nothing to fall back to, the control returns to where it was" in probe, probe


def test_a_mask_arriving_late_unlocks_its_options(probe):
    """Segmentation conversion runs in the background and can finish minutes
    into a session. It used to trigger a page reload; now the control gains the
    options the mask enables, in place -- which is the reason the buttons stay
    in the DOM while hidden rather than being rendered conditionally."""
    assert "while there is no mask, the two modes it would unlock are off the row" in probe, probe
    assert "a mask finishing conversion enables the options it unlocks" in probe, probe
    assert "and they come back onto the row, with nothing left to ask for" in probe, probe


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


def test_the_control_edits_the_active_layer_rather_than_the_viewer(probe):
    """One control, several layers. It writes to whichever layer is selected, so
    a mode chosen for Cell Explorer cannot be read back as a mode chosen for
    Thresholding -- which is what a single shared `cellDisplayMode` made
    unavoidable."""
    assert "choosing a mode writes it to the active layer, not to core" in probe, probe
    assert "and the layer remembers that the user chose it" in probe, probe
    assert "selecting another tool re-points the control at ITS mode" in probe, probe
    assert "the mode event names the layer it is about" in probe, probe


def test_the_control_offers_what_the_open_tool_can_actually_use(probe):
    """Two filters, and they mean different things. A mode the PROJECT cannot
    draw stays visible and disabled with the reason on it. A mode the active
    PLUGIN does not use is hidden -- there is no explanation worth a tooltip,
    and a row of permanently greyed buttons is worse than a shorter row. "None"
    goes with them: the tool's own card is what turns its layer off."""
    assert "with a layer active, None is taken off the control" in probe, probe
    assert "a plugin's declared modes narrow what the control offers" in probe, probe
    assert "a mode this project cannot draw is still shown, disabled, with a reason" in probe, probe


def test_a_second_plugin_still_gets_the_mode_it_asked_for(probe):
    """enableCellLayer used to ask "is anything showing?", which was true as soon
    as ANY tool had turned the mask on -- so the second plugin to open silently
    never got its preferred mode. Asked per layer now."""
    assert "and a second plugin still gets the mode it asked for" in probe, probe


def test_the_shared_layers_stay_on_for_whoever_still_wants_them(probe):
    """The label item and the point overlay are one each, shared by every layer.
    Switching the active layer to centroids while another is drawing outlines
    must not take the mask away from it."""
    assert "the mask stays on while any OTHER layer is still drawing one" in probe, probe
    assert "and the points go on at the same time" in probe, probe
    assert "choosing a mode for a switched-off layer turns it back on" in probe, probe
    # The card's eye does not go through selectMode, so the surfaces have to be
    # reconciled from the layer set rather than from the click.
    assert "switching every layer off takes the mask item down with them" in probe, probe
    assert "and turning one back on brings it up again" in probe, probe
    assert "and a layer that was already drawing does not re-read the pyramid" in probe, probe


def test_opacity_is_core_s_control_and_the_plugin_s_memory(probe):
    """It used to be a slider inside Cell Explorer's panel, where a second such
    plugin would have needed a duplicate -- and where it moved whichever layer
    happened to be active rather than the one the panel was about."""
    assert "the opacity row is hidden while no plugin is colouring cells" in probe, probe
    assert "and appears with the active layer's own value on it" in probe, probe
    assert "dragging it moves the active layer and nothing else" in probe, probe
    assert "releasing it announces the value, tagged with the layer" in probe, probe


def test_the_opacity_slider_is_in_the_template_and_out_of_the_plugin():
    """Both halves of the move, because either one alone is a broken control: a
    plugin still rendering its own slider would leave two that disagree."""
    markup = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="cell_layer_opacity"' in markup
    assert 'id="cell_layer_opacity_value"' in markup
    panel = (REPO_ROOT / "plexora" / "plugins" / "cell_explorer" / "templates"
             / "cell_explorer" / "panel.html").read_text(encoding="utf-8")
    assert 'id="cell_explorer_opacity"' not in panel


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
