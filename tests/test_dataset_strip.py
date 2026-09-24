"""The dataset's thumbnail grid, behind the "2 / 12" counter.

Where it may go on the canvas (never over the channel names, the caption or a
plugin's dock), what closes it without navigating, and that its capture-phase
key handler takes Escape and nothing else -- so B and N still walk while it is
open. Navigation itself is datasetNav.js's go(), fenced in test_dataset_nav.py.

The checks live in tests/js/dataset_strip_probe.mjs and run against the shipped
file; this wrapper runs it and pins its lines.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "dataset_strip_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "it hangs under the chip at exactly the chip's width",
    "one thumbnail per row fills it, at 4:3",
    "the caption and the expand button leave it alone",
    "a plugin dock moves it left, at the same width",
    "a dock stacked under the chip puts it beside the dock, full height",
    "the legend under the chip caps the rows, and keeps a gap",
    "something low that is not under it costs it nothing",
    "rows never exceed the members and never fall below one",
    "a column holds what fits, and the rest start a column beside it",
    "rows leave room for the scrollbar only when it is drawn",
    "a hidden legend is not an obstacle",
    "plugin chrome is found by its attribute, and the chip never is",
    "nothing is built until the counter is pressed",
    "opening builds one tile per member, in the dataset's own order",
    "each tile carries the sample's thumbnail and its name as text",
    "a name is text, never markup",
    "the current sample is marked, and choosing it only closes",
    "choosing another tile closes the strip, then hands the name back",
    "a thumbnail that will not load falls back to an icon",
    "the counter says it is expanded while the strip is open",
    "it is placed from measurement, inline",
    "the current tile has focus when it opens",
    "Escape shuts it and goes no further",
    "B, N, PageUp and PageDown pass straight through",
    "a press outside shuts it; a press inside does not",
    "a press on the counter is left to the counter",
    "one strip at a time",
    "a page routed over the viewer closes it",
    "a resize refits rather than closes",
    "the wrapper and the furniture are watched while open and released on close",
    "a page with no ResizeObserver still opens",
    "arrows walk the grid: down one, right one column",
    "a vertical wheel scrolls it sideways only when there is overflow",
    "a page with no canvas opens nothing",
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
