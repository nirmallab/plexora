"""Where the channel row's popups live once the viewer goes fullscreen.

The marker dropdown and the colour palette are portaled out of their row --
a dimmed row has opacity < 1 and would trap them in its own stacking context.
The portal was <body>, which the Fullscreen API quietly turns into a hiding
place: fullscreening #bodyDiv paints an opaque ::backdrop over everything that
is not the fullscreen element or a descendant of it, so a menu on <body> opens
below the backdrop. It is positioned, it is "open", and it cannot be seen or
reached -- which is what "clicking a channel does nothing in fullscreen" was.

The probe runs the shipped popoverPortal.js, searchableSelect.js and
colorSwatchPicker.js against a DOM stand-in that tracks parentage, so what is
pinned is the real code's choice of parent rather than a description of it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "popover_portal_probe.mjs"


def test_popups_follow_the_viewer_into_and_out_of_fullscreen():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # The probe asserts internally, but pin its lines here too so a future edit
    # that quietly drops a check is not mistaken for a passing test.
    for line in (
        "outside fullscreen the popups portal onto <body> as before",
        "entering fullscreen moves already-built popups inside the fullscreen element",
        "a popup built during fullscreen lands inside it immediately",
        "a reparented menu still positions in viewport coordinates",
        "leaving fullscreen returns the popups to <body>",
        "a destroyed popup leaves the portal and is not re-attached",
    ):
        assert line in proc.stdout, proc.stdout


def test_the_portal_is_loaded_before_the_widgets_that_need_it():
    """Classic scripts, no modules: both widgets call PopoverPortal while
    constructing, so a later <script> tag is a ReferenceError on the first
    channel row rather than something the bundler would have caught."""
    base = (REPO_ROOT / "plexora" / "client" / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    order = [base.index(f"views/{name}.js") for name in
             ("popoverPortal", "searchableSelect", "colorSwatchPicker")]
    assert order == sorted(order), "popoverPortal.js must precede both widgets in base.html"


#: Every floating popup on the viewer page, by path from the repo root. The
#: import pages are deliberately absent -- segmentationProgress.js appends to
#: <body> and is right to: those pages have no #bodyDiv and no way to go
#: fullscreen, and its overlay takes over the whole page rather than floating
#: over part of it.
VIEWER_POPUPS = (
    "plexora/client/src/js/views/searchableSelect.js",
    "plexora/client/src/js/views/colorSwatchPicker.js",
    "plexora/plugins/cell_explorer/static/cellExplorerRoiBridge.js",
)


@pytest.mark.parametrize("path", VIEWER_POPUPS)
def test_no_viewer_popup_portals_straight_onto_body(path):
    """The bug is a one-line habit -- `document.body.appendChild(popup)` -- and
    it reappears every time a new popover is written. Anything that needs to
    escape its own subtree has to go through PopoverPortal, which is the only
    place that knows about fullscreen.

    Only popups are listed. The other body appends in the tree are download
    anchors and file inputs, which are never painted.
    """
    source = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert "document.body.appendChild" not in source, (
        f"{path} portals onto <body> again -- invisible under the fullscreen backdrop"
    )
    assert "PopoverPortal.attach" in source, f"{path} no longer uses the portal"
    assert "PopoverPortal.detach" in source, (
        f"{path} must leave the portal on teardown, or the portal re-attaches its orphan"
    )
