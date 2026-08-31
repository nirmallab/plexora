"""Local/Remote for every file button, asked once and shared.

The behaviour is in tests/js/file_location_probe.mjs, run in node below. What
is left here is the wiring and the two properties that cannot be seen from
inside the file:

**It is loaded on every page, before anything with a file button.** A plugin's
upload arrow that fires before the click delegate is on `document` is a button
that silently means "this laptop" -- which is the whole bug.

**The two exports that cannot be intercepted call in instead.** A form
submitted with `form.submit()` fires no event and a detached anchor never
bubbles to this document, so gating's CSV and ROI's export hand their bytes to
`deliver()`. Nothing else notices they are special, and only a grep notices
when one of them stops doing it.

The server half is tests/test_transfer_routes.py and
tests/test_file_transfer_node.py.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "file_location_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"
PLUGINS = REPO_ROOT / "plexora" / "plugins"


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


# -- when not to ask ---------------------------------------------------------


def test_one_machine_means_no_question_at_all(probe):
    """Every single-server install, which is most of them. A dialog in front of
    an upload button that has exactly one possible answer is a toll gate."""
    assert "with nowhere else to go it says there is nowhere else to go" in probe, probe
    assert "...so an upload button opens the file dialog, untouched" in probe, probe
    assert "...and nothing was asked of the server" in probe, probe


def test_it_watches_passively(probe):
    """`PlexoraRemotes` stops polling once every connection is settled. A layer
    on every page subscribing `active` would turn that back into a request a
    second, for a question that is only asked when a button is pressed."""
    assert "it watches passively, so a settled connection costs no polling" in probe, probe


def test_a_field_with_its_own_switch_is_left_alone(probe):
    """The core import forms already ask which machine, in a switch built into
    the row. Asking again in a modal is the same question in two shapes."""
    assert "a field with its own Local/Remote switch is not asked twice" in probe, probe


def test_a_modified_click_is_not_ours_to_intercept(probe):
    """Command-click on a download link is the user talking to their browser."""
    assert "a command-click is the user talking to their browser, not to us" in probe, probe


# -- and what happens when it does -------------------------------------------


def test_choosing_this_computer_re_clicks_exactly_once(probe):
    """The layer clicks the very element it just intercepted. Without the
    bypass that is an infinite loop, and with a bypass that is never cleared it
    is a button that works once."""
    assert "choosing this computer opens the dialog it was going to open" in probe, probe
    assert "...exactly once -- the re-click is not intercepted again" in probe, probe


def test_a_remote_file_reaches_the_plugin_the_ordinary_way(probe):
    """The point of the whole design: the plugin's `change` handler runs with
    files on the input, exactly as a real file dialog would have left them, and
    nothing downstream learns a picker was involved."""
    assert "choosing the machine browses ITS filesystem, not this one" in probe, probe
    assert "...the bytes coming back through this server" in probe, probe
    assert "the file lands on the input, named as the far side named it" in probe, probe
    assert "...and `change` fires, which is all the plugin was ever listening for" in probe, probe


def test_the_picker_filter_is_conservative(probe):
    """Greying out a file the form would have taken is a dead end with no way
    past it from inside the picker; offering one it refuses costs a sentence
    from the form's own validation. So anything uncertain is "any"."""
    assert "an image field browses with the image filter, several at a time" in probe, probe
    assert "...and a field asking for something no filter covers greys out nothing" in probe, probe


def test_a_node_that_outlived_its_session_is_still_a_machine(probe):
    """A data node outlives the Plexora that started it, so after a restart
    `place.node` is empty for a machine that is up and answering. Filtering on
    it alone makes a reachable cluster invisible the morning after."""
    assert "a node that outlived the session that started it is still a machine" in probe, probe


def test_cancelling_leaves_everything_as_it_was(probe):
    assert "cancelling leaves the input alone and opens nothing" in probe, probe


def test_a_name_already_taken_is_a_question(probe):
    """The user picked that name, and whether it should replace last week's
    export is theirs to answer -- not something to route around by saving a
    `report (2).pdf` nobody asked for."""
    assert "a name already taken is a question, not a dead end" in probe, probe
    assert "...and Replace says so, rather than saving under another name" in probe, probe


def test_deliver_saves_locally_without_touching_the_network(probe):
    """One caller is an emergency export offered precisely when the server has
    stopped answering. A "where would you like this?" that needs a live server
    to answer would fail exactly when it was needed."""
    assert "with nowhere else to go, deliver saves here and asks nothing" in probe, probe
    assert "...touching the network not at all, which is the emergency path" in probe, probe
    assert "...through an anchor it does not then intercept itself" in probe, probe


