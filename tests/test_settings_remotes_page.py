"""The Settings page's server cards, and the log that is on them.

The behaviour is in tests/js/settings_remotes_probe.mjs, run in node below.
What is left here is what the probe cannot see: which file the terminal comes
from, and that the page no longer claims Plexora can be moved somewhere else
from inside itself.

The connection dialog those cards open is tests/test_connection_modal.py; the
poll they subscribe to is tests/test_remote_state.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "settings_remotes_probe.mjs"
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


# -- the log ------------------------------------------------------------------


def test_the_log_keeps_its_place_through_a_poll(probe):
    """The reported bug, and it was worse than it looked. The cards were thrown
    away and rebuilt on every update, so the pane was a NEW element once a
    second -- and a new element starts at the top. Reading anything in a live
    connection's log was impossible for exactly as long as the connection was
    live, which is the only time it matters."""
    assert "the log is on the card, as a terminal" in probe, probe
    assert "the pane survives the poll rather than being replaced" in probe, probe
    assert "...and scrolling up to read stops it yanking itself back down" in probe, probe
    assert "...and coming back to the bottom sets it following again" in probe, probe
    assert "...pinned to the bottom while nobody is reading it" in probe, probe


def test_the_far_machines_own_output_is_in_there_and_marked(probe):
    """ssh relays the remote command's stdout and stderr on one stream (they
    are merged in connect._Watched), and every line of it is already echoed
    into the session log. What was missing was any sign of which lines those
    were -- so the far machine's own words are marked as its own, in the same
    stream and in order, because the interesting moments are exactly where
    Plexora's narration and the machine's output interleave."""
    assert "...with what the far machine said marked as its own" in probe, probe


def test_opening_a_log_asks_for_the_whole_tail(probe):
    """The list payload carries eight lines, which is enough for a card to say
    something and never enough to diagnose anything. Two hundred is what the
    server keeps, and it is fetched for the connection somebody has just said
    they want to read -- not for every card, and not on a timer."""
    assert "a closed log asks for no deep tail" in probe, probe
    assert "opening one asks for that connection's whole log" in probe, probe
    assert "...and asks for it now, rather than at the next tick" in probe, probe
    assert "...and the deeper answer is what is drawn once it arrives" in probe, probe


def test_one_terminal_implementation_for_both_surfaces():
    """The connection modal and these cards show the same log for the same
    connection. Two implementations of "a terminal follows its own output" is
    how one of them ends up not doing it."""
    settings = source("src", "js", "views", "settingsPage.js")
    modal = source("src", "js", "services", "connectionModal.js")
    assert "PlexoraLogTerminal.create" in settings
    assert "PlexoraLogTerminal.create" in modal
    # Neither keeps a second copy of the scroll arithmetic.
    for text in (settings, modal):
        assert "scrollHeight" not in text
    base = source("templates", "base.html")
    assert base.index("services/logTerminal.js") < base.index(
        "services/connectionModal.js")


# -- what a poll must not disturb ---------------------------------------------


def test_a_poll_disturbs_nothing_somebody_is_using(probe):
    """Three things on the card hold state the DOM owns rather than the script:
    the log's scroll position, the password box's contents, and which button
    has the focus. All three used to be destroyed once a second; two of them
    had hand-written workarounds, which this removes rather than improves."""
    assert "a poll repaints the card rather than replacing it" in probe, probe
    assert "...including the button somebody may have tabbed to" in probe, probe
    assert "a poll while the same question stands leaves the box alone" in probe, probe


def test_a_forgotten_server_leaves_no_stale_card(probe):
    """The other half of keeping the cards: they have to go when the profile
    does, or a card outlives the thing it describes."""
    assert ("forgetting the last server leaves the empty note, not a stale card"
            in probe), probe


# -- one kind of connection ---------------------------------------------------


def test_connect_here_means_what_it_means_everywhere(probe):
    """A data node on the other machine, through the dialog every other surface
    opens. See test_connection_modal.test_one_connection_concept."""
    assert "Connect opens the shared dialog, for a DATA NODE" in probe, probe
    assert "...and nothing here offers to move Plexora itself" in probe, probe
    assert "...and sending it answers the NODE's prompt, not a viewer's" in probe, probe
    assert "...which ends the data node, not somebody's viewer" in probe, probe


def test_the_presets_are_reachable_from_the_page_that_adds_servers(probe):
    """They shipped reachable only by flipping a data field to Remote with
    nothing saved -- the one place somebody adding their first server was not
    looking. The button that says "Add a server" is on Settings."""
    assert ("the presets are reachable from the page that adds servers"
            in probe), probe
    page = source("templates", "settings.html")
    assert 'id="settings_remote_preset"' in page
    assert page.index('id="settings_remote_preset"') < page.index(
        'id="settings_remote_name"')
