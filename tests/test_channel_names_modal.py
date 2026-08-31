"""The dialog that asks for a channel-name file, and what it is wired to.

The behaviour is in tests/js/channel_names_probe.mjs, run in node below,
because nothing in the Python suite executes client JS. What is left here is
the wiring -- the handful of facts spread across four files that have to agree
before any of that behaviour is reachable at all:

  index.html            loads the dialog, and no longer parks a file input
                        in the sidebar
  channelList.js        opens it, and takes the new names on when it lands
  dataLayer.js          no longer has a second way to post the same thing
  native_dialog.py      knows the filter its Browse button asks for
  fileLocation.js       lends it the node relay, so the Remote side of its
                        switch has bytes to post

The server half is tests/test_channel_names_upload.py; what happens to the
page once a rename lands is tests/test_channel_rename_state.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "channel_names_probe.mjs"
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


# -- the dialog --------------------------------------------------------------


def test_the_common_case_costs_no_questions(probe):
    assert "loading it uploads straight away" in probe, probe
    assert "...and asking for no column, so the server may work it out" in probe, probe
    assert "...handing the caller the names it applied, in image order" in probe, probe


def test_the_file_is_only_ever_picked_once(probe):
    """Every stage re-posts the same source. Being sent back to the file
    picker because a checkbox was ticked, or because a column was wrong, is
    the dialog throwing away the one thing it asked for."""
    assert "...still holding the file, rather than sending the user back for it" in probe, probe
    assert "...and the same file, not a second pick" in probe, probe
    assert "...landing back on the picker with the file still in hand" in probe, probe


def test_the_column_question_is_asked_with_the_table_in_view(probe):
    assert "a table of several columns asks which one holds the names" in probe, probe
    assert "a preview of the file is shown" in probe, probe
    assert "...with the header row drawn as headings, not as data" in probe, probe
    assert "a wide file says the preview is only part of it" in probe, probe


def test_the_header_checkbox_changes_what_the_answer_would_be(probe):
    assert "the header checkbox is there, and pre-ticked on the server's reading" in probe, probe
    assert "un-ticking it renames the columns by position" in probe, probe
    assert "...counts the first row as a name again" in probe, probe


def test_a_wrong_count_stops_rather_than_partly_applying(probe):
    assert "a count that does not match is its own screen, not an error line" in probe, probe
    assert "...stating both numbers" in probe, probe
    assert "...having changed nothing" in probe, probe
    assert "...offering another file rather than a way to apply it anyway" in probe, probe


def test_the_path_box_is_there_for_the_machine_that_can_see_the_file(probe):
    assert "loading it uploads straight away" in probe, probe
    assert "...sending the path rather than any bytes" in probe, probe
    assert "a path that is not there leaves Load off and says so on the field" in probe, probe


def test_there_is_one_control_on_the_first_stage(probe):
    """A browser upload used to sit above the path box with an "or" between
    them. Locally the Browse button opens a native file dialog too, so the pair
    asked the user to choose between two spellings of the same act before they
    had done anything."""
    assert "the whole of the first stage is one path field and its buttons" in probe, probe
    assert "...so there is no second control offering to do it another way" in probe, probe
    assert "...with the path still in the box, ready to be edited" in probe, probe


# -- the wiring --------------------------------------------------------------


def test_the_viewer_loads_the_dialog_and_stops_parking_an_input_in_the_sidebar():
    index = (CLIENT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "views/channelNamesUpload.js" in index
    # The hidden file input and its wrapper form. The dialog builds its own and
    # throws it away, so a leftover here is a control in the sidebar that looks
    # like it does something.
    assert 'id="channels-upload-from-arrow"' not in index
    assert 'id="channels_arrow-upload-form"' not in index
    # The button that opens it stays, and it is still the sidebar's.
    assert 'id="channels_upload_icon"' in index


def test_the_channel_list_opens_the_dialog_and_takes_the_names_on():
    """In place, with the reload kept only for the one case that cannot be
    patched -- see tests/test_channel_rename_state.py for what "in place"
    has to cover."""
    channels = source("src", "js", "views", "channelList.js")
    assert "window.PlexoraChannelNames.open({" in channels
    assert "__plexora.adoptChannelNames(names)" in channels
    assert "if (!applied) window.location.reload();" in channels
    # The old inline handler and everything it implied -- one format, no way
    # to name a column, and window.alert() for a count that did not match.
    # The statements, not the words: both files say in a comment what used to
    # be there and why it went, which is worth keeping and is not code.
    assert "channels-upload-from-arrow" not in channels
    assert "alert('Failed to rename channels" not in channels


def test_there_is_one_way_to_post_a_channel_list():
    """dataLayer had its own. Two callers of one route drift: the dialog has
    to read `needs_column` and `mismatch` off the response, and a second
    poster that treats anything but `success` as an error would turn the
    column question into a failure."""
    layer = source("src", "js", "services", "dataLayer.js")
    assert "async submitChannelUpload(" not in layer
    assert "plexoraUrl('upload_channels')" not in layer
    assert 'plexoraUrl("upload_channels")' in source(
        "src", "js", "views", "channelNamesUpload.js")


def test_the_browse_button_asks_for_a_filter_the_server_has_heard_of():
    """attachBrowseButton posts the filter name to /browse_path, which
    validates it against native_dialog's own table and 400s on anything else
    -- a button that looks ordinary and does nothing when clicked."""
    from plexora.server.utils.native_dialog import FILTER_NAMES, _APPLESCRIPT_EXTENSIONS

    assert 'filter: "channels"' in source("src", "js", "views", "channelNamesUpload.js")
    assert "channels" in FILTER_NAMES
    # Both tables, because they are two platforms' copies of one list and the
    # macOS one has been missed before.
    assert "channels" in _APPLESCRIPT_EXTENSIONS


def test_the_browse_dialog_and_the_reader_agree_on_what_can_be_picked():
    """The filter is what the user is shown when they click Browse; the
    server's suffix check is the rule. They are allowed to differ in kind but
    not in content -- a filter that offers something the reader refuses is a
    file dialog that lets the user pick a file it will then reject, and one
    that omits something readable hides a file that would have worked.

    The dialog's own `accept` list used to be the thing pinned here. It went
    with the browser upload: this filter is now the only list of extensions
    the user meets.
    """
    from plexora.server.utils import channel_file
    from plexora.server.utils.native_dialog import _TK_FILTERS

    offered = set()
    for label, patterns in _TK_FILTERS["channels"]:
        if label == "All files":
            continue
        offered.update(pattern.lstrip("*") for pattern in patterns.split())
    readable = set(channel_file.DELIMITED_SUFFIXES) | set(channel_file.EXCEL_SUFFIXES)
    assert offered == readable, f"{sorted(offered)} vs {sorted(readable)}"


def test_a_desktop_launch_still_has_one_way_in_rather_than_two(probe):
    """Browse opens a native dialog on the machine that is also running the
    server and writes the path it comes back with, so an upload beside it would
    be a choice between two spellings of one act -- offered before the user had
    done anything."""
    assert "on a desktop launch there is one way in, not two" in probe, probe


def test_a_server_on_another_machine_gets_the_upload_back(probe):
    """There the two stop being the same thing: the box means the server's
    paths, and a marker list on this laptop has no way in at all -- which is
    where it usually is, because the panel came from a collaborator by email.
    It is on the Local side of the switch, one flip away, and it is the only
    live control there when nothing can read this computer's paths."""
    assert "this computer is one flip away, and says what it can and cannot do" in probe, probe
    assert "choosing a file sends it, without a path to name it by" in probe, probe
    assert "...and the names it came back with are applied" in probe, probe
    assert ("a file staged from this computer survives being asked about columns"
            in probe), probe


