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


# -- the split button --------------------------------------------------------
#
# A desktop that cannot answer "a file OR a folder" in one panel is not a
# desktop with no picker. These are the four ways that distinction gets lost.


def test_the_control_replaces_the_browse_button_rather_than_opening_from_it():
    """It IS the button. On Windows and Linux the file-or-folder question
    cannot be avoided, so asking it up front is one click where a menu hanging
    off Browse was two."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert "function applyCapability(" in browse
    assert 'if (mode === "any") {' in browse
    assert "applyCapability(buttonEl, inputEl, examples || filter" in browse
    # Swapped in place, with the button kept rather than destroyed -- the
    # answer can change back when the Local/Remote switch moves the field.
    assert "buttonEl.after(split)" in browse
    assert "buttonEl.hidden = true" in browse
    assert "buttonEl.hidden = false" in browse


def test_only_a_field_that_takes_either_kind_is_split():
    """A field asking for a file, or for a folder, has nothing to split: one
    button already says which. Splitting those would invent a choice."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert 'if (mode === "any") {' in browse


def test_the_control_follows_the_local_remote_switch():
    """That switch points a mounted field at a different computer. A laptop's
    File/Folder pair left over on a field now aimed at a cluster would offer
    two routes to the same in-app listing."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert 'inputEl.addEventListener("change", ask)' in browse
    # And a late answer for the machine the field has since moved off is
    # dropped rather than applied.
    assert "if (nodeNow() === at) render(kind)" in browse


def test_a_capability_failure_is_not_remembered():
    """Caching a network blip would leave that field on the substitute picker
    for the rest of the session, on a machine with a perfectly good dialog."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert "capabilityCache.delete(key)" in browse


def test_a_single_kind_desktop_reaches_a_native_dialog_rather_than_the_listing():
    """The regression this exists for: mode "any" arrived, every non-Mac
    desktop refused it, the refusal said "list", and Windows quietly lost its
    system file browser to an in-app listing it never needed."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert 'result.fallback === "kinds" && anchorEl' in browse
    # ...and what the menu picks is re-posted as an ordinary single-kind
    # request, which is the whole point: that one opens the real dialog.
    assert "return browseForPath({mode: kind" in browse


def test_every_browse_button_is_somewhere_for_the_menu_to_hang():
    """The menu is anchored to the button, so a button that does not pass
    itself gets the listing picker instead -- silently, and looking for all the
    world like the platform simply cannot do better."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert "anchorEl: buttonEl" in browse


def test_the_home_page_never_needs_an_anchor_because_it_never_asks_late():
    """The one caller that has no anchor to give, and needs none.

    "kinds" comes back only for mode "any" (see native_dialog.py), and the home
    page never sends it: each half of its Select File / Select Folder pair
    passes its own kind straight through. So the popup that would need
    somewhere to hang cannot arise -- which is the point. It used to arise on
    every machine but a Mac, and it opened where the swapped-out control had
    been rather than where the eye was.
    """
    landing = source("src", "js", "views", "quickViewLanding.js")
    # The passed property, not the word -- which this file's own comments use
    # to explain why it is absent.
    assert "anchorEl:" not in landing
    assert "async function browseForImage(mode)" in landing

def test_a_hidden_anchor_does_not_pin_the_menu_to_the_corner():
    """getBoundingClientRect() on a hidden element is four zeros, and
    `applyCapability` hides the button it replaces rather than removing it. So
    the one caller that anchored to a button on a page where that button had
    been swapped opened its menu at the top-left corner of the window -- drawn
    correctly, a whole page from the click, and reliably missed.

    The call site is fixed; this is the guard that stops the next one.
    """
    browse = source("src", "js", "services", "browsePicker.js")
    assert "function anchorBox(anchorEl)" in browse
    assert "if (box.width || box.height) return box;" in browse
    # And the fallback is the control that REPLACED it, which is the thing
    # actually on screen at the position the user is looking at.
    assert '.parentElement?.querySelector(".browse-kind-split")' in browse
    # Used, not merely defined.
    assert "const box = anchorBox(anchorEl);" in browse


