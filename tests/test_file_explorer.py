"""The file explorer: what every Browse opens, on this machine or a node.

The behaviour is in tests/js/file_explorer_probe.mjs, run in node below,
because nothing in the Python suite executes client JS. What is left here is
the wiring -- the facts spread across several files that all have to agree
before any of that behaviour is reachable at all:

  base.html          loads the six explorer modules, in order, before the
                     button that opens them, and explorer.css on every page
  browsePicker.js    opens the explorer first, with the field's current value,
                     the node to list, and the machine's own dialog as a hook
  dataLocation.js    listens for the event browsePicker fires afterwards
  services/explorer  does no path arithmetic of its own

The server halves are tests/test_browse_routes.py (/list_dir, /stat_entries,
/fs_places, /search_dir, /picker_prefs), tests/test_dir_listing.py,
tests/test_fs_places.py, tests/test_dir_search.py and
tests/test_node_dynamic.py (the same routes, relayed).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "file_explorer_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"
EXPLORER = CLIENT / "src" / "js" / "services" / "explorer"
#: base.html's order, which is also the order the probe loads them in.
MODULES = ("explorerTypes.js", "floatingWindow.js", "explorerListing.js",
           "explorerCrumbs.js", "explorerSidebar.js", "fileExplorer.js")


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


def module(name):
    return (EXPLORER / name).read_text(encoding="utf-8")


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


@pytest.mark.parametrize("name", ("fileExplorer.js", "explorerCrumbs.js",
                                  "explorerListing.js", "explorerSidebar.js"))
def test_the_picker_does_no_path_surgery(name):
    """A regression guard with teeth: the two expressions that were wrong are
    absent from every module that handles a path, rather than merely unused."""
    picker = module(name)
    assert "replace(/\\/+$/" not in picker
    assert "replace(/\\/[^/]*$/" not in picker


def test_the_folder_on_screen_is_assigned_in_one_place():
    """`state.here` changes only from a server answer, in show(). A second
    assignment is how a failed listing would start moving the user."""
    controller = module("fileExplorer.js")
    assert controller.count("state.here = ") == 1
    assert "state.here = payload.path;" in controller


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
    assert "Forward is live after Back" in probe, probe
    assert "going somewhere new forgets the way forward" in probe, probe


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


def test_the_explorer_is_loaded_in_order_before_the_button_that_opens_it():
    base = source("templates", "base.html")
    order = [base.index(f"services/explorer/{name}") for name in MODULES]
    assert order == sorted(order), "the explorer modules are out of order in base.html"
    assert order[-1] < base.index("services/browsePicker.js")
    for name in MODULES:
        assert f'services/explorer/{name}?v=' in base and "defer" in base.split(
            f"services/explorer/{name}")[1].split("\n")[0], name


def test_the_explorer_s_stylesheet_is_on_every_page():
    """In base.html beside main.css, not in a page block: the explorer opens
    from every page, and appRouter.adoptStyles disables page stylesheets on
    navigation."""
    base = source("templates", "base.html")
    head = base.split("{% block style %}")[0]
    assert "css/explorer.css?v=" in head
    assert head.index("css/main.css") < head.index("css/explorer.css")


def test_the_old_picker_is_gone():
    """One explorer, not two: the old file, its stylesheet block and its
    global as anything but an alias."""
    assert not (CLIENT / "src" / "js" / "services" / "pathPicker.js").exists()
    assert ".path-picker" not in source("src", "css", "main.css")
    assert "services/pathPicker.js" not in source("templates", "base.html")
    controller = module("fileExplorer.js")
    assert "window.PlexoraPathPicker = window.PlexoraFileExplorer;" in controller


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


# -- one Browse, either kind ---------------------------------------------------
#
# A field asking for "your image" or "your mask" does not ask whether it is a
# file or a folder -- that is a fact about the format -- so mode "any" opens the
# one panel that takes both, on every machine: macOS's own, and everywhere else
# the in-app listing. A field asking for exactly one kind still gets that
# machine's own single-kind dialog.


def test_browse_is_one_button_on_every_machine():
    """No File | Folder pair swapped in for the button on Windows and Linux:
    the pair made the user say which kind of thing their format is before
    they could point at it, and the sidebar's Add Seg Mask could not take a
    folder at all where the swap had not happened."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert "function applyCapability(" not in browse
    assert "applyCapability(" not in browse
    assert 'buttonEl.addEventListener("click", () => go(mode));' in browse


def _function(text, name):
    """One top-level function's text, up to the next top-level declaration."""
    start = text.index(f"function {name}(")
    rest = text[start:]
    end = re.search(r"\n(?:async )?function |\n/\*\*", rest[1:])
    return rest[: end.start() + 1] if end else rest


def test_every_browse_opens_the_explorer_first():
    """On every machine, in a tab and in the desktop app: the explorer is the
    answer, and the machine's own dialog is a hook it offers in its overflow
    menu. A Browse that opened Finder on one machine and a listing on another
    kept no pins, no Recent and no window size between the two."""
    browse = source("src", "js", "services", "browsePicker.js")
    body = _function(browse, "browseForPath")
    assert "window.PlexoraFileExplorer" in body
    assert body.index("window.PlexoraFileExplorer") < body.index("openSystemDialog(")
    assert "systemDialog: systemDialogFor(" in body
    # The explorer branch asks nothing first: no anchor, no capability.
    explorer_branch = body.split("if (explorer) {")[1].split("return;")[0]
    assert "anchorEl" not in explorer_branch.replace("anchorEl, examples", "")
    assert "browseCapability" not in explorer_branch
    # Only the system dialog posts /browse_path.
    for name in ("browseForPath", "systemDialogFor", "attachBrowseButton"):
        assert 'plexoraUrl("browse_path")' not in _function(browse, name), name
    assert 'plexoraUrl("browse_path")' in _function(browse, "openSystemDialog")
    # The desktop app on a Mac still reaches the panel that takes either kind.
    assert 'if (await serverDialogs() === "hybrid") {' in browse


def test_a_capability_failure_is_not_remembered():
    """Caching a network blip would leave that field on the substitute picker
    for the rest of the session, on a machine with a perfectly good dialog."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert "capabilityCache.delete(key)" in browse


def test_a_single_kind_desktop_reaches_a_native_dialog_from_the_explorer():
    """"System file browser" on a Windows or Linux desktop asked for either
    kind asks which first -- the chooser hung under the button itself, inside
    the explorer -- and only then closes the explorer and opens that
    single-kind dialog on the right machine."""
    browse = source("src", "js", "services", "browsePicker.js")
    hook = _function(browse, "systemDialogFor")
    assert "kind = await chooseKind(anchor || anchorEl, examples || filter);" in hook
    assert "if (!kind) return null;" in hook
    # The explorer closes after the question, never before it.
    assert hook.index("if (!kind) return null;") < hook.index("if (onChosen) onChosen();")
    assert "return openSystemDialog({kind, filter, node, start, multiple});" in hook
    # The capability is the machine's, asked when the explorer opens.
    assert "await browseCapability(node)" in hook
    # The request names the machine whose dialog to open.
    assert "body: JSON.stringify({mode: kind, filter: filter, node: node})" in browse


def test_every_browse_button_is_somewhere_for_the_menu_to_hang():
    """The menu is anchored to the button, so a button that does not pass
    itself gets the listing picker instead -- silently, and looking for all the
    world like the platform simply cannot do better."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert "anchorEl: buttonEl" in browse


def test_the_import_dialog_never_needs_an_anchor_because_it_never_asks_late():
    """The one caller that has no anchor to give, and needs none.

    "kinds" comes back only for mode "any" (see native_dialog.py), and the
    Import Sample dialog never sends it: each half of its Select File / Select
    Folder pair passes its own kind straight through. So the popup that would
    need somewhere to hang cannot arise -- which is the point. It used to arise
    on every machine but a Mac, and it opened where the swapped-out control had
    been rather than where the eye was.
    """
    dialog = source("src", "js", "views", "importSample.js")
    # The passed property, not the word -- which this file's own comments use
    # to explain why it is absent.
    assert "anchorEl:" not in dialog
    assert "function pickWith(mode)" in dialog

    # The home page is the other one, for the same reason: its File / Folder
    # tabs pass their own kind straight through. It is a separate surface, so
    # it is separately asserted.
    landing = source("src", "js", "views", "quickViewLanding.js")
    assert "anchorEl:" not in landing
    assert "async function browseForImage(mode)" in landing
    assert "browseForImage(kind);" in landing

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


def test_the_home_page_asks_up_front_and_the_dialog_does_not_ask_at_all():
    """The home page keeps its File | Folder tabs above one path box -- its
    one control is the whole page, and the chips under it are read from the
    same table the chooser's example lines are. The import dialog asks
    nothing: its Browse takes either kind, so a scoped "+ Add Layer" -- the
    sidebar's Add Seg Mask included -- can be pointed at an OME-Zarr folder
    or a TIFF alike."""
    landing = source("src", "js", "views", "quickViewLanding.js")
    index = source("templates", "index.html")
    assert 'data-kind="file"' in index and 'data-kind="directory"' in index
    assert "KIND_EXAMPLES.image" in landing
    assert "applyCapability" not in landing

    dialog = source("src", "js", "views", "importSample.js")
    css = source("src", "css", "main.css")
    assert "plx-import-kind" not in dialog
    assert 'pickWith("any")' in dialog
    assert "KIND_EXAMPLES[examples]" in dialog
    assert "applyCapability" not in dialog
    assert ".plx-import-pick[data-kind=\"any\"] .plx-import-chip {" in css


def test_the_halves_are_a_control_and_the_drop_target_is_separate():
    """The split control once inherited a 2px dashed edge from a dropzone it
    replaced. A dashed rectangle says "drop something here", and two buttons
    are not that.

    The import dialog DOES take a drop -- for small tables, which a browser can
    hand over as bytes -- on its home-style pick control, whose placeholder
    says what it takes and whose field lights up while something is held over
    it. The panel-sized variant of the halves it used to be is gone with it.
    """
    import re

    dialog = source("src", "js", "views", "importSample.js")
    css = source("src", "css", "main.css")
    assert "pick.addEventListener(\"dragover\"" in dialog
    assert ".plx-import-pick.is-over .plx-import-field" in css
    split = css.split(".browse-kind-split {")[1].split("}")[0]
    # Declarations only: a comment may explain the dashed edge it replaced.
    split = re.sub(r"/\*.*?\*/", "", split, flags=re.S)
    assert "dashed" not in split
    assert "border: 1px solid var(--border-strong);" in split
    for text in (css, dialog, source("src", "js", "services", "browsePicker.js")):
        assert "is-panel" not in text


def test_both_surfaces_ask_which_machine_once_for_the_whole_surface():
    """Everywhere else the Local/Remote chip sits inside the path box of the
    one field it governs. The home page and the import dialog each take one pick,
    so each asks once, as a chip at the front of its one path box that it
    draws itself (`bare`), with the service's status line under the box. Two
    switches on a surface that takes one pick would be the same question
    asked twice."""
    # The home page asks it as a chip in its one path box -- the one surface
    # whose single field IS the whole pick -- and draws that chip itself.
    landing = source("src", "js", "views", "quickViewLanding.js")
    assert "bare: true," in landing
    assert "statusMount: whereStatus," in landing

    dialog = source("src", "js", "views", "importSample.js")
    location = source("src", "js", "services", "dataLocation.js")
    assert "bare: true," in dialog
    assert "statusMount: machine.status," in dialog
    # The service honours them, and still defaults to the in-box chip every
    # form field depends on.
    assert "const chip = !options.bare && Chip ? Chip.create({" in location
    assert "if (chip) Chip.wrap(input, chip);" in location
    assert "(options.statusMount || field).appendChild(status);" in location

def test_the_panel_halves_stop_taking_clicks_while_an_image_loads():
    """The dropzone was disabled by pointer-events for the length of a load.
    The panel is two real buttons, which that trick does not reach -- and a
    second press mid-load submits the same slide over again."""
    landing = source("src", "js", "views", "quickViewLanding.js")
    dialog = source("src", "js", "views", "importSample.js")
    css = source("src", "css", "main.css")
    # The home page's File / Folder tabs, which choose what Browse opens.
    assert "option.disabled = busy();" in landing
    assert ".quick-view-kind-option:disabled {" in source("src", "css", "quickView.css")
    # The dialog's Browse button, held for exactly this.
    assert "state.pickControls = [go];" in dialog
    assert "control.disabled = busy;" in dialog
    # And it has to look disabled: a control that still lights up under the
    # pointer is inviting exactly the click it will not accept.
    assert ".plx-import-browse:disabled {" in css
    assert ".browse-kind-half:disabled {" in css

def test_a_caller_with_no_anchor_still_gets_a_picker():
    """`browseForPath` is callable without a button, and on a page without
    the explorer "kinds" must not be a dead end for one: a file dialog opens."""
    browse = source("src", "js", "services", "browsePicker.js")
    assert 'kind = anchorEl ? await chooseKind(anchorEl, examples || filter) : "file";' in browse


def test_the_chooser_says_which_formats_are_which():
    """The whole reason "file or folder?" is answerable: it is a question about
    the FORMAT, not about the user. An OME-Zarr is a directory and an OME-TIFF
    is a file, and the control says so rather than expecting it to be known.

    Keyed by filter, because the example that helps on the image field is a
    confident lie on the Data one -- which takes a .csv, a .parquet or an
    .h5ad and has never taken a .svs in its life.
    """
    browse = source("src", "js", "services", "browsePicker.js")
    assert 'image: { file: ".ome.tiff · .svs", directory: ".ome.zarr · DICOM" }' in browse
    assert ('data: { file: ".csv · .parquet · .h5ad", '
            'directory: "SpatialData (.zarr)" }') in browse
    # And the mask, which shares filter "image" with the image field and must
    # NOT share its examples: nobody has a segmentation mask in .svs, and a
    # DICOM folder is a slide (segmentation_pyramid.mask_format_refusal). A
    # .png is Cellpose's own mask format.
    assert 'mask: { file: ".ome.tiff · .png", directory: ".ome.zarr · SpatialData" }' in browse
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


# -- the explorer's own rules --------------------------------------------------


node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node is not installed")
@pytest.mark.parametrize("path", [*(EXPLORER / name for name in MODULES),
                                  CLIENT / "src" / "js" / "services" / "browsePicker.js",
                                  PROBE],
                         ids=lambda path: path.name)
def test_the_scripts_parse(path):
    done = subprocess.run([node, "--check", str(path)], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]


@pytest.mark.parametrize("name", MODULES)
def test_the_explorer_builds_its_dom_node_by_node(name):
    """No HTML strings: every label in the explorer is a file name or a path
    from a machine somebody else may control."""
    assert "innerHTML" not in module(name)
    assert "insertAdjacentHTML" not in module(name)


def test_the_window_never_becomes_a_containing_block():
    """transform, filter, contain or will-change on the window would make it
    the containing block for the `position: fixed` menus PopoverPortal hosts
    inside it, and every menu would open offset by the window's position. The
    spinner's keyframes are the one transform allowed."""
    css = source("src", "css", "explorer.css")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"@keyframes plx-explorer-spin \{.*?\n\}", "", css, flags=re.S)
    css = css.replace("text-transform:", "")
    for banned in ("transform:", "filter:", "contain:", "will-change"):
        assert banned not in css, banned