def test_the_two_ways_in_are_never_sent_together():
    """`_channel_file_source` prefers the upload and would quietly ignore a
    path beside it, so sending both would make the wrong one look like it had
    been read."""
    dialog = source("src", "js", "views", "channelNamesUpload.js")
    assert 'if (session.file) form.append("file", session.file);' in dialog
    assert 'else form.append("path", session.path);' in dialog


# -- which machine the box means ---------------------------------------------


def test_the_switch_opens_on_the_side_the_box_has_always_meant(probe):
    """A control appearing beside a field must not change what the field does
    for somebody who ignores it. The box has always meant the machine running
    Plexora, so that is the side it opens on -- which is Local on a desktop
    launch and Remote-the-server when Plexora is elsewhere."""
    assert "...and the switch opens on the side the box has always meant" in probe, probe
    assert "with Plexora elsewhere, the box still opens meaning the server" in probe, probe
    assert "...with the hint saying which machine that is" in probe, probe


def test_a_marker_list_on_a_data_node_now_has_a_way_in(probe):
    """The case that had none. The panel is beside the image on a cluster
    reached through a data node -- neither the browser's filesystem nor the
    server's -- and until the node relay existed, naming it here was impossible
    in both directions: a node path typed into the box named nothing the server
    could open, and Upload could only send what the browser could already see.
    """
    assert "one connected machine is adopted rather than asked about" in probe, probe
    assert "...with Browse now asking the node" in probe, probe
    assert "loading it reads the bytes through the node" in probe, probe
    assert ("...and posts them as an upload, since the server has no path for it"
            in probe), probe


