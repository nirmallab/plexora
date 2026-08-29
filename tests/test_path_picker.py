"""Browsing a machine that has no desktop.

The behaviour is in tests/js/path_picker_probe.mjs, run in node below, because
nothing in the Python suite executes client JS. What is left here is the wiring
-- the facts spread across several files that all have to agree before any of
that behaviour is reachable at all:

  base.html          loads the picker, before the button that falls back to it
  browsePicker.js    hands it the field's current value and the node to list
  dataLocation.js    listens for the event browsePicker fires afterwards
  pathPicker.js      does no path arithmetic of its own

The server halves are tests/test_browse_routes.py (/list_dir and
/picker_prefs) and tests/test_node_dynamic.py (the same listing, relayed).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "path_picker_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


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


# -- every path came from the server -----------------------------------------


def test_the_client_never_builds_a_path_of_its_own(probe):
    """The Windows fix, and the reason /list_dir sends `entry.path` and
    `crumbs` at all.

    The picker used to join a name onto the directory it was standing in with
    a "/", and walk up by chopping the last segment off with a regex. Both are
    right until the node on the other end is a Windows box -- then opening a
    folder under `C:\\data` asks for `C:\\data/runs`, and Up from `C:\\data`
    asks for the empty string, which lists the user's home."""
    assert "opening a folder asks for the path the SERVER gave that row" in probe, probe
    assert "Up follows the server's own parent, separators and all" in probe, probe
    assert "a Windows node opens where it was asked to" in probe, probe
    assert "...and at the top of the tree Up is not offered" in probe, probe


def test_the_picker_does_no_path_surgery():
    """A regression guard with teeth: the two expressions that were wrong are
    gone from the file, rather than merely unused."""
    picker = source("src", "js", "services", "pathPicker.js")
    assert "replace(/\\/+$/" not in picker
    assert "replace(/\\/[^/]*$/" not in picker


# -- getting somewhere -------------------------------------------------------


def test_the_address_bar_is_both_a_trail_and_a_box(probe):
    """The HPC gesture is paste-a-path-and-press-Enter: somebody who knows
    their data is in /n/groups/lab/2024-03-scans should not have to click
    through six directories to say so. Breadcrumbs are for the other half of
    the job -- finding out where you already are, and stepping back up.

    Which half you get is decided by where in the bar you click, so the bar
    has to be a big enough thing to click AT. The gesture was first hung on a
    pencil glyph at the end of the trail: correct, and the width of a pencil.
    """
    assert "the crumb trail is the server's, drawn as buttons" in probe, probe
    assert "clicking a crumb goes to the path that crumb carries" in probe, probe
    assert "clicking the address bar turns the trail into a box, pre-filled" in probe, probe
    assert "...with the path selected, so a pasted one replaces it" in probe, probe
    assert "clicking inside the box being edited leaves the text alone" in probe, probe
    assert "a crumb inside the bar navigates rather than opening the box" in probe, probe
    assert "a typed path is trimmed and gone to -- the HPC gesture" in probe, probe


def test_back_and_refresh_move_through_server_answers_only(probe):
    assert "Back is dead until there is somewhere to go back to" in probe, probe
    assert "Back returns to the previous directory" in probe, probe
    assert "Refresh re-reads where you are without adding a step back" in probe, probe


def test_a_listing_that_fails_changes_nothing(probe):
    """`state.here` is assigned in exactly one place, from a server answer.
    Being thrown back to your home directory because you mistyped one folder
    name is a worse outcome than the mistake."""
    assert "a folder that cannot be read leaves you where you were" in probe, probe
    assert "...and says why, in the picker rather than the console" in probe, probe


def test_it_opens_where_the_user_already_is(probe):
    """Three answers in order: what the field holds, where this machine was
    left last time, home. A locator is none of them -- a field in verbatim mode
    holds `node://laptop/cells-7f3a91c2`, which is an address."""
    assert "a field's current value is handed over as-is" in probe, probe
    assert "a node locator is not a path, and is not opened as one" in probe, probe
    assert "with nothing to go on it opens where this machine was left" in probe, probe
    assert "a remembered folder that has since been deleted falls back home" in probe, probe
    assert "...and says that is what happened" in probe, probe


# -- reading one directory ---------------------------------------------------


def test_a_folder_can_be_filtered_where_you_stand(probe):
    """No request, and no glob: this narrows what was already fetched. Which is
    also why the count says which entries it searched -- "0 of 2000 shown"
    reads as "your file is not there" when it means "your file is past the
    cut"."""
    assert "typing narrows the listing in place, no request" in probe, probe
    assert "...and says how much of the folder that is" in probe, probe
    assert "a filter belongs to the folder it was typed in" in probe, probe
    assert "...and a filter over a cut-off folder says WHICH entries it read" in probe, probe


def test_escape_in_a_box_does_not_close_the_picker(probe):
    """A <dialog> cancels itself on Escape, which the browser does before
    anything here runs. Clearing a filter took the whole picker with it until
    both the default and the propagation were stopped."""
    assert "Esc empties the filter instead of closing the picker" in probe, probe
    assert "...and is stopped from reaching the dialog" in probe, probe
    assert "Esc in the path box restores the crumbs" in probe, probe
    assert "...and is stopped, or the browser closes the whole dialog" in probe, probe


