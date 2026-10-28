"""The "which machine?" chip on every form field, and the dialogs that carry it.

The behaviour is in tests/js/machine_chip_probe.mjs, run in node below,
because nothing in the Python suite executes client JS. What the field does
with a machine once it is picked is tests/test_data_location.py; the
channel-names dialog's own use of it is tests/test_channel_names_modal.py.

What is left here is the wiring: that the home page's chip is the one shape
the question has, everywhere a path is typed, and that the dialogs it sits in
close the way every other Plexora dialog does -- an X in the corner.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "machine_chip_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"
VIEWS = CLIENT / "src" / "js" / "views"


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


# -- the chip ----------------------------------------------------------------


def test_the_chip_says_where_the_field_is(probe):
    """Read from the field on every paint: a chip that kept its own copy would
    disagree with the field the first time a drop moved it."""
    assert "the chip sits in the path box, before the input" in probe, probe
    assert "...and reads Local for a field on this computer" in probe, probe
    assert "...and the server's name for a field on one, tinted as remote" in probe, probe
    assert "...read from the field on every paint, never kept" in probe, probe


def test_its_menu_is_the_home_pages(probe):
    """One question, asked one way: the home page's list, item for item."""
    assert "...listing Local, every saved server, Add a server… and Manage servers" in probe, probe
    assert "...each saying whether it is up" in probe, probe
    assert "...with one still connecting not pickable until it is" in probe, probe
    assert "picking a connected server hands it to the field" in probe, probe
    assert "picking Local hands the field back to this computer" in probe, probe


def test_a_server_that_is_not_up_is_connected_not_adopted(probe):
    """Adopting it would put the field on a filesystem nothing can read a path
    on, which is the state the question exists to avoid."""
    assert "a saved server that is down opens the connection dialog for it" in probe, probe
    assert "...and the machine it brought up is the one handed over" in probe, probe
    assert "Add a server… opens the dialog for a new one" in probe, probe
    assert "...and backing out of it leaves the field where it was" in probe, probe


def test_the_menu_belongs_to_the_dialog_it_opens_from(probe):
    """A modal dialog is the top layer: a menu under <body> opens behind it.
    And Escape is the menu's before it is the dialog's."""
    assert "the menu opens inside the dialog the chip is in" in probe, probe
    assert "Escape closes the menu and goes back to the chip, not the dialog" in probe, probe
    assert "a press anywhere else puts the menu away" in probe, probe


def test_the_chip_loads_before_the_fields_that_mount_it():
    base = source("templates", "base.html")
    chip = base.index("views/machineChip.js")
    assert base.index("services/remoteState.js") < chip
    assert base.index("services/connectionModal.js") < chip
    assert chip < base.index("services/dataLocation.js")
    assert chip < base.index("views/fileSourceRow.js")


def test_no_field_draws_the_old_switch():
    """The L | R switch is gone everywhere, not just where somebody looked:
    a second shape for the question is what this replaced."""
    for path in VIEWS.parent.rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert "data-location-option" not in text, path
        assert "(L)ocal | (R)emote" not in text, path
    css = source("src", "css", "main.css")
    assert ".data-location-toggle" not in css
    assert ".data-location-place" not in css
    for module in ("fileSourceRow.js", "channelNamesUpload.js"):
        assert "PlexoraMachineChip" in (VIEWS / module).read_text(encoding="utf-8"), module
    assert "PlexoraMachineChip" in source("src", "js", "services", "dataLocation.js")


# -- the dialogs it sits in ---------------------------------------------------


def test_the_dialogs_close_with_an_x_not_a_button_among_the_answers():
    """The requirements form, the channel-names dialog and the gene-group
    dialog each had a Cancel, Close or Done beside their answers, and two of
    them an X as well. The way out is the X in the corner, once."""
    requirements = (VIEWS / "requirementsModal.js").read_text(encoding="utf-8")
    assert 'data-action="close"' in requirements
    assert "fa-xmark" in requirements
    assert 'data-action="cancel"' not in requirements
    assert ">Cancel<" not in requirements

    channels = (VIEWS / "channelNamesUpload.js").read_text(encoding="utf-8")
    assert "channel-names-close" in channels
    assert not re.search(r'button\("btn[^"]*", "(Cancel|Close)"', channels)

    genes = (VIEWS / "geneGroupModal.js").read_text(encoding="utf-8")
    assert "gene-modal-close" in genes
    # The button, not the word: the header comment says why it went.
    assert not re.search(r'button\(\s*"gene-modal-btn", "Done"', genes)


def test_skip_stays_where_it_records_a_decline():
    """Not a close button: on a form where nothing is required it saves the
    blanks, which is what stops the offer coming back on every open. It is
    hidden everywhere else -- and `.btn` sets `display`, which defeats the
    hidden attribute without a rule saying otherwise."""
    requirements = (VIEWS / "requirementsModal.js").read_text(encoding="utf-8")
    assert 'data-action="skip" hidden' in requirements
    assert "skip.addEventListener(\"click\", () => submit());" in requirements
    assert ".requirements-actions > [hidden]" in source("src", "css", "main.css")
