"""The column classifier's CSS has to be reachable wherever it is mounted.

columnClassifier.js is loaded by base.html, so the component can be mounted on
any page. Its styles used to live in import.css, which only the upload, edit and
column pages link -- so the one caller that opens over the viewer, the
requirements modal, drew it unstyled: two bare <ul>s. Sortable was attached and
the drag still worked, but there was no chip to take hold of, no box-shaped drop
target and no second column, so it read as a printed list of column names.

This pins the pairing rather than the rules: whatever classes the component
writes into the DOM must be defined in the stylesheet base.html itself loads.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "plexora" / "client"


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


# Every class columnClassifier.js puts on an element, plus the ghost class it
# hands to Sortable. Kept by hand because the file writes most of them inside a
# template literal; `test_the_classifier_still_uses_these_classes` is what stops
# this list from drifting away from the component.
CLASSIFIER_CLASSES = (
    "column-classifier",
    "column-classifier-hint",
    "column-box",
    "column-box-head",
    "column-roles",
    "column-role",
    "column-list",
    "column-chip",
    "column-chip-ghost",
)


def defines(css, class_name):
    """Does this stylesheet carry a rule for `.class_name`?"""
    # The trailing boundary matters: `.column-box` must not be answered by
    # `.column-box-head`, which is a different rule.
    return re.search(rf"\.{re.escape(class_name)}(?![\w-])", css) is not None


def test_base_html_loads_the_classifier_on_every_page():
    """The premise. If this stops being true the rest of the file is moot."""
    assert "columnClassifier.js" in source("templates", "base.html")


def test_base_html_loads_main_css_and_not_import_css():
    base = source("templates", "base.html")
    assert "css/main.css" in base
    assert "css/import.css" not in base


def test_main_css_defines_every_classifier_class():
    css = source("src", "css", "main.css")
    missing = [name for name in CLASSIFIER_CLASSES if not defines(css, name)]
    assert not missing, (
        "main.css is the only stylesheet the requirements modal can reach, and "
        f"it does not define: {missing}"
    )


def test_import_css_does_not_redefine_them():
    """One definition, so the two copies cannot drift on the pages loading both."""
    css = source("src", "css", "import.css")
    duplicated = [name for name in CLASSIFIER_CLASSES if defines(css, name)]
    assert not duplicated, f"import.css re-defines: {duplicated}"


def test_the_classifier_still_uses_these_classes():
    js = source("src", "js", "views", "columnClassifier.js")
    unused = [name for name in CLASSIFIER_CLASSES if name not in js]
    assert not unused, (
        f"columnClassifier.js no longer writes {unused} -- update "
        "CLASSIFIER_CLASSES, and move the CSS with it"
    )


def test_field_hint_is_styled_where_every_component_that_writes_it_runs():
    """`.field-hint` came from import.css too, and it is the classifier's box
    hints, the data-source field's notes and the channel-names dialog's -- all
    of them mounted over the viewer, none of them able to reach that file."""
    main = source("src", "css", "main.css")
    assert re.search(r"^\.field-hint\s*\{", main, re.M), (
        "main.css does not carry the base .field-hint rule"
    )

    writers = ("columnClassifier.js", "dataSourceField.js",
               "requirementsModal.js", "channelNamesUpload.js")
    # base.html is every page; index.html is the viewer. Neither links
    # import.css, which is the whole point.
    viewer_side = source("templates", "base.html") + source("templates", "index.html")
    for name in writers:
        assert "field-hint" in source("src", "js", "views", name)
        assert name in viewer_side


def test_the_drop_target_keeps_a_height_of_its_own():
    """An empty box has no chips to give it height; without this a user who
    dragged every column out of one box could never drag one back in."""
    css = source("src", "css", "main.css")
    rule = re.search(r"\.column-list\s*\{(.*?)\}", css, re.S)
    assert rule is not None
    assert "min-height" in rule.group(1)
