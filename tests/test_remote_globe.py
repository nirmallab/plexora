"""Where the connections are, from anywhere in Plexora.

The behaviour is in tests/js/remote_globe_probe.mjs, run in node below. What is
left here is the wiring, and the one property that is not visible from inside
the file: this icon is in the navbar of EVERY page, the viewer included, so
what it costs when nothing is happening is the thing that decides whether it
can be there at all.

The portal rule it shares with every other floating popup on the viewer page is
enforced in tests/test_popover_portal.py, which lists it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "remote_globe_probe.mjs"
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


# -- what it costs to be everywhere ------------------------------------------


def test_being_grey_costs_nothing(probe):
    """A connected session sits settled for hours. An icon that polled a
    request a second for the privilege of reporting that would not be worth
    having on the viewer, which is the page it matters most on."""
    assert "the icon watches passively, so nothing is polled while it is grey" in probe, probe
    assert "nothing is fetched until somebody opens it" in probe, probe


def test_opening_it_starts_watching_and_closing_it_stops(probe):
    """Both halves. A panel that left its document listeners behind would go on
    handling clicks for something that is no longer on the page."""
    assert "opening the panel starts watching properly" in probe, probe
    assert "...and closes the panel behind it, so nothing is left watching" in probe, probe
    assert "closing takes its document listeners with it" in probe, probe


def test_navigating_does_not_mount_a_second_globe(probe):
    """This button is in the NAVBAR, which the router never swaps -- it
    replaces the page below it and runs every controller again against the very
    same element. Without a guard, each internal navigation would add another
    click handler, and one click would open the panel and close it in the same
    breath. views/segmentationWait.js guards its navbar chip for exactly this.
    """
    assert "navigating does not mount a second globe onto the same button" in probe, probe
    assert "...so one click still opens the panel, rather than toggling twice" in probe, probe
    assert "...and a second click closes it, leaving nothing behind" in probe, probe


def test_it_hands_the_page_registry_no_teardown():
    """A teardown is for state that outlives the MARKUP it was built against,
    and this button's markup is the navbar's. Tearing down on a page swap would
    mean the globe went grey and re-read the whole state on every internal
    navigation, for nothing."""
    globe = source("src", "js", "services", "remoteGlobe.js")
    tail = globe[globe.index("PlexoraPage.register"):]
    assert "return null;" in tail
    assert "return window.PlexoraRemoteGlobe.mount" not in tail


def test_it_says_what_is_happening_without_being_opened(probe):
    """The first sign of a dead tunnel is a tile that will not load, and the
    page that would explain it used to be a navigation away. Four states, and
    each one names the machine it is about -- "1 machine connected" is a fact
    nobody can act on."""
    assert "...and says what it is, rather than being an unexplained icon" in probe, probe
    assert "a connection lights it, and the tooltip names which" in probe, probe
    assert "...and one on its way makes it the one moving thing in the navbar" in probe, probe
    assert "...and a failure marks the icon without taking over the navbar" in probe, probe


# -- what the panel says -----------------------------------------------------


def test_the_list_is_saved_machines_and_nothing_else(probe):
    """One row per saved machine, in a fixed two-line shape, so a column of
    them is read downwards. "This computer" and "this server" used to be rows
    of their own -- but they are not connections, they cannot be connected or
    disconnected, and putting them in the list made every row's shape a
    special case. What is worth saying about them is one sentence, above."""
    assert "one row per saved machine, and nothing else in the list" in probe, probe
    assert "...saying what it is doing, in a word" in probe, probe
    assert "a computer the server cannot read says how to attach it" in probe, probe


def test_it_shows_nothing_worth_hiding_and_nothing_to_fill_in(probe):
    """A status board with a switch on it. An address, a username or an ssh
    option on a panel that opens over the viewer is both a configuration
    surface in the wrong place and something on screen in every screen-share;
    the page that edits those is one link away."""
    assert "no address, username or ssh setting is shown here" in probe, probe
    assert "...and nothing on it can be typed into" in probe, probe
    assert ("...and adding a machine goes to the page that configures machines"
            in probe), probe


def test_answering_now_is_not_the_same_claim_as_connected(probe):
    """Session state is what Plexora DID -- it started an ssh, the node
    announced. Whether the node answers now is a different question, and the
    gap between them is a slept laptop or a job that hit its walltime, both of
    which leave a session reading `connected` forever."""
    assert "the health of an open node is asked once, when the panel opens" in probe, probe
    assert "...and reported beside the round trip it took" in probe, probe
    assert "a machine with nothing open is not probed, and claims no latency" in probe, probe


def test_it_says_which_machine_the_picture_is_coming_from(probe):
    """The one thing this panel knows that the Settings page does not, and the
    question somebody has when a tile will not load. Matched on the NODE's
    name, which is not necessarily the profile's. One icon with three readings
    -- attached, connected but not the source, still attaching -- and two of
    them are not problems, so it says which in words as well."""
    assert "the routing is asked once, when the panel opens" in probe, probe
    assert "...and the machine the image comes from is the one marked" in probe, probe
    assert "...in words as well, for anything that cannot see an icon" in probe, probe


def test_it_is_a_switch_as_well_as_a_board(probe):
    """One control per row, and it is the one thing anybody wants from here:
    the machine is down and should be up, or it is up and should not be. Both
    act on the DATA NODE -- the only kind of connection Plexora opens from
    inside itself -- and connecting goes through the same dialog every other
    surface opens, rather than being a third way to start an ssh."""
    assert "a connected machine can be disconnected from here" in probe, probe
    assert "...and that ends the DATA NODE, not somebody's viewer" in probe, probe
    assert "connecting one goes through the connection dialog" in probe, probe


# -- the wiring --------------------------------------------------------------


def test_the_navbar_has_somewhere_to_mount_it():
    base = source("templates", "base.html")
    assert 'id="remote_globe"' in base
    # Beside the other two navbar mounts, and before the app's own indicator.
    assert base.index('id="remote_globe"') < base.index('id="app_status"')
    assert base.index('id="segmentation_chip"') < base.index('id="remote_globe"')


def test_it_is_loaded_after_the_portal_and_the_state_it_reads():
    """Classic scripts, no modules: a later <script> tag is a ReferenceError on
    the first page load rather than something a bundler would have caught."""
    base = source("templates", "base.html")
    for earlier in ("views/popoverPortal.js", "services/remoteState.js",
                    "services/connectionModal.js"):
        assert base.index(earlier) < base.index("services/remoteGlobe.js"), earlier


def test_it_registers_through_the_page_registry():
    """Never DOMContentLoaded: an internal navigation re-renders the page
    without one, and a globe wired on that event would be dead after the first
    click into a project."""
    globe = source("src", "js", "services", "remoteGlobe.js")
    assert "PlexoraPage.register" in globe
    # The event, not the word -- the comment beside the registration names it.
    assert 'addEventListener("DOMContentLoaded"' not in globe


def test_it_probes_when_asked_and_never_on_a_timer():
    """Two fetches, both of them once per panel open: which node the image
    comes from, and whether the open nodes answer. Neither is polled, and
    neither happens at all while the panel is closed -- that is what keeps a
    grey globe free on the viewer.

    A background health poll would be a second opinion running against every
    connection forever, and the first thing it would do is disagree with the
    session state at a moment nobody was watching."""
    globe = source("src", "js", "services", "remoteGlobe.js")
    assert globe.count("fetch(") == 2
    assert "resource_routing" in globe and "remote_health" in globe
    # Asked from the open path, not from a timer of its own.
    assert "setInterval" not in globe
    assert "setTimeout" not in globe


def test_its_styles_ship_where_every_page_loads_them():
    main = source("src", "css", "main.css")
    for rule in (".remote-globe", ".remote-panel", ".remote-conn",
                 ".remote-conn-health", ".remote-conn-screen"):
        assert rule in main