def test_the_home_panel_is_the_halves_on_every_platform():
    """The home page's primary action is the shared File/Folder control, built
    once and unconditionally -- not a single target that a capability probe
    swaps for the halves after the page has loaded.

    The probe existed to decide whether the question could be avoided, and only
    macOS could avoid it. Asking it up front costs macOS one decision and buys
    every platform the same page, no round trip on load, and no popup that can
    open away from the click.
    """
    landing = source("src", "js", "views", "quickViewLanding.js")
    css = source("src", "css", "main.css")
    # Built from the shared control, so the halves, their icons and the format
    # examples under them stay defined in exactly one place.
    assert 'buildSplitControl("image", browseForImage,' in landing
    assert 'panel.classList.add("is-panel");' in landing
    # And no longer swapped in by the probe.
    assert "applyCapability" not in landing
    assert ".browse-kind-split.is-panel {" in css
    # A variant of the one control, not a second component: only size and
    # arrangement are restated.
    assert ".browse-kind-split.is-panel .browse-kind-half {" in css


def test_the_home_panel_is_a_control_rather_than_a_drop_target():
    """It inherited a 2px dashed edge from the dropzone it replaced. A dashed
    rectangle says "drop something here", and this page has never had a
    dragover or drop handler to accept one -- it is two buttons."""
    import re

    landing = source("src", "js", "views", "quickViewLanding.js")
    css = source("src", "css", "main.css")
    assert "dragover" not in landing
    panel = css.split(".browse-kind-split.is-panel {")[1].split("}")[0]
    # Declarations only: the comment above them explains the dashed edge this
    # replaced, and would match the very word being ruled out.
    panel = re.sub(r"/\*.*?\*/", "", panel, flags=re.S)
    assert "dashed" not in panel
    assert "border: 1px solid var(--border-strong);" in panel


def test_the_home_page_asks_which_machine_once_for_the_whole_page():
    """Everywhere else the Local/Remote switch sits inside the row of the one
    field it governs. Here it governs two controls -- the File/Folder pair and
    the path box -- so it is mounted above them both, which is what `mount`
    and `statusMount` exist for. Two switches on a page that takes one image
    would be the same question asked twice."""
    landing = source("src", "js", "views", "quickViewLanding.js")
    location = source("src", "js", "services", "dataLocation.js")
    assert "mount: whereMount," in landing
    assert "statusMount: whereStatus," in landing
    # The service honours them, and still defaults to the in-row placement
    # every form field depends on.
    assert "if (options.mount) options.mount.appendChild(root);" in location
    assert "else row.insertBefore(root, row.children[0]);" in location
    assert "(options.statusMount || field).appendChild(status);" in location

def test_the_panel_halves_stop_taking_clicks_while_an_image_loads():
    """The dropzone was disabled by pointer-events for the length of a load.
    The panel is two real buttons, which that trick does not reach -- and a
    second press mid-load submits the same slide over again."""
    landing = source("src", "js", "views", "quickViewLanding.js")
    css = source("src", "css", "main.css")
    assert 'panel.querySelectorAll(".browse-kind-half")' in landing
    assert "halves.forEach((half) => { half.disabled = busy; });" in landing
    # And it has to look disabled: a half that still lights up under the
    # pointer is inviting exactly the click it will not accept.
    assert ".browse-kind-half:disabled {" in css
    assert ".browse-kind-half:disabled:hover {" in css