def test_choosing_the_machine_is_the_same_question_asked_the_same_way(probe):
    """One reachable machine is adopted rather than asked about, none opens a
    connection rather than an empty list, and several is the picker -- the
    shape dataLocation.choosePlace established, because a user meeting this in
    two dialogs should not meet two behaviours."""
    assert "more than one machine is a question, asked in the picker" in probe, probe
    assert "...and the chip asks it again, so adopting one is not a one-way door" in probe, probe
    assert "with nothing connected, Remote opens a connection rather than a list" in probe, probe
    assert "cancelling the picker leaves the field on a side that works" in probe, probe


def test_the_chosen_machine_survives_the_stages(probe):
    """For the same reason the path does. Being sent back to the wrong machine
    after a column question is the dialog throwing away an answer it asked
    for."""
    assert ("going back for another file lands on the machine the last one was on"
            in probe), probe


def test_a_box_nothing_can_read_stops_taking_a_path(probe):
    """And Browse with it. A Browse button left live on the Local side of a
    Plexora running elsewhere opens a dialog on the SERVER -- a machine the
    user has just said the file is not on."""
    assert "...and Browse goes with the box, rather than opening the server's" in probe, probe
    assert "a client node makes this computer's paths readable again" in probe, probe


def test_a_node_path_is_not_checked_against_the_servers_disk(probe):
    """/check_file_existence stats the machine running Plexora. Run against a
    cluster path it answers "no" about a file that is plainly there, which
    disarmed Load and marked the box red."""
    assert "a path on the node is not checked against the server's disk" in probe, probe


def test_the_node_read_goes_through_the_one_place_that_knows_the_route():
    """`POST /fetch_file` and the header it answers with are fileLocation's,
    and a second copy here is two spellings of one relay that drift the first
    time either changes."""
    dialog = source("src", "js", "views", "channelNamesUpload.js")
    assert "PlexoraFileLocation" in dialog
    # Named in the header, so a reader knows what is behind the call. Not
    # POSTed to -- that is the half that would drift.
    assert 'plexoraUrl("fetch_file")' not in dialog
    assert "X-Plexora-File-Name" not in dialog
    assert "read: fetchFile" in source("src", "js", "services", "fileLocation.js")


def test_the_row_asks_which_machine_so_the_shared_layer_does_not_ask_again():
    """The Upload… chooser is an `input[type=file]`, which fileLocation
    intercepts on click. Without the opt-out, pressing it after answering the
    switch means answering one question twice in two different shapes."""
    dialog = source("src", "js", "views", "channelNamesUpload.js")
    assert 'setAttribute("data-file-location", "local")' in dialog
    assert 'group.setAttribute("data-tooltip"' in dialog


def test_the_switch_is_the_same_control_the_import_fields_use():
    """Same classes, so it is the same control and not a lookalike: the CSS is
    in main.css beside dataLocation's own, and a restyle there has to reach
    both or one dialog quietly stops matching the app."""
    dialog = source("src", "js", "views", "channelNamesUpload.js")
    for name in ("data-location", "data-location-toggle", "data-location-option",
                 "data-location-place", "data-location-status"):
        assert f'"{name}"' in dialog, name
    css = source("src", "css", "main.css")
    assert ".data-location-option.is-active" in css
