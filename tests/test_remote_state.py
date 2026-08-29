"""One client-side owner of "what are the remote connections doing?".

The behaviour is in tests/js/remote_state_probe.mjs, run in node below, because
nothing in the Python suite executes client JS. What is left here is the part a
probe cannot see: that the duplicates are actually GONE.

Four surfaces watch the same three ssh processes -- the Settings cards, the
machine picker, the connection modal and the navbar globe. Two of them used to
run their own timer against their own endpoint with their own copy of the state
list and their own guess at whether a prompt was confidential, which is how
Settings came to mask a host-key fingerprint that the picker showed in the
clear. A module that merely OFFERS a shared implementation fixes nothing; what
fixes it is that there is nowhere else left to make the judgement.

The server halves are tests/test_remote_connect.py (the sessions and the
routes) and tests/test_connect.py (the ssh underneath them).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "remote_state_probe.mjs"
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


# -- the poll is scoped ------------------------------------------------------


def test_nobody_watching_means_nothing_asked(probe):
    """The globe this feeds sits in the navbar of every page, the viewer
    included. A module that started polling when it loaded would cost every
    user a request a second for the length of their session, in exchange for
    an icon nobody is looking at."""
    assert "a module nobody subscribed to makes no request" in probe, probe
    assert "dropping every subscriber cancels the timer" in probe, probe


def test_a_settled_connection_watched_passively_is_not_polled(probe):
    """The state a connection spends hours in. Nothing can change it but an
    action, and whoever takes the action refreshes -- so asking again every
    second is asking a question whose answer cannot change on its own."""
    assert "a passive watcher of a settled connection polls at nothing" in probe, probe


def test_something_happening_or_somebody_looking_means_poll(probe):
    """Both halves are needed. A queued job moves on its own with nobody
    watching; an open dialog has to notice a connection another tab opened
    even while nothing here is moving."""
    assert "a connection on its way up is polled even by a passive watcher" in probe, probe
    assert "an active subscriber is re-read even with everything settled" in probe, probe


def test_two_surfaces_cost_one_round_trip(probe):
    """Settings open beside the connection modal is the ordinary case, and the
    reason this module exists rather than a shared helper function."""
    assert "two subscribers cost one pair of fetches, not two" in probe, probe
    assert "...and both are told" in probe, probe


def test_a_broken_renderer_does_not_freeze_the_others(probe):
    """One throw inside a subscriber used to be a throw inside the poll."""
    assert "a subscriber that throws does not starve the ones after it" in probe, probe


def test_a_failed_poll_keeps_the_last_account_of_the_world(probe):
    """Blanking every card because one request failed loses the error message
    that was on them."""
    assert "a failed poll notes the error without blanking the list" in probe, probe


# -- the merge ---------------------------------------------------------------


def test_a_profile_carries_both_of_its_halves(probe):
    """One saved login can be running two unrelated things at once: a viewer
    over there with the browser tunnelled to it, and a data node over there
    serving bytes to the Plexora here. Every surface shows both, and none of
    them should do the join itself."""
    assert "a profile's viewer and data node are two halves of one row" in probe, probe
    assert "...and the node's own name is what the row carries" in probe, probe
    assert "...a profile that runs inside a job says so before you connect" in probe, probe


def test_a_focused_watcher_gets_the_whole_log(probe):
    """The list carries a short tail so a card can show the last thing that
    happened. A modal watching one connection wants all of it, because a stack
    of authentication failures is exactly what somebody needs to read."""
    assert "a focused watcher asks for that connection's whole log" in probe, probe
    assert "...and asks about the right kind of connection" in probe, probe
    assert "...which is handed back keyed by kind and name" in probe, probe


def test_acting_on_a_connection_names_which_kind(probe):
    """`?kind=node` is the difference between disconnecting somebody's data
    node and disconnecting the viewer they are looking at. Building that URL
    in four places is how one of them forgets."""
    assert "connecting a data node says so in the request" in probe, probe
    assert "...and a viewer disconnect carries no kind, as it always did" in probe, probe


# -- one judgement, not four -------------------------------------------------


def test_whether_a_prompt_is_a_secret_is_decided_once(probe):
    """Masking a host-key question means somebody types `yes` into a row of
    dots, beside a fingerprint they are being asked to check. The fingerprint
    is public by construction; hiding the answer to it protects nothing."""
    assert "a password prompt is a secret" in probe, probe
    assert "a Duo prompt is a secret" in probe, probe
    assert "a host-key question is not, and neither is its fingerprint" in probe, probe


def test_every_state_has_one_spelling(probe):
    assert "every session state has a label" in probe, probe
    assert "the opening states are the server's opening states" in probe, probe


# -- and the duplicates are gone ---------------------------------------------


def test_the_picker_has_no_poller_or_heuristic_of_its_own():
    picker = source("src", "js", "services", "placePicker.js")
    assert "POLL_MS" not in picker
    assert "function isSecret" not in picker
    assert "setTimeout(tick" not in picker
    # And it reads the shared one rather than a copy of the list.
    assert "Remotes().isOpening" in picker


def test_the_settings_page_has_no_poller_or_heuristic_of_its_own():
    settings = source("src", "js", "views", "settingsPage.js")
    assert "LIVE_STATES" not in settings
    assert "/(yes\\/no/i" not in settings
    assert "PlexoraRemotes.subscribe" in settings
    # The migration poll is a different thing entirely and stays: it watches a
    # job on this server, not an ssh.
    assert "settings/data/migration" in settings


def test_the_opening_states_match_the_servers():
    """A state missing from the client's copy is a connection that stops being
    polled halfway up, which shows only under load or on a slow cluster."""
    client = source("src", "js", "services", "remoteState.js")
    server = (REPO_ROOT / "plexora" / "server" / "models"
              / "remote_sessions.py").read_text(encoding="utf-8")
    for state in ("connecting", "authenticating", "waiting_for_job",
                  "tunneling", "waiting_for_app"):
        assert f'"{state}"' in client
        assert f'STATE_{state.upper()} = "{state}"' in server


def test_remote_state_is_loaded_before_everything_that_reads_it():
    base = source("templates", "base.html")
    assert base.index("services/remoteState.js") < base.index("services/placePicker.js")
