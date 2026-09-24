"""The View menu: how the image is shown, and nothing the sidebar already has.

It used to be a two-column palette of seven controls, and five of them --
Sidebar, the four Cells modes, HD mode -- were mirrors of controls the sidebar
carries, each a second answer to the same question kept in step by hand. They
are gone. What is left:

  * **Rotate & Flip**, core's own tool (plexora/server/core_tools.py), one row
    for both, in the Tools menu's row markup exactly --
    `a.dropdown-item[data-tool]` -- so toolLoader.js opens, marks and
    remembers it like any other tool.
  * **Scalebar**, the one checkbox, because nothing else shows or hides the bar.

What is easy to break here, and what each test below holds:

  * **The input is still the state.** The checkbox is taken out of the flow,
    not removed; a `display: none` "tidy-up" would take it off the tab order.
  * **Nothing removed comes back by half.** A deleted row whose wiring
    survived, or wiring whose row survived, is a control that does nothing.
  * **The chord moved, it did not vanish.** mod+\\ used to live on the
    Sidebar row; it is on the sidebar's own collapse button now.
  * **An icon name is either real or silent.** Font Awesome draws nothing at
    all for a name it does not have.
"""

import re
from pathlib import Path

from plexora.server import core_tools

REPO_ROOT = Path(__file__).resolve().parent.parent
NAVBAR = REPO_ROOT / "plexora" / "client" / "templates" / "base.html"
VIEWER_PAGE = REPO_ROOT / "plexora" / "client" / "templates" / "index.html"
MAIN_CSS = REPO_ROOT / "plexora" / "client" / "src" / "css" / "main.css"
VIEWER_CSS = REPO_ROOT / "plexora" / "client" / "src" / "css" / "viewer.css"
CONTROLS = (REPO_ROOT / "plexora" / "client" / "src" / "js" / "views"
            / "navbarControls.js")
BUNDLE = REPO_ROOT / "plexora" / "client" / "dist" / "vendor_bundle.js"

#: Ids the View menu used to own. Every one mirrored a sidebar control.
REMOVED_IDS = (
    "nav_toggle_sidebar",
    "nav_cell_mode_none",
    "nav_cell_mode_centroids",
    "nav_cell_mode_outlines",
    "nav_cell_mode_filled",
    "nav_toggle_hd",
)


def view_menu() -> str:
    """Just the menu, so a match cannot come from the File or Tools one."""
    markup = NAVBAR.read_text(encoding="utf-8")
    start = markup.index('<div class="dropdown-menu view-menu"')
    end = markup.index("</li>", start)
    return markup[start:end]


def _without_comments(source: str) -> str:
    source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def test_the_tool_rows_are_the_tools_menus_row():
    """Same element, same attribute, same href shape as the Tools menu, so the
    click delegate, the open mark and the key all apply without a branch."""
    menu = view_menu()
    assert "{% for tool in data.view_tools %}" in menu
    assert 'data-tool="{{ tool.name }}"' in menu
    assert 'href="{{ data.base_url }}/{{ data.datasource }}/tools/{{ tool.name }}"' in menu
    assert "nav-item-icon" in menu and "nav-item-label" in menu
    # Guarded, so a sample they do not apply to has no empty group above
    # Scalebar and no divider leading nowhere.
    guard = menu.index("{% if data.datasource and data.view_tools %}")
    assert guard < menu.index("{% for tool in data.view_tools %}")
    loop_end = menu.index("{% endfor %}")
    divider = menu.index('class="dropdown-divider"')
    assert loop_end < divider < menu.index("{% endif %}", divider) < menu.index("nav_toggle_scalebar")


def test_scalebar_is_the_only_input_left():
    menu = view_menu()
    assert re.findall(r'<input[^>]*id="([a-z_]+)"', menu) == ["nav_toggle_scalebar"]
    row = menu[menu.index('id="nav_toggle_scalebar"') - 200:]
    assert 'type="checkbox"' in row[:220]
    assert "nav_toggle_scalebar" in CONTROLS.read_text(encoding="utf-8")


def test_the_removed_controls_are_gone_from_markup_and_wiring():
    markup = _without_comments(NAVBAR.read_text(encoding="utf-8"))
    wiring = _without_comments(CONTROLS.read_text(encoding="utf-8"))
    for control_id in REMOVED_IDS:
        assert control_id not in markup, control_id
        assert control_id not in wiring, control_id
    assert "nav_cell_mode" not in wiring
    assert "wireMirror" not in wiring


def test_the_sidebar_chord_moved_to_the_collapse_button():
    page = VIEWER_PAGE.read_text(encoding="utf-8")
    button = page[page.index('id="sidebar_collapse_button"'):]
    button = button[:button.index(">")]
    assert 'data-shortcut="mod+\\"' in button
    assert 'data-shortcut="mod+\\"' not in NAVBAR.read_text(encoding="utf-8")
    # The printed key cap has no room on a 28px glyph button.
    css = VIEWER_CSS.read_text(encoding="utf-8")
    rule = css[css.index(".icon-button .nav-item-key {"):]
    assert "display: none" in rule[:rule.index("}")]


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
    assert "#topBar .view-menu-item:has(input:checked) .view-menu-icon" in css
    assert "#topBar .view-menu-item:has(input:focus-visible)" in css


def test_the_palette_rules_went_with_the_palette():
    css = _without_comments(MAIN_CSS.read_text(encoding="utf-8"))
    assert ".view-menu-grid" not in css
    assert "#topBar .view-menu-item[hidden]" not in css
    assert "view-menu-grid" not in view_menu()


def test_no_icon_here_is_a_name_font_awesome_does_not_have():
    """A missing name draws nothing and says nothing. The shipped bundle is the
    only authority on which names exist, so it is what this asks -- for the
    Scalebar glyph, and for the ones the two tools declare."""
    bundle = BUNDLE.read_text(encoding="utf-8", errors="ignore")
    names = [name[3:] for _, name
             in re.findall(r'class="(fas|far) (fa-[a-z-]+) view-menu-icon"', view_menu())]
    names += [tool.icon for tool in core_tools.CORE_TOOLS]
    assert "ruler-horizontal" in names
    for name in names:
        # Font Awesome's JS build stores each icon as `name:[...]`, quoted
        # only when the name is not a bare identifier.
        assert (f'"{name}":[' in bundle
                or re.search(r"[,{]" + re.escape(name) + r":\[", bundle)), name
