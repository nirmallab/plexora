"""What survives a walk from one sample to the next, and what must not.

Walking a dataset carries the viewer's ARRANGEMENT -- which channels are on,
what colour each is, which tools are open, which marker is being gated -- and
deliberately leaves every MEASUREMENT behind: a contrast window, a gate
threshold, a viewport are readings off one image, and the next one has its own.

Those are decisions made in JavaScript against sessionStorage and a live
sidebar, so the checks live in tests/js/carry_over_probe.mjs and run against the
shipped file; this wrapper runs it and pins its lines, so an edit that quietly
drops a check does not read as a passing test.

The source guard at the bottom covers the part the probe cannot see: the page
has to load the module before main.js, or the channel restore reads a snapshot
that has not been taken yet.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "carry_over_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "only the channels that are on, and only their names and colours",
    "a contrast window is never captured",
    "HD comes off the checkbox, not off a guess",
    "the mask and the centroids are left out of the layers",
    "a layer carries what it IS, so a sibling can be recognised",
    "a panel that throws costs the other components nothing",
    "a plugin that throws in its own hook costs the tool list nothing",
    "a layer section's state is captured even though no tool holds it",
    "a snapshot is taken by the sample it was meant for",
    "and refused by any other sample",
    "a walk that never happened leaves nothing behind to fire later",
    "taking consumes it, so a reload is a fresh open",
    "a snapshot older than the walk it belongs to is refused",
    "a snapshot from a previous build is dropped rather than half-read",
    "unparseable storage is nothing to apply, not an exception",
    "a browser that refuses storage still navigates",
    "nothing is said when everything applied",
    "one notice, however many components report",
    "a second flush says nothing",
    "the same line reported twice is said once",
    "a long list is summarised rather than shown whole",
    "nothing in common reads as a fresh open, not as a failure",
    "the notice stays quiet while the canvas is explaining itself",
    "a page nobody walked to is never told anything",
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


def test_the_viewer_loads_the_hand_off_before_the_viewer_boots():
    """Both tags are deferred, so they run in document order. The channel
    restore reads the snapshot while ViewerSidebar is deciding which channels
    to turn on -- and that happens inside main.js's init(), so a module defined
    after it would be asked for a snapshot it has not taken yet."""
    html = (REPO_ROOT / "plexora" / "client" / "templates" / "index.html").read_text(
        encoding="utf-8")
    carry = html.find("services/carryOver.js")
    main = html.find("js/main.js")
    assert carry != -1, "the viewer page does not load carryOver.js at all"
    assert main != -1, "the viewer page does not load main.js"
    assert carry < main, "carryOver.js is loaded after main.js has already run"
