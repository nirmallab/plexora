"""The project browser -- what the folder metaphor must not be allowed to imply.

Run in node against the real openProjectPage.js, because nothing else can: the
Python suite renders the template and stops, and `node --check` sees syntax.

The half that IS Python is the contract between this page and the two others
that render from the same classes. Figure Builder's Figures and Captures pages
reuse `.project-card`, `.project-thumb` and `.project-actions` verbatim and
link this stylesheet, so a redesign here that restructured a card would change
two pages nobody was looking at.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "open_project_probe.mjs"
TEMPLATE = REPO_ROOT / "plexora" / "client" / "templates" / "open_project.html"
CONTROLLER = (REPO_ROOT / "plexora" / "client" / "src" / "js" / "views"
              / "openProjectPage.js")
STYLESHEET = REPO_ROOT / "plexora" / "client" / "src" / "css" / "openProject.css"
MAIN_CSS = REPO_ROOT / "plexora" / "client" / "src" / "css" / "main.css"
BASE = REPO_ROOT / "plexora" / "client" / "templates" / "base.html"


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


# --------------------------------------------------------------------------
# A dataset is a folder, and owns nothing
# --------------------------------------------------------------------------

def test_deleting_a_dataset_is_not_deleting_what_is_in_it(probe):
    """The one thing a folder metaphor gets wrong by default. Deleting a folder
    normally deletes its contents; this one releases them, and the dialog has
    to say so before the user finds out either way."""
    assert "deleting a dataset offers keeping or deleting its samples" in probe, probe
    assert "and says keeping them deletes nothing from disk" in probe, probe
    assert "dataset only posts to the dataset, never to a project" in probe, probe
    assert "dataset and samples deletes every member, then the dataset" in probe, probe
    assert "and does nothing when the question is dismissed" in probe, probe


def test_a_project_leaving_a_dataset_is_not_a_project_being_deleted(probe):
    assert "dropping on the root crumb takes a project out of its dataset" in probe, probe
    assert "which is an assign, not a delete" in probe, probe
    assert "Remove from dataset posts an assign to nothing" in probe, probe


# --------------------------------------------------------------------------
# Navigating
# --------------------------------------------------------------------------

def test_the_top_level_puts_folders_first(probe):
    """A cohort is found by its name, not by scrolling past forty slides."""
    assert "folders come before the projects that are in none" in probe, probe
    assert "a project inside a dataset is not also shown at the top level" in probe, probe
    assert "an empty folder is still drawn" in probe, probe


def test_a_folder_shows_only_what_is_in_it(probe):
    assert "opening a folder shows its members and nothing else" in probe, probe
    assert "and no folder cards, because datasets do not nest" in probe, probe
    assert "the crumb says where you are" in probe, probe


def test_where_you_are_survives_a_reload(probe):
    """The URL carries the folder, so a refresh and a pasted link both land in
    the same place. `replaceState`, not `pushState`: Back on this page means
    "go back to where I came from", not "up one folder"."""
    assert "and the URL does too, so a reload lands in the same place" in probe, probe
    assert "leaving takes the folder back out of the URL" in probe, probe
    assert "a pasted link opens inside the folder it names" in probe, probe


def test_search_looks_everywhere(probe):
    """Somebody who is searching has stopped navigating. A search that looked
    only in the current folder would silently hide what they wanted."""
    assert "search reaches outside the folder you are standing in" in probe, probe
    assert "a search result tags the dataset each project is in" in probe, probe


# --------------------------------------------------------------------------
# Selecting and moving
# --------------------------------------------------------------------------

def test_a_selection_is_what_gets_moved(probe):
    """Dragging one card of five selected moves all five; dragging one that is
    not selected moves that one and leaves the selection alone. The rule every
    file browser uses, and the one people rely on without noticing."""
    assert "dragging a selected card carries the whole selection" in probe, probe
    assert "dragging an unselected card moves only that one" in probe, probe


def test_a_move_is_one_request(probe):
    """Half a move that failed in the middle is a state nothing on this page
    can draw."""
    assert "dropping on a folder is one request for the whole lot" in probe, probe


def test_the_drag_payload_is_ours_alone(probe):
    """A custom MIME type, so a drag from anywhere else does nothing at all
    rather than being parsed as a project list."""
    assert "a drag from somewhere else is ignored" in probe, probe


def test_selecting_works_by_tick_and_by_modifier(probe):
    assert "the tick selects without opening" in probe, probe
    assert "ctrl-click selects rather than opening" in probe, probe
    assert "shift-click extends the range" in probe, probe


def test_what_cannot_be_done_is_not_offered(probe):
    """A shared project belongs to whoever provisioned the root it sits on and
    the server 403s a delete. Finding that out after confirming a dialog is the
    failure this prevents."""
    assert "Delete is refused for a shared project before it is asked for" in probe, probe
    assert "a shared project is never posted for deletion, dialog or not" in probe, probe
    assert "and Remove from dataset is hidden for one in none" in probe, probe


def test_a_card_says_what_the_project_has(probe):
    assert "a project whose data is undecided says so" in probe, probe
    assert "a converting mask is shown dimmed rather than omitted" in probe, probe
    assert "an image-only project is not flagged" in probe, probe


def test_an_emptied_folder_is_still_somewhere_to_drop_things(probe):
    """The panel says "drag projects here". A sentence that invites a gesture
    and then ignores it is worse than not offering it -- and an emptied dataset
    with no way back into it is a dataset you have to delete and remake."""
    assert "an emptied folder shows a panel rather than 'no results'" in probe, probe
    assert "and that panel is a real drop target for the folder it stands for" in probe, probe
    assert "dropping on it moves the project in" in probe, probe
    assert "and the panel carries no folder at the top level" in probe, probe


def test_the_page_cleans_up_after_itself(probe):
    """Open Project is mounted as a fragment by the app shell and torn down on
    every navigation. The Escape handler is the only thing here bound to
    `document` rather than to an element that goes with the fragment."""
    assert "Escape is listened for on the document" in probe, probe
    assert "and the page takes its listener off when it is torn down" in probe, probe


# --------------------------------------------------------------------------
# The contract with the pages that borrow these classes
# --------------------------------------------------------------------------

def test_the_card_classes_figure_builder_borrows_are_unchanged(probe):
    """Figure Builder's library and captures pages render `.project-card`,
    `.project-thumb` and `.project-actions` from their own markup and link this
    stylesheet. Everything the redesign added is a new class.

    Checked twice, because the two halves can fail independently: the probe
    asserts this page still EMITS them, and the stylesheet has to go on
    defining them whether or not this page uses them -- Figure Builder's
    markup is where they are written by hand."""
    assert "a project card still carries every class other pages borrow" in probe, probe
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    for selector in (".project-card", ".project-card-link", ".project-card-name",
                     ".project-card-date", ".project-thumb", ".project-actions",
                     ".project-action"):
        assert selector in stylesheet, selector


def test_every_id_the_controller_reads_is_in_the_template():
    """The controller returns early when `#project-results` is absent, so a
    renamed id elsewhere fails silently -- the page renders and one control
    does nothing."""
    markup = TEMPLATE.read_text(encoding="utf-8")
    source = CONTROLLER.read_text(encoding="utf-8")
    for element_id in set(re.findall(r'getElementById\("([a-z-]+)"\)', source)):
        assert f'id="{element_id}"' in markup, element_id


def test_the_dialog_primitives_live_where_every_page_can_use_them():
    """confirmDialog.js is loaded from base.html, and the app shell disables a
    page's own stylesheet when it navigates away -- so a `.plx-` rule in
    openProject.css would leave the dialog unstyled on every other page."""
    assert "confirmDialog.js" in BASE.read_text(encoding="utf-8")
    main = MAIN_CSS.read_text(encoding="utf-8")
    for selector in (".plx-dialog", ".plx-dialog-actions", ".plx-confirm",
                     ".plx-button", ".plx-prompt-input"):
        assert selector in main, selector


def test_the_bootstrap_delete_modal_is_gone():
    """One dialog mechanism on the page. The Bootstrap modal needed markup in
    the template for every question the page might ask, a data-attribute
    handshake to open it and a `relatedTarget` to say which project it was
    about -- and its confirm button could only ever ask one thing."""
    markup = TEMPLATE.read_text(encoding="utf-8")
    assert "deleteProjectModal" not in markup
    assert "data-bs-toggle" not in markup
