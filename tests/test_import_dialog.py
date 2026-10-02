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
    "plx-import-substeps",
    "plx-import-link",
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
    no longer exist.

    The server's own questions about the pick's rows go too, by the ids in
    each row's `needs` -- so nothing here has to rebuild how the server
    spells a `mask-or-image:` id.
    """
    for key in ("added-as", "sample-for"):
        assert f"delete state.answers[`{key}:${{pick}}`]" in dialog
    assert "(layer.needs || []).forEach((id) => { delete state.answers[id]; })" in dialog
    assert "forgetAbout(pick);" in dialog


def test_a_removed_or_abandoned_pick_is_released_on_its_node(dialog):
    """Reading a browsed path on a data node shares it there. A pick removed,
    or a dialog closed without importing, tells the server so, or the node
    keeps serving a file nothing reads -- under whatever kind it was last
    read as, which is what later refused the same file from the viewer."""
    assert 'plexoraUrl("import/release")' in dialog
    assert "release([pick]);" in dialog
    assert 'state.phase !== "importing") release(state.picks)' in dialog


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
    """Both in `answers`, both keyed by the WHOLE pick.

    Not a second array beside `paths`: that would have to stay aligned with a
    list the server filters and the user removes entries from, and two filters
    on the way already shift it.

    Not the basename either. That was the bug behind two samples' masks
    landing on one: an mcmicro run names every sample's mask
    `cellRing.ome.tif`, so the second card's answer overwrote the first's.
    """
    assert "state.answers[`sample-for:${pick}`] = intent.key" in dialog
    assert "state.answers[`added-as:${pick}`] = intent.role" in dialog
    assert "sample-for:${name}" not in dialog
    assert "added-as:${name}" not in dialog


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


def test_a_pasted_path_that_cannot_be_read_is_said_beside_its_box(dialog):
    """A path pasted into a card on a data node that was not there: the box
    closed before the answer came, the failed pick took the only-file card
    with it, and the reason was a loose row nobody tied to what they typed.
    The refusal now goes back to the box -- pick withdrawn, card restored,
    text kept, reason directly under it."""
    assert "function bouncePendingPick(" in dialog
    # Checked before the refusing proposal replaces the one on screen.
    assert dialog.index("if (bouncePendingPick(proposal, sent)) return;") < \
        dialog.index("state.proposal = proposal;\n            state.picked = sent;"
                     "\n            setStatus(null);")
    assert "state.addError = {typed: pending.path, reason: refused.reason};" \
        in dialog
    assert '"plx-import-path-error"' in dialog


def test_the_refusal_under_the_box_is_styled(dialog):
    css = _read(MAIN_CSS)
    assert ".plx-import-path-error" in css
    assert '.plx-import-path-input[aria-invalid="true"]' in css


def test_an_answer_that_is_not_json_is_a_sentence_not_a_parser_error(dialog):
    """`Unexpected token '<', "<!doctype "... is not valid JSON` was what a
    server error during import looked like."""
    assert "async function readJson(response)" in dialog
    for route in ('plexoraUrl("import/sample")', 'plexoraUrl("import/layers")',
                  'plexoraUrl("import/inspect")'):
        after = dialog[dialog.index(route):]
        assert after.index("readJson(response)") < after.index("catch (")


# -- many samples: telling them apart, and filing them together ------------

def test_the_first_dataset_chosen_is_every_samples_until_one_is_set_apart(dialog):
    """Twelve slides into one dataset used to be twelve trips through the
    picker. The first choice becomes the default for every card and every
    sample added later; after that a choice is that card's alone, and a card
    that differs offers "Use for all" -- a default, never a lock."""
    assert "const shared = !state.datasetSaid;" in dialog
    assert "if (shared) useForAll(chosen);" in dialog
    assert "function useForAll(dataset)" in dialog
    assert "state.dataset = dataset;" in dialog
    assert '"Use for all"' in dialog
    assert ".plx-import-dataset-all" in _read(MAIN_CSS)


def test_each_card_keeps_its_own_muted_outline_while_the_dialog_is_open(dialog):
    """Keyed by what the sample IS, not by its position: a card that moves up
    when another is removed keeps its colour. Only with more than one card."""
    assert "function tintFor(sample)" in dialog
    assert "state.tints[key] = Object.keys(state.tints).length % TINTS;" in dialog
    assert "if (!scoped() && total > 1) {" in dialog
    css = _read(MAIN_CSS)
    for index in range(8):
        assert f"--plx-sample-tint-{index}:" in css
    assert ".plx-import-sample.is-tinted" in css


def test_a_row_named_by_its_role_still_says_which_file(dialog):
    """Two cards that each say "Segmentation mask" give no way to check that
    sample 2's mask is sample 2's -- mcmicro names them all the same, so the
    tail of the path is shown, read from the PICK rather than `src`."""
    assert "function fileHint(layer)" in dialog
    assert 'String(pickOf(layer) || layer.src || "")' in dialog
    assert ".plx-import-row-file" in _read(MAIN_CSS)


def test_the_dialog_scrolls_with_plexoras_scrollbar_not_the_platforms(dialog):
    """The list of samples is what grows to twenty cards, and Windows drew a
    white trough down the side of it."""
    css = _read(MAIN_CSS)
    assert ".plx-dialog,\n.plx-dialog * {\n    scrollbar-color:" in css
    assert ".plx-dialog *::-webkit-scrollbar-thumb" in css


def test_a_pyramid_confirmation_is_a_note_under_its_row_and_a_modal_at_import(dialog):
    """The pyramidize question is put as a modal when Import is pressed --
    before anything is written, so a dismissed modal leaves the proposal as
    it was -- and under the row it is only a read-only note. It is never a
    form field and never disables the button."""
    render = dialog.index("function renderQuestion(question, sample)")
    confirm = dialog.index('if (question.kind === "confirm") return renderConfirmNote', render)
    select = dialog.index('question.kind === "select"', render)
    assert confirm < select
    submit = dialog[dialog.index("async function submit(replace, only)"):]
    assert submit.index("await askConfirmations(targets)") < submit.index('render("importing")')
    assert "if (value === null || !state) return false;" in dialog
    assert 'title: "This image is not pyramidized"' in dialog
    assert 'part("go").disabled = !importable;' in dialog
    css = _read(MAIN_CSS)
    assert ".plx-import-question.is-confirm" in css


def test_a_pyramid_build_is_shown_from_the_first_moment(dialog):
    """The first status poll is REGISTER_POLL_MS away, and opening a large
    stack can take longer than that -- so the row is painted active, on its
    first sub-step, synchronously before the POST, with a sliding bar until
    a number arrives. The sub-rail's keys are the server's stage keys."""
    submit = dialog[dialog.index("async function submit(replace, only)"):]
    assert submit.index('substage: "opening"') < submit.index('fetch(plexoraUrl("import/sample")')
    from plexora.server.models.data_model import IMAGE_PYRAMID_STAGES

    keys = re.findall(r'\["([a-z]+)", "[^"]+"\],', dialog[
        dialog.index("const PYRAMID_STEPS = ["):dialog.index("let dialog = null;")])
    assert keys == list(IMAGE_PYRAMID_STAGES)
    assert 'line.bar.classList.toggle("is-indeterminate", indeterminate);' in dialog
    assert ".plx-import-step-bar.is-indeterminate .plx-import-step-fill" in _read(MAIN_CSS)


def test_what_the_post_finished_stays_finished(dialog):
    """`finishSamples` re-renders every row at "waiting"; the rows the POST
    itself completed -- a pyramidized image above all -- are painted ready
    from its own answer rather than flipping back."""
    finish = dialog[dialog.index("function finishSamples(results, at)"):]
    assert 'paintLine(state.bars?.get(`${sampleAt}:${entry.id}`), {status: "ready"});' in finish
