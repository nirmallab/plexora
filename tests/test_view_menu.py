"""The View menu: a palette, not a settings form.

Seven controls that are three kinds of thing -- what is on screen, how cells
are drawn, how well the image is drawn. As a single column of checkboxes and
radios it was nine rows deep and read as a form, which is the wrong shape for
a menu whose every row is a state rather than a command.

Two columns and an icon each say it in half the height. The grouping is
carried by position and by two hairline dividers, with no headings: a heading
over two rows is a label longer than the thing it labels.

What is easy to break here, and what each test below holds:

  * **The input is still the state.** The checkbox and the radio are the thing
    everything else reads; they are taken out of the flow, not removed. A
    `display: none` "tidy-up" would take them off the tab order and out of the
    accessibility tree, and nothing on screen would look any different.
  * **A hidden row leaves no hole.** `display: flex` outranks the `[hidden]`
    attribute's UA rule, so a mode the project cannot draw would go on holding
    its cell in the grid.
  * **An icon name is either real or silent.** Font Awesome draws nothing at
    all for a name it does not have, so a typo is invisible until somebody
    opens the menu.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NAVBAR = REPO_ROOT / "plexora" / "client" / "templates" / "base.html"
MAIN_CSS = REPO_ROOT / "plexora" / "client" / "src" / "css" / "main.css"
CONTROLS = (REPO_ROOT / "plexora" / "client" / "src" / "js" / "views"
            / "navbarControls.js")
BUNDLE = REPO_ROOT / "plexora" / "client" / "dist" / "vendor_bundle.js"

#: Every id the View menu owns, and what kind of control each one is. The
#: wiring in navbarControls.js reads all seven by id.
CONTROL_IDS = {
    "nav_toggle_sidebar": "checkbox",
    "nav_toggle_scalebar": "checkbox",
    "nav_cell_mode_none": "radio",
    "nav_cell_mode_centroids": "radio",
    "nav_cell_mode_outlines": "radio",
    "nav_cell_mode_filled": "radio",
    "nav_toggle_hd": "checkbox",
}


def view_menu() -> str:
    """Just the menu, so a match cannot come from the File or Tools one."""
    markup = NAVBAR.read_text(encoding="utf-8")
    start = markup.index('<div class="dropdown-menu view-menu"')
    end = markup.index("</li>", start)
    return markup[start:end]


def test_every_control_is_still_the_input_the_wiring_reads():
    """The redesign is a rendering. Nothing that reads `.checked` moved."""
    menu = view_menu()
    for control_id, kind in CONTROL_IDS.items():
        assert f'id="{control_id}"' in menu, control_id
        row = menu[menu.index(f'id="{control_id}"') - 200:]
        assert f'type="{kind}"' in row[:220], control_id

    source = CONTROLS.read_text(encoding="utf-8")
    for control_id in ("nav_toggle_sidebar", "nav_toggle_scalebar",
                       "nav_toggle_hd"):
        assert control_id in source, control_id
    assert 'querySelectorAll(\'input[name="nav_cell_mode"]\')' in source


def test_the_input_is_taken_out_of_the_flow_rather_than_hidden():
    """`display: none` would take the control off the tab order and out of the
    accessibility tree -- and the menu would look exactly the same, which is
    why this is worth a test rather than a comment."""
    css = MAIN_CSS.read_text(encoding="utf-8")
    rule = css[css.index('#topBar .view-menu-item input[type="checkbox"]'):]
    rule = rule[:rule.index("}")]
    assert "position: absolute" in rule
    assert "opacity: 0" in rule
    assert "display: none" not in rule


def test_a_mode_this_project_cannot_draw_leaves_no_gap_in_the_grid():
    """navbarControls.js hides a row by setting `hidden`. The row is a flex
    container, and a class rule outranks the UA's `[hidden]`, so without an
    explicit override the hidden row goes on holding its cell."""
    css = MAIN_CSS.read_text(encoding="utf-8")
    rule = css[css.index("#topBar .view-menu-item[hidden]"):]
    assert "display: none" in rule[:rule.index("}")]
    # ...and the class the script reaches for is the class the rule is on.
    assert '.closest(".view-menu-item")' in CONTROLS.read_text(encoding="utf-8")


def test_the_state_is_drawn_by_the_row_rather_than_by_a_checkbox():
    """Accent on the icon, a faint tint on the row -- the same "this is the
    one" this app uses everywhere else. The tint is a quarter of the focus
    ring's weight on purpose: four rows can be on at once."""
    css = MAIN_CSS.read_text(encoding="utf-8")
    assert "#topBar .view-menu-item:has(input:checked)" in css
    assert ("#topBar .view-menu-item:has(input:checked) .view-menu-icon"
            in css)
    assert "var(--accent-channel-tint)" in css
    # Keyboard focus has to be visible on a control that is invisible.
    assert "#topBar .view-menu-item:has(input:focus-visible)" in css


def test_the_groups_are_told_by_position_rather_than_by_headings():
    """Three grids, two dividers, no headings. The spec's point, and the
    reason the menu is 160px tall rather than 300."""
    menu = view_menu()
    assert menu.count('class="view-menu-grid"') == 3
    assert menu.count('class="dropdown-divider"') == 2
    assert "dropdown-header" not in menu


def test_two_columns_of_the_width_a_menu_can_have():
    """A palette, not a column. The one-column fallback is for a viewport too
    narrow to hold "Centroids" -- the one label that has to be read rather
    than recognised."""
    css = MAIN_CSS.read_text(encoding="utf-8")
    grid = css[css.index("#topBar .view-menu-grid {"):]
    assert "grid-template-columns: 1fr 1fr" in grid[:grid.index("}")]
    fallback = css[css.index("@media (max-width: 22rem)"):]
    fallback = fallback[:fallback.index("\n}")]
    assert "#topBar .view-menu-grid" in fallback
    assert "grid-template-columns: 1fr;" in fallback


def test_every_row_carries_an_icon_and_no_two_cell_modes_share_one():
    """The icons ARE the cell modes -- nothing, scattered points, a hollow
    ring, a solid disc -- so two rows wearing the same glyph would be two rows
    the menu cannot tell apart."""
    menu = view_menu()
    icons = re.findall(r'class="(fas|far) (fa-[a-z-]+) view-menu-icon"', menu)
    assert len(icons) == len(CONTROL_IDS)
    # Outlines and Filled are the same glyph in two styles, which is the
    # point; every other pair differs by name.
    assert len(set(icons)) == len(CONTROL_IDS)


def test_no_icon_here_is_a_name_font_awesome_does_not_have():
    """A missing name draws nothing and says nothing -- no console warning, no
    empty box, just a row that lost its landmark. The shipped bundle is the
    only authority on which names exist, so it is what this asks."""
    bundle = BUNDLE.read_text(encoding="utf-8", errors="ignore")
    names = [name[3:] for _, name
             in re.findall(r'class="(fas|far) (fa-[a-z-]+) view-menu-icon"',
                           view_menu())]
    assert names, "the View menu has no icons at all"
    for name in names:
        # Font Awesome's JS build stores each icon as `name:[...]`, quoted
        # only when the name is not a bare identifier.
        assert (f'"{name}":[' in bundle
                or re.search(r"[,{]" + re.escape(name) + r":\[", bundle)), name
