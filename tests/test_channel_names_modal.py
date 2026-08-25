"""The dialog that asks for a channel-name file, and what it is wired to.

The behaviour is in tests/js/channel_names_probe.mjs, run in node below,
because nothing in the Python suite executes client JS. What is left here is
the wiring -- the handful of facts spread across four files that have to agree
before any of that behaviour is reachable at all:

  index.html            loads the dialog, and no longer parks a file input
                        in the sidebar
  channelList.js        opens it, and reloads once a rename lands
  dataLayer.js          no longer has a second way to post the same thing
  native_dialog.py      knows the filter its Browse button asks for

The server half is tests/test_channel_names_upload.py.
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
    assert "picking a file uploads it straight away" in probe, probe
    assert "...and asking for no column, so the server may work it out" in probe, probe


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
    assert "the path box is offered without being asked for" in probe, probe
    assert "loading it sends the path rather than any bytes" in probe, probe
    assert "a path that is not there leaves Load off and says so on the field" in probe, probe


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


def test_the_channel_list_opens_the_dialog_and_reloads_when_it_lands():
    """A rename changes names on disk. The channel list, the gating panel and
    the cached description all read them, so the page is reloaded rather than
    each of those being patched in place."""
    channels = source("src", "js", "views", "channelList.js")
    assert "window.PlexoraChannelNames.open({" in channels
    assert "onApplied: () => window.location.reload()," in channels
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


def test_the_dialog_and_the_reader_agree_on_what_can_be_dropped_in():
    """The `accept` on the file picker is a hint; the server's suffix check is
    the rule. They are allowed to differ in kind but not in content -- an
    accept list that offers something the reader refuses is a file dialog that
    lets the user pick a file it will then reject."""
    from plexora.server.utils import channel_file

    accepted = source("src", "js", "views", "channelNamesUpload.js")
    offered = accepted.split('const ACCEPT = "', 1)[1].split('"', 1)[0].split(",")
    readable = set(channel_file.DELIMITED_SUFFIXES) | set(channel_file.EXCEL_SUFFIXES)
    assert set(offered) == readable, f"{sorted(offered)} vs {sorted(readable)}"
