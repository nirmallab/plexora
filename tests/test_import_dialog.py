"""The import dialog's own rules, held to its source.

There is no DOM in this suite -- no jsdom, no headless browser in CI -- so what
can be pinned here is the shape of the file rather than the pixels it draws.
That is enough for the three things that went wrong, because each of them was a
decision visible in the source:

**Remove compared the wrong two strings.** A row's `src` is where the data will
be READ from; the pick is what the user chose. For a browsed path on a node
those differ by the whole address -- the node derives a resource id -- so the
filter matched nothing and the ✕ on a remote image did nothing at all, twice,
silently. The rule is that removal never sees `src`.

**Nothing said which sample a file joined.** The contextual actions on a card
send `sample-for:` and `added-as:` in `answers`, which is the channel that
survives re-inspection and reaches the Python API unchanged.

**The dialog draws itself from one stylesheet.** `base.html` loads `main.css`
on every surface this dialog opens from. `import.css` is a near-miss with a
historical name -- it is the project EDIT page's, loaded by one template -- so
a `.plx-import-*` rule that lands there applies on one page and nowhere this
dialog is actually opened.

Companion to test_import_help.py, which pins the same file's other invariant:
a question never blocks the import.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "plexora" / "client"
DIALOG = CLIENT / "src" / "js" / "views" / "importSample.js"
MAIN_CSS = CLIENT / "src" / "css" / "main.css"

#: Everything the proposal card draws that did not exist before. Each is
#: checked against main.css, because a class with no rule is an unstyled box
#: and nothing else on the page would say so.
CARD_CLASSES = (
    "plx-import-card-head",
    "plx-import-card-ordinal",
    "plx-import-name-inline",
    "plx-import-name-edit",
    "plx-import-dataset-inline",
    "plx-import-card-actions",
    "plx-import-slot",
    "plx-import-inline-pick",
    "plx-import-back",
    "plx-import-steps-head",
)


def _read(path):
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def dialog():
    return _read(DIALOG)


# -- removal ---------------------------------------------------------------

def test_removal_never_compares_a_rows_source_to_a_pick(dialog):
    """The reported bug, pinned as the thing that caused it.

    `removeButton(layer.src)` is the whole of it: a node rewrites a browsed
    path into `node://<node>/<derived id>`, and a pasted `~/slide.tif` comes
    back expanded, so the filter matched neither and ran to no effect.
    """
    assert "removeButton(layer.src)" not in dialog
    assert "removeButton(entry.path)" not in dialog
    assert "function pickOf(" in dialog


def test_a_row_resolves_its_pick_against_the_list_it_indexes(dialog):
    """`state.picked`, not `state.picks`.

    A removal edits `state.picks` and re-renders the OLD proposal while the
    next inspection is in flight, so indexing the live array during that
    window resolves a row to its neighbour -- which is a second fast ✕ taking
    out the wrong file. `state.picked` is the array the proposal on screen was
    computed from, and it only changes when a new one lands.
    """
    assert "state.picked[row.pick]" in dialog
    assert "state.picked = sent" in dialog


def test_removing_a_pick_forgets_what_was_said_about_it(dialog):
    """Or a file removed and picked again arrives carrying the role and the
    sample it had last time -- including a `sample-for` naming a card that may
    no longer exist."""
    for key in ("added-as", "sample-for", "mask-or-image"):
        assert f"delete state.answers[`{key}:${{name}}`]" in dialog


# -- contextual actions ----------------------------------------------------

def test_a_card_offers_what_its_sample_has_not_got(dialog):
    """Driven by `sample.missing` from the server, not by a guess here.

    Nothing in this dialog decides what a file is or what a sample needs --
    that is `/import/inspect`'s answer, and this draws it.
    """
    assert "sample.missing" in dialog
    assert "Add segmentation mask" in dialog
    assert "Add data" in dialog


def test_an_added_file_says_which_sample_and_which_role(dialog):
    """Both in `answers`, both keyed by the pick's own basename.

    Not a second array beside `paths`: that would have to stay aligned with a
    list the server filters and the user removes entries from, and two filters
    on the way already shift it.
    """
    assert "state.answers[`sample-for:${name}`] = intent.key" in dialog
    assert "state.answers[`added-as:${name}`] = intent.role" in dialog


def test_the_footer_add_is_for_another_sample(dialog):
    """It carries no intent, which is exactly what makes it the other thing:
    a file belonging to a sample already on screen is added on that card."""
    assert '"Add more samples"' in dialog
    assert '"Add more files"' in dialog, "the scoped dialog adds no samples"
    assert "</span> Add files" not in dialog, "the old, intentless label"


def test_every_sample_on_screen_is_registered(dialog):
    """`/import/sample` registers ONE sample, and this used to post once.

    "Import 3 samples" created one project and dropped the other two without
    saying so.
    """
    assert "index: at," in dialog
    assert "key: sample.key || undefined," in dialog


# -- where the rules live --------------------------------------------------

def test_the_cards_chrome_is_styled_in_the_stylesheet_every_page_loads(dialog):
    css = _read(MAIN_CSS)
    undrawn = [name for name in CARD_CLASSES if f".{name}" not in css]
    assert not undrawn, (
        f"{undrawn} are drawn by importSample.js and styled nowhere in "
        "main.css -- base.html loads main.css on every surface this dialog "
        "opens from, which is why the rules belong there"
    )
    unused = [name for name in CARD_CLASSES if name not in dialog]
    assert not unused, f"{unused} are styled but never drawn"


def test_the_name_and_dataset_are_no_longer_form_fields(dialog):
    """They arrive filled in, and a form field is how a screen asks a
    question. Drawn as muted metadata that becomes editable where it stands."""
    assert "renderNameRow" not in dialog
    css = _read(MAIN_CSS)
    for gone in (".plx-import-meta {", ".plx-import-meta-label {",
                 ".plx-import-name {", ".plx-import-dataset {"):
        assert gone not in css, f"{gone} outlived the row it styled"


def test_none_of_this_dialogs_rules_land_in_the_edit_pages_stylesheet():
    """`import.css` is the project EDIT page's, and `project_edit.html` is the
    only template that loads it -- the name is historical. A `.plx-import-*`
    rule put there would apply on that one page and nowhere this dialog
    actually opens from."""
    css = _read(CLIENT / "src" / "css" / "import.css")
    strays = sorted(set(re.findall(r"\.plx-import-[\w-]+", css)))
    assert not strays, strays


def test_the_question_gate_is_still_the_one_line_it_was(dialog):
    """Restated from test_import_help.py because this file rewrote the
    function around it: `importable` counts LAYERS, and a question -- which
    always has a default -- never enters into it."""
    assert 'part("go").disabled = !importable;' in dialog


def test_the_split_control_is_the_one_the_rest_of_the_app_uses(dialog):
    """The inline picker inside a card is the same two halves as the pick
    state's, with the format examples of the slot that opened it -- not a
    third file-choosing control with its own manners."""
    opened = re.findall(r"buildSplitControl\((.+?),", dialog)
    assert len(opened) == 2, opened
    assert "spec.examples" in opened[1]
