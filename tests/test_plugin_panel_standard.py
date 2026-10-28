"""Every tool panel in the sidebar is built to one standard.

Four panels open at once used to read as four authors: three eye styles in
three places, buttons at 6px corners beside fields at 2px and Run buttons
drawn as pills, rows at 24px beside rows at 32px, and a footer of keys on two
of them. The measures are core's now
-- tokens.css's `--panel-*`, viewer.css's `.plx-panel-field` and
`.plx-panel-footer` -- and the rule they make up, with its reasons, is the
plugin guide's "Design the sidebar panel" (build-a-plugin.mdx).

What is checked here is what a new plugin is most likely to get wrong and what
a reader notices first: a panel that does not end on the footer of its keys, a
solid eye where every other panel has an outlined one, and a private copy of a
shared part. The finer measures are the guide's to explain; a test that
policed every font size would mostly police hover cards.

A plugin that has not been brought to the standard yet is named in PENDING,
with the reason, so the list only ever gets shorter.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plexora" / "plugins"
CORE_CSS = REPO_ROOT / "plexora" / "client" / "src" / "css"
GUIDE = REPO_ROOT / "website" / "content" / "docs" / "(guide)" / "plugins" / "build-a-plugin.mdx"

#: `"tool_panel_slot": "roi/panel.html"` in a plugin's descriptor.
TOOL_PANEL = re.compile(r"""["']tool_panel_slot["']\s*:\s*["']([\w/.-]+)["']""")
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)

#: Tool panels not yet on the standard, and why.
PENDING = {
    # 3-D only, a short form with no keys of its own yet; its footer needs a
    # key worth having (Enter to segment, say) before it can have one.
    "segmentation",
}


def _tool_panels():
    found = []
    for init in sorted(PLUGINS_DIR.glob("*/__init__.py")):
        match = TOOL_PANEL.search(init.read_text(encoding="utf-8"))
        if match:
            found.append((init.parent.name, init.parent / "templates" / match.group(1)))
    return found


PANELS = [(name, path) for name, path in _tool_panels() if name not in PENDING]


def test_the_tool_panels_are_found():
    names = {name for name, _ in PANELS}
    assert {"cell_explorer", "gating", "qc", "roi"} <= names
    # PENDING names real tool panels, so a removed plugin leaves no stale entry.
    assert PENDING <= {name for name, _ in _tool_panels()}


def test_core_carries_the_shared_parts():
    tokens = (CORE_CSS / "tokens.css").read_text(encoding="utf-8")
    for token in ("--panel-font-body: 13px;", "--panel-font-meta: 12px;",
                  "--panel-font-note: 11px;", "--panel-font-footer: 10px;",
                  "--panel-control-height: 34px;", "--panel-button-height: 28px;",
                  "--panel-row-height: 32px;", "--panel-icon-button: 24px;",
                  "--radius-control: var(--radius-md);", "--radius-card: var(--radius-lg);",
                  "--panel-rule:", "--panel-gap: 8px;"):
        assert token in tokens, token
    viewer = CSS_COMMENT.sub("", (CORE_CSS / "viewer.css").read_text(encoding="utf-8"))
    # The card's stripe is given back once, in core, for every panel.
    assert "--tool-stripe: 3px;" in viewer
    assert "padding-left: calc(var(--space-2) + var(--tool-stripe));" in viewer
    for rule in (".plx-panel-field > .sidebar-select,", ".plx-panel-footer {",
                 ".tool-card-body .plx-panel-footer {", ".plx-panel-shortcut {",
                 ".plx-panel-shortcut:disabled {"):
        assert rule in viewer, rule
    # One corner for every control and one for every card, in the shared
    # components themselves -- a button and the field beside it were 6px and
    # 2px, the cards 6px around 8px buttons.
    for selector, radius in ((".tool-card {", "var(--radius-card)"),
                             (".layer-card {", "var(--radius-card)"),
                             (".layer-card-action {", "var(--radius-control)"),
                             (".cell-mode-options {", "var(--radius-control)"),
                             (".plx-panel-shortcut {", "var(--radius-control)")):
        rule = viewer[viewer.index("\n" + selector) + 1:]
        assert f"border-radius: {radius};" in rule[:rule.index("}")], selector
    assert "border-radius: var(--radius-control);" in viewer[viewer.index(".plx-panel-field > .sidebar-select,"):]
    assert "height: var(--panel-button-height);" in viewer[viewer.index("\n.cell-mode-options {"):][:400]
    assert "## Design the sidebar panel" in GUIDE.read_text(encoding="utf-8")


@pytest.mark.parametrize("name, template", PANELS, ids=[name for name, _ in PANELS])
def test_the_panel_ends_on_a_footer_of_its_keys(name, template):
    """The card's last line is core's footer band, holding the panel's keys
    as buttons -- each a key cap and words, each doing what its key does."""
    panel = JINJA_COMMENT.sub("", template.read_text(encoding="utf-8"))
    assert re.search(r"<footer\b[^>]*\bclass=\"plx-panel-footer\"", panel), name
    footer = panel[panel.index("<footer"):panel.index("</footer>")]
    keys = re.findall(r'<button\b[^>]*\bclass="plx-panel-shortcut"[^>]*>\s*<kbd class="plx-kbd"', footer)
    assert keys, f"{name}: no key in the footer"
    # Last: nothing of the panel's own after it.
    assert re.fullmatch(r"\s*</section>\s*", panel[panel.index("</footer>") + len("</footer>"):]), name


#: Every plugin that draws in the sidebar, tool card or layer section.
SIDEBAR_PLUGINS = ["cell_explorer", "gating", "qc", "roi", "segmentation", "transcripts", "visium_hd"]
#: Retired panel tokens: their two radii are --radius-control now.
RETIRED = ("--panel-field-radius", "--panel-hover-radius")


@pytest.mark.parametrize("name", SIDEBAR_PLUGINS)
def test_the_sidebar_plugin_rounds_its_controls_with_the_shared_corner(name):
    """No button drawn as a pill beside an 8px field, and no retired token.
    Pills are kept for what is not a button -- a count badge, a progress
    track -- which is why the check is on the button rules."""
    for sheet in (PLUGINS_DIR / name / "static").glob("*.css"):
        css = CSS_COMMENT.sub("", sheet.read_text(encoding="utf-8"))
        for token in RETIRED:
            assert token not in css, (sheet.name, token)
        for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
            if re.search(r"(button|-run|-play|-add-roi|-action)\b", selector) and "999px" in body:
                pytest.fail(f"{sheet.name}: {selector.strip()} is a pill")


@pytest.mark.parametrize("name, template", PANELS, ids=[name for name, _ in PANELS])
def test_the_panel_uses_the_shared_parts_rather_than_copies(name, template):
    """No solid eye (every panel's are `far`), no private footer band, no
    private stripe compensation."""
    plugin = PLUGINS_DIR / name
    sources = list((plugin / "static").glob("*.js")) + list((plugin / "templates").rglob("*.html"))
    for path in sources:
        assert "fas fa-eye" not in path.read_text(encoding="utf-8"), path.name
    for sheet in (plugin / "static").glob("*.css"):
        css = CSS_COMMENT.sub("", sheet.read_text(encoding="utf-8"))
        assert not re.search(r"[\w-]+-panel-footer\s*\{", css.replace(".plx-panel-footer", "")), sheet.name
        assert "transparent 0 3px" not in css, sheet.name
