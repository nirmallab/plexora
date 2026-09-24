"""The import help has to keep describing the importer that actually ships.

The `?` in the Sample Import dialog opens a catalogue: one card per format,
with the marker the detector keys on and the layers that come out. Nothing
enforces that a catalogue is true -- a modality added to the server appears on
a user's screen immediately and in the documentation never, and documentation
that is quietly wrong is worse than none, because it is the thing somebody
reads before deciding their data is unsupported.

So the catalogue is held to the server's own vocabulary. `importHelp.js` spells
the modality strings, bundle names and question ids exactly as
`import_proposal.py` emits them, and the first test below reads them back out
of the Python and fails if any of them is unmentioned. Adding a format is one
entry in `FORMATS` and one line here if it brought a new modality with it.

The rest is the pairing `test_column_classifier_css.py` pins for the classifier:
the dialog is opened over the viewer, so everything it draws must be styled by
the stylesheet base.html itself loads.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "plexora" / "client"
SERVER = REPO_ROOT / "plexora" / "server"


def source(path):
    return path.read_text(encoding="utf-8")


def help_js():
    return source(CLIENT / "src" / "js" / "views" / "importHelp.js")


#: Every modality the CORE importer can put on a layer.
#:
#: Written out rather than only derived, so that a modality added to the server
#: fails this file twice: once because it is not here, and once because it is
#: not in the help. Excluded on purpose: SpatialData element modalities, which
#: come from whatever wrote the store and cannot be enumerated; `centroids`,
#: which `project.py` derives after an import rather than proposing one; and
#: anything a plugin detector contributes, which is that plugin's to document.
CORE_MODALITIES = frozenset({
    "xenium_morphology",
    "multiplex",
    "he",
    "picture",
    "mask",
    "transcripts",
    "cell_boundaries",
    "nucleus_boundaries",
    "visium_spots",
    "visium_bins",
    "cells",
    "expression",
    "annotations",
    "blank",
})

#: What `_bundle_record` is called with: the run layouts a single pick can
#: expand into several layers of one sample.
CORE_BUNDLES = frozenset({"xenium", "spatialdata", "visium", "visium_hd"})

#: Every question the detector can ask, by id. `mask-or-image` carries the
#: file's name after a colon; the prefix is the part that is a question.
CORE_QUESTIONS = frozenset({
    "reference", "table", "image", "mask-or-image", "images-grouping",
    # Carries the file's name after a colon, like `mask-or-image`. Asked when
    # a loose mask or table matches no sample's filename and there is more
    # than one sample it could belong to.
    "sample-for",
    # Which Visium HD bin level -- or the segmented cells -- is the table.
    "bin-size",
})


def observed_modalities():
    """The modality strings the import path actually emits."""
    text = "".join(source(path) for path in (
        SERVER / "models" / "import_proposal.py",
        SERVER / "models" / "import_sample.py",
        SERVER / "utils" / "spatial_scene.py",
    ))
    found = set(re.findall(r'modality\s*=\s*"([a-z_]+)"', text))
    # `describe()`'s own table: the modalities it has a name for are by
    # definition modalities that reach a row.
    named = re.search(r"named = \{(.*?)\}", text, re.S)
    if named:
        found |= set(re.findall(r'"([a-z_]+)":', named.group(1)))
    # XENIUM_FILES maps a filename to (layer id, kind, modality).
    xenium = re.search(r"XENIUM_FILES = \{(.*?)\n\}", text, re.S)
    if xenium:
        found |= set(re.findall(r'\(\s*"[^"]+",\s*"[^"]+",\s*"([a-z_]+)"\)',
                                xenium.group(1)))
    return found


def test_the_server_emits_no_modality_this_file_has_not_been_told_about():
    """CORE_MODALITIES is the premise of the next test, so it is checked first
    -- otherwise a new modality would silently pass by being in neither list."""
    unexpected = observed_modalities() - CORE_MODALITIES - {"centroids"}
    assert not unexpected, (
        f"the importer emits {sorted(unexpected)}, which CORE_MODALITIES does "
        "not list -- add it here and to FORMATS in importHelp.js"
    )


def test_the_help_names_every_modality_the_importer_can_produce():
    js = help_js()
    missing = [name for name in sorted(CORE_MODALITIES)
               if f'"{name}"' not in js and f"{name}:" not in js]
    assert not missing, (
        f"importHelp.js documents no format that produces {missing} -- a user "
        "reading it would conclude Plexora cannot read their data"
    )


def test_the_help_names_every_run_layout_a_single_pick_can_expand():
    js = help_js()
    recorded = set(re.findall(
        r'_bundle_record\([^,]+,\s*"([a-z_]+)"',
        source(SERVER / "models" / "import_proposal.py")))
    assert recorded == CORE_BUNDLES, (
        f"the importer bundles {sorted(recorded)}; CORE_BUNDLES says "
        f"{sorted(CORE_BUNDLES)}"
    )
    missing = [name for name in sorted(CORE_BUNDLES) if f'"{name}"' not in js]
    assert not missing, f"importHelp.js does not mention the {missing} layout"


def test_the_help_explains_every_question_the_detector_can_ask():
    """A question on screen is a failure of detection, and the one thing a
    first-time user cannot answer from the dialog alone: "which image is this
    sample drawn in?" means nothing without knowing what a reference is."""
    asked = set(re.findall(
        r'id=f?"([a-z-]+)(?::|",)',
        source(SERVER / "models" / "import_proposal.py")))
    assert CORE_QUESTIONS <= asked, (
        f"questions {sorted(CORE_QUESTIONS - asked)} are documented but no "
        "longer asked -- drop them from QUESTIONS in importHelp.js"
    )
    js = help_js()
    missing = [name for name in sorted(CORE_QUESTIONS)
               if f'"{name}"' not in js and f"{name}:" not in js]
    assert not missing, f"importHelp.js does not explain {missing}"