def test_closing_always_answers_the_caller():
    """The close handler tidies up, then resolves -- in a `finally`, because a
    tidy-up that threw once (a PopoverPortal method that does not exist) left
    every pick unanswered while the window had already gone."""
    controller = module("fileExplorer.js")
    handler = controller.split('dialog.addEventListener("close", () => {')[1]
    assert handler.index("} finally {") < handler.index("resolve(answer);")


def test_the_explorer_never_attaches_itself_to_the_portal():
    """It is a top-layer dialog: menus are portaled INTO it while it is open.
    Attached to the portal itself, it would be moved into whatever modal is
    on top -- itself."""
    for name in MODULES:
        assert "PopoverPortal.attach" not in module(name), name


def test_every_variable_the_stylesheet_reads_is_declared():
    """The picker this replaced read variables nothing declared and drew its
    hard-coded fallbacks without anyone noticing."""
    css = source("src", "css", "explorer.css")
    tokens = source("src", "css", "tokens.css")
    declared = set(re.findall(r"(--[\w-]+)\s*:", tokens + css))
    used = set(re.findall(r"var\((--[\w-]+)", css))
    assert used <= declared, sorted(used - declared)


def test_the_new_checks_are_there(probe):
    """The explorer's own behaviour, beyond what the old picker did."""
    for label in (
        "Cmd/Ctrl+L opens the path box from anywhere",
        "a long trail keeps the root and the open folder and folds the rest",
        "Cmd/Ctrl+Shift+. toggles them too",
        "drives are listed by their own names, with how full they are",
        "End goes to the last row, Home to the first",
        "Cmd/Ctrl+F goes to the filter box",
        "...but says why, in the footer",
        "a locked folder and a broken link are drawn, and say so",
        "a run folder the server recognised selects in one click",
        "inside one, a banner says what it is and offers it whole",
        "a caller's own suffixes grey the rest",
        "a column sorts on a click, largest first, folders still first",
        "the view is remembered in one write, not one per click",
        "ten thousand entries draw a window of rows, not ten thousand",
        "scrolling moves the window",
        "a partial listing asks for sizes and dates in chunks of 500",
        "...the rows on screen first",
        "a size sort on a partial listing keeps names in order until the sizes are in",
        "leaving a folder stops asking about it, and a late answer is dropped",
        "Enter in the box searches below the open folder, within a budget",
        "typing while searching waits for a pause before asking again",
        "going somewhere else leaves the search behind",
        "a node too old to search says so and keeps the filter",
        "dragging the title bar moves the window",
        "...but a press on its buttons does not",
        "the right edge widens the window",
        "maximize fills the viewport bar a 12px margin",
        "minimize rolls the window up to its title bar",
        "the window's place is kept for the session",
        "a viewport narrower than the window gets the viewport, minus the margin",
        "Esc while a menu is open is kept from closing the window",
        "...and focus goes back to what opened it",
        "synced cloud folders are listed after the disks, before project storage",
        "...each with its provider's mark and no free-space caption",
        "the title bar offers the system file browser where there is one",
        "...and the overflow no longer does",
        "while the system file browser starts, the explorer stays up and says so",
        "...and closes once that browser has the focus",
        "dismissing the file-or-folder question leaves the explorer open",
        "cancelling the system file browser answers nothing",
        "an answer that comes before the focus moves is taken all the same",
        "a cancel before it took the focus leaves the explorer as it was",
        "a system file browser that fails to start says why, in the explorer",
        "one that fails after the explorer closed answers nothing and says why",
        "Esc while it starts stops waiting rather than closing the explorer",
        "...and a pick made there later still counts",
        "several files chosen there come back as a list",
        "a file's menu chooses it or copies its path",
        "a pin's menu unpins it",
        "...and its document listeners",
    ):
        assert f"ok  {label}" in probe, label
