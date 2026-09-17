"""When the viewer says it is loading, and when it stops saying it.

The viewer's centre spinner used to be `display: none` by default with exactly
one caller -- ImageViewer.setLoading -- wired to the centroid and segmentation
fetches. Nothing showed it while the image tiles themselves were arriving, which
is the wait the user actually notices, so opening a project was several seconds
of unexplained black rectangle. viewerLoader.js drives it from what is in the
viewer's world and whether any of it has drawn.

Those are decisions made in JavaScript against OpenSeadragon events, so the
checks live in tests/js/viewer_loader_probe.mjs and run against the shipped
file; this wrapper runs it and pins its lines, so an edit that quietly drops a
check does not read as a passing test.

The two source guards at the bottom cover the parts the probe cannot see: the
markup has to load the service before the viewer is built, and the old
hide-it-directly path has to be gone rather than merely unused.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "viewer_loader_probe.mjs"

#: Every line the probe prints. The first two carry the reported bug: the page
#: renders the spinner itself, and a layer with nothing drawn yet is exactly the
#: blank screen the user was looking at.
CHECKS = (
    "the spinner the page rendered is left up",
    "a layer that has not drawn yet keeps it up",
    "the first drawn tile takes it down",
    "explicit work puts it back up, and releasing takes it down",
    "two overlapping holds do not cancel each other",
    "releasing twice counts once",
    "an emptied world shows it again until the next layer draws",
    "an empty world after boot is not loading",
    "boot settling under a layer that has not drawn leaves it up",
    "a layer arriving in the same turn as boot never blinks it",
    "a tile that fails to load does not leave it spinning",
    "a drawer that never draws tiles hides on the layer arriving",
    "a page with no loader in it is left alone",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )


def test_the_viewer_loader_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert line in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert f"{len(CHECKS)} checks passed" in probe.stdout


def test_the_viewer_page_loads_the_service_before_the_viewer():
    """Both tags are deferred, so they run in document order. main.js builds the
    ImageViewer and calls watch() on it; a service defined after that point is
    a silent no-op and the spinner never comes down."""
    html = (REPO_ROOT / "plexora" / "client" / "templates" / "index.html").read_text(
        encoding="utf-8")
    loader = html.find("services/viewerLoader.js")
    main = html.find("js/main.js")
    assert loader != -1, "the viewer page does not load viewerLoader.js at all"
    assert main != -1, "the viewer page does not load main.js"
    assert loader < main, "viewerLoader.js is loaded after main.js has already run"


def test_nothing_else_reaches_past_the_service_to_the_element():
    """ImageViewer used to set `loader.style.display` directly, in both
    directions. One of those hid the spinner in the constructor, before a single
    tile had been asked for. Any surviving direct write would put the element
    into a state the ref-counted service cannot see or undo."""
    source = (REPO_ROOT / "plexora" / "client" / "src" / "js" / "views"
              / "imageViewer.js").read_text(encoding="utf-8")
    assert not re.search(r"openseadragon_loader", source), \
        "imageViewer.js still reaches for the loader element itself"