def test_hidden_files_are_a_question_asked_of_the_server(probe):
    """Not a client-side filter: the server skips them while scanning, so the
    2000-entry limit is not spent on a `.snakemake` directory nobody asked
    for."""
    assert "hidden files are off to begin with" in probe, probe
    assert "asking for them re-reads the same folder, hidden ones included" in probe, probe


def test_each_row_says_what_it_is(probe):
    assert "the Type column names the thing, not the extension" in probe, probe
    assert "...and a size is only ever on a file" in probe, probe
    assert "a file the field cannot take is shown, greyed" in probe, probe
    assert "...and clicking it selects nothing" in probe, probe


def test_the_listing_can_be_walked_from_the_keyboard(probe):
    assert "the listing is a listbox of options" in probe, probe
    assert "arrows walk the listing and say where they are" in probe, probe
    assert "Backspace is Up" in probe, probe
    assert "...but not while somebody is typing in a box" in probe, probe
    assert "Enter on a folder opens it" in probe, probe
    assert "Enter on a file chooses it and closes" in probe, probe
    assert "Enter on a focused button belongs to the browser" in probe, probe


# -- what comes back ---------------------------------------------------------


def test_choosing_resolves_with_what_the_caller_asked_for(probe):
    """`multiple` is answered but unwired: no field sets it yet, and the point
    of pinning it here is that the first one to need it does not have to
    reopen this file."""
    assert "choosing a file resolves with its server-given path" in probe, probe
    assert "standing in a folder IS choosing it" in probe, probe
    assert "cmd-click adds to the selection" in probe, probe
    assert "shift-click takes the run between them" in probe, probe
    assert "without `multiple` the answer is one path, as it always was" in probe, probe


def test_places_are_remembered_per_machine(probe):
    """/n/scratch/aj means nothing on the laptop, so the record is keyed by
    node -- and it is written once per picker session rather than once per step
    through the tree, because it is a file on disk."""
    assert "saved places are offered by name, home first" in probe, probe
    assert "...and clicking one goes to the whole path behind it" in probe, probe
    assert "pinning asks the server to keep it, per machine" in probe, probe
    assert "un-pinning asks the server to forget it" in probe, probe
    assert "...and records the folder once, with both facts in one write" in probe, probe


def test_where_you_got_to_survives_closing_the_picker(probe):
    """The write is hung on the dialog's `close` rather than on a successful
    pick, which is the only thing Esc and a click on the backdrop both go
    through -- and, more to the point, browsing is what costs the effort.
    Walking six directories into /n/scratch, not finding the file and closing
    the picker should not mean walking all six again.

    Two things stay off it: `add_recent`, because Recent is a list of places
    that turned out to be worth something rather than a history; and any write
    at all when nothing moved, because this is a read-modify-write of a file
    on disk."""
    assert "cancelling resolves with nothing, and still remembers the folder" in probe, probe
    assert ("...but nothing was taken from it, so Recent does not claim it was"
            in probe), probe
    assert "opening where it was left and closing again writes nothing" in probe, probe


def test_remembering_places_never_blocks_browsing(probe):
    """A picker that will not open because a preferences file could not be
    read is a much worse failure than one with no Recent list."""
    assert ("a preferences file that cannot be read costs the Recent list, "
            "not the picker") in probe, probe


def test_a_node_too_old_to_send_paths_still_works(probe):
    """Reduces to what the picker did before this: a text box for the path and
    names joined onto the directory it is standing in."""
    assert "a node that sends no crumbs leaves the path box in place of them" in probe, probe
    assert "...and a row with no path of its own is still openable" in probe, probe


def test_browsing_a_node_lists_that_node(probe):
    assert "browsing a node lists THAT machine, and nothing else would do" in probe, probe


# -- the wiring around it ----------------------------------------------------


def test_the_picker_is_loaded_before_the_button_that_falls_back_to_it():
    base = source("templates", "base.html")
    assert base.index("services/pathPicker.js") < base.index("services/browsePicker.js")


def test_browse_hands_the_picker_the_field_it_is_filling():
    """Both halves are read at click time, because both change while the form
    is open: the switch decides whose filesystem is listed, and the box may
    have been typed into since the button was wired."""
    picker = source("src", "js", "services", "browsePicker.js")
    assert "start: inputEl.value.trim()" in picker
    assert 'typeof node === "function" ? node() : node' in picker


def test_a_browse_filled_box_tells_the_switch_it_changed():
    """Assigning to `input.value` fires nothing, and `change` is the event the
    Local/Remote switch waits for -- sharing a file with another machine is not
    something to do per keystroke. Without it, browsing to a file on a cluster
    filled the box and shared nothing, and the form posted an empty locator."""
    browse = source("src", "js", "services", "browsePicker.js")
    location = source("src", "js", "services", "dataLocation.js")
    assert 'new Event("change"' in browse
    assert 'addEventListener("change"' in location
