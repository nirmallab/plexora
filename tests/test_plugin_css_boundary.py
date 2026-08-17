"""Core styles core's elements; a plugin styles its own. Both directions.

Two separate bugs live here, one found by each direction.

PLUGIN -> CORE, which shipped and was reported by the user:
`gating.css` carried `#channels_arrow-upload-form { display: none }` -- a rule
for a CORE element, the hidden file input behind the channel-rename upload
icon. That was invisible for as long as index.html linked gating.css
unconditionally. Once a plugin's stylesheet loaded only while its tool was
open, the rule went with it, and core's file input appeared as a raw
"Choose File" button in any project that could not open gating.

CORE -> PLUGIN, which the first version of this file did not check:
core's `viewer.css` held ~150 lines styling gating's panels. Nothing looked
broken, because unused CSS is inert -- but a core-only build shipped a missing
tool's appearance, and gating alone got a look it never had to ship. Any
third-party plugin has to bring its own CSS, so styling one plugin from core
made that plugin privileged in exactly the way the plugin API exists to
prevent.

Only the first direction can produce a visible bug, but a guard that checks
one direction is how the second one lasted this long.

Ownership is decided by which templates declare an id, plus the ids each side
creates in its own JavaScript -- `#csv_gating_list` is built by
csvGatingList.js and never appears in a template, so templates alone would
call it orphaned.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_TEMPLATES = REPO_ROOT / "plexora" / "client" / "templates"
CORE_CSS_DIR = REPO_ROOT / "plexora" / "client" / "src" / "css"
CORE_JS = REPO_ROOT / "plexora" / "client" / "src" / "js"
PLUGINS_DIR = REPO_ROOT / "plexora" / "plugins"

#: `id="thing"` in a template.
TEMPLATE_ID = re.compile(r"""\bid=["']([A-Za-z][\w:-]*)["']""")
#: `#thing` used as a selector.
CSS_ID = re.compile(r"#([A-Za-z][\w-]*)")
#: A comment is not a selector. Leaving these in made this file's own prose
#: ("a shared one with #channel_list_wrapper") register as a violation.
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
#: Ids an element acquires at runtime rather than in a template.
JS_ID = [
    re.compile(r"""setAttribute\(\s*["']id["']\s*,\s*["']([\w-]+)["']"""),
    re.compile(r"""\.id\s*=\s*["']([\w-]+)["']"""),
    re.compile(r"""getElementById\(\s*["']([\w-]+)["']"""),
    re.compile(r"""id=\\?["']([\w-]+)\\?["']"""),
]


def _read(path):
    return path.read_text(encoding="utf-8", errors="replace")


def _ids_owned_by(template_dirs, js_dirs):
    """Every element id a side puts on the page, from templates and from JS."""
    found = set()
    for directory in template_dirs:
        for path in sorted(directory.rglob("*.html")) if directory.is_dir() else []:
            found |= set(TEMPLATE_ID.findall(_read(path)))
    for directory in js_dirs:
        for path in sorted(directory.rglob("*.js")) if directory.is_dir() else []:
            text = _read(path)
            for pattern in JS_ID:
                found |= set(pattern.findall(text))
    return found


def _styled_ids(text):
    """Ids in selector position: outside any `{ ... }` body, and not in a comment.

    Keeps `#gating_download_panel { color: #eef3f8 }` from reporting the colour
    as a styled element.
    """
    text = CSS_COMMENT.sub(" ", text)
    selectors = [block.split("{")[0] for block in text.split("}")]
    return {name for chunk in selectors for name in CSS_ID.findall(chunk)}


def _plugin_dirs():
    if not PLUGINS_DIR.is_dir():
        return []
    return [p for p in sorted(PLUGINS_DIR.iterdir()) if (p / "__init__.py").exists()]


def _core_ids():
    return _ids_owned_by([CORE_TEMPLATES], [CORE_JS])


def _plugin_ids(plugin_dir):
    return _ids_owned_by([plugin_dir / "templates"], [plugin_dir / "static"])


@pytest.mark.parametrize("plugin_dir", _plugin_dirs(), ids=lambda p: p.name)
def test_a_plugin_stylesheet_does_not_style_core_elements(plugin_dir):
    """The reported bug: the rule stops applying the moment the tool closes."""
    stylesheets = sorted((plugin_dir / "static").glob("*.css"))
    if not stylesheets:
        pytest.skip(f"{plugin_dir.name} ships no stylesheet")

    core_ids, own_ids = _core_ids(), _plugin_ids(plugin_dir)

    offenders = {}
    for sheet in stylesheets:
        # Shared ids are fine: the plugin renders one too, so it owns that look.
        trespass = sorted((_styled_ids(_read(sheet)) & core_ids) - own_ids)
        if trespass:
            offenders[sheet.name] = trespass

    assert not offenders, (
        f"{plugin_dir.name} styles core-owned element ids: {offenders}. "
        "Those rules stop applying the moment the tool is closed, leaving the "
        "core control unstyled -- move them into plexora/client/src/css/."
    )


@pytest.mark.parametrize("plugin_dir", _plugin_dirs(), ids=lambda p: p.name)
def test_core_stylesheets_do_not_style_plugin_elements(plugin_dir):
    """The inverse: core must not carry a plugin's appearance, or a core-only
    build ships CSS for a tool it does not have and that one plugin gets a look
    no other plugin would be given."""
    core_ids, own_ids = _core_ids(), _plugin_ids(plugin_dir)

    offenders = {}
    for sheet in sorted(CORE_CSS_DIR.glob("*.css")):
        trespass = sorted((_styled_ids(_read(sheet)) & own_ids) - core_ids)
        if trespass:
            offenders[sheet.name] = trespass

    assert not offenders, (
        f"core stylesheets style ids only the {plugin_dir.name} plugin renders: "
        f"{offenders}. Move them into that plugin's static/ directory -- core "
        "cannot ship the appearance of a tool it may not have installed."
    )


# --------------------------------------------------------------------------
# The detector has to be able to fail, in both directions
# --------------------------------------------------------------------------

def test_the_plugin_to_core_check_can_fail():
    """The regression exactly as it occurred."""
    styled = _styled_ids("#channels_arrow-upload-form, #channels_arrow-upload-form * { display: none }")
    assert styled & {"channels_arrow-upload-form"}


def test_the_core_to_plugin_check_can_fail():
    styled = _styled_ids("#gating_save_anndata_panel { margin-top: 8px }")
    assert styled & {"gating_save_anndata_panel"}


def test_colours_are_not_mistaken_for_elements():
    assert _styled_ids("#gating_download_panel { color: #eef3f8; background: #101721; }") == {
        "gating_download_panel"
    }


def test_comments_are_not_mistaken_for_selectors():
    """This file's own explanatory comments name ids in prose; parsing those as
    selectors reported violations that did not exist."""
    assert _styled_ids("/* was shared with #channel_list_wrapper */ #a { color: red }") == {"a"}


def test_ids_created_in_javascript_count_as_owned():
    """#csv_gating_list is built by csvGatingList.js and is in no template, so
    template-only ownership would call every rule for it an orphan."""
    assert "csv_gating_list" in _plugin_ids(PLUGINS_DIR / "gating")


# --------------------------------------------------------------------------
# Rules that style nothing at all
# --------------------------------------------------------------------------

def test_core_css_has_no_rules_for_elements_that_do_not_exist():
    """Left behind by deletions: #seg_controls_panel (the live panel is
    #viewer_controls_panel), three *_upload_icon_db ids, #boxHeading2,
    #scalebar-header-div."""
    known = _core_ids() | set().union(*(_plugin_ids(p) for p in _plugin_dirs()) or [set()])
    # Ids that exist only in karma fixtures, which are not shipped pages.
    fixtures = REPO_ROOT / "plexora" / "client" / "test" / "fixtures"
    if fixtures.is_dir():
        for path in sorted(fixtures.rglob("*.html")):
            known |= set(TEMPLATE_ID.findall(_read(path)))

    orphans = {}
    for sheet in sorted(CORE_CSS_DIR.glob("*.css")):
        stale = sorted(_styled_ids(_read(sheet)) - known)
        if stale:
            orphans[sheet.name] = stale

    assert not orphans, f"core stylesheets style ids nothing renders: {orphans}"
