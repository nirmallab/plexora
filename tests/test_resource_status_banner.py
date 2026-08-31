"""Why a layer is missing, said out loud.

`/resource_status` has known the answer since data nodes arrived and nothing
in the browser asked. A project whose cell table is on a node that was asleep
at load time opens with the images and without the colours, which is the right
behaviour and was, until now, completely silent.

The decisions worth pinning are all about NOT being noisy: an ordinary project
must draw nothing, a node that is merely slow must not look broken, and
somebody who has already read the message must not read it again on every
navigation. Those live in tests/js/resource_status_probe.mjs, run in node
against the shipped file, because the Python suite renders templates and
`node --check` sees only syntax.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "resource_status_probe.mjs"


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


def test_an_ordinary_project_is_told_nothing(probe):
    """Every project with its data on this machine reaches this code."""
    assert "a project with everything here draws no banner" in probe, probe


def test_a_missing_layer_names_the_node_and_the_fix(probe):
    """The node's name is the name in Settings and in the profile that
    reconnects it; without it the message is true and unactionable."""
    assert "names the node and points at Settings" in probe, probe


def test_a_relayed_node_is_not_reported_as_broken(probe):
    assert "raises no banner" in probe, probe
    assert "a footnote on a banner that already exists" in probe, probe


def test_dismissal_is_remembered(probe):
    assert "remembered for this tab" in probe, probe


def test_a_machine_this_plexora_can_connect_is_asked_about(probe):
    """A layer missing because a machine is not connected, where one button
    fixes it, is a question with an answer -- and a dismissible strip at the
    top of a viewer is not how you ask one. The server says which case it is:
    `profiles` names a saved connection THIS server could open."""
    assert "a connectable machine is asked about, not announced" in probe, probe
    assert "Connect hands off to the one dialog that connects" in probe, probe
    assert "...and the project is read again, then the page" in probe, probe


def test_declining_or_failing_still_leaves_the_note(probe):
    """The two ways out of that dialog that are not a connection. Neither may
    leave somebody with a viewer missing a layer and nothing on screen saying
    so -- which is exactly the silence this whole file exists to end."""
    assert "a connection that did not happen leaves the note behind" in probe, probe
    assert "declining leaves a banner with the same button on it" in probe, probe
    assert "the question is asked once, the note stays" in probe, probe


def test_a_machine_this_server_cannot_reach_is_still_a_banner(probe):
    """A node whose tunnel belongs to a computer this server cannot reach --
    `plexora connect` from somebody's laptop -- has no button that could work
    from here. Naming the command is the only actionable thing to say."""
    assert "a machine this server cannot reach still names the command" in probe, probe


def test_a_failing_status_route_is_not_a_broken_page(probe):
    assert "draws nothing and throws nothing" in probe, probe


def test_the_banner_is_loaded_on_every_page_that_can_open_a_viewer():
    """It is main.js that calls it, and main.js is on the viewer page -- but
    the script tag is in base.html, so a page added later gets it for free."""
    base = (REPO_ROOT / "plexora" / "client" / "templates" / "base.html").read_text(
        encoding="utf-8")
    assert "services/resourceStatus.js?v=" in base
