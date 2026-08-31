"""Saying so before a scheduled job ends.

The behaviour is in tests/js/session_expiry_probe.mjs, run in node below. What
is left here is the wiring, and the one property that is not visible from
inside the file: this watcher is loaded on EVERY page, so what it costs when
nothing is on a clock is what decides whether it can be there at all.

Where the number itself comes from is tests/test_session_walltime.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "session_expiry_probe.mjs"
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


# -- when not to speak -------------------------------------------------------


def test_a_connection_with_no_walltime_is_never_mentioned(probe):
    """Most connections are not inside a job at all. A watcher that ran a timer
    for them, or said anything about them, would be spending attention on the
    common case to serve the uncommon one."""
    assert "a connection with no walltime starts no timer and says nothing" in probe, probe
    assert "a job with hours left is watched, quietly" in probe, probe
    assert "with no clock left anywhere, the timer is stopped" in probe, probe


def test_it_costs_nothing_while_a_job_is_simply_running(probe):
    """`PlexoraRemotes` stops polling once everything is settled -- which is
    the state a four-hour job sits in for four hours. A watcher subscribing
    `active` would turn that into a request a second for the privilege of
    watching a number go down."""
    assert "it watches passively, so a settled job costs no polling" in probe, probe


def test_it_says_it_once_per_session_not_once_per_check(probe):
    """It runs on a timer. A dialog per tick would be unusable, and one that
    never came back would miss the reconnect."""
    assert "having been told once, it is not told again on the next check" in probe, probe
    assert "a fresh job is warned about again, the clock having gone back up" in probe, probe
    assert "...and that too is said once" in probe, probe


# -- and what it says when it does -------------------------------------------


def test_the_warning_names_the_machine_and_shows_the_number(probe):
    """"A connection is expiring" is true and unactionable. The name is the one
    in Settings and in the profile that reconnects it, and the number is what
    somebody is deciding on -- so it is live, not a snapshot of the moment the
    dialog opened."""
    assert "ten minutes out, it says so" in probe, probe
    assert "...naming the machine, not just 'a connection'" in probe, probe
    assert "...with the number somebody is deciding on, live" in probe, probe
    assert "...and it is a countdown, not a static string" in probe, probe


def test_it_says_what_is_not_at_risk(probe):
    """The whole reason a job ending is survivable: the projects, ROIs, figures
    and gates are on this machine and stay here. Without that sentence the
    dialog reads as "you are about to lose your work"."""
    assert "...saying what is NOT at risk, which is everything on this computer" in probe, probe


def test_starting_a_new_session_ends_the_old_one_first(probe):
    """The old entry names a loopback port whose tunnel is gone or going.
    Leaving it on the map would have the new node land beside a stale one under
    the same name -- and it is what `nodes._disconnected` is keyed on, so work
    still holding the old address stops trying it."""
    assert "...and starting a new session ends the old one first" in probe, probe
    assert "...then opens the one dialog that connects a machine" in probe, probe
    assert "...and closes itself on the way, leaving one window on screen" in probe, probe


def test_a_job_that_has_already_ended_is_said_in_the_past_tense(probe):
    """By then there is nothing to save and no decision to make, so the dialog
    stops asking somebody to hurry and says what happened."""
    assert "a job that has ended is said out loud even after the warning was" in probe, probe
    assert "...in the past tense, because there is nothing left to save" in probe, probe


def test_closing_it_is_remembered_by_the_next_page(probe):
    """The one that made it unclosable. An ended job reports the same zero for
    as long as its node is on the map -- the registry entry outlives the
    allocation and `time_left` floors at zero rather than becoming null -- so a
    dismissal held in a variable met that unchanged fact again on every reload
    and in every second tab."""
    assert "...and a reload does not announce the same ended job again" in probe, probe


def test_zero_is_confirmed_with_the_server_before_it_is_announced(probe):
    """The countdown is interpolated off a snapshot that may be an hour old,
    because the poll deliberately stops once everything is settled. A session
    started from Settings, another tab or the command line is invisible from in
    here until somebody asks -- and announcing that zero tells somebody the
    machine they are working on has gone."""
    assert "an ended job is said out loud, the server having been asked first" in probe, probe
    assert "a zero the server disagrees with is not announced at all" in probe, probe
    assert "...and the machine goes back to being watched quietly" in probe, probe


# -- the wiring --------------------------------------------------------------


def test_it_is_loaded_on_every_page_after_the_dialog_it_opens():
    base = source("templates", "base.html")
    assert "services/sessionExpiry.js" in base
    assert base.index("services/connectionModal.js") < base.index(
        "services/sessionExpiry.js"), (
        "its one button opens the connection dialog"
    )
    assert base.index("services/remoteState.js") < base.index(
        "services/sessionExpiry.js"), (
        "it reads the shared snapshot rather than polling for itself"
    )


def test_it_registers_through_the_page_registry():
    """A router swap never fires DOMContentLoaded, and this has to be watching
    from whichever page the user landed on. The guard inside start() is what
    makes a second run a no-op -- the same shape remoteGlobe's `mounted` is."""
    watcher = source("src", "js", "services", "sessionExpiry.js")
    assert "PlexoraPage.register" in watcher
    assert "if (started) return null;" in watcher


def test_a_dismissal_outlives_the_page_that_heard_it():
    """Storage rather than a variable, and it has to survive storage being
    unavailable: a private window throws on access rather than answering null,
    and a watcher that threw on every check would be a worse bug than one that
    repeats itself."""
    watcher = source("src", "js", "services", "sessionExpiry.js")
    assert "localStorage" in watcher
    assert watcher.count("catch (e)") >= 2, "both halves have to be guarded"


def test_the_threshold_is_one_number_in_one_place():
    """The dialog fires on it, the Settings card marks itself with it and the
    navbar row goes amber on it. Three spellings of "ten minutes" is how one
    surface ends up warning about a connection another calls fine."""
    state = source("src", "js", "services", "remoteState.js")
    assert "const WARN_SECONDS = 600;" in state
    for name in ("services/sessionExpiry.js", "services/remoteGlobe.js",
                 "views/settingsPage.js"):
        text = source("src", "js", *name.split("/"))
        assert "WARN_SECONDS" in text, name
