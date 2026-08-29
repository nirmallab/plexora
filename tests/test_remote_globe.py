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
    page that would explain it used to be a navigation away."""
    assert "...and it says so, rather than being invisible" in probe, probe
    assert "a connection lights it" in probe, probe
    assert "...and one on its way makes it the one moving thing in the navbar" in probe, probe


# -- what the panel says -----------------------------------------------------


def test_every_machine_is_a_row_including_the_two_that_are_not_profiles(probe):
    """"This computer" and "this server" are different machines in every
    arrangement except the ordinary desktop launch, and which is which is the
    thing people get wrong."""
    assert "this computer is a row, whatever it currently means" in probe, probe
    assert "...and so is the server, when that is somewhere else" in probe, probe
    assert "...then one row per saved machine" in probe, probe
    assert "a computer the server cannot read says how to attach it" in probe, probe


def test_it_says_which_machine_the_picture_is_coming_from(probe):
    """The one thing this panel knows that the Settings page does not, and the
    question somebody has when a tile will not load. Matched on the NODE's
    name, which is not necessarily the profile's."""
    assert "the routing is asked once, when the panel opens" in probe, probe
    assert "...and the machine the image comes from is the one marked" in probe, probe


def test_it_acts_on_data_nodes_and_leaves_viewers_to_settings(probe):
    """A viewer connection replaces the page being looked at, which is not
    something to offer from an icon in that page's own navbar."""
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


def test_it_adds_no_health_check_of_its_own():
    """What it shows is the session state the server already keeps, plus one
    routing lookup. A second opinion polled from here would disagree with
    Settings at some point, and the disagreement is what people would
    remember."""
    globe = source("src", "js", "services", "remoteGlobe.js")
    assert "/health" not in globe
    assert globe.count("fetch(") == 1


def test_its_styles_ship_where_every_page_loads_them():
    main = source("src", "css", "main.css")
    for rule in (".remote-globe", ".remote-panel", ".remote-panel-viewing"):
        assert rule in main