def test_the_last_machine_is_offered_first(probe):
    """Somebody exporting three figures in a row is answering one question
    three times. Same reasoning as dataLocation.js's `lastPlace`."""
    assert "...remembering where the last one went" in probe, probe


# -- the wiring --------------------------------------------------------------


def test_it_is_loaded_on_every_page_before_anything_with_a_file_button():
    base = source("templates", "base.html")
    assert "services/fileLocation.js" in base
    for earlier in ("services/remoteState.js", "services/pathPicker.js",
                    "services/placePicker.js", "services/connectionModal.js"):
        assert base.index(earlier) < base.index("services/fileLocation.js"), (
            f"{earlier} is what fileLocation.js opens"
        )
    assert base.index("services/fileLocation.js") < base.index(
        "views/toolLoader.js"), (
        "a plugin's upload arrow must not be able to fire before the delegate "
        "is on `document`"
    )


def test_it_installs_the_delegate_once_per_process_not_once_per_page():
    """The listener is on `document` and survives every routed page swap, so
    registering through PlexoraPage would add a second one on the first
    navigation -- and two delegates means two dialogs per click."""
    layer = source("src", "js", "services", "fileLocation.js")
    assert "PlexoraPage.register" not in layer
    assert "if (started) return;" in layer


def test_its_styling_lives_with_the_dialog_it_borrows():
    """A `.connect-modal` shell, so this and the dialog that connects a machine
    read as one flow. import.css is not loaded on the pages plugins run on."""
    assert ".file-location-modal" in source("src", "css", "main.css")
    assert ".file-location-modal" not in source("src", "css", "import.css")


def test_the_core_fields_that_have_their_own_switch_opt_out():
    """Both build a hidden file input beside a control that has already asked
    which machine. Without the attribute they ask twice, in two shapes."""
    for parts in (("src", "js", "services", "dataLocation.js"),
                  ("src", "js", "views", "channelNamesUpload.js")):
        assert 'data-file-location", "local"' in source(*parts), parts


def test_the_layer_names_no_plugin():
    """The contract is primitive-level -- `input[type=file]`, `a[download]`, an
    opt-out attribute -- which is what makes it cover a plugin written next
    year. A plugin name in core JS is also refused by
    tests/test_datalayer_requests.py."""
    layer = source("src", "js", "services", "fileLocation.js")
    assert not re.search(r"plugins/\w+/", layer)


# -- the two that cannot be intercepted --------------------------------------


def test_the_gating_upload_arrow_dispatches_a_cancelable_click():
    """It used to build its own MouseEvent with `initEvent("click", true,
    false)` -- bubbling and NOT cancelable -- so the delegate could see the
    click go past and not stop it. The file dialog opened regardless, and that
    one button was the only upload in Plexora that could never reach a remote
    machine."""
    listing = (PLUGINS / "gating" / "static" / "csvGatingList.js").read_text(
        encoding="utf-8")
    # The comment above the fix still names it, which is the point of the
    # comment -- what must be gone is the call.
    assert 'createEvent("MouseEvents")' not in listing
    assert re.search(r"^\s*evt\.initEvent", listing, re.MULTILINE) is None
    assert "elem.click()" in listing


def test_both_client_built_exports_hand_their_bytes_to_the_layer():
    """Neither can be intercepted: a `form.submit()` fires no event, and a blob
    saved through a detached anchor never bubbles to this document. So they
    call in, and `deliver` is the documented way for anything a plugin builds
    client-side."""
    for parts in (("gating", "static", "gatingApi.js"),
                  ("roi", "static", "roiApi.js")):
        text = PLUGINS.joinpath(*parts).read_text(encoding="utf-8")
        assert "PlexoraFileLocation" in text, parts
        assert "deliver(" in text, parts


def test_the_streaming_csv_download_is_kept_for_when_there_is_one_machine():
    """The hidden form writes the response straight to disk, so a full CSV of
    two million cells never exists in the tab. Turning that into a Blob
    unconditionally would put the largest export in the app through memory to
    serve a question nobody asked."""
    api = (PLUGINS / "gating" / "static" / "gatingApi.js").read_text(
        encoding="utf-8")
    assert "_downloadGatingCSVViaForm" in api
    assert "form.submit()" in api
