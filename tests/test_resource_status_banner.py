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


def test_a_failing_status_route_is_not_a_broken_page(probe):
    assert "draws nothing and throws nothing" in probe, probe


def test_the_banner_is_loaded_on_every_page_that_can_open_a_viewer():
    """It is main.js that calls it, and main.js is on the viewer page -- but
    the script tag is in base.html, so a page added later gets it for free."""
    base = (REPO_ROOT / "plexora" / "client" / "templates" / "base.html").read_text(
        encoding="utf-8")
    assert "services/resourceStatus.js?v=" in base
