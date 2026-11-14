"""The navbar's bottom-edge bar: how far the view has sharpened.

A view is drawn coarse first, and its full-resolution tiles arrive after --
from the web, for seconds to minutes. The navbar used to say "Loading", and a
chip swept back and forth, neither saying how far along it was. The bar's
width is the share of the view's full-resolution tiles that have arrived,
measured from OpenSeadragon's own pass over each image's levels.

The checks live in tests/js/tile_progress_probe.mjs (a fake viewer raising
OSD's update-level / update-tile events, and a controllable clock) and run
against the shipped file; this wrapper runs it and pins its lines.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "tile_progress_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "a load that finishes inside the delay draws nothing",
    "the bar is the share of the finest level's tiles that have arrived",
    "two images count together",
    "it never moves back while the view stands still",
    "a moved view is measured as it is",
    "full, then gone once every image is complete",
    "a new load during the fade takes the bar back",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert line in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert f"{len(CHECKS)} checks passed" in probe.stdout


def test_the_bar_is_in_every_navbar_and_started_for_every_viewer():
    """Its mount is in base.html's navbar and its script loads there; every
    viewer reaches it through appStatus.js's watchViewer, which no longer
    says "Loading" for tiles itself."""
    client = REPO_ROOT / "plexora" / "client"
    html = (client / "templates" / "base.html").read_text(encoding="utf-8")
    assert 'id="tile_progress"' in html and "services/tileProgress.js" in html
    assert html.index("services/appStatus.js") < html.index("services/tileProgress.js")
    status = (client / "src" / "js" / "services" / "appStatus.js").read_text(encoding="utf-8")
    assert "PlexoraTileProgress?.watch(viewer)" in status
    assert 'begin("Loading")' not in status