def test_a_caller_with_no_anchor_still_gets_a_picker():
    """`browseForPath` is callable without a button, and "kinds" must not be a
    dead end for one: the listing answers, exactly as it did before."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert ('(result.fallback === "list" || result.fallback === "kinds")'
            in browse)


def test_the_chooser_says_which_formats_are_which():
    """The whole reason "file or folder?" is answerable: it is a question about
    the FORMAT, not about the user. An OME-Zarr is a directory and an OME-TIFF
    is a file, and the control says so rather than expecting it to be known.

    Keyed by filter, because the example that helps on the image field is a
    confident lie on the Data one -- which takes a .csv or an .h5ad and has
    never taken a .svs in its life.
    """
    browse = source("src", "js", "services", "browsePicker.js")
    assert 'image: { file: ".ome.tiff · .svs", directory: ".ome.zarr · dicom" }' in browse
    assert 'data: { file: ".csv · .h5ad", directory: "SpatialData (.zarr)" }' in browse
    # And the mask, which shares filter "image" with the image field and must
    # NOT share its examples: nobody has a segmentation mask in .svs.
    assert 'mask: { file: ".ome.tiff · .tiff", directory: ".ome.zarr · dicom" }' in browse
    # Chosen at open time, by a name of its own rather than by the filter.
    assert "chooseKind(anchorEl, examples || filter)" in browse
    assert "KIND_EXAMPLES[examples] || {}" in browse


def test_the_chooser_is_one_control_rather_than_two_buttons():
    """A shared outer border and radius with a hairline between the halves --
    not two adjacent buttons. `overflow: hidden` is what does it: it clips each
    half's hover to the container's rounded corners, so the halves carry no
    radius of their own and the seam stays a single line."""
    css = source("src", "css", "main.css")
    assert ".browse-kind-split {" in css
    assert ".browse-kind-half + .browse-kind-half {" in css
    # Equal halves regardless of which example string is longer.
    assert "flex: 1 1 0;" in css
    for rule in ("overflow: hidden;", "border-radius: var(--radius-md);"):
        assert rule in css.split(".browse-kind-split {")[1].split("}")[0] + "}", rule


def test_the_icons_are_the_app_s_icons_rather_than_emoji():
    """Emoji render in their own colours whatever the surface, which put two
    full-saturation glyphs in a UI whose every other icon is a muted outline.
    Font Awesome spans, styled after the View menu's icons -- which is where
    the app already settled the question of an icon that labels a row without
    competing with it."""
    browse = source("src", "js", "services", "browsePicker.js")
    css = source("src", "css", "main.css")
    assert 'icon.className = `fas ${glyph} browse-kind-icon`' in browse
    assert '["file", labels?.file || "File", "fa-file"]' in browse
    assert '["directory", labels?.directory || "Folder", "fa-folder"]' in browse
    # Muted, and no emoji left behind on the label it used to hang off.
    assert ".browse-kind-icon {" in css
    assert "1F4C4" not in css
    icon = css.split(".browse-kind-icon {")[1].split("}")[0]
    assert "color: var(--text-muted)" in icon


def test_the_floating_chooser_focuses_something_that_exists():
    """The class was renamed under this selector and the popup quietly stopped
    taking focus -- it opened with the keyboard still on the page behind it,
    which no test noticed because nothing throws when querySelector finds
    nothing."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert ".browse-kind-item" not in browse
    assert 'menu.querySelector(".browse-kind-half")?.focus();' in browse


def test_the_menu_opens_inside_the_modal_that_asked_for_it():
    """Two of the Browse buttons live in a <dialog> opened with showModal(),
    which the browser puts in the TOP LAYER -- above every z-index on the page,
    including one raised to clear it. A menu appended to <body> is drawn behind
    that modal: present, positioned correctly, and invisible.

    Esc has the same shape of problem. The dialog closes on it by default, so a
    keydown handler that does not preventDefault dismisses the whole modal when
    the user meant to dismiss the menu.
    """
    browse = source("src", "js", "services", "browsePicker.js")
    assert 'anchorEl.closest?.("dialog[open]") || document.body' in browse
    assert "event.preventDefault()" in browse

    # And the two modals that make this real, so a third one arriving without
    # a Browse button does not quietly make this test vacuous.
    for view in ("requirementsModal.js", "channelNamesUpload.js"):
        assert "showModal()" in source("src", "js", "views", view), view


def test_dismissing_the_menu_is_a_cancel_rather_than_a_failure():
    """Closing the menu is the same answer as closing the dialog it would have
    opened. Routing it to onUnavailable would put "could not open the file
    browser" in front of somebody who just changed their mind."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert "if (!kind) return;" in browse