def test_a_question_never_blocks_the_import():
    """The invariant the help states out loud, pinned where it is implemented.

    `importable` is the only gate on the button. A question that disabled it
    would turn the dialog into a form with required fields, which is the thing
    this design exists to not be.
    """
    js = source(CLIENT / "src" / "js" / "views" / "importSample.js")
    assert 'part("go").disabled = !importable;' in js
    assert "A question never stops the import" in help_js()


# -- the pairing ------------------------------------------------------------

#: Every class importHelp.js writes. By hand, because several are built from
#: template strings; the last test is what stops the list from drifting.
HELP_CLASSES = (
    "plx-help",
    "plx-help-head",
    "plx-help-tabs",
    "plx-help-tab",
    "plx-help-panel",
    "plx-help-steps",
    "plx-help-step",
    "plx-help-step-num",
    "plx-help-step-name",
    "plx-help-step-text",
    "plx-help-cols",
    "plx-help-col",
    "plx-help-col-head",
    "plx-help-col-row",
    "plx-help-col-text",
    "plx-help-foot",
    "plx-help-grid",
    "plx-help-card",
    "plx-help-card-head",
    "plx-help-card-name",
    "plx-help-shape",
    "plx-help-card-line",
    "plx-help-card-key",
    "plx-help-card-pair",
    "plx-help-chip-bag",
    "plx-help-chip",
    "plx-help-card-ask",
    "plx-help-card-note",
    "plx-help-example",
    "plx-help-tree",
    "plx-help-bullets",
)


def defines(css, class_name):
    # The trailing boundary matters: `.plx-help-card` must not be answered by
    # `.plx-help-card-head`, which is a different rule.
    return re.search(rf"\.{re.escape(class_name)}(?![\w-])", css) is not None


def test_base_html_loads_the_help_before_the_dialog_that_opens_it():
    base = source(CLIENT / "templates" / "base.html")
    assert "views/importHelp.js" in base
    assert base.index("views/importHelp.js") < base.index("views/importSample.js")


