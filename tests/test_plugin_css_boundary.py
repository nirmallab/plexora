"""A plugin's stylesheet may not be what keeps a core control styled.

The bug this exists to prevent, in full:

`gating.css` carried `#channels_arrow-upload-form { display: none }` -- a rule
for a CORE element, the hidden file input behind the channel-rename upload icon.
That was invisible as a problem for as long as index.html linked gating.css
unconditionally, because the rule applied on every page either way.

Once a plugin's stylesheet loaded only while its tool was open, the rule went
with it, and core's file input appeared as a raw "Choose File" button in the
Image Channels panel of any project that could not open gating.

Nothing failed. No test, no console error -- just a stray control on a page,
which is exactly the kind of thing only a person looking at the screen finds.
So the boundary is asserted here instead: a plugin styles its own ids, and core
styles core's.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_TEMPLATES = REPO_ROOT / "plexora" / "client" / "templates"
PLUGINS_DIR = REPO_ROOT / "plexora" / "plugins"

#: `id="thing"` in a template.
TEMPLATE_ID = re.compile(r"""\bid=["']([A-Za-z][\w:-]*)["']""")
#: `#thing` used as a selector. Hex colours are excluded by requiring at least
#: one non-hex character, and confirmed later against real template ids anyway.
CSS_ID = re.compile(r"#([A-Za-z][\w-]*)")


def _ids_in(paths):
    found = set()
    for path in paths:
        found |= set(TEMPLATE_ID.findall(path.read_text(encoding="utf-8")))
    return found


def _plugin_dirs():
    if not PLUGINS_DIR.is_dir():
        return []
    return [p for p in sorted(PLUGINS_DIR.iterdir()) if (p / "__init__.py").exists()]


def _css_declarations(text):
    """Ids appearing in selector position, i.e. outside a `{ ... }` body.

    Keeps `#gating_download_panel { color: #eef3f8 }` from reporting the colour
    as a styled element.
    """
    selectors = []
    for block in text.split("}"):
        selectors.append(block.split("{")[0])
    return {name for chunk in selectors for name in CSS_ID.findall(chunk)}


@pytest.mark.parametrize("plugin_dir", _plugin_dirs(), ids=lambda p: p.name)
def test_a_plugin_stylesheet_does_not_style_core_elements(plugin_dir):
    stylesheets = sorted((plugin_dir / "static").glob("*.css"))
    if not stylesheets:
        pytest.skip(f"{plugin_dir.name} ships no stylesheet")

    core_ids = _ids_in(sorted(CORE_TEMPLATES.rglob("*.html")))
    own_ids = _ids_in(sorted((plugin_dir / "templates").rglob("*.html")))

    offenders = {}
    for sheet in stylesheets:
        styled = _css_declarations(sheet.read_text(encoding="utf-8"))
        # Shared ids are fine: the plugin renders one too, so it owns that look.
        trespass = sorted((styled & core_ids) - own_ids)
        if trespass:
            offenders[sheet.name] = trespass

    assert not offenders, (
        f"{plugin_dir.name} styles core-owned element ids: {offenders}. "
        "Those rules stop applying the moment the tool is closed, leaving the "
        "core control unstyled -- move them into plexora/client/src/css/."
    )


def test_the_check_can_actually_fail():
    """A guard that cannot fail guards nothing -- this is the regression as it
    actually occurred, and the detector must catch it."""
    core_ids = {"channels_arrow-upload-form"}
    styled = _css_declarations("#channels_arrow-upload-form, #channels_arrow-upload-form * { display: none }")
    assert styled & core_ids


def test_colours_are_not_mistaken_for_elements():
    styled = _css_declarations("#gating_download_panel { color: #eef3f8; background: #101721; }")
    assert styled == {"gating_download_panel"}