def test_the_home_pages_own_button_is_styled_by_the_home_pages_own_sheet():
    """`.quick-view-help` is page chrome, not part of the modal, so it goes
    where the rest of that page's chrome goes -- index.html links quickView.css
    itself. The modal's own `.plx-help-*` still has to be in main.css, which is
    the next test: base.html loads that one everywhere, and the modal opens
    over the viewer as readily as over this page."""
    assert ".quick-view-help {" in source(
        CLIENT / "src" / "css" / "quickView.css")
    assert ".quick-view-help" not in source(CLIENT / "src" / "css" / "main.css")


def test_main_css_defines_every_help_class():
    css = source(CLIENT / "src" / "css" / "main.css")
    missing = [name for name in HELP_CLASSES if not defines(css, name)]
    assert not missing, (
        "the help opens over the viewer, which links no other stylesheet, and "
        f"main.css does not define: {missing}"
    )


def test_import_css_defines_none_of_them():
    """One definition. import.css is not loaded by base.html at all."""
    css = source(CLIENT / "src" / "css" / "import.css")
    duplicated = [name for name in HELP_CLASSES if defines(css, name)]
    assert not duplicated, f"import.css re-defines: {duplicated}"


def test_the_help_still_writes_these_classes():
    js = help_js()
    unused = [name for name in HELP_CLASSES if name not in js]
    assert not unused, (
        f"importHelp.js no longer writes {unused} -- update HELP_CLASSES, and "
        "move the CSS with it"
    )


def test_the_help_borrows_no_data_role_the_import_dialog_answers_to():
    """importSample's `part(role)` is a querySelector over its whole subtree,
    and this dialog is IN that subtree while it is open -- so a `data-role` the
    two share means the import dialog writes its status into the help."""
    js = help_js()
    borrowed = [role for role in ("close", "title", "body", "status", "go",
                                  "add", "where-mount", "where-status",
                                  "reason")
                if f'"{role}"' in js or f'dataset.role = "{role}"' in js]
    assert not borrowed, (
        f"importHelp.js uses data-role {borrowed}, which importSample.js "
        "already queries for"
    )


def test_the_help_is_hosted_beside_the_import_dialog_and_not_inside_it():
    """`PopoverPortal` moves every element it hosts into whichever modal is
    topmost, on every `close` and every fullscreen change. A help dialog hosted
    by the portal AND on top would therefore be told to adopt the import dialog
    -- appending it into itself -- which is a broken page, not a mislaid menu.

    Staying out of the portal is what makes that unreachable, and it costs
    nothing: a sibling modal still enters the top layer above the one already
    there. It also puts this dialog outside the subtree importSample's
    `part(role)` searches.
    """
    js = help_js()
    assert "PopoverPortal.attach" not in js and "PopoverPortal[verb]" not in js
    assert "function hostFor(from)" in js
    assert 'from.closest("dialog")' in js
    assert "host.appendChild(node);" in js


def test_every_surface_that_asks_which_file_can_reach_the_answer():
    """Both of them, and for one reason: the home page and the import dialog
    ask the same first question -- what can I point this at? -- and a catalogue
    reachable from only one of them is a catalogue half the users never see.

    The home page is NOT a second importer (see `test_home_landing.py`); this
    is the same modal, opened from `window`, with no controller of its own.
    """
    js = source(CLIENT / "src" / "js" / "views" / "importSample.js")
    assert "PlexoraImportHelp" in js
    assert 'aria-haspopup="dialog"' in js

    landing = source(CLIENT / "src" / "js" / "views" / "quickViewLanding.js")
    index = source(CLIENT / "templates" / "index.html")
    assert "PlexoraImportHelp" in landing
    assert 'id="quick_view_help"' in index
    assert 'aria-haspopup="dialog"' in index


def test_escape_closes_the_help_and_not_the_import_behind_it():
    """`cancel` does not bubble, so each dialog gets its own listener and the
    topmost one is the one that hears Escape. Both have to have one: without
    the help's, Escape would reach nothing; without the import's, Escape during
    an import would leave the work running with nothing on screen saying so."""
    assert 'addEventListener("cancel"' in help_js()
    assert 'addEventListener("cancel"' in source(
        CLIENT / "src" / "js" / "views" / "importSample.js")
